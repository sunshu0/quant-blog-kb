"""站点级最小请求间隔（站点配置顶层 crawl_delay，对应 robots.txt 的 Crawl-delay）。"""

from __future__ import annotations

from typing import Any

import pytest

from qbkb import config
from qbkb.fetch.settings import (
    DOWNLOAD_DELAY_SECONDS,
    apply_crawl_delay,
    build_settings,
    site_crawl_delay,
)


def test_jitter_is_off_so_delay_is_a_real_minimum() -> None:
    # Scrapy 默认 ±50% 随机，DOWNLOAD_DELAY=10.5 实际可能只等 5.25 秒
    assert build_settings()["DOWNLOAD_DELAY_JITTER"] == 0.0


def test_site_crawl_delay_missing() -> None:
    assert site_crawl_delay({}) is None


@pytest.mark.parametrize("value", [10, 10.5])
def test_site_crawl_delay_number(value: float) -> None:
    assert site_crawl_delay({"crawl_delay": value}) == float(value)


@pytest.mark.parametrize("bad", [0, -1, "10", True, [10]])
def test_site_crawl_delay_rejects_bad_values(bad: Any) -> None:
    with pytest.raises(ValueError, match="crawl_delay"):
        site_crawl_delay({"crawl_delay": bad})


def test_apply_crawl_delay_raises_default() -> None:
    out = apply_crawl_delay({}, 10.5)
    assert out["DOWNLOAD_DELAY"] == 10.5
    assert out["AUTOTHROTTLE_START_DELAY"] == 10.5
    assert out["AUTOTHROTTLE_ENABLED"] is True


def test_apply_crawl_delay_overrides_smaller_cli_value() -> None:
    # 命令行 --fetch-delay 5（关掉了 autothrottle 也一样）不能低于站点要求
    cli = {"DOWNLOAD_DELAY": 5.0, "AUTOTHROTTLE_START_DELAY": 5.0, "AUTOTHROTTLE_ENABLED": False}
    assert apply_crawl_delay(cli, 10.5)["DOWNLOAD_DELAY"] == 10.5


def test_apply_crawl_delay_keeps_larger_cli_value() -> None:
    cli = {"DOWNLOAD_DELAY": 21.0, "AUTOTHROTTLE_START_DELAY": 21.0, "AUTOTHROTTLE_ENABLED": True}
    assert apply_crawl_delay(cli, 10.5) == cli


def test_apply_crawl_delay_without_site_value_changes_nothing() -> None:
    assert apply_crawl_delay({"DOWNLOAD_DELAY": 0}, None) == {"DOWNLOAD_DELAY": 0}


def test_cfm_config_obeys_robots_crawl_delay() -> None:
    """cfm.com 的 robots.txt 写了 Crawl-delay: 10。"""
    delay = site_crawl_delay(config.load_site("cfm").raw)
    assert delay is not None and delay >= 10 > DOWNLOAD_DELAY_SECONDS


def test_man_config_has_crawl_delay() -> None:
    """man.com 的 robots.txt 没写 Crawl-delay，但站点配置仍要求 10.5 秒礼貌间隔。"""
    delay = site_crawl_delay(config.load_site("man").raw)
    assert delay == 10.5
