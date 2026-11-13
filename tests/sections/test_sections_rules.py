"""栏目配置解析和页面判定（确定性规则，不调用 LLM）。"""

from __future__ import annotations

import copy
from datetime import date

import pytest
from lxml.html import document_fromstring
from sections_site import BASE, SECTIONS, make_site, page

from qbkb import config
from qbkb.sections import load_section_rules, normalize_key, page_section
from qbkb.sections.rules import prefix_matches


def _page(html: str, path: str = "/insights/x") -> tuple[str, list[str]]:
    rules = load_section_rules(make_site(SECTIONS))
    p = page_section(document_fromstring(html), html, BASE + path, rules)
    return p.key, list(p.issues)


def test_no_sections_means_collect_everything() -> None:
    rules = load_section_rules(make_site(None))
    assert not rules.enabled
    assert rules.collects(BASE + "/insights/anything", None)


@pytest.mark.parametrize(
    ("tagline", "key"),
    [
        ("ARTICLE | 4 MIN | WEEKLY NOTES", "ARTICLE | WEEKLY NOTES"),
        ("VIDEO | 7 MIN", "VIDEO"),
        ("ARTICLE", "ARTICLE"),
        ("ARTICLE | 6 MIN ", "ARTICLE"),
        ("article | weekly   notes", "ARTICLE | WEEKLY NOTES"),  # 没有时长、大小写和空白不同
    ],
)
def test_tagline_type_and_series(tagline: str, key: str) -> None:
    assert _page(page("T", tagline)) == (key, [])


def test_series_maps_to_its_own_section() -> None:
    rules = load_section_rules(make_site(SECTIONS))
    weekly = rules.classify(BASE + "/insights/x", "ARTICLE | WEEKLY NOTES")
    plain = rules.classify(BASE + "/insights/x", "ARTICLE")
    assert weekly is not None and weekly.id == "weekly" and not weekly.collect
    assert plain is not None and plain.id == "article" and plain.collect


def test_cross_check_mismatch_is_flagged_tagline_wins() -> None:
    key, issues = _page(page("T", "ARTICLE | 5 MIN", content_type="Video"))
    assert key == "ARTICLE"
    assert len(issues) == 1 and "dataLayer contentType 是 Video" in issues[0]
    assert "以后者为准" in issues[0]
    # 大小写不同不算；equivalent 里列出的组合也不算
    assert _page(page("T", "VIDEO | 2 MIN", content_type="Video"))[1] == []
    assert _page(page("T", "PODCAST | 40 MIN", content_type="Article"))[1] == []


def test_missing_tagline_is_unknown_section_with_issue() -> None:
    key, issues = _page(page("T", None))
    assert key == ""
    assert issues and "没有读到栏目信息" in issues[0]


def test_url_prefix_section_needs_no_page() -> None:
    key, issues = _page(page("T", None), "/insights/macro-blog/rates")
    assert key == "url:/insights/macro-blog" and issues == []
    rules = load_section_rules(make_site(SECTIONS))
    assert rules.by_url(BASE + "/insights/macro-blog") is not None  # 列表页本身也算
    assert rules.by_url(BASE + "/insights/macro-blog-review") is None


def test_prefix_matches() -> None:
    assert prefix_matches("/a/b", "/a/b/") and prefix_matches("/a/b/c", "/a/b")
    assert not prefix_matches("/a/bc", "/a/b")


def test_normalize_key() -> None:
    assert normalize_key("  article |  the  road ahead ") == "ARTICLE | THE ROAD AHEAD"


def test_estimate_by_last_path_segment() -> None:
    rules = load_section_rules(make_site(SECTIONS))
    est = rules.estimate(BASE + "/insights/weekly-notes-2025-03")
    assert est is not None and est.id == "weekly"
    assert rules.estimate(BASE + "/insights/something-else") is None


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"collect": None}, "collect"),
        ({"url_prefix": "/insights/y"}, "url_prefix 和 key"),
        ({"id": "Bad Id"}, "id"),
        ({"id": "macro"}, "重复的栏目 id"),
    ],
)
def test_bad_section_config(patch: dict[str, object], message: str) -> None:
    sections = copy.deepcopy(SECTIONS)
    item = sections["list"][1]
    for k, v in patch.items():
        if v is None:
            item.pop(k)
        else:
            item[k] = v
    with pytest.raises(ValueError, match=message):
        load_section_rules(make_site(sections))


def test_page_key_parts_must_exist() -> None:
    sections = copy.deepcopy(SECTIONS)
    sections["page_key"]["parts"] = ["type", "nope"]
    with pytest.raises(ValueError, match="nope"):
        load_section_rules(make_site(sections))


def test_man_config_lists_sections_all_uncollected() -> None:
    """用户已决定：只采「独立文章」（article），其余栏目保持 collect: false、标"待调研"。"""
    rules = load_section_rules(config.load_site("man"))
    ids = [s.id for s in rules.sections]
    assert "a-moment-in-macro" in ids and "views-from-the-floor" in ids and "article" in ids
    collected = [s.id for s in rules.sections if s.collect]
    assert collected == ["article"]
    assert all(s.status == "待调研" for s in rules.sections if not s.collect)
    article = rules.by_id("article")
    assert article is not None and article.collect and article.status == "已选择"
    assert article.survey == {
        "sampled": 20,
        "wanted": 10,
        "ratio": 0.50,
        "date": date(2026, 11, 13),
        "by": "孙硕（AI 初标，用户决定）",
    }
    macro = rules.by_id("a-moment-in-macro")
    assert macro is not None and macro.url_prefix == "/insights/a-moment-in-macro"
    vftf = rules.by_key("ARTICLE | VIEWS FROM THE FLOOR")
    assert vftf is not None and vftf.id == "views-from-the-floor"
