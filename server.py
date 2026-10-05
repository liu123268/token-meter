"""Loopback dashboard with local logs and infrequent documented official usage reads."""

import argparse
import json
import os
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from collector import statistics, connect, valid_time, time_string
from datetime import datetime, timezone, date
from adapters import LocalCollector
from analytics import export_csv
from official_usage import AccountUsageSync
from reconciliation import augment_reference, reconciliation_csv

ROOT = Path(__file__).resolve().parent
DATA_DIRECTORY = ROOT / "data"


def notify_error(message):
    # Background mode (pythonw) has no console; surface fatal startup errors visually.
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, "本地 Token 看板", 0x10)
    except Exception:
        pass


def port_busy(port, host="127.0.0.1"):
    # On Windows a second bind with SO_REUSEADDR succeeds even while another
    # dashboard is serving, so probe before binding instead of trusting bind().
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


def account_reference(db_path, include_identity=False):
    """Read cached official activity and same-date local GPT counts, never fetch here."""
    with connect(db_path) as db:
        local = {r["day"]: r["tokens"] for r in db.execute(
            "SELECT date(time,'+8 hours') day,SUM(total_tokens) tokens FROM usage "
            "WHERE software='Codex' AND is_gpt_model(model) AND time<=? GROUP BY day",
            (time_string(datetime.now(timezone.utc)),))}
    unavailable = {"status": "unavailable", "buckets": [], "local_gpt_days": local}
    if include_identity:
        unavailable["_account_fingerprint"] = None
    path = Path(db_path).parent / "codex-account-usage.json"
    if not path.is_file():
        return unavailable
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        checked = valid_time(snapshot.get("checked_at"))
        buckets = snapshot.get("daily_usage_buckets")
        if snapshot.get("status") != "available" or checked is None or not isinstance(buckets, list) or len(buckets) > 10000:
            return unavailable
        days = {}
        for bucket in buckets:
            day = bucket.get("startDate")
            tokens = bucket.get("tokens")
            if date.fromisoformat(day).isoformat() != day or type(tokens) is not int or not 0 <= tokens <= 2**53 - 1 or day in days:
                return unavailable
            days[day] = tokens
        row_times = snapshot.get("bucket_checked_at", {})
        if not isinstance(row_times, dict):
            row_times = {}
        result = {
            "status": "cached", "checked_at": time_string(checked),
            "latest_date": max(days) if days else None,
            "daily_buckets_available": snapshot.get("daily_buckets_available", True),
            "local_gpt_days": local,
            "buckets": [{"date": day, "official_tokens": tokens,
                         "local_gpt_tokens": local.get(day, 0),
                         "official_checked_at": time_string(valid_time(row_times.get(day)) or checked)}
                        for day, tokens in sorted(days.items())],
        }
        if include_identity:
            fingerprint = snapshot.get("account_fingerprint")
            result["_account_fingerprint"] = fingerprint if isinstance(fingerprint, str) and len(fingerprint) == 64 and all(c in "0123456789abcdef" for c in fingerprint) else None
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        return unavailable


def make_handler(collector, assets=None, official=None):
    assets = assets or ROOT / "public"
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # URLs and model names are not written to logs.

        def send(self, status, body, content_type="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                             "style-src 'self'; connect-src 'self'; img-src 'self'; "
                             "object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            port = self.server.server_port
            allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
            origin = self.headers.get("Origin")
            if self.headers.get("Host") not in allowed or (origin and origin not in {f"http://{h}" for h in allowed}):
                self.send(403, b'{"error":"Local access only"}')
                return
            if len(self.path) > 2048:
                self.send(414, b'{"error":"URL too long"}')
                return
            url = urlsplit(self.path)
            static = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if url.path in static:
                filename, content_type = static[url.path]
                self.send(200, (assets / filename).read_bytes(), content_type)
                return
            if url.path not in ("/api/stats", "/api/health", "/api/export",
                                "/api/reconciliation", "/api/reconciliation/export"):
                self.send(404, b'{"error":"Not found"}')
                return
            try:
                data = {"application": "token-meter", "version": 4,
                        "storage_directory": str(ROOT), "collector": collector.get_status()}
                if url.path in ("/api/reconciliation", "/api/reconciliation/export"):
                    query = parse_qs(url.query, keep_blank_values=True)
                    if url.path == "/api/reconciliation/export":
                        if set(query) - {"basis", "period"} or any(len(values) != 1 for values in query.values()):
                            raise ValueError("Invalid reconciliation filter")
                        basis = query.get("basis", ["utc"])[0]
                        period = query.get("period", ["30d"])[0]
                        if basis not in ("utc", "beijing") or period not in ("7d", "30d", "all"):
                            raise ValueError("Invalid reconciliation filter")
                    elif query:
                        raise ValueError("Reconciliation does not accept global filters")
                    reference = augment_reference(collector.db_path, account_reference(collector.db_path, include_identity=True))
                    if official is not None:
                        reference["sync"] = official.get_status()
                    if url.path == "/api/reconciliation/export":
                        self.send(200, reconciliation_csv(reference, basis, period), "text/csv; charset=utf-8")
                        return
                    data["account_reference"] = reference
                if url.path in ("/api/stats", "/api/export"):
                    query = parse_qs(url.query)
                    data.update(statistics(collector.db_path, query.get("period", ["all"])[0], query.get("model", [""])[0], software=query.get("software", [""])[0],
                        start_date=query.get("start", [""])[0], end_date=query.get("end", [""])[0]))
                    if url.path == "/api/stats":
                        data["account_reference"] = account_reference(collector.db_path)
                        if official is not None:
                            data["account_reference"]["sync"] = official.get_status()
                    if url.path == "/api/export":
                        self.send(200, export_csv(data,query.get("view",["rows"])[0],query.get("search",[""])[0]), "text/csv; charset=utf-8")
                        return
                self.send(200, json.dumps(data, ensure_ascii=False).encode())
            except ValueError:
                self.send(400, b'{"error":"Invalid filter"}')
            except Exception:
                self.send(503, b'{"error":"Local index temporarily unavailable"}')

        def do_POST(self):
            port = self.server.server_port
            allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in allowed or self.headers.get("Origin") not in {f"http://{h}" for h in allowed}:
                self.send(403, b'{"error":"Local access only"}')
                return
            if self.path != "/api/account-usage/refresh":
                self.send(405, b'{"error":"Read only"}')
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 64 or self.headers.get_content_type() != "application/json":
                    raise ValueError()
                if json.loads(self.rfile.read(length) or b"{}") != {}:
                    raise ValueError()
            except (ValueError, TypeError):
                self.send(400, b'{"error":"Invalid refresh request"}')
                return
            if official is None:
                self.send(503, b'{"error":"Official sync unavailable"}')
                return
            result = official.request_refresh()
            status = 429 if result.get("reason") == "cooldown" else 202
            self.send(status, json.dumps(result, ensure_ascii=False).encode())

    return Handler


def main():
    global DATA_DIRECTORY
    # Background mode runs under pythonw, which has no console streams; keep print() alive.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    parser = argparse.ArgumentParser(description="Codex 本地只读 Token 看板")
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "token-meter.sqlite3")
    parser.add_argument("--assets", type=Path, default=ROOT / "public")
    parser.add_argument("--port", type=int, default=18741)
    parser.add_argument("--open", action="store_true", help="启动后在浏览器中打开看板")
    args = parser.parse_args()
    DATA_DIRECTORY = args.database.parent
    collector = LocalCollector(args.codex_home, args.database)
    official = AccountUsageSync(args.database.parent / "codex-account-usage.json", args.codex_home)
    server = None
    if not port_busy(args.port):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(collector, args.assets, official))
        except OSError:
            server = None
    if server is None:
        if args.open:
            try:
                with urlopen(f"http://127.0.0.1:{args.port}/api/health", timeout=2) as response:
                    health = json.load(response)
                if health.get("application") == "token-meter" and health.get("storage_directory") == str(ROOT):
                    webbrowser.open(f"http://127.0.0.1:{args.port}")
                    print("本目录的看板已运行，已打开浏览器。", flush=True)
                    return
            except (OSError, ValueError):
                pass
        if sys.stdout is None:
            notify_error("端口 18741 被其他程序占用,无法启动看板。可换端口或结束占用程序后重试。")
        parser.exit(1, "端口不可用，可通过 --port 指定其他端口。已有服务不会被关闭。\n")
    server.daemon_threads = True
    stop = threading.Event()
    worker = threading.Thread(target=collector.run, args=(stop,), daemon=True)
    worker.start()
    account_worker = threading.Thread(target=official.run, args=(stop,), daemon=True)
    account_worker.start()
    address = f"http://127.0.0.1:{server.server_port}"
    print(f"本地 Token 看板：{address}\n按 Ctrl+C 停止。首次历史索引可能需要一些时间。", flush=True)
    if args.open:
        webbrowser.open(address)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        worker.join(timeout=5)
        account_worker.join(timeout=1)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # One bounded local traceback lets startup failures be diagnosed later.
        import traceback
        try:
            error_path = DATA_DIRECTORY / "service-error.log"
            error_path.parent.mkdir(parents=True, exist_ok=True)
            error_path.write_text(traceback.format_exc()[-32768:], encoding="utf-8")
        except OSError:
            pass
        raise
