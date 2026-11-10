"""用真实页面做的检查，只在本地跑（verify.sh 跳过 network 标记）。

真实页面放在被忽略的 tests/local_samples/man/<slug>.html，不提交（design.md D5）。
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
