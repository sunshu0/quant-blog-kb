"""黄金样本回归：tests/golden/man/*.html 逐个比对人工确认过的 *.json。

改了抽取规则、确认新输出是对的之后，用 QBKB_UPDATE_GOLDEN=1 重新生成期望文件，
再逐个看 git diff，确认每一处变化都是有意的。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from qbkb import config
from qbkb.extract import check_quality, extract, extract_full, load_rules

GOLDEN = Path(__file__).resolve().parents[1] / "golden"
CASES = sorted((GOLDEN / "man").glob("*.html"))
UPDATE = os.environ.get("QBKB_UPDATE_GOLDEN") == "1"


def _actual(html_path: Path, url: str) -> dict[str, Any]:
    rules = load_rules(config.load_site("man"))
    result = extract_full(html_path.read_bytes(), url, rules)
    meta = result.article.meta
    return {
        "url": url,
        "method": result.method,
        "meta": {
            "title": meta.title,
            "author": meta.author,
            "published": meta.published,
            "tags": list(meta.tags),
        },
        "n_body": result.n_body,
        "issues": check_quality(result, rules),
        "segments": [s.html for s in result.article.segments],
    }


def test_golden_cases_exist() -> None:
    assert len(CASES) >= 5


@pytest.mark.parametrize("html_path", CASES, ids=[p.stem for p in CASES])
def test_golden(html_path: Path) -> None:
    expected_path = html_path.with_suffix(".json")
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    actual = _actual(html_path, expected["url"])
    if UPDATE:
        expected_path.write_text(
            json.dumps(actual, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        return
    assert actual["method"] == expected["method"]
    assert actual["meta"] == expected["meta"]
    assert actual["issues"] == expected["issues"]
    assert actual["n_body"] == expected["n_body"]
    # 逐段比对，失败时直接指出是第几段
    for i, (a, e) in enumerate(zip(actual["segments"], expected["segments"], strict=False)):
        assert a == e, f"段落 {i} 不同"
    assert len(actual["segments"]) == len(expected["segments"])


@pytest.mark.parametrize("html_path", CASES, ids=[p.stem for p in CASES])
def test_golden_is_deterministic(html_path: Path) -> None:
    """同一个输入抽两次，段落和 body_hash 完全一样（流程幂等的前提）。"""
    url = json.loads(html_path.with_suffix(".json").read_text(encoding="utf-8"))["url"]
    rules = load_rules(config.load_site("man"))
    a = extract(html_path.read_bytes(), url, rules)
    b = extract(html_path.read_text(encoding="utf-8"), url, rules)
    assert a.segments == b.segments
    assert a.body_hash == b.body_hash
