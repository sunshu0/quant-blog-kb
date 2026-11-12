"""CLI translate 的异常分级：单篇请求错误继续下一篇，后端鉴权失败整体停下（退出码 1）。"""

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from qbkb import cli, store
from qbkb.models import DiscoveredUrl, Segment, Status, segment_hash
from qbkb.translate import (
    ArticleTranslationError,
    BackendAuthError,
    FakeTranslator,
    FixedTranslationError,
    Glossary,
    fixed_translation_errors,
    translate_article,
)

SITE = "man"


def _db(tmp_path: Path, slugs: list[str]) -> Path:
    db = tmp_path / "state.db"
    conn = store.connect(db)
    for slug in slugs:
        url = f"https://www.man.com/insights/{slug}"
        store.upsert_discovered(conn, DiscoveredUrl(url, SITE, slug))
        store.replace_segments(conn, url, [Segment(0, f"<p>poison-{slug} text</p>")])
        store.set_status(conn, url, Status.EXTRACTED)
    conn.close()
    return db


def _states(db: str | Path) -> dict[str, tuple[str, str | None]]:
    conn = store.connect(db)
    out = {r["slug"]: (r["status"], r["error"]) for r in store.site_rows(conn, SITE)}
    conn.close()
    return out


class FailOn(FakeTranslator):
    """含 marker 的段落抛给定异常，其余照常翻译。"""

    def __init__(self, marker: str, exc: Exception) -> None:
        super().__init__()
        self.marker = marker
        self.exc = exc

    def translate(
        self, segments: Sequence[str], glossary: Mapping[str, str], context: str = ""
    ) -> list[str]:
        if any(self.marker in s for s in segments):
            raise self.exc
        return super().translate(segments, glossary, context)


@pytest.mark.usefixtures("man_without_sections")
def test_article_error_is_recorded_and_next_article_continues(tmp_path: Path) -> None:
    db = _db(tmp_path, ["a", "b", "c"])
    deps = cli.Deps(
        translator=FailOn("poison-b", ArticleTranslationError("Azure 返回 1 条，送出 2 条"))
    )
    code = cli.main(["translate", "--site", SITE, "--db", str(db)], deps=deps)
    assert code == cli.EXIT_ATTENTION
    states = _states(db)
    assert states["a"][0] == Status.TRANSLATED and states["c"][0] == Status.TRANSLATED
    assert states["b"][0] == Status.NEEDS_REVIEW and "translate:" in (states["b"][1] or "")


@pytest.mark.usefixtures("man_without_sections")
def test_backend_auth_error_stops_whole_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _db(tmp_path, ["a", "b", "c"])
    deps = cli.Deps(translator=FailOn("poison-b", BackendAuthError("DeepL 鉴权失败（HTTP 403）")))
    code = cli.main(["translate", "--site", SITE, "--db", str(db)], deps=deps)
    assert code == cli.EXIT_ERROR
    assert "鉴权失败" in capsys.readouterr().out
    states = _states(db)
    # a 已经翻完；b 的鉴权失败是后端整体问题，停下，c 不再处理
    assert states["a"][0] == Status.TRANSLATED
    assert states["b"][0] == Status.EXTRACTED and states["c"][0] == Status.EXTRACTED


@pytest.mark.usefixtures("man_without_sections")
def test_article_without_segments_marks_needs_review_and_continues(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = store.connect(db)
    for slug in ("a", "b"):
        url = f"https://www.man.com/insights/{slug}"
        store.upsert_discovered(conn, DiscoveredUrl(url, SITE, slug))
        if slug == "b":
            store.replace_segments(conn, url, [Segment(0, "<p>ok text</p>")])
        store.set_status(conn, url, Status.EXTRACTED)
    conn.close()
    code = cli.main(
        ["translate", "--site", SITE, "--db", str(db)], deps=cli.Deps(translator=FakeTranslator())
    )
    assert code == cli.EXIT_ATTENTION
    states = _states(db)
    assert states["a"][0] == Status.NEEDS_REVIEW and "没有段落" in (states["a"][1] or "")
    assert states["b"][0] == Status.TRANSLATED


@pytest.mark.usefixtures("man_without_sections")
def test_consecutive_article_errors_stop_and_restore(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _db(tmp_path, ["a", "b", "c"])

    class AlwaysReject(FakeTranslator):
        def translate(
            self, segments: Sequence[str], glossary: Mapping[str, str], context: str = ""
        ) -> list[str]:
            raise ArticleTranslationError("DeepL 拒绝这次请求（HTTP 404）：Glossary not found")

    code = cli.main(
        ["translate", "--site", SITE, "--db", str(db)],
        deps=cli.Deps(translator=AlwaysReject()),
    )
    assert code == cli.EXIT_ERROR
    assert "连续 3 篇" in capsys.readouterr().out
    states = _states(db)
    # 连续同类错误被当成配置问题：这几篇恢复原状态，不留一堆 needs_review
    for slug in ("a", "b", "c"):
        assert states[slug][0] == Status.EXTRACTED


@pytest.mark.usefixtures("man_without_sections")
def test_non_consecutive_article_errors_do_not_stop(tmp_path: Path) -> None:
    db = _db(tmp_path, ["a", "b", "c"])

    class RejectExceptB(FakeTranslator):
        def translate(
            self, segments: Sequence[str], glossary: Mapping[str, str], context: str = ""
        ) -> list[str]:
            if any("poison-b" in s for s in segments):
                return super().translate(segments, glossary, context)
            raise ArticleTranslationError("DeepL 拒绝这次请求（HTTP 404）：Glossary not found")

    code = cli.main(
        ["translate", "--site", SITE, "--db", str(db)],
        deps=cli.Deps(translator=RejectExceptB()),
    )
    # a、c 同类失败但中间隔着成功的 b，不连续，不触发"连续同类错误就停"
    assert code == cli.EXIT_ATTENTION
    states = _states(db)
    assert states["b"][0] == Status.TRANSLATED
    assert states["a"][0] == Status.NEEDS_REVIEW and states["c"][0] == Status.NEEDS_REVIEW


def test_fixed_translation_mismatch_is_a_config_error(tmp_path: Path) -> None:
    src = "<p>Important disclaimer.</p>"
    bad = "<p>重要声明。"  # 缺闭合 </p>，结构不一致
    db = tmp_path / "state.db"
    conn = store.connect(db)
    url = "https://www.man.com/insights/x"
    store.upsert_discovered(conn, DiscoveredUrl(url, SITE, "x"))
    store.replace_segments(conn, url, [Segment(0, src)])
    conn.close()
    g = Glossary("1", {}, fixed={segment_hash(src): bad})
    conn = store.connect(db)
    with pytest.raises(FixedTranslationError):
        translate_article(conn, url, FakeTranslator(), g)
    conn.close()


def test_fixed_translation_errors_only_reports_mismatch(tmp_path: Path) -> None:
    good, good_zh = "<p>Good.</p>", "<p>好。</p>"
    bad, bad_zh = "<p>Bad.</p>", "<p>坏。"  # 结构不一致
    db = tmp_path / "state.db"
    conn = store.connect(db)
    url = "https://www.man.com/insights/x"
    store.upsert_discovered(conn, DiscoveredUrl(url, SITE, "x"))
    store.replace_segments(conn, url, [Segment(0, good), Segment(1, bad)])
    g = Glossary("1", {}, fixed={segment_hash(good): good_zh, segment_hash(bad): bad_zh})
    problems = fixed_translation_errors(conn, [url], g)
    assert len(problems) == 1 and "和原文结构不一致" in problems[0]  # 只报不一致的那条
    assert fixed_translation_errors(conn, [], g) == []
    conn.close()
