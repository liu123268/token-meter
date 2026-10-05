"""Derived evidence for native OpenAI aliases, separate from GPT statistics."""

import json
import re
from datetime import timezone

HEADER_TYPE = re.compile(rb'"type"\s*:\s*"([a-z_]+)"')
MIGRATION = "native_openai_alias_v1"


def initialize_auxiliary(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS codex_auxiliary_usage (
            event_key TEXT PRIMARY KEY, software TEXT NOT NULL,
            client TEXT NOT NULL, model TEXT NOT NULL, thread_id TEXT NOT NULL,
            time TEXT NOT NULL, input_tokens INTEGER NOT NULL,
            cached_input_tokens INTEGER, cache_write_input_tokens INTEGER,
            output_tokens INTEGER NOT NULL, reasoning_output_tokens INTEGER,
            total_tokens INTEGER NOT NULL, source TEXT NOT NULL,
            subagent INTEGER NOT NULL DEFAULT 0,
            provider TEXT NOT NULL CHECK(provider='openai'),
            conflict INTEGER NOT NULL DEFAULT 0 CHECK(conflict IN (0,1))
        );
        CREATE INDEX IF NOT EXISTS codex_auxiliary_time ON codex_auxiliary_usage(time);
        CREATE TABLE IF NOT EXISTS codex_auxiliary_backfill (
            path TEXT PRIMARY KEY, identity TEXT NOT NULL, offset INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS codex_auxiliary_migrations (
            name TEXT PRIMARY KEY, completed INTEGER NOT NULL DEFAULT 0
        );
    """)


def disagrees(record, usage, fields):
    return any(record[k] is not None and usage[k] is not None
               and record[k] != usage[k] for k in fields)


def save_auxiliary_usage(db, key, usage, when, model, thread, meta, fields):
    """Only the caller's validated native record reaches this function."""
    primary = db.execute("SELECT * FROM usage WHERE event_key=?", (key,)).fetchone()
    existing = db.execute("SELECT * FROM codex_auxiliary_usage WHERE event_key=?", (key,)).fetchone()
    conflict = bool(primary and disagrees(primary, usage, fields))
    if existing and (disagrees(existing, usage, fields) or existing["model"] != model):
        db.execute("UPDATE codex_auxiliary_usage SET conflict=1 WHERE event_key=?", (key,))
        return "conflict"
    if primary and not conflict:
        if existing and not existing["conflict"]:
            db.execute("DELETE FROM codex_auxiliary_usage WHERE event_key=?", (key,))
        return "excluded"
    values = (key, "Codex", str(meta.get("originator") or "Codex")[:80], model,
              str(thread)[:160], when.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
              *(usage[k] for k in fields), "request", int(bool(meta.get("subagent"))), "openai", int(conflict))
    db.execute("""INSERT INTO codex_auxiliary_usage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_key) DO UPDATE SET
        cached_input_tokens=COALESCE(codex_auxiliary_usage.cached_input_tokens,excluded.cached_input_tokens),
        cache_write_input_tokens=COALESCE(codex_auxiliary_usage.cache_write_input_tokens,excluded.cache_write_input_tokens),
        reasoning_output_tokens=COALESCE(codex_auxiliary_usage.reasoning_output_tokens,excluded.reasoning_output_tokens),
        conflict=MAX(codex_auxiliary_usage.conflict,excluded.conflict)""", values)
    return "conflict" if conflict or (existing and existing["conflict"]) else "excluded"


def reconcile_auxiliary_primary(db, key, usage, fields):
    """Keep contradictory evidence, but never add it to the GPT count."""
    existing = db.execute("SELECT * FROM codex_auxiliary_usage WHERE event_key=?", (key,)).fetchone()
    if existing is None:
        return False
    if existing["conflict"] or disagrees(existing, usage, fields):
        db.execute("UPDATE codex_auxiliary_usage SET conflict=1 WHERE event_key=?", (key,))
        return True
    db.execute("DELETE FROM codex_auxiliary_usage WHERE event_key=?", (key,))
    return False


def backfill_file(db, path, limit, normalize_usage, valid_time, is_gpt_model, fields):
    """Replay only public metadata and native usage, never checkpoints/messages."""
    meta = {}; model = "未知模型"
    with path.open("rb") as stream:
        while stream.tell() < limit:
            line = stream.readline()
            if not line or stream.tell() > limit or not line.endswith(b"\n"):
                break
            header = HEADER_TYPE.search(line[:512])
            kind = header.group(1).decode() if header else None
            if kind not in ("session_meta", "turn_context", "token_usage_record"):
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict) or event.get("type") != kind:
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if kind == "session_meta":
                meta = {k: payload.get(k) for k in ("id", "timestamp", "originator", "model_provider")}
                meta["subagent"] = isinstance(payload.get("source"), dict) and "subagent" in payload["source"]
                continue
            if kind == "turn_context":
                if isinstance(payload.get("model"), str) and payload["model"]:
                    model = payload["model"][:120]
                continue
            provider = meta.get("model_provider")
            if not isinstance(provider, str) or provider.strip().casefold() != "openai":
                continue
            alias = str(payload.get("model") or model)[:120]
            if is_gpt_model(alias):
                continue
            usage = normalize_usage(payload.get("usage")); when = valid_time(event.get("timestamp"))
            response = payload.get("response_id"); created = valid_time(meta.get("timestamp"))
            if usage is None or when is None or not isinstance(response, str) or not response or (created is not None and when < created):
                continue
            save_auxiliary_usage(db, "codex:response:" + response, usage, when, alias,
                                 payload.get("thread_id") or meta.get("id") or "未知会话", meta, fields)


def backfill_auxiliary(db, paths, normalize_usage, valid_time, is_gpt_model, fields):
    saved = db.execute("SELECT completed FROM codex_auxiliary_migrations WHERE name=?", (MIGRATION,)).fetchone()
    if saved and saved[0]:
        return True
    successful = True
    for path in paths:
        cursor = db.execute("SELECT identity,offset,state FROM cursors WHERE path=?", (str(path),)).fetchone()
        if cursor is None:
            continue
        state = json.loads(cursor["state"])
        if not state.get("excluded_records", 0):
            continue
        done = db.execute("SELECT identity,offset FROM codex_auxiliary_backfill WHERE path=?", (str(path),)).fetchone()
        if done and done["identity"] == cursor["identity"] and done["offset"] >= cursor["offset"]:
            continue
        try:
            stat = path.stat()
            identity = f"{stat.st_dev}:{stat.st_ino}"
            if identity != cursor["identity"] or stat.st_size < cursor["offset"]:
                # The normal incremental scanner will read this replacement fresh.
                continue
            backfill_file(db, path, cursor["offset"], normalize_usage, valid_time, is_gpt_model, fields)
            db.execute("INSERT INTO codex_auxiliary_backfill VALUES (?, ?, ?) ON CONFLICT(path) DO UPDATE SET identity=excluded.identity,offset=excluded.offset",
                       (str(path), identity, cursor["offset"]))
            db.commit()
        except OSError:
            db.rollback(); successful = False
    if successful:
        db.execute("INSERT INTO codex_auxiliary_migrations VALUES (?,1) ON CONFLICT(name) DO UPDATE SET completed=1", (MIGRATION,))
        db.commit()
    return successful
