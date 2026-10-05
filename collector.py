"""Read Codex's existing JSONL records; never access credentials or model APIs."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codex_auxiliary import (initialize_auxiliary, save_auxiliary_usage,
                             reconcile_auxiliary_primary, backfill_auxiliary)

LOCAL_ZONE = timezone(timedelta(hours=8))
HEADER_TYPE = re.compile(rb'"type"\s*:\s*"([a-z_]+)"')
GPT_MODEL = re.compile(r"gpt-\d+(?:\.\d+)*(?:-[a-z0-9]+)*", re.IGNORECASE)
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens", "total_tokens")


def is_gpt_model(model):
    # Opaque aliases (gpt-reserve, codex-auto-review) do not prove a GPT model.
    return isinstance(model, str) and GPT_MODEL.fullmatch(model) is not None


def normalize_usage(raw):
    """Keep unknown cache/reasoning counts as None; never turn them into zero."""
    if not isinstance(raw, dict):
        return None
    result = {key: raw.get(key) for key in TOKEN_FIELDS}
    for value in result.values():
        if value is not None and (type(value) is not int or value < 0 or value > 2**53 - 1):
            return None
    if result["input_tokens"] is None or result["output_tokens"] is None:
        return None
    computed = result["input_tokens"] + result["output_tokens"]
    if result["total_tokens"] is None:
        result["total_tokens"] = computed
    if computed > 2**53 - 1 or result["total_tokens"] != computed:
        return None
    cached, written = result["cached_input_tokens"], result["cache_write_input_tokens"]
    if cached is not None and cached > result["input_tokens"]:
        return None
    if written is not None and written + (cached or 0) > result["input_tokens"]:
        return None
    reasoning = result["reasoning_output_tokens"]
    if reasoning is not None and reasoning > result["output_tokens"]:
        return None
    return result


def valid_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def time_string(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def usage_delta(previous, current):
    if previous is None:
        return None
    delta = {}
    for key in TOKEN_FIELDS:
        before, after = previous.get(key), current.get(key)
        if before is None or after is None:
            delta[key] = None
        elif after < before:
            return None
        else:
            delta[key] = after - before
    return normalize_usage(delta)


@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.create_function("is_gpt_model", 1, is_gpt_model, deterministic=True)
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize_db(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS usage (
                event_key TEXT PRIMARY KEY, software TEXT NOT NULL,
                client TEXT NOT NULL, model TEXT NOT NULL, thread_id TEXT NOT NULL,
                time TEXT NOT NULL, input_tokens INTEGER NOT NULL,
                cached_input_tokens INTEGER, cache_write_input_tokens INTEGER,
                output_tokens INTEGER NOT NULL, reasoning_output_tokens INTEGER,
                total_tokens INTEGER NOT NULL, source TEXT NOT NULL,
                subagent INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS usage_time ON usage(time);
            CREATE TABLE IF NOT EXISTS request_timing (event_key TEXT PRIMARY KEY, duration_ms REAL NOT NULL, ttft_ms REAL);
            CREATE TABLE IF NOT EXISTS generation_timing (event_key TEXT PRIMARY KEY, decode_ms REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS timing_cursors (path TEXT PRIMARY KEY, identity TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS codex_turn_timing (event_key TEXT PRIMARY KEY,software TEXT NOT NULL,model TEXT NOT NULL,time TEXT NOT NULL,duration_ms REAL NOT NULL,ttft_ms REAL,output_tokens INTEGER);
            CREATE TABLE IF NOT EXISTS adapter_states (software TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cursors (
                path TEXT PRIMARY KEY, identity TEXT NOT NULL, offset INTEGER NOT NULL,
                state TEXT NOT NULL
            );
        """)
        initialize_auxiliary(db)
        if db.execute("PRAGMA user_version").fetchone()[0] < 3:
            # Only our derived index is rebuilt; Codex history is never modified.
            db.execute("DELETE FROM usage")
            db.execute("DELETE FROM cursors")
            db.execute("DELETE FROM codex_auxiliary_usage")
            db.execute("DELETE FROM codex_auxiliary_backfill")
            db.execute("DELETE FROM codex_auxiliary_migrations")
            db.execute("PRAGMA user_version=3")


class CodexCollector:
    def __init__(self, codex_home, db_path):
        self.home = Path(codex_home).resolve()
        self.db_path = Path(db_path)
        initialize_db(self.db_path)
        self._lock = threading.Lock()
        self.status = {"phase": "starting", "files": 0, "indexed_files": 0,
                       "last_scan": None, "error": None, "source": "Codex 本地用量记录"}

    def get_status(self):
        with self._lock:
            return dict(self.status)

    def set_status(self, **values):
        with self._lock:
            self.status.update(values)

    def scan_once(self):
        # Strict directory allowlist: auth.json, config.toml and other files are never opened.
        paths = []
        for name in ("sessions", "archived_sessions"):
            directory = self.home / name
            if directory.is_dir() and not directory.is_symlink():
                paths.extend(p for p in directory.rglob("*.jsonl")
                             if not p.is_symlink() and p.resolve().is_relative_to(directory.resolve()))
        paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        self.set_status(phase="scanning", files=len(paths), indexed_files=0, error=None)
        with connect(self.db_path) as db:
            if not backfill_auxiliary(db, paths, normalize_usage, valid_time, is_gpt_model, TOKEN_FIELDS):
                self.set_status(error="部分辅助用量历史暂时无法读取，下一次扫描会重试。")
            for number, path in enumerate(paths, 1):
                try:
                    self.read_file(db, path)
                    db.commit()
                except (OSError, sqlite3.Error):
                    db.rollback()
                    self.set_status(error="部分本地文件暂时无法读取，下一次扫描会重试。")
                self.set_status(indexed_files=number)
        self.set_status(phase="ready", last_scan=time_string(datetime.now(timezone.utc)))

    def run(self, stop, interval=3):
        while not stop.is_set():
            try:
                self.scan_once()
            except (OSError, sqlite3.Error):
                self.set_status(phase="error", error="本地索引暂时不可用，请检查目录权限。")
            stop.wait(interval)

    def read_file(self, db, path):
        stat = path.stat()
        identity = f"{stat.st_dev}:{stat.st_ino}"
        saved = db.execute("SELECT * FROM cursors WHERE path=?", (str(path),)).fetchone()
        state = {"model": "未知模型", "meta": {}, "previous": None, "pending_native": None, "pending_native_ids": [], "seen_native_ids": [],
                 "native_records": 0, "legacy_records": 0, "invalid": 0,
                 "baseline_gaps": 0, "resets": 0, "malformed": 0,
                 "mixed_gaps": 0, "conflicts": 0, "excluded_records": 0, "epoch": 0}
        offset = 0
        metadata_updated = False
        if saved and saved["identity"] == identity and stat.st_size >= saved["offset"]:
            offset = saved["offset"]
            state.update(json.loads(saved["state"]))
            meta = state.setdefault("meta", {})
            if "model_provider" not in meta:
                # Upgrade old derived cursors once using bounded public metadata.
                # Matching identity fields avoids attaching copied ancestry's
                # provider to a later session_meta in the same file.
                meta["model_provider"] = None
                with path.open("rb") as header_stream:
                    first = header_stream.readline(1024 * 1024 + 1)
                if first.endswith(b"\n") and len(first) <= 1024 * 1024:
                    try:
                        header = json.loads(first)
                    except (ValueError, UnicodeDecodeError):
                        header = None
                    header_meta = header.get("payload") if isinstance(header, dict) and header.get("type") == "session_meta" else None
                    if (isinstance(header_meta, dict)
                            and header_meta.get("id") == meta.get("id")
                            and header_meta.get("timestamp") == meta.get("timestamp")):
                        provider = header_meta.get("model_provider")
                        meta["model_provider"] = provider[:120] if isinstance(provider, str) else None
                metadata_updated = True
        if offset == stat.st_size and saved:
            if metadata_updated:
                db.execute("UPDATE cursors SET state=? WHERE path=?",
                           (json.dumps(state, ensure_ascii=False), str(path)))
            return
        with path.open("rb") as stream:
            stream.seek(offset)
            while True:
                start = stream.tell()
                line = stream.readline()
                if not line:
                    break
                # A partially written final line is retried, including UTF-8 split characters.
                if not line.endswith(b"\n"):
                    stream.seek(start)
                    break
                header = HEADER_TYPE.search(line[:512])
                kind = header.group(1).decode() if header else None
                if kind not in ("session_meta", "turn_context", "token_usage_record", "event_msg"):
                    continue
                if kind == "event_msg" and b'"token_count"' not in line[:1024]:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    state["malformed"] += 1
                    continue
                if not isinstance(event, dict):
                    state["malformed"] += 1
                    continue
                self.consume(db, event, state, str(path))
            offset = stream.tell()
        db.execute("INSERT INTO cursors VALUES (?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
                   "identity=excluded.identity, offset=excluded.offset, state=excluded.state",
                   (str(path), identity, offset, json.dumps(state, ensure_ascii=False)))

    def consume(self, db, event, state, path):
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        kind = event.get("type")
        if kind == "session_meta":
            # Persist metadata needed for counting only, never instructions/messages/account IDs.
            state["meta"] = {k: payload.get(k) for k in
                             ("id", "timestamp", "originator", "parent_thread_id",
                              "forked_from_id", "history_base", "subagent_history_start_ordinal", "model_provider")}
            state["meta"]["subagent"] = isinstance(payload.get("source"), dict) and "subagent" in payload["source"]
            return
        if kind == "turn_context":
            if isinstance(payload.get("model"), str) and payload["model"]:
                state["model"] = payload["model"][:120]
            return
        when = valid_time(event.get("timestamp"))
        if when is None:
            state["invalid"] += 1
            return
        meta = state["meta"]
        created = valid_time(meta.get("timestamp"))
        # Copied ancestry is not spending by the newly created fork/subagent.
        inherited = created is not None and when < created
        if kind == "token_usage_record":
            raw = normalize_usage(payload.get("usage"))
            response = payload.get("response_id")
            if raw is None or not isinstance(response, str) or not response:
                state["invalid"] += 1
                return
            if inherited:
                return
            pending = state["pending_native"]
            if response in state.setdefault("seen_native_ids", []):
                result = self.save_usage(db, f"codex:response:{response}", raw, when,
                    payload.get("model") or state["model"], payload.get("thread_id") or meta.get("id") or "未知会话", meta, "request")
                if result == "conflict":
                    state["conflicts"] += 1
                return
            state["seen_native_ids"].append(response)
            state["pending_native_ids"].append(response)
            state["pending_native"] = {k: raw[k] + pending[k] if pending and raw[k] is not None and pending[k] is not None
                                       else raw[k] if pending is None else None for k in TOKEN_FIELDS}
            state["native_records"] += 1
            model = payload.get("model") or state["model"]
            thread = payload.get("thread_id") or meta.get("id") or "未知会话"
            result = self.save_usage(db, f"codex:response:{response}", raw, when, model, thread, meta, "request")
            if result == "excluded":
                state["excluded_records"] += 1
            elif result == "conflict":
                state["conflicts"] += 1
            return
        if kind != "event_msg" or payload.get("type") != "token_count":
            return
        info = payload.get("info")
        if not isinstance(info, dict):
            return  # rate-limit updates with info=null are not zero-token requests
        current = normalize_usage(info.get("total_token_usage"))
        last = normalize_usage(info.get("last_token_usage"))
        previous = state["previous"]
        pending = state["pending_native"]
        if current is None:
            state["invalid"] += 1
            return
        state["previous"] = current
        if inherited or current == previous:
            return
        # Rate-limit notifications can repeat the old cumulative counters after
        # a native response. Keep that response pending until counters actually
        # advance/reset, so its usage is subtracted from the correct window.
        state["pending_native"] = None
        state["pending_native_ids"] = []
        delta = usage_delta(previous, current)
        if previous is None:
            if current == last or (pending and all(current[k] == pending[k]
                                                   for k in ("input_tokens", "output_tokens", "total_tokens"))):
                delta = current
            else:
                # Only the latest request is attributable. Earlier inherited/missing totals stay out.
                delta = last
                state["baseline_gaps"] += 1
        elif delta is None:
            # Counter rollback/reset is not evidence of a new request; flag rather than guess.
            state["resets"] += 1
            state["epoch"] += 1
            return
        if pending and delta:
            # A cumulative window may contain both native and legacy calls.
            # Subtract ALL native usage rather than skipping the entire window.
            residual = usage_delta(pending, delta)
            if residual is None:
                if previous is not None:
                    state["mixed_gaps"] += 1
                return
            delta = residual
        if delta is None or not delta["total_tokens"]:
            return
        thread = meta.get("id") or "未知会话"
        fingerprint = json.dumps([thread, state["epoch"], current], sort_keys=True)
        event_key = "codex:snapshot:" + hashlib.sha256(fingerprint.encode()).hexdigest()
        state["legacy_records"] += 1
        result = self.save_usage(db, event_key, delta, when, state["model"], thread, meta, "snapshot_delta")
        if result == "excluded":
            state["excluded_records"] += 1
        elif result == "conflict":
            state["conflicts"] += 1

    @staticmethod
    def save_usage(db, key, usage, when, model, thread, meta, source):
        model = str(model)[:120]
        provider = meta.get("model_provider")
        if isinstance(provider, str) and provider.strip() and provider.strip().casefold() != "openai":
            return "excluded"
        if not is_gpt_model(model):
            if source == "request" and isinstance(provider, str) and provider.strip().casefold() == "openai":
                return save_auxiliary_usage(db, key, usage, when, model, thread, meta, TOKEN_FIELDS)
            return "excluded"
        existing = db.execute("SELECT * FROM usage WHERE event_key=?", (key,)).fetchone()
        if existing and any(existing[k] is not None and usage[k] is not None and existing[k] != usage[k]
                            for k in TOKEN_FIELDS):
            # Never silently merge disagreeing records into an invalid count.
            return "conflict"
        auxiliary_conflict = source == "request" and reconcile_auxiliary_primary(db, key, usage, TOKEN_FIELDS)
        client = str(meta.get("originator") or "Codex")[:80]
        values = (key, "Codex", client, model, str(thread)[:160], time_string(when),
                  *(usage[k] for k in TOKEN_FIELDS), source, int(bool(meta.get("subagent"))))
        # Response IDs deduplicate copied histories and sessions moved into archive.
        db.execute("""INSERT INTO usage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_key) DO UPDATE SET
                model=CASE WHEN usage.model='未知模型' THEN excluded.model ELSE usage.model END,
                cached_input_tokens=COALESCE(usage.cached_input_tokens, excluded.cached_input_tokens),
                cache_write_input_tokens=COALESCE(usage.cache_write_input_tokens, excluded.cache_write_input_tokens),
                reasoning_output_tokens=COALESCE(usage.reasoning_output_tokens, excluded.reasoning_output_tokens)
            """, values)
        if auxiliary_conflict:
            return "conflict"


SUMMARY_SELECT = """COUNT(*) AS records,
        COALESCE(SUM(CASE WHEN reasoning_output_tokens IS NULL THEN 1 ELSE 0 END),0) AS reasoning_unknown_records,
        COALESCE(SUM(CASE WHEN cache_write_input_tokens IS NULL THEN 1 ELSE 0 END),0) AS write_unknown_records,
        COALESCE(SUM(input_tokens),0) AS input_tokens,
        COALESCE(SUM(output_tokens),0) AS output_tokens,
        COALESCE(SUM(total_tokens),0) AS total_tokens,
        COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens,
        COALESCE(SUM(cache_write_input_tokens),0) AS cache_write_input_tokens,
        COALESCE(SUM(reasoning_output_tokens),0) AS reasoning_output_tokens,
        COALESCE(SUM(CASE WHEN cached_input_tokens IS NULL THEN 1 ELSE 0 END),0) AS cache_unknown_records,
        COALESCE(SUM(CASE WHEN model='未知模型' THEN 1 ELSE 0 END),0) AS model_unknown_records,
        COALESCE(SUM(CASE WHEN source='snapshot_delta' THEN 1 ELSE 0 END),0) AS legacy_records,
        COALESCE(SUM(CASE WHEN source='snapshot_delta' THEN total_tokens ELSE 0 END),0) AS legacy_tokens,
        COALESCE(SUM(CASE WHEN source='request' THEN total_tokens ELSE 0 END),0) AS request_tokens,
        COALESCE(SUM(subagent),0) AS subagent_records, MAX(time) AS last_usage
"""


def complete_summary(row):
    complete = row["cache_unknown_records"] == 0 and row["records"] > 0
    row["uncached_input_tokens"] = row["input_tokens"] - row["cached_input_tokens"] if complete else None
    row["cache_hit_ratio"] = row["cached_input_tokens"] / row["input_tokens"] if complete and row["input_tokens"] else None
    row["cache_miss_ratio"] = 1 - row["cache_hit_ratio"] if row["cache_hit_ratio"] is not None else None
    return row


def summarize(db, where="", parameters=()):
    row = dict(db.execute(f"SELECT {SUMMARY_SELECT} FROM usage {where}", parameters).fetchone())
    return complete_summary(row)


def statistics(db_path, period="all", model="", now=None, software="", start_date="", end_date=""):
    from analytics import dashboard_statistics
    return dashboard_statistics(db_path, period, model, now, software, start_date, end_date)
