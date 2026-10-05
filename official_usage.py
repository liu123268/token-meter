"""Read Codex account token activity through the official stdio app-server API.

The dashboard never reads credential files directly. The official client uses
its existing login; no model requests or external listeners are started.
Only a validated last-good snapshot is persisted; failures never replace it.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone

SOURCE = "Codex app-server account/usage/read"
MAX_INTEGER = 2**53 - 1
MAX_BUCKETS = 10000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SUMMARY_FIELDS = (
    "lifetimeTokens", "peakDailyTokens", "longestRunningTurnSec",
    "currentStreakDays", "longestStreakDays",
)
SUPPORTED_ACCOUNTS = {"chatgpt", "chatgptAuthTokens", "agentIdentity", "personalAccessToken"}


class OfficialUsageError(RuntimeError):
    """A safe, typed error: messages never contain subprocess or account data."""

    MESSAGES = {
        "client_missing": "未找到官方 Codex 客户端，请安装或更新 Codex 后重试。",
        "client_start": "无法启动官方 Codex 用量读取进程，稍后自动重试。",
        "timeout": "官方用量读取超时，稍后自动重试。",
        "client_closed": "官方 Codex 用量读取进程提前结束，稍后自动重试。",
        "invalid_response": "官方用量响应格式异常，已保留上次成功结果。",
        "unsupported": "当前 Codex 客户端不支持该官方接口，请更新后重试。",
        "authentication": "当前登录方式不支持官方账户用量，请在 Codex 中检查登录。",
        "upstream": "官方账户用量暂时无法读取，已保留上次成功结果。",
        "storage": "官方用量已读取，但无法保存快照，已保留上次成功结果。",
    }

    def __init__(self, code):
        self.code = code if code in self.MESSAGES else "upstream"
        super().__init__(self.MESSAGES[self.code])


def _utc_now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.isoformat()


def _valid_time(value):
    if not isinstance(value, str) or len(value) > 64:
        raise OfficialUsageError("invalid_response")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return _iso(parsed.astimezone(timezone.utc))
    except ValueError:
        raise OfficialUsageError("invalid_response") from None


def _integer(value, nullable=False):
    if nullable and value is None:
        return None
    if type(value) is not int or not 0 <= value <= MAX_INTEGER:
        raise OfficialUsageError("invalid_response")
    return value


def _buckets(value):
    if not isinstance(value, list) or len(value) > MAX_BUCKETS:
        raise OfficialUsageError("invalid_response")
    days = {}
    for item in value:
        if not isinstance(item, dict):
            raise OfficialUsageError("invalid_response")
        day = item.get("startDate")
        try:
            if not isinstance(day, str) or len(day) != 10 or date.fromisoformat(day).isoformat() != day:
                raise ValueError()
        except ValueError:
            raise OfficialUsageError("invalid_response") from None
        if day in days:
            raise OfficialUsageError("invalid_response")
        days[day] = _integer(item.get("tokens"))
    return [{"startDate": day, "tokens": tokens} for day, tokens in sorted(days.items())]


def _normalize_usage(result, fingerprint=None, client_version=None, checked_at=None):
    if (not isinstance(result, dict) or not isinstance(result.get("summary"), dict)
            or not any(key in result["summary"] for key in SUMMARY_FIELDS)
            or "dailyUsageBuckets" not in result):
        raise OfficialUsageError("invalid_response")
    checked_at = _valid_time(checked_at or _iso(_utc_now()))
    available = result["dailyUsageBuckets"] is not None
    buckets = _buckets(result["dailyUsageBuckets"]) if available else []
    snapshot = {
        "checked_at": checked_at, "source": SOURCE, "status": "available",
        "summary": {key: _integer(result["summary"].get(key), nullable=True) for key in SUMMARY_FIELDS},
        "daily_usage_buckets": buckets, "daily_buckets_available": available,
        "account_fingerprint": fingerprint,
        "bucket_checked_at": {bucket["startDate"]: checked_at for bucket in buckets},
    }
    if isinstance(client_version, str) and re.fullmatch(r"[0-9][A-Za-z0-9.+_-]{0,63}", client_version):
        snapshot["client_version"] = client_version
    return snapshot


def _read_snapshot(path):
    """Load only allowed fields, including safe legacy snapshots without an identity."""
    try:
        if path.stat().st_size > MAX_RESPONSE_BYTES:
            return None
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict) or raw.get("status") != "available":
            return None
        fingerprint = raw.get("account_fingerprint")
        if not (isinstance(fingerprint, str) and re.fullmatch(r"[a-f0-9]{64}", fingerprint)):
            fingerprint = None
        normalized = _normalize_usage(
            {"summary": raw.get("summary"), "dailyUsageBuckets": raw.get("daily_usage_buckets")},
            fingerprint, raw.get("client_version"), raw.get("checked_at"),
        )
        if raw.get("daily_buckets_available") is False:
            normalized["daily_buckets_available"] = False
        timestamps = raw.get("bucket_checked_at")
        if isinstance(timestamps, dict):
            for bucket in normalized["daily_usage_buckets"]:
                day = bucket["startDate"]
                if day in timestamps:
                    normalized["bucket_checked_at"][day] = _valid_time(timestamps[day])
        return normalized
    except (OSError, ValueError, TypeError, OfficialUsageError):
        return None


def _merge_snapshots(previous, current):
    """Replace matching days; never add cumulative buckets or mix unknown accounts."""
    identity = current.get("account_fingerprint")
    if not previous or not identity or previous.get("account_fingerprint") != identity:
        return current
    days = {item["startDate"]: item["tokens"] for item in previous["daily_usage_buckets"]}
    timestamps = dict(previous["bucket_checked_at"])
    for item in current["daily_usage_buckets"]:
        day = item["startDate"]
        days[day] = item["tokens"]
        timestamps[day] = current["bucket_checked_at"][day]
    # Keep the most recent dates if the service's history reaches the bounded limit.
    kept_days = sorted(days)[-MAX_BUCKETS:]
    return {
        **current,
        "daily_usage_buckets": [{"startDate": day, "tokens": days[day]} for day in kept_days],
        "bucket_checked_at": {day: timestamps[day] for day in kept_days},
    }


def _atomic_save(path, snapshot):
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(snapshot, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except (OSError, ValueError, TypeError):
        raise OfficialUsageError("storage") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _find_codex():
    configured = shutil.which("codex.exe")
    if configured and Path(configured).is_file():
        return Path(configured)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        directory = Path(local) / "OpenAI" / "Codex" / "bin"
        try:
            candidates = [path for path in directory.glob("*/codex.exe") if path.is_file()]
            if candidates:
                return max(candidates, key=lambda path: path.stat().st_mtime_ns)
        except OSError:
            pass
    raise OfficialUsageError("client_missing")


def fetch_official_usage(codex_home, timeout_seconds=25):
    """Run just initialize, account/read(false), and account/usage/read."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    executable = _find_codex()
    process = None
    reader_thread = None
    reader_stop = threading.Event()
    lines = queue.Queue(maxsize=32)
    deadline = time.monotonic() + timeout_seconds

    def enqueue(kind, value=None):
        while not reader_stop.is_set():
            try:
                lines.put((kind, value), timeout=0.1)
                return
            except queue.Full:
                pass

    def read_lines():
        try:
            while not reader_stop.is_set():
                line = process.stdout.readline(MAX_RESPONSE_BYTES + 1)
                if not line:
                    enqueue("closed")
                    return
                if len(line) > MAX_RESPONSE_BYTES:
                    enqueue("invalid")
                    return
                enqueue("line", line)
        except (OSError, ValueError):
            enqueue("closed")
        finally:
            # Only this thread closes its buffered stream; another thread could
            # otherwise block forever on its readline() lock after a failed kill.
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass

    def send(message):
        try:
            process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
            process.stdin.flush()
        except (OSError, ValueError):
            raise OfficialUsageError("client_closed") from None

    def request(request_id, method, params=None):
        payload = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        send(payload)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OfficialUsageError("timeout")
            try:
                kind, line = lines.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue
            if kind != "line":
                raise OfficialUsageError("invalid_response" if kind == "invalid" else "client_closed")
            try:
                message = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                raise OfficialUsageError("invalid_response") from None
            if not isinstance(message, dict):
                raise OfficialUsageError("invalid_response")
            if type(message.get("id")) is not int or message["id"] != request_id:
                continue
            if "error" in message:
                error = message["error"]
                if isinstance(error, dict) and error.get("code") == -32601:
                    raise OfficialUsageError("unsupported")
                raise OfficialUsageError("upstream")
            result = message.get("result")
            if not isinstance(result, dict):
                raise OfficialUsageError("invalid_response")
            return result

    try:
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(Path(codex_home).expanduser())
        try:
            process = subprocess.Popen(
                [str(executable), "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=environment, shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except OSError:
            raise OfficialUsageError("client_start") from None
        reader_thread = threading.Thread(target=read_lines, name="official-usage-stdout", daemon=True)
        reader_thread.start()
        initialized = request(1, "initialize", {"clientInfo": {
            "name": "token_meter_dashboard", "title": "Token Meter Dashboard", "version": "1.0.0",
        }})
        send({"method": "initialized", "params": {}})
        account = request(2, "account/read", {"refreshToken": False}).get("account")
        if not isinstance(account, dict) or account.get("type") not in SUPPORTED_ACCOUNTS:
            raise OfficialUsageError("authentication")
        email = account.get("email")
        fingerprint = None
        if isinstance(email, str) and email.strip() and len(email) <= 512:
            identity = account["type"] + "\0" + email.strip().casefold()
            fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        usage = request(3, "account/usage/read")
        agent = initialized.get("userAgent", "")
        version = re.search(r"\b(?:codex|codex_cli_rs)/([0-9][A-Za-z0-9.+_-]{0,63})", agent) if isinstance(agent, str) else None
        return _normalize_usage(usage, fingerprint, version.group(1) if version else None)
    finally:
        reader_stop.set()
        if process is not None:
            # EOF lets our own app-server finish normally before forced cleanup.
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass
            if process.poll() is None:
                try:
                    process.wait(timeout=0.3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                        process.wait(timeout=1)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            if reader_thread is not None:
                reader_thread.join(timeout=0.1)


class AccountUsageSync:
    """Single-job background refresh with independent automatic and manual timers."""

    def __init__(self, snapshot_path, codex_home, interval_seconds=3600,
                 min_refresh_seconds=300, timeout_seconds=25):
        for value in (interval_seconds, min_refresh_seconds, timeout_seconds):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("refresh timing values must be positive")
        self.snapshot_path = Path(snapshot_path)
        self.codex_home = Path(codex_home)
        self.interval_seconds = interval_seconds
        self.min_refresh_seconds = min_refresh_seconds
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = None
        self._closed = False
        self._running = False
        self._busy = False
        self._phase = "waiting"
        self._last_attempt = None
        self._last_attempt_mono = None
        self._next_refresh = None
        self._next_refresh_mono = 0.0
        self._last_error = None

    def _status_locked(self):
        retry = 0
        if self._last_attempt_mono is not None:
            retry = max(0, math.ceil(self.min_refresh_seconds - (time.monotonic() - self._last_attempt_mono)))
        return {
            "phase": self._phase, "last_attempt": self._last_attempt,
            "next_refresh": self._next_refresh, "last_error": self._last_error,
            "interval_seconds": self.interval_seconds, "min_refresh_seconds": self.min_refresh_seconds,
            "retry_after_seconds": retry,
        }

    def get_status(self):
        with self._lock:
            return self._status_locked()

    def _start_locked(self):
        self._busy = True
        self._phase = "syncing"
        self._last_attempt = _iso(_utc_now())
        self._last_attempt_mono = time.monotonic()
        self._next_refresh = None
        self._next_refresh_mono = float("inf")
        threading.Thread(target=self._refresh, name="official-usage-refresh", daemon=True).start()

    def request_refresh(self):
        with self._lock:
            if self._closed or (self._stop is not None and self._stop.is_set()):
                accepted, reason = False, "stopped"
            elif self._busy:
                accepted, reason = False, "already_syncing"
            elif self._status_locked()["retry_after_seconds"]:
                accepted, reason = False, "cooldown"
            else:
                self._start_locked()
                accepted, reason = True, "started"
            result = {"accepted": accepted, "reason": reason, **self._status_locked()}
        self._wake.set()
        return result

    def _refresh(self):
        error = None
        try:
            current = fetch_official_usage(self.codex_home, self.timeout_seconds)
            previous = _read_snapshot(self.snapshot_path)
            with self._lock:
                stopped = self._closed or (self._stop is not None and self._stop.is_set())
                if not stopped:
                    # Coordinate the commit with run() closing; no late replacement.
                    _atomic_save(self.snapshot_path, _merge_snapshots(previous, current))
        except OfficialUsageError as failure:
            error = str(failure)
        except Exception:
            # Never expose exception text: it can contain paths or account response data.
            error = OfficialUsageError.MESSAGES["upstream"]
        finally:
            with self._lock:
                self._busy = False
                self._last_error = error
                self._phase = "error" if error else "ready"
                if not self._closed and not (self._stop is not None and self._stop.is_set()):
                    delay = 900 if error else self.interval_seconds
                    self._next_refresh = _iso(_utc_now() + timedelta(seconds=delay))
                    self._next_refresh_mono = time.monotonic() + delay
                else:
                    self._next_refresh = None
                    self._next_refresh_mono = float("inf")
            self._wake.set()

    def run(self, stop):
        with self._lock:
            if self._running or self._closed:
                return
            self._running = True
            self._stop = stop
        try:
            while not stop.is_set():
                with self._lock:
                    if stop.is_set() or self._closed:
                        break
                    if not self._busy and time.monotonic() >= self._next_refresh_mono:
                        self._start_locked()
                    delay = min(1.0, max(0.01, self._next_refresh_mono - time.monotonic()))
                self._wake.wait(delay)
                self._wake.clear()
        finally:
            with self._lock:
                self._closed = True
                self._running = False
                self._next_refresh = None
                self._next_refresh_mono = float("inf")

