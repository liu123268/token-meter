"""Verified main-agent output throughput across complete native task boundaries."""
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from collector import HEADER_TYPE, is_gpt_model, time_string, valid_time

SOFTWARES=('Codex','WorkBuddy','ZCode','MiMo Desktop','DeepSeek Harness')
SOURCES={
    'Codex':'主会话 task_started → task_complete',
    'WorkBuddy':'主 CLI Agent 的明确开始 → 成功结束；排除标题生成与提示词增强',
    'ZCode':'主会话原生 turn_usage 的开始 → completed',
    'MiMo Desktop':'主 Actor 用户消息创建 → 关联的最终 stop 回复完成',
    'DeepSeek Harness':'主会话 turn/start → completed turn/end',
}


def as_time(value):
    if isinstance(value,datetime):
        return value.astimezone(timezone.utc) if value.tzinfo is not None else None
    if isinstance(value,str):return valid_time(value)
    if type(value) in (int,float) and math.isfinite(value) and 0<=value<=2**53-1:
        try:return datetime.fromtimestamp(value/1000,timezone.utc)
        except (ValueError,OverflowError,OSError):return None
    return None


def model_label(models):
    names={str(value)[:120] for value in models if isinstance(value,str) and value}
    return next(iter(names)) if len(names)==1 else '混合模型' if names else '未知模型'


def completion(key,software,models,start,end):
    start,end=as_time(start),as_time(end)
    if start is None or end is None or end<=start:return None
    duration=(end-start).total_seconds()*1000
    if not math.isfinite(duration) or not 0<duration<=2**53-1:return None
    return (key,software,model_label(models),time_string(end),time_string(start),duration,SOURCES[software])


def codex_completions(path):
    meta={};current=None;active={};result=[]
    with path.open('rb') as stream:
        for line in stream:
            if not line.endswith(b'\n'):break
            header=HEADER_TYPE.search(line[:512]);kind=header.group(1).decode() if header else None
            if kind not in ('session_meta','turn_context','event_msg'):continue
            if kind=='event_msg' and b'"task_complete"' not in line[:1024] and b'"task_started"' not in line[:1024]:continue
            try:event=json.loads(line)
            except (ValueError,UnicodeDecodeError):continue
            data=event.get('payload') or {}
            if not isinstance(data,dict):continue
            if kind=='session_meta':
                meta={k:data.get(k) for k in ('id','timestamp','source','model_provider')}
                current=None;active={}
                continue
            source=meta.get('source');provider=meta.get('model_provider')
            if isinstance(source,dict) and 'subagent' in source:continue
            if isinstance(provider,str) and provider.strip().casefold()!='openai':continue
            when=as_time(event.get('timestamp'));created=as_time(meta.get('timestamp'))
            if when is None or created is not None and when<created:continue
            if kind=='turn_context':
                current=data.get('model') or current
                if data.get('turn_id') in active:active[data['turn_id']]['models'].add(current)
                continue
            turn=data.get('turn_id')
            if not isinstance(turn,str) or not isinstance(meta.get('id'),str):continue
            if data.get('type')=='task_started':active.setdefault(turn,{'start':when,'models':set()})
            elif data.get('type')=='task_complete':
                state=active.pop(turn,{})
                if data.get('error') is not None:continue
                models={m for m in (state.get('models') or {current}) if isinstance(m,str) and m}
                if meta.get('model_provider') is None and (not models or not all(is_gpt_model(m) for m in models)):continue
                # Event envelope timestamps retain milliseconds. The numeric
                # started_at/completed_at payload fields use Unix seconds;
                # they must not be parsed as the other adapters' epoch millis.
                start=state.get('start');end=when
                if start is None or created is not None and start<created:continue
                row=completion(f"codex:completion:{meta['id']}:{turn}",'Codex',models,start,end)
                if row:result.append(row)
    return result


def workbuddy_completions(path):
    from adapters import read_workbuddy_trace
    document=read_workbuddy_trace(path);trace=document.get('trace') or {};spans=document.get('spans') or []
    tid=trace.get('traceId')
    if not isinstance(tid,str) or not tid:return []
    result=[]
    for root in spans:
        if not isinstance(root,dict) or root.get('type')!='agent' or root.get('agentName')!='cli' or root.get('parentId'):continue
        if root.get('status')!='ok' or not isinstance(root.get('spanId'),str):continue
        start,end=as_time(root.get('startedAt')),as_time(root.get('endedAt'))
        if start is None or end is None or end<=start:continue
        models=set();generations=0
        for span in spans:
            if not isinstance(span,dict) or span.get('type')!='generation':continue
            a,b=as_time(span.get('startedAt')),as_time(span.get('endedAt'))
            if a is None or b is None or not start<=a<=b<=end:continue
            generations+=1
            try:response=json.loads(span.get('toolOutput') or 'null')
            except (TypeError,ValueError):continue
            for item in response if isinstance(response,list) else [response]:
                if isinstance(item,dict) and isinstance(item.get('model'),str):models.add(item['model'])
        if not generations:continue
        row=completion(f"workbuddy:completion:{tid}:{root['spanId']}",'WorkBuddy',models,start,end)
        if row:result.append(row)
    return result


def dsh_completions(path):
    from deepseek_harness import open_session
    header={};current=None;active={};result=[]
    kinds=('session','subagent/descriptor','request/context','model/selection','turn/start','turn/end')
    markers=[('"'+kind+'"') for kind in kinds]
    with open_session(path) as stream:
        for line in stream:
            if not line.endswith('\n'):break
            if not any(marker in line[:256] for marker in markers):continue
            try:event=json.loads(line)
            except (ValueError,UnicodeDecodeError):continue
            kind=event.get('type');data=event.get('data') or {};when=event.get('time')
            if kind=='session':
                if event.get('version') not in (3,4):return []
                header={k:event.get(k) for k in ('id','createdAt')};continue
            if not isinstance(data,dict) or type(when) is not int or when<header.get('createdAt',when+1):continue
            if kind=='subagent/descriptor':return []
            if kind in ('request/context','model/selection'):
                current=data.get('model') or current
                turn=data.get('turn')
                if turn in active:active[turn]['models'].add(current)
                elif len(active)==1:next(iter(active.values()))['models'].add(current)
            elif kind=='turn/start':active[data.get('turn')]={'start':when,'models':{current}}
            elif kind=='turn/end':
                state=active.pop(data.get('turn'),None)
                if state is None or (data.get('reason') or {}).get('kind')!='completed':continue
                if not isinstance(header.get('id'),str):continue
                row=completion(f"dsh:completion:{header['id']}:{data.get('turn')}",'DeepSeek Harness',state['models'],state['start'],when)
                if row:result.append(row)
    return result


def open_database(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
    db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
    return db


def tables(db):return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def zcode_completions(path):
    db=open_database(path)
    try:
        if not {'turn_usage','session','model_usage'}<=tables(db):return []
        rows=list(db.execute("""SELECT t.session_id,t.turn_id,t.started_at,t.completed_at
            FROM turn_usage t JOIN session s ON s.id=t.session_id
            WHERE t.status='completed' AND s.parent_id IS NULL
            AND t.started_at>=s.time_created AND t.completed_at>t.started_at"""))
        models=defaultdict(set)
        for r in db.execute('SELECT session_id,turn_id,model_id FROM model_usage'):
            if isinstance(r['model_id'],str):models[(r['session_id'],r['turn_id'])].add(r['model_id'])
        return [value for r in rows if (value:=completion(f"zcode:completion:{r['session_id']}:{r['turn_id']}",'ZCode',models[(r['session_id'],r['turn_id'])],r['started_at'],r['completed_at']))]
    finally:db.close()


def mimo_completions(path):
    db=open_database(path)
    try:
        present=tables(db)
        if not {'message','session'}<=present:return []
        columns={r[1] for r in db.execute('PRAGMA table_info(message)')}
        extra=" AND (m.agent_id IS NULL OR m.agent_id='main') AND (u.agent_id IS NULL OR u.agent_id='main')" if 'agent_id' in columns else ''
        for table in ('external_import','claude_import'):
            if table in present:
                extra+=f" AND m.id NOT IN (SELECT CAST(value AS TEXT) FROM {table},json_each(message_ids)) AND u.id NOT IN (SELECT CAST(value AS TEXT) FROM {table},json_each(message_ids))"
        rows=list(db.execute("""SELECT m.session_id,u.id user_id,u.time_created user_created,
            json_extract(u.data,'$.time.created') user_start,
            m.time_created assistant_created,json_extract(m.data,'$.time.completed') completed,
            json_extract(m.data,'$.finish') finish,json_extract(m.data,'$.modelID') model,
            json_extract(m.data,'$.error') error
            FROM message m JOIN message u ON u.id=json_extract(m.data,'$.parentID') AND u.session_id=m.session_id
            JOIN session s ON s.id=m.session_id
            WHERE json_extract(m.data,'$.role')='assistant' AND json_extract(u.data,'$.role')='user'
            AND s.parent_id IS NULL AND m.time_created>=s.time_created AND u.time_created>=s.time_created
            """+extra))
        groups=defaultdict(list)
        for r in rows:groups[(r['session_id'],r['user_id'])].append(r)
        result=[]
        for (sid,uid),items in groups.items():
            last=max(items,key=lambda r:(r['assistant_created'],r['completed'] or 0))
            if last['finish']!='stop' or last['error'] is not None:continue
            row=completion(f'mimo:completion:{sid}:{uid}','MiMo Desktop',{r['model'] for r in items},last['user_start'] if last['user_start'] is not None else last['user_created'],last['completed'])
            if row:result.append(row)
        return result
    finally:db.close()


def packed(bound, references, complete=True, expected=None):
    output=sum(item[1] for item in references.values())
    valid=complete and bool(references) and (expected is None or output==expected)
    model=model_label({item[0] for item in references.values()}) if references else bound[2]
    return {'bound':(*bound[:2],model,*bound[3:]),'output':output if valid else None,'references':references}


def add_reference(references, key, model, output):
    if not isinstance(key,str) or not key or not isinstance(model,str) or not model or type(output) is not int or not 0<=output<=2**53-1:
        return False
    value=(model,output)
    if key in references and references[key]!=value:return False
    references[key]=value
    return True


def codex_samples(path):
    from codex_tasks import read_tasks
    bounds=codex_completions(path)
    if not bounds:return []
    native={r[0]:r for r in read_tasks(path)}
    result=[]
    for bound in bounds:
        timing=native.get(bound[0].replace('codex:completion:','codex:task:',1))
        output=timing[6] if timing else None
        model=timing[2] if timing else bound[2]
        if model=='混合 GPT':model='混合模型'
        result.append({'bound':(*bound[:2],model,*bound[3:]),'output':output,'references':{}})
    return result


def workbuddy_samples(path):
    from adapters import read_workbuddy_trace, response_usage
    bounds=workbuddy_completions(path)
    if not bounds:return []
    document=read_workbuddy_trace(path);spans=document.get('spans') or []
    by_id={s.get('spanId'):s for s in spans if isinstance(s,dict)}
    def owner(span):
        parent=span.get('parentId');visited=set()
        while parent and parent not in visited:
            visited.add(parent);ancestor=by_id.get(parent)
            if ancestor is None:return None
            if ancestor.get('type')=='agent':return ancestor.get('spanId')
            parent=ancestor.get('parentId')
        return None
    result=[]
    for bound in bounds:
        root=bound[0].rsplit(':',1)[1];references={};complete=True
        start,end=as_time(bound[4]),as_time(bound[3])
        for span in spans:
            if not isinstance(span,dict) or span.get('type')!='generation':continue
            parent=owner(span)
            a,b=as_time(span.get('startedAt')),as_time(span.get('endedAt'))
            if parent is None:
                if a is not None and start<=a<=end:complete=False
                continue
            if parent!=root:continue
            if a is None or b is None or not start<=a<=b<=end:
                complete=False;continue
            try:response=json.loads(span.get('toolOutput') or 'null')
            except (TypeError,ValueError):complete=False;continue
            for item in response if isinstance(response,list) else [response]:
                usage=response_usage(item.get('usage')) if isinstance(item,dict) else None
                if usage is None or not isinstance(item.get('id'),str) or not item['id'] or not add_reference(references,'workbuddy:response:'+item['id'],item.get('model'),usage['output_tokens']):complete=False
        result.append(packed(bound,references,complete))
    return result


def zcode_samples(path):
    import hashlib
    from adapters import zcode_records
    bounds=zcode_completions(path)
    if not bounds:return []
    records={r['key']:r for r in zcode_records(path)};db=open_database(path)
    try:
        turns={(r['session_id'],str(r['turn_id'])):r for r in db.execute('SELECT session_id,turn_id,output_tokens,model_request_count FROM turn_usage')}
        groups=defaultdict(list)
        for r in db.execute('SELECT id,session_id,turn_id,logical_request_id,attempt_index FROM model_usage'):
            groups[(r['session_id'],str(r['turn_id']))].append(r)
        result=[]
        for bound in bounds:
            session,turn=bound[0].removeprefix('zcode:completion:').split(':',1)
            references={};complete=True;rows=groups[(session,turn)];native=turns[(session,turn)]
            for row in rows:
                identity=[row['logical_request_id'],row['attempt_index']] if row['logical_request_id'] else [row['id']]
                key='zcode:request:'+hashlib.sha256(json.dumps(identity).encode()).hexdigest()
                record=records.get(key);usage=record.get('usage') if record else None
                if usage is None or not add_reference(references,key,record['model'],usage['output_tokens']):complete=False
            if type(native['output_tokens']) is not int or type(native['model_request_count']) is not int or native['model_request_count']!=len(rows):complete=False
            result.append(packed(bound,references,complete,native['output_tokens']))
        return result
    finally:db.close()


def mimo_samples(path):
    from adapters import mimo_records,mimo_usage
    bounds=mimo_completions(path)
    if not bounds:return []
    records={r['key']:r for r in mimo_records(path)};db=open_database(path)
    try:
        groups=defaultdict(list)
        for r in db.execute("""SELECT id,session_id,json_extract(data,'$.parentID') parent,
            json_extract(data,'$.tokens') tokens FROM message
            WHERE json_extract(data,'$.role')='assistant'"""):
            groups[(r['session_id'],r['parent'])].append(r)
        parts=defaultdict(list)
        for r in db.execute("SELECT id,message_id FROM part WHERE json_extract(data,'$.type')='step-finish'"):
            parts[r['message_id']].append(r['id'])
        result=[]
        for bound in bounds:
            session,user=bound[0].removeprefix('mimo:completion:').split(':',1)
            references={};complete=True;expected=0
            for message in groups[(session,user)]:
                subtotal=0;ids=parts[message['id']]
                try:message_usage=mimo_usage(json.loads(message['tokens'] or 'null'))
                except (TypeError,ValueError):message_usage=None
                if not ids or message_usage is None:complete=False
                for part in ids:
                    key='mimo:step:'+part;record=records.get(key);usage=record.get('usage') if record else None
                    if usage is None or not add_reference(references,key,record['model'],usage['output_tokens']):complete=False
                    else:subtotal+=usage['output_tokens']
                if message_usage is not None:
                    if subtotal!=message_usage['output_tokens']:complete=False
                    expected+=message_usage['output_tokens']
            result.append(packed(bound,references,complete,expected))
        return result
    finally:db.close()


def dsh_samples(path):
    from deepseek_harness import open_session,session_records
    bounds=dsh_completions(path)
    if not bounds:return []
    raw,_=session_records(path);records={r['key']:r for r in raw}
    groups=defaultdict(dict);invalid=set();header={}
    with open_session(path) as stream:
        for line in stream:
            if not line.endswith('\n'):break
            if not any(marker in line[:256] for marker in ('"session"','"assistant/message"','"assistant/attempt"')):continue
            try:event=json.loads(line)
            except (ValueError,UnicodeDecodeError):continue
            kind=event.get('type');data=event.get('data') or {};when=event.get('time')
            if kind=='session':header={k:event.get(k) for k in ('id','createdAt')};continue
            if kind not in ('assistant/message','assistant/attempt') or not isinstance(data,dict) or type(when) is not int or when<header.get('createdAt',when+1):continue
            turn=str(data.get('turn'));mid=(data.get('message') or {}).get('id')
            key='dsh:message:'+mid if kind=='assistant/message' and isinstance(mid,str) else 'dsh:attempt:'+str(header.get('id'))+':'+str(event.get('seq'))
            record=records.get(key)
            if record is None or record.get('subagent') or not add_reference(groups[turn],key,record['model'],record['usage']['output_tokens']):invalid.add(turn)
    return [packed(bound,groups[bound[0].rsplit(':',1)[1]],bound[0].rsplit(':',1)[1] not in invalid) for bound in bounds]


READERS={'Codex':codex_samples,'WorkBuddy':workbuddy_samples,'ZCode':zcode_samples,'MiMo Desktop':mimo_samples,'DeepSeek Harness':dsh_samples}


def file_identity(path):
    info=path.stat();value=f'execution-output-v1:{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}'
    wal=Path(str(path)+'-wal')
    if wal.is_file():
        info=wal.stat();value+=f':{info.st_size}:{info.st_mtime_ns}'
    return value


def scan_execution_samples(db,software,paths):
    errors=0
    for path in paths:
        try:
            identity=file_identity(path)
            saved=db.execute('SELECT identity FROM execution_cursors WHERE software=? AND path=?',(software,str(path))).fetchone()
            if saved and saved[0]==identity:continue
            samples=READERS[software](path)
            if identity!=file_identity(path):raise OSError('Task evidence changed during read')
            for sample in samples:
                bound=sample['bound'];output=sample['output']
                if type(output) is not int or not 0<=output<=2**53-1:output=None
                if output is not None:
                    for key,(_,count) in sample['references'].items():
                        recorded=db.execute('SELECT software,output_tokens FROM usage WHERE event_key=?',(key,)).fetchone()
                        if recorded is None or recorded[0]!=software or recorded[1]!=count:
                            output=None;break
                db.execute('''INSERT INTO execution_samples VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(event_key) DO UPDATE SET
                    model=excluded.model,output_tokens=excluded.output_tokens,verification=excluded.verification
                    WHERE execution_samples.time=excluded.time
                    AND execution_samples.started_at=excluded.started_at
                    AND execution_samples.duration_ms=excluded.duration_ms''',
                    (*bound,output,'verified' if output is not None else 'usage_unverified'))
            db.execute('INSERT INTO execution_cursors VALUES (?,?,?) ON CONFLICT(software,path) DO UPDATE SET identity=excluded.identity',(software,str(path),identity))
        except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):errors+=1
    return errors


def summarize_execution(rows):
    valid=[r for r in rows if r['verification']=='verified' and r['output_tokens'] is not None]
    duration=sum(r['duration_ms'] for r in valid);output=sum(r['output_tokens'] for r in valid)
    return {'records':len(rows),'valid_tasks':len(valid),'unverified_tasks':len(rows)-len(valid),
        'output_tokens':output,'duration_ms':duration,'output_tps':output*1000/duration if duration else None,
        'first_completed_at':min((r['time'] for r in valid),default=None),
        'last_completed_at':max((r['time'] for r in valid),default=None)}


def execution_statistics(db,where,params,software,model,now):
    columns='software,model,time,duration_ms,output_tokens,verification'
    current=[dict(r) for r in db.execute('SELECT '+columns+' FROM execution_samples '+where,params)]
    history_where='WHERE time<=?';history_params=[time_string(now)]
    if software:history_where+=' AND software=?';history_params.append(software)
    if model:history_where+=' AND model=?';history_params.append(model)
    history=[dict(r) for r in db.execute('SELECT '+columns+' FROM execution_samples '+history_where,history_params)]
    rows=[{'software':name,'current':summarize_execution([r for r in current if r['software']==name]),
        'history':summarize_execution([r for r in history if r['software']==name]),'source':SOURCES[name]}
        for name in ((software,) if software else SOFTWARES)]
    groups=defaultdict(list)
    for row in current:groups[(row['software'],row['model'])].append(row)
    models=[{'software':name,'model':route,**summarize_execution(values)} for (name,route),values in groups.items()]
    return {'metric':'verified_main_task_output_tokens_per_wall_second','unit':'tok/s',
        'formula':'sum(output_tokens) / sum(task_wall_seconds)',
        'rows':rows,'totals':summarize_execution(current),'model_rows':models,
        'scope':'Main-agent output only; tools and waiting included; child/background output excluded'}
