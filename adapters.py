"""Read only allowlisted local usage sources. Never read credentials or call model APIs."""
from performance import interval_ms, save_timing
from deepseek_harness import session_records
from codex_tasks import scan_tasks
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from collector import CodexCollector, TOKEN_FIELDS, connect, normalize_usage, time_string, valid_time

SOFTWARES = ("Codex", "WorkBuddy", "ZCode", "MiMo Desktop", "DeepSeek Harness")


def epoch_time(value):
    if type(value) not in (int, float) or value < 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def epoch_ms(value):
    parsed=valid_time(value)
    return parsed.timestamp()*1000 if parsed is not None else None


def response_usage(raw):
    if not isinstance(raw, dict):
        return None
    prompt = raw.get("prompt_tokens_details") or {}
    completion = raw.get("completion_tokens_details") or {}
    if not isinstance(prompt, dict) or not isinstance(completion, dict):
        return None
    reasoning = completion.get("reasoning_tokens")
    output = raw.get("completion_tokens")
    if reasoning is not None and (type(reasoning) is not int or reasoning < 0 or type(output) is not int or reasoning > output):
        reasoning = None
    return normalize_usage({"input_tokens": raw.get("prompt_tokens"),
        "output_tokens": raw.get("completion_tokens"), "total_tokens": raw.get("total_tokens"),
        "cached_input_tokens": prompt.get("cached_tokens"),
        "reasoning_output_tokens": reasoning})


def mimo_usage(tokens):
    # This engine stores uncached input and visible output as separate buckets.
    # Its reported total identifies whether reasoning is already included in output.
    if not isinstance(tokens, dict) or not isinstance(tokens.get("cache"), dict):
        return None
    values = [tokens.get("input"), tokens.get("output"), tokens.get("reasoning", 0),
              tokens["cache"].get("read"), tokens["cache"].get("write"), tokens.get("total")]
    if any(type(v) is not int or v < 0 for v in values):
        return None
    uncached, visible, reasoning, cached, written, total = values
    inputs = uncached + cached + written
    outputs = total - inputs
    if outputs not in (visible, visible + reasoning):
        return None
    return normalize_usage({"input_tokens": inputs, "output_tokens": outputs,
        "cached_input_tokens": cached, "cache_write_input_tokens": written,
        "reasoning_output_tokens": reasoning, "total_tokens": total})


def store_record(db, software, key, usage, when, model, thread, client="", timing=None):
    if usage is None or when is None:
        return "invalid"
    model = str(model or "未知模型")[:120]
    existing = db.execute("SELECT * FROM usage WHERE event_key=?", (key,)).fetchone()
    if existing:
        if any(existing[k] is not None and usage[k] is not None and existing[k] != usage[k]
               for k in TOKEN_FIELDS):
            return "conflicts"
        preferred = software == "WorkBuddy" and client == "WorkBuddy trace" and existing["client"] == "WorkBuddy transcript"
        changed = preferred or (existing["model"] == "未知模型" and model != "未知模型")
        changed |= any(existing[k] is None and usage[k] is not None for k in TOKEN_FIELDS)
        if not changed:
            save_timing(db, key, timing)
            return "duplicate"
    values = (key, software, str(client or software)[:80], model, str(thread or "未知会话")[:160],
              time_string(when), *(usage[k] for k in TOKEN_FIELDS), "request", 0)
    db.execute("""INSERT INTO usage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_key) DO UPDATE SET
        model=CASE WHEN usage.model='未知模型' OR (excluded.client='WorkBuddy trace' AND usage.client='WorkBuddy transcript') THEN excluded.model ELSE usage.model END,
        time=CASE WHEN excluded.client='WorkBuddy trace' AND usage.client='WorkBuddy transcript' THEN excluded.time ELSE usage.time END,
        client=CASE WHEN excluded.client='WorkBuddy trace' AND usage.client='WorkBuddy transcript' THEN excluded.client ELSE usage.client END,
        cached_input_tokens=COALESCE(usage.cached_input_tokens,excluded.cached_input_tokens),
        cache_write_input_tokens=COALESCE(usage.cache_write_input_tokens,excluded.cache_write_input_tokens),
        reasoning_output_tokens=COALESCE(usage.reasoning_output_tokens,excluded.reasoning_output_tokens)
        """, values)
    save_timing(db, key, timing)
    return "saved"


def workbuddy_records(document):
    """Only generation response objects are authoritative, not trace rollup counters."""
    records = []
    counts = Counter()
    if not isinstance(document, dict) or not isinstance(document.get("spans"), list):
        raise ValueError("Invalid trace container")
    trace = document.get("trace") or {}
    for span in document["spans"]:
        if not isinstance(span, dict) or span.get("type") != "generation":
            continue
        try:
            result = json.loads(span.get("toolOutput") or "null")
        except (ValueError, TypeError):
            counts["missing_usage"] += 1
            continue
        responses = result if isinstance(result, list) else [result]
        reported = False
        for response in responses:
            if not isinstance(response, dict) or not isinstance(response.get("usage"), dict):
                continue
            reported = True
            usage = response_usage(response["usage"])
            rid = response.get("id")
            when = valid_time(span.get("endedAt")) or valid_time(span.get("startedAt"))
            if not isinstance(rid, str) or not rid or usage is None or when is None:
                counts["invalid"] += 1
                continue
            if (response["usage"].get("completion_tokens_details") or {}).get("reasoning_tokens") is not None and usage["reasoning_output_tokens"] is None:
                counts["invalid_detail"] += 1
            records.append({"key": "workbuddy:response:" + rid, "usage": usage, "when": when,
                "model": response.get("model"), "thread": trace.get("sessionId"), "client": "WorkBuddy trace",
                "timing": interval_ms(epoch_ms(span.get("startedAt")),epoch_ms(span.get("endedAt"))) if len(responses)==1 else None})
        if not reported:
            counts["missing_usage"] += 1
    counts["native_records"] = len(records)
    if records:
        rollup = trace.get("modelInfo") or {}
        totals = {"totalInputTokens": sum(r["usage"]["input_tokens"] for r in records),
                  "totalOutputTokens": sum(r["usage"]["output_tokens"] for r in records),
                  "totalCachedTokens": sum(r["usage"]["cached_input_tokens"] or 0 for r in records)}
        counts["rollup_mismatch"] = int(any(rollup.get(k) != v for k, v in totals.items())
            or trace.get("totalTokens") != sum(r["usage"]["total_tokens"] for r in records))
    return records, dict(counts)


def readonly_database(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def zcode_records(path):
    db = readonly_database(path)
    try:
        fields={r[1] for r in db.execute("PRAGMA table_info(model_usage)")}
        first="first_token_at" if "first_token_at" in fields else "NULL"
        rows = list(db.execute(f"""SELECT {first} AS first_token_at, id, logical_request_id, attempt_index, session_id,
            model_id, started_at, completed_at, input_tokens, output_tokens, reasoning_tokens,
            cache_creation_input_tokens, cache_read_input_tokens, provider_total_tokens,
            computed_total_tokens FROM model_usage WHERE raw_usage_json IS NOT NULL
            AND (input_tokens>0 OR output_tokens>0)"""))
    finally:
        db.close()
    records = []
    for r in rows:
        usage = normalize_usage({"input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
            "cached_input_tokens": r["cache_read_input_tokens"],
            "cache_write_input_tokens": r["cache_creation_input_tokens"],
            "reasoning_output_tokens": r["reasoning_tokens"], "total_tokens": r["computed_total_tokens"]})
        if r["provider_total_tokens"] is not None and r["provider_total_tokens"] != r["computed_total_tokens"]:
            usage = None
        identity = [r["logical_request_id"], r["attempt_index"]] if r["logical_request_id"] else [r["id"]]
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        records.append({"key": "zcode:request:" + key, "usage": usage,
            "when": epoch_time(r["completed_at"] or r["started_at"]), "model": r["model_id"], "thread": r["session_id"],
            "timing": interval_ms(r["started_at"],r["completed_at"],r["first_token_at"])})
    return records


def mimo_records(path):
    db = readonly_database(path)
    try:
        rows = list(db.execute("""SELECT p.id, p.session_id, p.time_created,
            json_extract(p.data,'$.tokens') AS tokens,
            json_extract(m.data,'$.modelID') AS model,
            json_extract(m.data,'$.providerID') AS provider,
            json_extract(m.data,'$.time.completed') AS completed,
            json_extract(m.data,'$.time.created') AS started,
            (SELECT COUNT(*) FROM part p2 WHERE p2.message_id=p.message_id
             AND json_extract(p2.data,'$.type')='step-finish') AS finish_parts
            FROM part p JOIN message m ON m.id=p.message_id
            JOIN session s ON s.id=p.session_id
            WHERE json_extract(p.data,'$.type')='step-finish'
            AND json_extract(m.data,'$.role')='assistant'
            AND p.time_created>=s.time_created
            AND m.id NOT IN (SELECT CAST(value AS TEXT) FROM external_import,json_each(message_ids))
            AND m.id NOT IN (SELECT CAST(value AS TEXT) FROM claude_import,json_each(message_ids))"""))
    finally:
        db.close()
    records = []
    for r in rows:
        try:
            usage = mimo_usage(json.loads(r["tokens"]))
        except (TypeError, ValueError):
            usage = None
        records.append({"key": "mimo:step:" + r["id"], "usage": usage,
            "when": epoch_time(r["completed"] or r["time_created"]),
            "model": r["model"], "thread": r["session_id"], "client": r["provider"],
            "timing": interval_ms(r["started"],r["completed"]) if r["finish_parts"]==1 else None})
    return records


class LocalCollector(CodexCollector):
    def __init__(self, codex_home, db_path, user_home=None):
        super().__init__(codex_home, db_path)
        self.user_home = Path(user_home or Path.home()).resolve()
        self.workbuddy_projects = self.user_home / ".workbuddy/projects"
        self.sources = {"WorkBuddy": self.user_home / ".workbuddy/traces",
            "ZCode": self.user_home / ".zcode/cli/db/db.sqlite",
            "MiMo Desktop": self.user_home / ".local/share/mimocode/mimocode.db",
            "DeepSeek Harness": self.user_home / ".dsh/sessions"}

    def scan_once(self):
        super().scan_once()
        self.set_status(phase="scanning", source="本地 Agent 用量记录")
        with connect(self.db_path) as db:
            codex_paths=[]
            for folder in (self.home/'sessions',self.home/'archived_sessions'):
                if folder.is_dir() and not folder.is_symlink():codex_paths.extend(p for p in folder.rglob('*.jsonl') if not p.is_symlink() and p.resolve().is_relative_to(folder.resolve()))
            scan_tasks(db,codex_paths)
            db.commit()
            for software, path in self.sources.items():
                state = {"status": "connected", "missing_usage": 0, "invalid": 0, "conflicts": 0,
                         "files": 0, "candidates": 0, "rollup_mismatch": 0, "invalid_detail": 0,
                         "transcript_files": 0, "transcript_candidates": 0, "trace_candidates": 0}
                try:
                    if software == "DeepSeek Harness":
                        if path.is_symlink():raise OSError('Linked session root')
                        files=[p for p in path.glob('*/*/session.v*.jsonl*') if p.name in ('session.v3.jsonl','session.v4.jsonl','session.v3.jsonl.zstd','session.v4.jsonl.zstd') and not p.is_symlink() and p.resolve().is_relative_to(path.resolve())]
                        state['files']=len(files)
                        if not path.is_dir():state['status']='not_found'
                        for file in files:self.read_deepseek(db,file)
                        for row in db.execute('SELECT state FROM cursors WHERE path LIKE ?',(str(path)+'%',)):
                            detail=json.loads(row[0]);state['candidates']+=detail.get('native_records',0)
                            for key in ('missing_usage','invalid','conflicts'):state[key]+=detail.get(key,0)
                    elif software == "WorkBuddy":
                        if path.is_symlink():
                            raise OSError("Linked source root")
                        files = [p for p in path.glob("*/trace_*.json") if not p.is_symlink()
                                 and p.resolve().is_relative_to(path.resolve())]
                        state["files"] = len(files)
                        for file in files:
                            self.read_workbuddy(db, file)
                        projects = self.workbuddy_projects
                        if projects.is_symlink():
                            raise OSError("Linked transcript source root")
                        transcripts = [p for p in projects.glob("*/*.jsonl") if not p.is_symlink()
                                       and p.resolve().is_relative_to(projects.resolve())]
                        state["transcript_files"] = len(transcripts)
                        for file in transcripts:
                            self.read_workbuddy_transcript(db, file)
                        for row in db.execute("SELECT state FROM cursors WHERE path LIKE ? OR path LIKE ?",
                                              (str(path) + "%", str(projects) + "%")):
                            file_state = json.loads(row[0])
                            for key in ("missing_usage", "invalid", "conflicts", "rollup_mismatch", "invalid_detail"):
                                state[key] += file_state.get(key, 0)
                            candidates = file_state.get("native_records", 0)
                            state["candidates"] += candidates
                            field = "transcript_candidates" if file_state.get("format") == "transcript" else "trace_candidates"
                            state[field] += candidates
                        if not path.is_dir() and not projects.is_dir():
                            state["status"] = "not_found"
                    else:
                        if not path.is_file():
                            state["status"] = "not_found"
                        elif path.is_symlink():
                            raise OSError("Linked source database")
                        else:
                            records = zcode_records(path) if software == "ZCode" else mimo_records(path)
                            state["files"] = 1
                            if software == "ZCode":
                                source_db = readonly_database(path)
                                try:
                                    state["missing_usage"] = source_db.execute("SELECT COUNT(*) FROM model_usage WHERE raw_usage_json IS NULL").fetchone()[0]
                                finally:
                                    source_db.close()
                            state["candidates"] = len(records)
                            for record in records:
                                result = store_record(db, software, **record)
                                if result in ("invalid", "conflicts"):
                                    state[result] += 1
                    db.execute("INSERT INTO adapter_states VALUES (?,?) ON CONFLICT(software) DO UPDATE SET state=excluded.state",
                               (software, json.dumps(state)))
                    db.commit()
                except (OSError, sqlite3.Error, ValueError, TypeError, RuntimeError):
                    db.rollback()
                    state["status"] = "error"
                    db.execute("INSERT INTO adapter_states VALUES (?,?) ON CONFLICT(software) DO UPDATE SET state=excluded.state",
                               (software, json.dumps(state)))
                    db.commit()
        self.set_status(phase="ready", last_scan=time_string(datetime.now(timezone.utc)))

    def read_workbuddy(self, db, path):
        stat = path.stat()
        identity = f"{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}"
        saved = db.execute("SELECT identity,state FROM cursors WHERE path=?", (str(path),)).fetchone()
        if saved and saved[0] == identity and json.loads(saved[1]).get("timing_version")==1:
            return
        # Changed JSON traces are reread, but response IDs prevent counting them twice.
        with path.open("r", encoding="utf-8") as stream:
            records, state = workbuddy_records(json.load(stream))
        state.update(software="WorkBuddy", conflicts=0, timing_version=1)
        for record in records:
            result = store_record(db, "WorkBuddy", **record)
            if result == "conflicts":
                state["conflicts"] += 1
        db.execute("INSERT INTO cursors VALUES (?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
            "identity=excluded.identity,offset=excluded.offset,state=excluded.state",
            (str(path), identity, stat.st_size, json.dumps(state)))


    def read_workbuddy_transcript(self, db, path):
        """Read append-only request usage; response IDs deduplicate overlap with traces."""
        stat = path.stat()
        identity = f"{stat.st_dev}:{stat.st_ino}"
        saved = db.execute("SELECT * FROM cursors WHERE path=?", (str(path),)).fetchone()
        state = {"software":"WorkBuddy", "format":"transcript", "native_records":0,
                 "missing_usage":0, "invalid":0, "conflicts":0, "invalid_detail":0}
        offset = 0
        if saved and saved["identity"] == identity and stat.st_size >= saved["offset"]:
            state.update(json.loads(saved["state"]))
            offset = saved["offset"]
        if saved and offset == stat.st_size:
            return
        with path.open("rb") as stream:
            stream.seek(offset)
            while True:
                start = stream.tell()
                line = stream.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    stream.seek(start)
                    break
                if b'"providerData"' not in line or b'"usage"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    state["invalid"] += 1
                    continue
                if not isinstance(event, dict) or not (event.get("type") == "function_call"
                        or event.get("type") == "message" and event.get("role") == "assistant"):
                    continue
                provider = event.get("providerData")
                if not isinstance(provider, dict) or not isinstance(provider.get("usage"), dict):
                    continue
                raw = provider.get("rawUsage")
                if not isinstance(raw, dict) or not raw:
                    state["missing_usage"] += 1
                    continue  # Normalized zero defaults do not prove reported zero usage.
                usage = response_usage(raw)
                rid = provider.get("messageId")
                when = epoch_time(event.get("timestamp")) or valid_time(event.get("timestamp"))
                if usage is None or not isinstance(rid, str) or not rid or when is None:
                    state["invalid"] += 1
                    continue
                if (raw.get("completion_tokens_details") or {}).get("reasoning_tokens") is not None and usage["reasoning_output_tokens"] is None:
                    state["invalid_detail"] += 1
                result = store_record(db, "WorkBuddy", "workbuddy:response:" + rid, usage, when,
                                      provider.get("model"), event.get("sessionId"), "WorkBuddy transcript")
                state["native_records"] += 1
                if result == "conflicts":
                    state["conflicts"] += 1
            offset = stream.tell()
        db.execute("INSERT INTO cursors VALUES (?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                   "identity=excluded.identity, offset=excluded.offset, state=excluded.state",
                   (str(path), identity, offset, json.dumps(state)))

    def read_deepseek(self,db,path):
        stat=path.stat();identity=f'{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}'
        saved=db.execute('SELECT identity FROM cursors WHERE path=?',(str(path),)).fetchone()
        if saved and saved[0]==identity:return
        try:records,state=session_records(path)
        except Exception as exc:
            if type(exc).__name__ in ('ZstdError','ZstdDecompressionError','EOFError'):raise OSError('Compressed session not ready') from exc
            raise
        after=path.stat()
        if after.st_size!=stat.st_size or after.st_mtime_ns!=stat.st_mtime_ns:raise OSError('Session changed while reading')
        state.update(software='DeepSeek Harness',conflicts=0)
        for record in records:
            record['when']=epoch_time(record.pop('when_ms'))
            if store_record(db,'DeepSeek Harness',**record)=='conflicts':state['conflicts']+=1
        db.execute('INSERT INTO cursors VALUES (?,?,?,?) ON CONFLICT(path) DO UPDATE SET identity=excluded.identity,offset=excluded.offset,state=excluded.state',
            (str(path),identity,stat.st_size,json.dumps(state)))
