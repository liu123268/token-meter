"""Read DeepSeek Harness v3/v4 durable sessions, never credentials or model APIs."""
import json
import math
from pathlib import Path
from collector import normalize_usage
from performance import interval_ms


def count(value):
    return type(value) is int and 0 <= value <= 2**53-1


def open_session(path):
    if path.name.endswith('.zstd'):
        try:
            from compression import zstd
        except ImportError:
            try:
                import zstandard as zstd
                return zstd.open(path,'rt',encoding='utf-8')
            except ImportError as exc:
                raise RuntimeError('Zstandard decoder unavailable') from exc
        return zstd.open(path,'rt',encoding='utf-8')
    return path.open('r',encoding='utf-8')


def first_token_time(stream):
    if not isinstance(stream,list):return None
    for record in stream:
        if not isinstance(record,dict):continue
        kind=record.get('type')
        if kind in ('text-chunks','reasoning-chunks','tool-call-chunks'):
            fragments=record.get('args' if kind=='tool-call-chunks' else 'texts')
            gaps=record.get('dt');when=record.get('time0')
            if not count(when) or not isinstance(fragments,list) or not fragments or not isinstance(gaps,list):continue
            if len(gaps)!=len(fragments)-1 or any(type(g) is not int or abs(g)>2**53-1 for g in gaps):continue
            if any(not isinstance(fragment,str) for fragment in fragments):continue
            for index,fragment in enumerate(fragments):
                if index:when+=gaps[index-1]
                if not count(when):return None
                if fragment!='' or kind=='tool-call-chunks' and 'name' in record:return when
        elif kind=='chunk':
            chunk=record.get('chunk');when=record.get('time')
            if not isinstance(chunk,dict) or not count(when):continue
            if chunk.get('type') in ('text-delta','reasoning-delta') and isinstance(chunk.get('text'),str) and chunk['text']!='':return when
            if chunk.get('type')=='tool-call-delta' and (isinstance(chunk.get('argumentsDelta'),str) and chunk['argumentsDelta']!='' or 'name' in chunk):return when
    return None


def reported_usage(raw):
    if not isinstance(raw,dict) or not count(raw.get('inputTokens')) or not count(raw.get('outputTokens')):return None
    uncached=raw['inputTokens'];output=raw['outputTokens'];read=raw.get('cacheReadTokens');write=raw.get('cacheWriteTokens');total=raw.get('totalTokens')
    if any(value is not None and not count(value) for value in (read,write,total)):return None
    known=uncached+(read or 0)+(write or 0)
    if total is not None:
        inputs=total-output
        if inputs<known or read is not None and write is not None and inputs!=known:return None
    elif read is not None and write is not None:inputs=known
    else:return None
    reasoning=raw.get('reasoningTokens')
    if reasoning is not None and (not count(reasoning) or reasoning>output):reasoning=None
    return normalize_usage(dict(input_tokens=inputs,output_tokens=output,total_tokens=inputs+output,
        cached_input_tokens=read,cache_write_input_tokens=write,reasoning_output_tokens=reasoning))


def session_records(path):
    """One authoritative final usage per assistant message; retired attempts are separate billing."""
    records=[];diagnostics=dict(missing_usage=0,invalid=0,inherited=0,native_records=0)
    meta=None;opened=None;model=None;provider=None
    with open_session(path) as stream:
        for line in stream:
            if not line.endswith('\n'):break
            try:event=json.loads(line)
            except (ValueError,UnicodeDecodeError):diagnostics['invalid']+=1;continue
            if not isinstance(event,dict):continue
            kind=event.get('type');data=event.get('data') or {};when=event.get('time')
            if kind=='session':
                if event.get('version') not in (3,4) or not isinstance(event.get('id'),str) or not count(event.get('createdAt')):
                    raise ValueError('Unsupported session header')
                meta={k:event.get(k) for k in ('id','createdAt','parentSession')};continue
            if meta is None or not isinstance(data,dict):continue
            if not count(when):continue
            if when<meta['createdAt']:
                if kind in ('assistant/message','assistant/attempt'):diagnostics['inherited']+=1
                continue
            if kind in ('request/context','model/selection'):
                model=data.get('model') or model;provider=data.get('provider') or provider
            if kind=='step/start':opened=dict(turn=data.get('turn'),step=data.get('step'),start=when,attempt_start=when,first=None)
            elif kind=='llm/retry-started' and opened is not None:opened['attempt_start']=when
            elif kind in ('assistant/message','assistant/attempt'):
                raw=data.get('usage')
                if raw is None:
                    for chunk in reversed(data.get('stream') or []):
                        if isinstance(chunk,dict) and chunk.get('type')=='chunk' and (chunk.get('chunk') or {}).get('type')=='usage':raw=chunk['chunk'].get('usage');break
                usage=reported_usage(raw)
                if raw is None:diagnostics['missing_usage']+=1;continue
                if usage is None:diagnostics['invalid']+=1;continue
                message=data.get('message') or {};source=message.get('source') or {}
                mid=message.get('id')
                if kind=='assistant/message' and (not isinstance(mid,str) or not mid):diagnostics['invalid']+=1;continue
                if kind=='assistant/attempt' and not count(event.get('seq')):diagnostics['invalid']+=1;continue
                key='dsh:message:'+mid if kind=='assistant/message' else 'dsh:attempt:'+meta['id']+':'+str(event['seq'])
                first=first_token_time(data.get('stream'));timing=None
                matching=opened is not None and opened['turn']==data.get('turn') and opened['step']==data.get('step')
                if matching:
                    if opened['first'] is None and first is not None:opened['first']=first
                    timing=interval_ms(opened['attempt_start'],when,first)
                    if timing is not None:
                        timing['include_decode']=False
                        if kind=='assistant/message' and opened['first'] is not None and opened['start']<=opened['first']<when:
                            timing['decode_ms']=when-opened['first']
                    if kind=='assistant/message':opened=None
                records.append(dict(key=key,usage=usage,when_ms=when,model=source.get('model') or model,
                    thread=meta['id'],client='DeepSeek Harness',timing=timing))
                diagnostics['native_records']+=1
            elif kind in ('step/end','turn/end'):opened=None
    if meta is None:raise ValueError('Missing session header')
    return records,diagnostics
