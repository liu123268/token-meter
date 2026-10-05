"""Request timing from explicit local boundaries; no inference from token timestamps."""
import math
from collections import defaultdict
from datetime import datetime


def interval_ms(start, end, first=None):
    def numeric(v):
        return type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 2**53-1
    if not numeric(start) or not numeric(end) or end <= start:
        return None
    duration = end - start
    ttft = first - start if numeric(first) and start <= first <= end else None
    return {"duration_ms": duration, "ttft_ms": ttft}


def save_timing(db, key, timing):
    if not isinstance(timing, dict):
        return
    duration, ttft = timing.get("duration_ms"), timing.get("ttft_ms")
    if type(duration) not in (int,float) or not math.isfinite(duration) or not 0 < duration <= 2**53-1:
        return
    if type(ttft) not in (int,float) or not math.isfinite(ttft) or not 0 <= ttft <= duration:
        ttft = None
    existing=db.execute("SELECT duration_ms FROM request_timing WHERE event_key=?",(key,)).fetchone()
    if existing is not None and existing[0]!=duration:return
    decode=timing.get("decode_ms")
    if decode is None and timing.get("include_decode",True) and ttft is not None and ttft<duration:decode=duration-ttft
    if type(decode) in (int,float) and math.isfinite(decode) and 0<decode<=2**53-1:
        db.execute("INSERT INTO generation_timing VALUES (?,?) ON CONFLICT(event_key) DO NOTHING",(key,decode))
    db.execute("""INSERT INTO request_timing VALUES (?,?,?) ON CONFLICT(event_key)
        DO UPDATE SET ttft_ms=COALESCE(request_timing.ttft_ms,excluded.ttft_ms)
        WHERE request_timing.duration_ms=excluded.duration_ms""", (key,duration,ttft))


def summarize_timing(rows):
    timed=[r for r in rows if r["duration_ms"] is not None and r["duration_ms"]>0]
    durations=sorted(r["duration_ms"] for r in timed)
    total_ms=sum(durations)
    output=sum(r["output_tokens"] for r in timed)
    waits=[r["ttft_ms"] for r in timed if r["ttft_ms"] is not None]
    decoded=[r for r in rows if r.get("decode_ms") is not None and r["decode_ms"]>0]
    decode_ms=sum(r["decode_ms"] for r in decoded)
    decode_output=sum(r["output_tokens"] for r in decoded)
    return {"decode_records":len(decoded),"decode_ms":decode_ms,"decode_output_tokens":decode_output,
        "decode_tps":decode_output*1000/decode_ms if decode_ms else None,"timed_records":len(timed), "timing_unknown_records":len(rows)-len(timed),
        "timed_output_tokens":output, "request_duration_ms":total_ms,
        "output_tps":output*1000/total_ms if total_ms else None,
        "mean_request_ms":total_ms/len(timed) if timed else None,
        "p95_request_ms":durations[math.ceil(len(durations)*.95)-1] if timed else None,
        "ttft_records":len(waits), "mean_ttft_ms":sum(waits)/len(waits) if waits else None}


def attach_performance(db, where, params, total, rows, software_rows, model_rows, series, software_series, grain):
    samples=[dict(r) for r in db.execute("""SELECT software,model,time,output_tokens,
        duration_ms,ttft_ms,decode_ms FROM usage LEFT JOIN request_timing USING(event_key) LEFT JOIN generation_timing USING(event_key) """+where,params)]
    groups={key:defaultdict(list) for key in ("pair","software","model","bucket","bucket_software")}
    for sample in samples:
        bucket=db_bucket(sample["time"],grain)
        for key,value in (("pair",(sample["software"],sample["model"])),("software",sample["software"]),
                          ("model",sample["model"]),("bucket",bucket),("bucket_software",(bucket,sample["software"]))):
            groups[key][value].append(sample)
    total.update(summarize_timing(samples))
    for collection,key,identity in ((rows,"pair",lambda r:(r["software"],r["model"])),
            (software_rows,"software",lambda r:r["software"]),(model_rows,"model",lambda r:r["model"]),
            (series,"bucket",lambda r:r["bucket"]),(software_series,"bucket_software",lambda r:(r["bucket"],r["software"]))):
        for row in collection:row.update(summarize_timing(groups[key].get(identity(row),[])))


def db_bucket(value,grain):
    from collector import LOCAL_ZONE
    day=datetime.fromisoformat(value.replace("Z","+00:00")).astimezone(LOCAL_ZONE)
    return day.strftime("%Y-%m-%d" if grain=="day" else "%Y-%m")
