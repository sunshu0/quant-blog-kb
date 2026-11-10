"""读取 config/ 下的 YAML，并执行合规闸门（design.md 3.1）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def list_sites(config_dir: Path = CONFIG_DIR) -> list[str]:
    return sorted(p.stem for p in (config_dir / "sites").glob("*.yaml"))
