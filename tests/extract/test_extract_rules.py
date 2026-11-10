from __future__ import annotations

from typing import Any

import pytest

from qbkb import config
from qbkb.config import SiteConfig
from qbkb.extract.rules import DEFAULT_REMOVE, load_rules


def _site(extract: Any) -> SiteConfig:
    return SiteConfig("t", "allowed", {"extract": extract})


def test_man_config_loads() -> None:
    rules = load_rules(config.load_site("man"))
    assert [t.name for t in rules.templates] == ["insight"]
    assert rules.keep, "man.com 的版权声明必须配置成 keep"
    assert rules.remove[: len(DEFAULT_REMOVE)] == DEFAULT_REMOVE


def test_empty_extract_section_means_fallback_only() -> None:
    rules = load_rules(_site({}))
    assert rules.templates == ()
    assert rules.remove == DEFAULT_REMOVE


def test_template_matching() -> None:
    rules = load_rules(
        _site(
            {
                "templates": [
                    {"name": "a", "url_prefix": "/insights/", "require": "#x", "body": "#x"}
                ]
            }
        )
    )
    t = rules.templates[0]
    assert t.matches("https://e.test/insights/p", lambda sel: True)
    assert not t.matches("https://e.test/other/p", lambda sel: True)
    assert not t.matches("https://e.test/insights/p", lambda sel: False)


@pytest.mark.parametrize(
    ("extract", "message"),
    [
        ([], "extract 节必须是映射"),
        ({"templates": {}}, "templates"),
        ({"templates": [{"name": "a"}]}, "templates[0].body"),
        ({"templates": [{"name": "a", "body": "div[["}]}, "CSS 选择器无效"),
        ({"templates": [{"body": "div"}]}, "templates[0].name"),
        (
            {"templates": [{"name": "a", "body": "x", "tags": {"selector": "x", "pattern": "a"}}]},
            "捕获组",
        ),
        ({"min_chars": -1}, "min_chars"),
        ({"min_chars": True}, "min_chars"),
        ({"noise_keywords": "cookie"}, "noise_keywords"),
        ({"title_strip": "("}, "正则无效"),
        ({"drop_text": ["ok", "("]}, "drop_text[1]"),
        ({"remove": [1]}, "remove[0]"),
        ({"date_formats": "%Y"}, "date_formats"),
        ({"keep": "#legal"}, "keep"),
        ({"keep": [{"selector": ""}]}, "keep[0].selector"),
        ({"keep": [{"selector": "#a", "remove": "[["}]}, "keep[0].remove[0]"),
    ],
)
def test_bad_config_is_rejected_with_path(extract: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message.replace("[", r"\[").replace("]", r"\]")):
        load_rules(_site(extract))
