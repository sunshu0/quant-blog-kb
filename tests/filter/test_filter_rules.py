"""filters.yaml、评分标准、人工覆盖、评测集的读取。"""

from pathlib import Path

import pytest

from qbkb.config import CONFIG_DIR
from qbkb.filter import (
    Decision,
    Override,
    load_eval_set,
    load_filter_config,
    load_overrides,
    load_rubric,
)


def test_repo_filters_yaml_loads() -> None:
    cfg = load_filter_config()
    assert "量化方法论" in cfg.include_categories
    assert "宏观周评" in cfg.exclude_categories
    assert cfg.categories == cfg.include_categories + cfg.exclude_categories
    assert cfg.site("man").exclude_prefixes == ("/insights/a-moment-in-macro/",)
    assert cfg.site("man").min_words == 300
    assert cfg.site("unknown").min_words == 0
    assert cfg.threshold == 7 and cfg.excerpt_words == 500
    assert cfg.base_url.startswith("https://") and cfg.model


def test_keeps_needs_score_and_included_category() -> None:
    cfg = load_filter_config()
    assert cfg.keeps(7, "量化方法论")
    assert not cfg.keeps(6.9, "量化方法论")
    assert not cfg.keeps(10, "宏观周评")


def test_repo_rubric_has_version_and_categories() -> None:
    cfg = load_filter_config()
    rubric = load_rubric(cfg.rubric_version, cfg)
    assert rubric.version == cfg.rubric_version
    for cat in cfg.categories:
        assert cat in rubric.prompt
    assert "{include_categories}" not in rubric.prompt
    assert '"score"' in rubric.prompt  # 要求 JSON 输出


def test_rubric_version_must_match_file_name(tmp_path: Path) -> None:
    cfg = load_filter_config()
    (tmp_path / "v2.md").write_text("---\nversion: v1\n---\n打分\n", encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        load_rubric("v2", cfg, tmp_path)


def test_overrides_short_and_long_form(tmp_path: Path) -> None:
    path = tmp_path / "overrides.yaml"
    path.write_text(
        "overrides:\n"
        "  https://example.test/a: include\n"
        "  https://example.test/b:\n"
        "    decision: exclude\n"
        "    category: 宏观周评\n"
        "    note: 只是行情点评\n",
        encoding="utf-8",
    )
    got = load_overrides(path)
    assert got["https://example.test/a"] == Override(Decision.INCLUDE)
    assert got["https://example.test/b"] == Override(Decision.EXCLUDE, "宏观周评", "只是行情点评")


def test_overrides_missing_file_and_bad_value(tmp_path: Path) -> None:
    assert load_overrides(tmp_path / "none.yaml") == {}
    bad = tmp_path / "bad.yaml"
    bad.write_text("overrides:\n  https://example.test/a: maybe\n", encoding="utf-8")
    with pytest.raises(ValueError, match="include 或 exclude"):
        load_overrides(bad)


def test_repo_overrides_and_eval_set_load() -> None:
    assert load_overrides() == {}
    items = load_eval_set(CONFIG_DIR / "eval" / "man.yaml")
    assert len(items) >= 5
    assert set(items.values()) == {Decision.INCLUDE, Decision.EXCLUDE}


def test_filters_yaml_rejects_overlapping_categories(tmp_path: Path) -> None:
    path = tmp_path / "filters.yaml"
    path.write_text(
        "include_categories: [甲]\nexclude_categories: [甲]\n"
        "l2: {rubric_version: v1, threshold: 7}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="既收又不收"):
        load_filter_config(path)
