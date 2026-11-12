"""本机 HTTP 服务的公共部分。"""

from __future__ import annotations

import socket
from http.server import ThreadingHTTPServer


class LocalServer(ThreadingHTTPServer):
    """独占端口的 ThreadingHTTPServer。

    标准库的 HTTPServer 打开了 SO_REUSEADDR；在 Windows 上这表示"可以和别人共用同一个端口"，
    第二个控制台会悄悄绑到同一端口上、请求被随机分走。这里关掉它，并在 Windows 上要求独占。
    """

    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()
