"""Codex whole-turn timing is distinct from request and decode timing."""
import json
from collector import HEADER_TYPE, is_gpt_model, normalize_usage, time_string, valid_time


def read_tasks(path):
    meta={};model=None;turns={};records=[]
    with path.open('rb') as stream:
        for line in stream:
            if not line.endswith(b'\n'):break
            header=HEADER_TYPE.search(line[:512]);kind=header.group(1).decode() if header else None
            if kind not in ('session_meta','turn_context','token_usage_record','event_msg'):continue
            if kind=='event_msg' and b'"task_complete"' not in line[:1024] and b'"task_started"' not in line[:1024]:continue
            try:event=json.loads(line)
            except (ValueError,UnicodeDecodeError):continue
            payload=event.get('payload') or {}
            if not isinstance(payload,dict):continue
            if kind=='session_meta':meta={k:payload.get(k) for k in ('id','timestamp')};continue
            if kind=='turn_context':model=payload.get('model') or model;continue
            when=valid_time(event.get('timestamp'));created=valid_time(meta.get('timestamp'))
            if when is None or created is not None and when<created:continue
            turn=payload.get('turn_id')
            if not isinstance(turn,str):continue
            state=turns.setdefault(turn,{'requests':{},'models':set(),'expected':None,'started':False})
            if kind=='token_usage_record':
                usage=normalize_usage(payload.get('usage'));rid=payload.get('response_id');route=payload.get('model') or model
                if usage is None or not isinstance(rid,str):state['models'].add('unknown');continue
                state['models'].add(route if isinstance(route,str) else 'unknown')
                state['requests'].setdefault(rid,usage['output_tokens'])
                last=normalize_usage(payload.get('turn_token_usage'))
                state['expected']=last['output_tokens'] if last else None
            elif payload.get('type')=='task_started':state['started']=True
            elif payload.get('type')=='task_complete':
                duration=payload.get('duration_ms');wait=payload.get('time_to_first_token_ms')
                if type(duration) not in (int,float) or not 0<duration<=2**53-1 or not state['started'] or not state['requests'] or not all(is_gpt_model(m) for m in state['models']):continue
                if type(wait) not in (int,float) or not 0<=wait<=duration:wait=None
                output=sum(state['requests'].values())
                verified=output==state['expected']
                records.append((f"codex:task:{meta.get('id')}:{turn}",'Codex',next(iter(state['models'])) if len(state['models'])==1 else '混合 GPT',time_string(when),duration,wait,output if verified else None))
    return records


def scan_tasks(db,paths):
    for path in paths:
        stat=path.stat();identity=f'{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}'
        saved=db.execute('SELECT identity FROM timing_cursors WHERE path=?',(str(path),)).fetchone()
        if saved and saved[0]==identity:continue
        for row in read_tasks(path):
            db.execute('INSERT INTO codex_turn_timing VALUES (?,?,?,?,?,?,?) ON CONFLICT(event_key) DO NOTHING',row)
        db.execute('INSERT INTO timing_cursors VALUES (?,?) ON CONFLICT(path) DO UPDATE SET identity=excluded.identity',(str(path),identity))


def task_summary(db,where,params):
    from performance import summarize_timing
    # Same user filter; this separate table contains only validated GPT-only Codex turns.
    data=[dict(r) for r in db.execute('SELECT model,duration_ms,ttft_ms,output_tokens FROM codex_turn_timing '+where,params)]
    timing=summarize_timing([{**r,'output_tokens':r['output_tokens'] or 0} for r in data])
    known=[r for r in data if r['output_tokens'] is not None]
    total_ms=sum(r['duration_ms'] for r in known)
    timing['output_tps']=sum(r['output_tokens'] for r in known)*1000/total_ms if total_ms else None
    timing.update(records=len(data),verified_output_turns=len(known))
    by_model={}
    for model in {r['model'] for r in data}:
        subset=[r for r in data if r['model']==model]
        s=summarize_timing([{**r,'output_tokens':r['output_tokens'] or 0} for r in subset]);matched=[r for r in subset if r['output_tokens'] is not None]
        ms=sum(r['duration_ms'] for r in matched)
        s['output_tps']=sum(r['output_tokens'] for r in matched)*1000/ms if ms else None
        by_model[model]=s
    return timing,by_model
