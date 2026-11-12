"""全局测试夹具。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from qbkb import config


@pytest.fixture
def man_without_sections(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """仓库 config/ 的副本，man 站点去掉 sections（不分栏目、全部采集），并设为 CLI 的默认配置目录。

    给不关心栏目的 CLI 测试用：它们直接往状态库里放 filtered / extracted 的文章，没有栏目信息；
    仓库里的 man.yaml 栏目全是 collect: false，这些文章不会被翻译。栏目本身的测试见 tests/sections。
    """
    cfg = tmp_path_factory.mktemp("cfg") / "config"
    shutil.copytree(config.CONFIG_DIR, cfg)
    path = cfg / "sites" / "man.yaml"
    man = yaml.safe_load(path.read_text(encoding="utf-8"))
    man.pop("sections", None)
    path.write_text(yaml.safe_dump(man, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    return cfg
