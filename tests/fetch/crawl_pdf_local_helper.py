"""PDF 抓取的真实通道集成测试子进程：本机 HTTP 服务 + 子域 DNS 映射。

验证 PDF 请求的严格同 host 边界：example.test 的 /a.pdf 302 到 child.example.test/outside.pdf
时，子域的 PDF 正文必须没有被请求。Twisted reactor 一个进程只能启动一次，所以每个场景单独起
一个子进程。用法：python crawl_pdf_local_helper.py <scenario> <workdir>；结果写到
<workdir>/result.json。不访问外网：example.test / child.example.test 都映射到 127.0.0.1。
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

from qbkb import store
from qbkb.config import SiteConfig
from qbkb.fetch import run_fetch
from qbkb.models import DiscoveredUrl, Status

# 把测试域解析到本机。要在 run_fetch 启动 Scrapy/twisted 的 reactor 之前生效（run_fetch 才惰性
# 导入 Scrapy），所以放在模块级、main() 之前即可。
_original_getaddrinfo = socket.getaddrinfo
_original_gethostbyname = socket.gethostbyname
_TEST_HOSTS = ("example.test", "child.example.test")


def _local_getaddrinfo(host: str | bytes | None, *args: object, **kwargs: object):
    if host in _TEST_HOSTS:
        host = "127.0.0.1"
    return _original_getaddrinfo(host, *args, **kwargs)  # type: ignore[arg-type]


def _local_gethostbyname(host: str) -> str:
    if host in _TEST_HOSTS:
        return "127.0.0.1"
    return _original_gethostbyname(host)


socket.getaddrinfo = _local_getaddrinfo  # type: ignore[assignment]
socket.gethostbyname = _local_gethostbyname  # type: ignore[assignment]

HITS: list[dict[str, str]] = []


def _make_handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            HITS.append({"path": self.path, "host": self.headers.get("Host") or ""})
            server = cast(ThreadingHTTPServer, self.server)
            status, body, headers = 200, b"%PDF-1.7 offline data", {}
            if self.path == "/robots.txt":
                body = b"User-agent: *\nDisallow:\n"
            elif self.path == "/a.pdf":
                status, body, headers = (
                    302,
                    b"",
                    {"Location": f"http://child.example.test:{server.server_port}/outside.pdf"},
                )
            self.send_response(status)
            self.send_header(
                "Content-Type", "text/plain" if self.path == "/robots.txt" else "application/pdf"
            )
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


def main() -> None:
    scenario, work = sys.argv[1], Path(sys.argv[2])
    del scenario  # 当前只有一个子域场景，保留参数以对齐其它 helper 的调用方式
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://example.test:{srv.server_address[1]}"
    db = work / "state.db"
    conn = store.connect(db)
    url = base + "/a"
    raw = work / "a.html"
    raw.write_text('<html><a class="pdf" href="/a.pdf">Download</a></html>')
    store.upsert_discovered(conn, DiscoveredUrl(url, "local", "a"))
    store.set_status(conn, url, Status.FETCHED, raw_path=str(raw))
    conn.close()
    try:
        stats = run_fetch(
            SiteConfig("local", "allowed", {"base_url": base, "pdf": {"selector": "a.pdf"}}),
            db,
            work / "raw",
            log_level="ERROR",
            settings_overrides={
                "HTTPPROXY_ENABLED": False,
                "DOWNLOAD_DELAY": 0,
                "DOWNLOAD_DELAY_JITTER": 0,
                "AUTOTHROTTLE_ENABLED": False,
            },
        )
    finally:
        srv.shutdown()
        srv.server_close()
    conn = store.connect(db)
    rows = [dict(row) for row in store.site_rows(conn, "local")]
    conn.close()
    (work / "result.json").write_text(
        json.dumps({"hits": HITS, "rows": rows, "pdf_failed": stats.pdf_failed}, ensure_ascii=True),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
