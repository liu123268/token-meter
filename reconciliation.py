"""Compare cached account activity with local usage evidence.

Only known GPT records feed the main dashboard. Explicit OpenAI native aliases
are separate evidence for reconciliation, not proof of official billing scope.
"""
import csv
import io
from datetime import date, datetime, timedelta, timezone

from collector import connect, time_string, valid_time

OFFICIAL_SOURCE = "Codex app-server account/usage/read"
LOCAL_SOURCE = "Codex local explicit GPT usage records"
AUXILIARY_SOURCE = "Codex native explicit OpenAI non-GPT-labelled usage"
LOCAL_ZONES = {"beijing": "Asia/Shanghai", "utc": "UTC"}
MAX_INTEGER = 2**53 - 1


def _safe_date(value):
    if not isinstance(value, str) or len(value) != 10 or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Invalid row date")
    return value


def _safe_count(value, nullable=False):
    if nullable and value is None:
        return None
    if type(value) is not int or not 0 <= value <= MAX_INTEGER:
        raise ValueError("Invalid token count")
    return value


def _safe_time(value):
    if value is None:
        return ""
    parsed = valid_time(value)
    if parsed is None:
        raise ValueError("Invalid snapshot time")
    return time_string(parsed)


def augment_reference(db_path, reference, now=None):
    """Keep GPT totals and add deduplicated explicit OpenAI auxiliary evidence."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("An aware current time is required")
    local_daily = {}
    auxiliary_daily = {}
    with connect(db_path) as db:
        auxiliary_available = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='codex_auxiliary_usage'").fetchone() is not None
        for basis, modifier in (("beijing", "+8 hours"), ("utc", "+0 hours")):
            local_daily[basis] = {
                row["day"]: {key: row[key] for key in ("tokens", "records", "legacy_tokens", "legacy_records")}
                for row in db.execute(
                    "SELECT date(time,?) day,SUM(total_tokens) tokens,COUNT(*) records,"
                    "SUM(CASE WHEN source='snapshot_delta' THEN total_tokens ELSE 0 END) legacy_tokens,"
                    "SUM(CASE WHEN source='snapshot_delta' THEN 1 ELSE 0 END) legacy_records "
                    "FROM usage WHERE software='Codex' AND is_gpt_model(model) AND time<=? "
                    "GROUP BY day", (modifier, time_string(now))) if row["day"] is not None
            }
            auxiliary_daily[basis] = {}
            if auxiliary_available:
                for row in db.execute(
                        "SELECT date(a.time,?) day,a.model,SUM(a.total_tokens) tokens,COUNT(*) records "
                        "FROM codex_auxiliary_usage a WHERE a.provider='openai' AND a.conflict=0 "
                        "AND a.software='Codex' AND a.source='request' AND NOT is_gpt_model(a.model) "
                        "AND a.time<=? AND NOT EXISTS(SELECT 1 FROM usage u WHERE u.event_key=a.event_key) "
                        "GROUP BY day,a.model", (modifier, time_string(now))):
                    if row["day"] is None:
                        continue
                    item = auxiliary_daily[basis].setdefault(row["day"], {"tokens": 0, "records": 0, "conflict_records": 0, "models": []})
                    item["tokens"] += row["tokens"]
                    item["records"] += row["records"]
                    item["models"].append({"model": row["model"], "tokens": row["tokens"], "records": row["records"]})
                for row in db.execute(
                        "SELECT date(time,?) day,COUNT(*) records FROM codex_auxiliary_usage "
                        "WHERE provider='openai' AND conflict<>0 AND time<=? GROUP BY day",
                        (modifier, time_string(now))):
                    if row["day"] is not None:
                        item = auxiliary_daily[basis].setdefault(row["day"], {"tokens": 0, "records": 0, "conflict_records": 0, "models": []})
                        item["conflict_records"] = row["records"]
    official = {row["date"]: row for row in reference.get("buckets", [])}
    dates = sorted(set(official) | set(local_daily["beijing"]) | set(local_daily["utc"])
        | set(auxiliary_daily["beijing"]) | set(auxiliary_daily["utc"]))
    empty = {"tokens": 0, "records": 0, "legacy_tokens": 0, "legacy_records": 0}
    auxiliary_empty = {"tokens": 0, "records": 0, "conflict_records": 0, "models": []}
    rows = []
    for day in dates:
        account = official.get(day, {})
        beijing = local_daily["beijing"].get(day, empty)
        utc = local_daily["utc"].get(day, empty)
        checked = account.get("official_checked_at")
        item = {"date": day, "official_tokens": account.get("official_tokens"),
            "local_gpt_tokens": beijing["tokens"], "local_gpt_utc_tokens": utc["tokens"],
            "beijing_records": beijing["records"], "utc_records": utc["records"],
            "beijing_legacy_tokens": beijing["legacy_tokens"], "utc_legacy_tokens": utc["legacy_tokens"],
            "beijing_legacy_records": beijing["legacy_records"], "utc_legacy_records": utc["legacy_records"],
            "official_checked_at": checked,
            "returned_in_latest_read": bool(checked and checked == reference.get("checked_at")
                and reference.get("daily_buckets_available") is not False)}
        for basis, local in (("beijing", beijing), ("utc", utc)):
            auxiliary = auxiliary_daily[basis].get(day, auxiliary_empty)
            item.update({basis + "_auxiliary_tokens": auxiliary["tokens"],
                basis + "_auxiliary_records": auxiliary["records"],
                basis + "_auxiliary_conflict_records": auxiliary["conflict_records"],
                basis + "_auxiliary_conflicts": auxiliary["conflict_records"],
                basis + "_auxiliary_models": auxiliary["models"],
                basis + "_observed_tokens": local["tokens"] + auxiliary["tokens"],
                basis + "_observed_records": local["records"] + auxiliary["records"]})
        rows.append(item)
    public_reference = {key: value for key, value in reference.items() if key != "_account_fingerprint"}
    return {**public_reference,
        "local_gpt_days": {day: item["tokens"] for day, item in local_daily["beijing"].items()},
        "local_gpt_utc_days": {day: item["tokens"] for day, item in local_daily["utc"].items()},
        "local_daily": local_daily, "auxiliary_daily": auxiliary_daily, "auxiliary_available": auxiliary_available,
        "rows": rows,
        "official_daily_timezone": "unknown", "local_account_attribution": "unknown",
        "official_statistics_cutoff": "unknown", "official_checked_at_meaning": "interface_read_time",
        "official_source": OFFICIAL_SOURCE, "local_source": LOCAL_SOURCE,
        "auxiliary_source": AUXILIARY_SOURCE,
    }


def reconciliation_csv(reference, basis="utc", period="30d", now=None):
    """Export source-separated aggregate evidence."""
    if basis not in LOCAL_ZONES or period not in ("7d", "30d", "all"):
        raise ValueError("Invalid reconciliation filter")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("An aware current time is required")
    today = now.astimezone(timezone(timedelta(hours=8))).date()
    start = today - timedelta(days=(7 if period == "7d" else 30) - 1) if period != "all" else None
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(("date", "official_tokens", "local_gpt_tokens", "difference_official_minus_local",
        "difference_ratio_of_official", "local_records", "legacy_tokens", "legacy_records",
        "local_day_boundary", "official_daily_timezone", "local_account_attribution",
        "official_checked_at", "returned_in_latest_read", "official_source", "local_source",
        "official_last_successful_read", "auxiliary_tokens", "auxiliary_records", "observed_local_tokens",
        "observed_local_records", "unexplained_official_minus_observed", "auxiliary_conflict_records",
        "auxiliary_source", "official_statistics_cutoff", "legacy_difference_scope", "unexplained_ratio_of_official"))
    for row in reference.get("rows", []):
        day = _safe_date(row["date"])
        if start is not None and not start.isoformat() <= day <= today.isoformat():
            continue
        account = _safe_count(row.get("official_tokens"), nullable=True)
        local = _safe_count(row["local_gpt_utc_tokens" if basis == "utc" else "local_gpt_tokens"])
        delta = account - local if account is not None else None
        ratio = format(delta / account, ".8f") if account else ""
        auxiliary = _safe_count(row.get(basis + "_auxiliary_tokens", 0))
        observed = _safe_count(row.get(basis + "_observed_tokens", local + auxiliary))
        writer.writerow((day, account, local, delta, ratio,
            _safe_count(row[basis + "_records"]), _safe_count(row[basis + "_legacy_tokens"]),
            _safe_count(row.get(basis + "_legacy_records", 0)), LOCAL_ZONES[basis], "unknown", "unknown",
            _safe_time(row.get("official_checked_at")), "true" if row.get("returned_in_latest_read") is True else "false",
            OFFICIAL_SOURCE, LOCAL_SOURCE, _safe_time(reference.get("checked_at")), auxiliary,
            _safe_count(row.get(basis + "_auxiliary_records", 0)), observed,
            _safe_count(row.get(basis + "_observed_records", row[basis + "_records"])),
            account - observed if account is not None else None,
            _safe_count(row.get(basis + "_auxiliary_conflict_records", 0)), AUXILIARY_SOURCE, "unknown",
            "legacy_gpt_scope",
            format((account - observed) / account, ".8f") if account else ""))
    return ("\ufeff" + stream.getvalue()).encode("utf-8")
