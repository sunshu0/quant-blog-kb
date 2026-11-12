"""本机 127.0.0.1 HTTP 服务的真实通道观察：整站停止机制（robots 跨站、Retry-After 太久）。

被 test_fetch_channel.py 用子进程跑（run_fetch 会启动 Twisted reactor，一个进程只能跑一次）。
每个场景把 hits（所有 GET 的 path）和 stop_reason、各文章状态写进 result.json。
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

from qbkb import store
from qbkb.config import SiteConfig
from qbkb.fetch import run_fetch
from qbkb.models import DiscoveredUrl


def main() -> None:
    scenario, work = sys.argv[1], Path(sys.argv[2])
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            hits.append(self.path)
            status, body, headers = 200, b"<html>ok</html>", {"Content-Type": "text/html"}
            if self.path == "/robots.txt":
                if scenario == "robots_retry":
                    # robots.txt 本身返回 429 + 长 Retry-After：也要停站，且文章不能被漏抓
                    status, body, headers = 429, b"busy", {"Retry-After": "3600"}
                elif scenario == "robots":
                    # robots.txt 302 到不同 hostname（localhost 而非 127.0.0.1）
                    port = cast(ThreadingHTTPServer, self.server).server_port
                    status, body, headers = (
                        302,
                        b"",
                        {"Location": f"http://localhost:{port}/robots.txt"},
                    )
                else:
                    body = b"User-agent: *\nDisallow:\n"
                    headers = {"Content-Type": "text/plain"}
            elif self.path == "/a" and scenario in ("retry_after", "queue"):
                status, body, headers = 429, b"busy", {"Retry-After": "3600"}
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    db = work / "state.db"
    conn = store.connect(db)
    for slug in ("a", "b", "c"):
        store.upsert_discovered(conn, DiscoveredUrl(base + "/" + slug, "local", slug))
    conn.close()
    try:
        stats = run_fetch(
            SiteConfig("local", "allowed", {"base_url": base}),
            db,
            work / "raw",
            log_level="ERROR",
            settings_overrides={
                "HTTPPROXY_ENABLED": False,
                "CONCURRENT_REQUESTS": 3 if scenario == "queue" else 1,
                "DOWNLOAD_DELAY": 0.4 if scenario == "queue" else 0,
                "DOWNLOAD_DELAY_JITTER": 0,
                "AUTOTHROTTLE_ENABLED": False,
            },
        )
    finally:
        srv.shutdown()
        srv.server_close()
    conn = store.connect(db)
    rows = {r["url"].split("/")[-1]: r["status"] for r in store.site_rows(conn, "local")}
    conn.close()
    (work / "result.json").write_text(
        json.dumps(
            {
                "hits": hits,
                "article_hits": [p for p in hits if p in ("/a", "/b", "/c")],
                "rows": rows,
                "failed": stats.failed,
                "fetched": stats.fetched,
                "stop_reason": stats.stop_reason,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
