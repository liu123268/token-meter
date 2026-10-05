"""Calendar analytics over the local derived index, never provider billing."""
from performance import attach_performance
import csv
import io
import json
from datetime import datetime, timedelta, date
from collector import LOCAL_ZONE, SUMMARY_SELECT, complete_summary, connect, summarize, time_string

SOFTWARE = ("Codex", "WorkBuddy", "ZCode", "MiMo Desktop", "DeepSeek Harness")
SCOPE = "((software='Codex' AND is_gpt_model(model)) OR software IN ('WorkBuddy','ZCode','MiMo Desktop','DeepSeek Harness'))"


def parse_day(value):
    try:
        if len(value) != 10:
            raise ValueError()
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return parsed
    except (TypeError, ValueError):
        raise ValueError("invalid calendar date") from None



def trend_context(db, base_where, params, start, end, grain, total, series, software_series, software_rows):
    """Keep filtered totals intact; give short selections seven days of context."""
    expanded = (end-start).days < 6
    if not expanded:
        return dict(range=dict(start=start.isoformat(),end=end.isoformat(),grain=grain),
                    totals=total,series=series,software_series=software_series,software_rows=software_rows,expanded=False)
    first = max(date(2000,1,1),end-timedelta(days=6))
    where = base_where + " AND time>=? AND time<?"
    values = [*params,time_string(datetime.combine(first,datetime.min.time(),LOCAL_ZONE)),
              time_string(datetime.combine(end+timedelta(days=1),datetime.min.time(),LOCAL_ZONE))]
    expression = "date(time,'+8 hours')"
    actual = {r["bucket"]:complete_summary(dict(r)) for r in db.execute(
        f"SELECT {expression} AS bucket,{SUMMARY_SELECT} FROM usage {where} GROUP BY {expression}",values)}
    empty = summarize(db,"WHERE 0")
    buckets = [{**actual.get((first+timedelta(days=i)).isoformat(),empty),
                "bucket":(first+timedelta(days=i)).isoformat()} for i in range((end-first).days+1)]
    by_software = [complete_summary(dict(r)) for r in db.execute(
        f"SELECT {expression} AS bucket,software,{SUMMARY_SELECT} FROM usage {where} GROUP BY {expression},software",values)]
    softwares = [complete_summary(dict(r)) for r in db.execute(
        f"SELECT software,{SUMMARY_SELECT} FROM usage {where} GROUP BY software",values)]
    softwares.sort(key=lambda r:r['total_tokens'],reverse=True)
    return dict(range=dict(start=first.isoformat(),end=end.isoformat(),grain='day'),
                totals=summarize(db,where,values),series=buckets,software_series=by_software,
                software_rows=softwares,expanded=True)


def dashboard_statistics(db_path, period, model, now, software, start_date, end_date):
    if software not in ("", *SOFTWARE) or period not in ("all", "today", "7d", "30d", "custom"):
        raise ValueError("invalid filter")
    now = now or datetime.now(LOCAL_ZONE)
    if now.tzinfo is None:
        raise ValueError("timezone required")
    now = now.astimezone(LOCAL_ZONE)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = today.date()
    start = None
    if period == "custom":
        start, end = parse_day(start_date), parse_day(end_date)
        if start > end or end > today.date() or start.year < 2000:
            raise ValueError("invalid date range")
    elif period != "all":
        start = (today - timedelta(days={"today":0,"7d":6,"30d":29}[period])).date()
    base, params = [SCOPE, "time<=?"], [time_string(now)]
    if software:
        base.append("software=?")
        params.append(software)
    if model:
        base.append("model=?")
        params.append(model)
    base_where = "WHERE " + " AND ".join(base)
    where, range_params = base_where, list(params)
    if start:
        where += " AND time>=? AND time<?"
        range_params.extend([time_string(datetime.combine(start, datetime.min.time(), LOCAL_ZONE)),
                             time_string(datetime.combine(end + timedelta(days=1), datetime.min.time(), LOCAL_ZONE))])
    with connect(db_path) as db:
        db.execute("BEGIN")
        total = summarize(db, where, range_params)
        today_total = summarize(db, base_where + " AND time>=?", [*params,time_string(today)])
        previous_day = summarize(db, base_where + " AND time>=? AND time<?",
                                 [*params,time_string(today-timedelta(days=1)),time_string(today)])
        day_before = summarize(db, base_where + " AND time>=? AND time<?",
                               [*params,time_string(today-timedelta(days=2)),time_string(today-timedelta(days=1))])
        recent_days = [dict(label=label,date=(today-timedelta(days=offset)).date().isoformat(),summary=value)
                       for label,offset,value in (("前天",2,day_before),("昨天",1,previous_day),("今天",0,today_total))]
        first = db.execute(f"SELECT MIN(time) FROM usage {where}",range_params).fetchone()[0]
        if start is None:
            start = datetime.fromisoformat(first.replace("Z","+00:00")).astimezone(LOCAL_ZONE).date() if first else end
        # Short ranges retain each calendar day; long histories use monthly buckets.
        grain = "day" if (end-start).days < 62 else "month"
        expression = "date(time,'+8 hours')" if grain == "day" else "strftime('%Y-%m',time,'+8 hours')"
        def grouped(group):
            return [complete_summary(dict(row)) for row in db.execute(
                f"SELECT {group},{SUMMARY_SELECT} FROM usage {where} GROUP BY {group}",range_params)]
        rows = grouped("software,model")
        rows.sort(key=lambda r:r["total_tokens"],reverse=True)
        software_rows = grouped("software")
        software_rows.sort(key=lambda r:r["total_tokens"],reverse=True)
        model_rows = grouped("model")
        model_rows.sort(key=lambda r:r["total_tokens"],reverse=True)
        actual = {r["bucket"]:complete_summary(dict(r)) for r in db.execute(
            f"SELECT {expression} AS bucket,{SUMMARY_SELECT} FROM usage {where} GROUP BY {expression}",range_params)}
        software_series = [complete_summary(dict(r)) for r in db.execute(
            f"SELECT {expression} AS bucket,software,{SUMMARY_SELECT} FROM usage {where} GROUP BY {expression},software",range_params)]
        # SQL provides a genuine zero-row aggregate; unknown ratios remain null.
        empty = summarize(db,"WHERE 0")
        buckets = []
        current = start if grain == "day" else start.replace(day=1)
        while current <= end:
            key = current.isoformat() if grain == "day" else current.strftime("%Y-%m")
            buckets.append({**actual.get(key,empty),"bucket":key})
            if grain == "day":
                current += timedelta(days=1)
            else:
                current = current.replace(year=current.year+1,month=1) if current.month == 12 else current.replace(month=current.month+1)
        attach_performance(db,where,range_params,total,rows,software_rows,model_rows,buckets,software_series,grain)
        trend = trend_context(db,base_where,params,start,end,grain,total,buckets,software_series,software_rows)
        from codex_tasks import task_summary
        codex_tasks,task_models=task_summary(db,where.replace(SCOPE,'1'),range_params)
        for row in rows:
            task=task_models.get(row['model'],{}) if row['software']=='Codex' else {}
            for key in ('output_tps','mean_request_ms','mean_ttft_ms','timed_records'):row['task_'+key]=task.get(key)
        model_params = [time_string(now)]
        model_where = "WHERE " + SCOPE + " AND time<=?"
        if software:
            model_where += " AND software=?"
            model_params.append(software)
        models = [r[0] for r in db.execute("SELECT DISTINCT model FROM usage " + model_where + " ORDER BY model",model_params)]
        states = [json.loads(r[0]) for r in db.execute("SELECT state FROM cursors")]
        states = [s for s in states if s.get("software","Codex") == "Codex"]
        adapters = {r[0]:json.loads(r[1]) for r in db.execute("SELECT software,state FROM adapter_states")}
    diagnostics = {"indexed_files":len(states),"files_without_usage":sum(not(s.get("native_records") or s.get("legacy_records")) for s in states),
        **{k:sum(s.get(k,0) for s in states) for k in ("invalid","baseline_gaps","resets","malformed","mixed_gaps","conflicts","excluded_records")}}
    capabilities = [{"software":name,"status":"connected" if name == "Codex" else
        adapters.get(name,{}).get("status","pending")} for name in SOFTWARE]
    return {"trend":trend,"recent_days":recent_days,"codex_tasks":codex_tasks,"totals":total,"today":today_total,"yesterday":previous_day,"rows":rows,"models":models,
        "software_rows":software_rows,"model_rows":model_rows,"series":buckets,"software_series":software_series,
        "range":{"start":start.isoformat(),"end":end.isoformat(),"grain":grain,"as_of":time_string(now)},
        "diagnostics":diagnostics,"adapter_diagnostics":adapters,"software":software,"period":period,
        "timezone":"Asia/Shanghai","scope":"Local agents; Codex GPT only","capabilities":capabilities}


def export_csv(data, view="rows", search=""):
    if view not in ("rows","series"):
        raise ValueError("invalid export")
    identity = [("software","软件"),("model","模型")] if view == "rows" else [("bucket","日期（北京时间）")]
    fields = identity + [("records","计数记录"),("total_tokens","总Token"),("input_tokens","输入Token"),
        ("output_tokens","输出Token"),("cached_input_tokens","缓存读取Token（已知部分）"),
        ("uncached_input_tokens","未缓存输入Token"),("cache_hit_ratio","缓存命中比例"),
        ("cache_unknown_records","缓存未知记录"),("legacy_tokens","旧格式推算Token"),
        ("reasoning_output_tokens","推理Token（已知部分）"),("reasoning_unknown_records","推理未知记录"),
        ("cache_write_input_tokens","缓存写入Token（已知部分）"),("write_unknown_records","缓存写入未知记录"),
        ("output_tps","端到端输出速度Token/s（有耗时样本）"),("mean_request_ms","平均请求耗时ms"),
        ("p95_request_ms","P95请求耗时ms"),("timed_records","有耗时记录"),
        ("timing_unknown_records","耗时未知记录"),("mean_ttft_ms","平均首Token等待ms"),("ttft_records","有首Token记录"),
        ("decode_tps","生成速度Token/s（首Token至完成）"),("decode_records","有生成耗时记录"),
        ("task_output_tps","Codex任务输出速度Token/s（含工具）"),("task_mean_request_ms","Codex任务平均耗时ms")]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow([title for _,title in fields])
    for row in data[view]:
        if view == "rows" and search.casefold() not in (row["software"]+" "+row["model"]).casefold():
            continue
        values = []
        for key,_ in fields:
            value = row.get(key)
            if isinstance(value,str) and value.lstrip().startswith(("=","+","-","@")):
                value = "'"+value
            values.append("" if value is None else value)
        writer.writerow(values)
    return ("\ufeff"+buffer.getvalue()).encode("utf-8")
