"""用真实页面做的检查，只在本地跑（verify.sh 跳过 network 标记）。

真实页面放在被忽略的 tests/local_samples/<站点>/<slug>.html（man、cfm），不提交（design.md D5）。
这里只断言结构性质，不比对原文内容。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qbkb import config
from qbkb.extract import check_quality, extract_full, load_rules
from qbkb.extract.clean import block_text

SAMPLES = Path(__file__).resolve().parents[1] / "local_samples" / "man"
FILES = sorted(SAMPLES.glob("*.html")) if SAMPLES.is_dir() else []


@pytest.mark.network
@pytest.mark.skipif(not FILES, reason="没有本地真实样本（tests/local_samples/man/*.html）")
@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
def test_real_page(path: Path) -> None:
    rules = load_rules(config.load_site("man"))
    url = f"https://www.man.com/insights/{path.stem}"
    result = extract_full(path.read_bytes(), url, rules)
    meta = result.article.meta
    assert result.method == "rule:insight"
    assert meta.title and meta.published and meta.tags
    assert result.keep_found
    issues = check_quality(result, rules)
    # 视频页面正文短是正常的；其他问题都不应该出现
    assert all(i.startswith("正文太短") for i in issues), issues

    texts = [block_text(s.html) for s in result.article.segments]
    joined = "\n".join(texts)
    for noise in (
        "You are now leaving",
        "Subscribe to Man Insights",
        "Related insights",
        "Glossary page",
    ):
        assert noise not in joined
    assert result.n_body < len(texts) and all(texts[result.n_body :]), "版权声明要保留"
    for s in result.article.segments:
        assert ' class="' not in s.html and " style=" not in s.html
        assert 'src="/' not in s.html and 'href="/' not in s.html


# ---- CFM（tests/local_samples/cfm/<slug>.html，网址 https://www.cfm.com/<slug>/） ----

CFM_SAMPLES = Path(__file__).resolve().parents[1] / "local_samples" / "cfm"
CFM_FILES = sorted(CFM_SAMPLES.glob("*.html")) if CFM_SAMPLES.is_dir() else []
# 只有摘要 + PDF 的栏目：正文太短是正常的
CFM_SHORT_OK = frozenset({"Academic Papers", "Whitepaper", "Commentary & Insights"})
CFM_SECTIONS = frozenset({"The Edge a CFM blog", "Commentary & Insights", *CFM_SHORT_OK})


@pytest.mark.network
@pytest.mark.skipif(not CFM_FILES, reason="没有本地真实样本（tests/local_samples/cfm/*.html）")
@pytest.mark.parametrize("path", CFM_FILES, ids=[p.stem for p in CFM_FILES])
def test_cfm_real_page(path: Path) -> None:
    rules = load_rules(config.load_site("cfm"))
    url = f"https://www.cfm.com/{path.stem}/"
    result = extract_full(path.read_bytes(), url, rules)
    meta = result.article.meta
    assert result.method == "rule:post"
    assert meta.title and meta.published
    assert "&#" not in meta.title and not meta.title.endswith("CFM")
    # 第一个标签是栏目（JSON-LD articleSection）
    assert meta.tags and meta.tags[0] in CFM_SECTIONS
    # 作者不会是机构名或网址；有作者时不会把职位粘进来
    for name in (meta.author or "").split(", "):
        assert name != "CFM" and not name.startswith("http")
    issues = check_quality(result, rules)
    if meta.tags[0] in CFM_SHORT_OK:
        assert all(i.startswith("正文太短") for i in issues), issues
    else:
        assert issues == []

    texts = [block_text(s.html) for s in result.article.segments]
    joined = "\n".join(texts)
    assert texts[0] != meta.title, "标题不该重复成第一段"
    for noise in (
        "Listen to this blog",
        "Share this article",
        "Cookie policy",
        "All Rights Reserved",
        "Client login",
        "Type to search",
    ):
        assert noise not in joined
    for s in result.article.segments:
        assert ' class="' not in s.html and " style=" not in s.html
        assert 'src="/' not in s.html and 'href="/' not in s.html
        assert "<iframe" not in s.html and "<audio" not in s.html
