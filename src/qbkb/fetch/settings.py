"""抓取的礼貌参数（design.md 4.2）。discover 拉 sitemap 时也用同一个 UA 和超时。"""

from __future__ import annotations

from typing import Any

USER_AGENT = "qbkb/0.1 (+https://github.com/sunshu0/quant-blog-kb)"
TIMEOUT_SECONDS = 30
DOWNLOAD_DELAY_SECONDS = 2.0
RETRY_TIMES = 3
# 只对"过一会儿可能就好"的状态重试；403/404 不重试。
RETRY_HTTP_CODES = (408, 429, 500, 502, 503, 504, 522, 524)
# 指数退避：第 n 次重试前等 BASE * 2**(n-1) 秒，不超过 MAX；429 的 Retry-After 优先。
RETRY_BACKOFF_BASE_SECONDS = 2.0
RETRY_BACKOFF_MAX_SECONDS = 60.0
DOWNLOAD_MAXSIZE_BYTES = 20 * 1024 * 1024


def build_settings(*, http_cache: bool = False, log_level: str = "INFO") -> dict[str, Any]:
    """CrawlerProcess 用的设置。http_cache 只在开发调试时打开（design.md 4.2）。"""
    return {
        "BOT_NAME": "qbkb",
        # 退避中间件用 asyncio.sleep，依赖 asyncio reactor（Scrapy 默认值，这里写明）
        "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        "USER_AGENT": USER_AGENT,
        "ROBOTSTXT_OBEY": True,
        "CONCURRENT_REQUESTS": 1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "DOWNLOAD_DELAY": DOWNLOAD_DELAY_SECONDS,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": DOWNLOAD_DELAY_SECONDS,
        "AUTOTHROTTLE_MAX_DELAY": RETRY_BACKOFF_MAX_SECONDS,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 1.0,
        "DOWNLOAD_TIMEOUT": TIMEOUT_SECONDS,
        "DOWNLOAD_MAXSIZE": DOWNLOAD_MAXSIZE_BYTES,
        "RETRY_ENABLED": True,
        "RETRY_TIMES": RETRY_TIMES,
        "RETRY_HTTP_CODES": list(RETRY_HTTP_CODES),
        "DOWNLOADER_MIDDLEWARES": {
            "scrapy.downloadermiddlewares.retry.RetryMiddleware": None,
            "qbkb.fetch.spider.BackoffRetryMiddleware": 550,
        },
        # 非 2xx 也交给回调，由 process_response 统一写状态库
        "HTTPERROR_ALLOW_ALL": True,
        "COOKIES_ENABLED": False,
        # 远程控制接口用不上；而且在中文用户名的 Windows 上写 job 文件会报 WinError 17
        "TELNETCONSOLE_ENABLED": False,
        "REMOTE_CONTROL_ENABLED": False,
        # Windows 上没有 resource 模块，不关掉每次都会打一条警告
        "MEMUSAGE_ENABLED": False,
        "HTTPCACHE_ENABLED": http_cache,
        "LOG_LEVEL": log_level,
    }
