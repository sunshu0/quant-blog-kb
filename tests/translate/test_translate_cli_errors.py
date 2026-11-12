"""CLI translate 的异常分级：单篇请求错误继续下一篇，后端鉴权失败整体停下（退出码 1）。"""

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from qbkb import cli, store
from qbkb.models import DiscoveredUrl, Segment, Status
from qbkb.translate import ArticleTranslationError, BackendAuthError, FakeTranslator

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
