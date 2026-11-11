"""读取 config/ 下的 YAML，并执行合规闸门（design.md 3.1）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

# 默认指向仓库里的 config/（源码目录运行，即 uv sync 的可编辑安装）；
# 打包安装或换配置目录时用环境变量 QBKB_CONFIG_DIR 覆盖。
CONFIG_DIR = Path(
    os.environ.get("QBKB_CONFIG_DIR") or Path(__file__).resolve().parents[2] / "config"
)
ALLOWED_DECISIONS = frozenset({"allowed", "permission_needed", "blocked"})


class ComplianceError(RuntimeError):
    """站点没有通过合规检查，爬虫拒绝运行。"""


@dataclass(frozen=True, slots=True)
class SiteConfig:
    """一个站点的配置。discover/extract 等部分由各条线自己解析 raw。"""

    name: str
    compliance_decision: str
    raw: dict[str, Any]


def load_site(name: str, config_dir: Path = CONFIG_DIR) -> SiteConfig:
    path = config_dir / "sites" / f"{name}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: 顶层必须是映射")
    compliance = data.get("compliance")
    if not isinstance(compliance, dict) or "decision" not in compliance:
        raise ValueError(f"{path}: 缺少 compliance.decision")
    decision = compliance["decision"]
    if decision not in ALLOWED_DECISIONS:
        raise ValueError(f"{path}: compliance.decision 必须是 {sorted(ALLOWED_DECISIONS)} 之一")
    return SiteConfig(name=name, compliance_decision=decision, raw=data)


def ensure_allowed(site: SiteConfig) -> None:
    """任何会访问站点的代码在开始前都必须调用它。"""
    if site.compliance_decision != "allowed":
        raise ComplianceError(f"站点 {site.name} 的合规结论是 {site.compliance_decision}，不能抓取")


LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_local_host(host: str | None) -> bool:
    """本机测试站点：localhost、127.0.0.1、::1、*.localhost、*.test（RFC 2606 保留域名）。

    只有这些站点可以把抓取 / 图片下载的间隔调到默认值以下。
    """
    h = (host or "").lower().rstrip(".")
    return h in LOCAL_HOSTS or h.endswith((".localhost", ".test")) or h == "test"


def is_local_site(site: SiteConfig) -> bool:
    base = site.raw.get("base_url")
    return isinstance(base, str) and is_local_host(urlsplit(base).hostname)


def list_sites(config_dir: Path = CONFIG_DIR) -> list[str]:
    return sorted(p.stem for p in (config_dir / "sites").glob("*.yaml"))
