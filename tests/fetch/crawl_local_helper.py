"""集成测试的子进程：起一个本机 HTTP 服务（127.0.0.1，临时端口），真跑一次 run_fetch。

Twisted reactor 一个进程只能启动一次，所以每个场景单独起一个子进程。
用法：python crawl_local_helper.py <scenario> <workdir>；结果写到 <workdir>/result.json。
不访问外网。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from qbkb import store
from qbkb.config import SiteConfig
from qbkb.fetch import run_fetch
from qbkb.models import DiscoveredUrl

HITS: list[dict[str, object]] = []
BUSY_FAILURES = 2  # /insights/busy 前两次 503，之后 200


def make_handler(scenario: str) -> type[BaseHTTPRequestHandler]:
    busy_seen = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path
            status, body, headers = 200, b"<html><title>t</title><body>ok</body></html>", {}
            if path == "/robots.txt":
                status, body = 200, b"User-agent: *\nDisallow:\n"
            elif path == "/insights/busy" and busy_seen["n"] < BUSY_FAILURES:
                busy_seen["n"] += 1
                status, body = 503, b"busy"
            elif path == "/insights/slow" and scenario == "retry_after":
                status, body, headers = 429, b"slow down", {"Retry-After": "3600"}
            HITS.append({"path": path, "t": time.monotonic(), "status": status})
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


def main() -> None:
    scenario, work = sys.argv[1], Path(sys.argv[2])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(scenario))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    site = SiteConfig("local", "allowed", {"base_url": base})
    db = work / "state.db"
    conn = store.connect(db)
    slugs = ["busy", "a", "b"] if scenario == "backoff" else ["slow", "a", "b", "c"]
    for slug in slugs:
        store.upsert_discovered(conn, DiscoveredUrl(f"{base}/insights/{slug}", "local", slug))
    conn.close()
    stats = run_fetch(
        site,
        db,
        work / "raw",
        log_level="ERROR",
        settings_overrides={
            "DOWNLOAD_DELAY": 0,
            "AUTOTHROTTLE_ENABLED": False,
            "QBKB_BACKOFF_BASE": 0.5,
        },
    )
    srv.shutdown()
    conn = store.connect(db)
    rows = {
        str(r["slug"]): {"status": r["status"], "error": r["error"]}
        for r in conn.execute("SELECT slug, status, error FROM articles")
    }
    conn.close()
    result = {"stats": stats.__repr__(), "hits": HITS, "rows": rows}
    (work / "result.json").write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    main()
