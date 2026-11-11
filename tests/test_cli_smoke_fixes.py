"""真实冒烟后的 CLI 修正：filter 显示 LLM 用量，translate 前后显示真实的已用 / 上限。"""

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from qbkb import cli, store
from qbkb.models import DiscoveredUrl, Score, Segment, Status, body_hash
from qbkb.translate import FREE_MONTHLY_CHARS, FakeTranslator

URL = "https://www.man.com/insights/what-is-volatility"


def _db(tmp_path: Path, status: Status) -> Path:
    db = tmp_path / "state.db"
    conn = store.connect(db)
    store.upsert_discovered(conn, DiscoveredUrl(URL, "man", "v"))
    segs = [Segment(0, "<p>" + "word " * 400 + "</p>")]
    store.replace_segments(conn, URL, segs)
    store.set_status(conn, URL, status, title="What is volatility", body_hash=body_hash(segs))
    conn.close()
    return db


class CountingScorer:
    """带用量计数的打分后端（和 DeepSeekScorer 同名的三个计数器）。"""

    version = "v2@counting"

    def __init__(self) -> None:
        self.requests = 0
        self.tokens = 0
        self.format_retries = 0

    def score(self, title: str, excerpt: str) -> Score:
        self.requests += 2
        self.tokens += 1500
        self.format_retries += 1
        return Score(8.0, "基础概念", True, "讲概念", self.version)


def test_filter_prints_llm_usage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _db(tmp_path, Status.EXTRACTED)
    deps = cli.Deps(scorer=CountingScorer())
    assert cli.main(["filter", "--site", "man", "--db", str(db)], deps=deps) == 0
    out = capsys.readouterr().out
    assert "LLM 用量：请求 2 次（其中格式重问 1 次），共 1500 token" in out


def test_filter_with_fake_scorer_prints_no_usage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _db(tmp_path, Status.EXTRACTED)
    assert cli.main(["filter", "--site", "man", "--db", str(db), "--scorer", "fake"]) == 0
    assert "LLM 用量" not in capsys.readouterr().out


class MeteredTranslator(FakeTranslator):
    """有 usage() 的假翻译后端：已用字符跟着翻译增长。"""

    def __init__(self, used: int | None, limit: int | None, *, broken: bool = False) -> None:
        super().__init__()
        self.used, self.limit, self.broken = used, limit, broken

    def translate(
        self, segments: Sequence[str], glossary: Mapping[str, str], context: str = ""
    ) -> list[str]:
        out = super().translate(segments, glossary, context)
        if self.used is not None:
            self.used += sum(len(s) for s in segments)
        return out

    def usage(self) -> tuple[int | None, int | None]:
        if self.broken:
            raise RuntimeError("usage 接口挂了")
        return self.used, self.limit


def _translate(tmp_path: Path, translator: FakeTranslator) -> int:
    db = _db(tmp_path, Status.FILTERED)
    return cli.main(
        ["translate", "--site", "man", "--db", str(db)], deps=cli.Deps(translator=translator)
    )


def test_translate_prints_real_quota_before_and_after(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    t = MeteredTranslator(73_921, 1_000_000)
    assert _translate(tmp_path, t) == 0
    out = capsys.readouterr().out
    assert "翻译前本月额度：已用 73921 / 上限 1000000 字符（剩余 926079）" in out
    assert t.used is not None and t.used > 73_921
    assert f"翻译后本月额度：已用 {t.used} / 上限 1000000 字符" in out
    assert "默认值" not in out


def test_translate_quota_falls_back_to_constant(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert FREE_MONTHLY_CHARS == 1_000_000
    assert _translate(tmp_path, MeteredTranslator(10, None)) == 0
    out = capsys.readouterr().out
    assert f"已用 10 / 上限 {FREE_MONTHLY_CHARS} 字符" in out and "按 API Free 的默认值" in out


def test_translate_survives_a_broken_usage_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _translate(tmp_path, MeteredTranslator(0, 0, broken=True)) == 0
    out = capsys.readouterr().out
    assert f"已用未知 / 上限 {FREE_MONTHLY_CHARS} 字符" in out and "完成 1 篇" in out


def test_fake_translator_prints_no_quota(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _translate(tmp_path, FakeTranslator()) == 0
    assert "本月额度" not in capsys.readouterr().out
