"""端到端（离线）：构造的 sitemap + 构造的页面 → discover … export，全部经过 CLI。

- 大部分用例注入 sitemap 下载、fetch 和图片下载（不起 Scrapy，快）；
- 最后一个用例起本机 HTTP 服务，真跑一次 `qbkb run`（fetch 在子进程里跑 Scrapy）。
FakeScorer + FakeTranslator，不花任何额度。
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
import yaml
from e2e_site import PAGES, PNG, SITE, make_config, responses, serve

from qbkb import cli, store
from qbkb.fetch.outcome import Kind, Outcome
from qbkb.fetch.process import apply_outcome
from qbkb.fetch.settings import USER_AGENT
from qbkb.filter import FakeScorer
from qbkb.models import Status
from qbkb.translate import FakeTranslator

BASE = "http://site.test"
KEPT = {"/insights/carry-basics": "2024-05-07", "/insights/momentum-method": "2023-06-12"}


class Env:
    """一次端到端测试的工作目录和注入的依赖。"""

    def __init__(self, root: Path, base: str = BASE) -> None:
        self.root = root
        self.cfg = make_config(root, base)
        self.db = root / "state.db"
        self.raw = root / "raw"
        self.out = root / "output"
        self.table = responses(base)
        self.image_hits: list[str] = []

    def argv(self, *extra: str) -> list[str]:
        return [
            "run",
            "--site",
            SITE,
            "--config-dir",
            str(self.cfg),
            "--db",
            str(self.db),
            "--raw-dir",
            str(self.raw),
            "--out",
            str(self.out),
            *extra,
        ]

    def sitemap_fetch(self, url: str) -> bytes:
        return self.table[urlsplit(url).path][1]

    def fake_fetch(self, args: argparse.Namespace, site: str) -> int:
        conn = store.connect(args.db)
        try:
            for url in store.list_by_status(conn, Status.DISCOVERED, site):
                body = self.table[urlsplit(url).path][1]
                apply_outcome(conn, url, Outcome(Kind.FETCHED, 200), body, Path(args.raw_dir))
        finally:
            conn.close()
        return 0

    def image_transport(self) -> httpx.MockTransport:
        def handler(req: httpx.Request) -> httpx.Response:
            self.image_hits.append(req.url.path)
            ctype, body = self.table.get(req.url.path, ("text/plain", b""))
            return httpx.Response(
                200 if body else 404, headers={"Content-Type": ctype}, content=body
            )

        return httpx.MockTransport(handler)

    def deps(
        self, scorer: FakeScorer | None = None, translator: FakeTranslator | None = None
    ) -> cli.Deps:
        return cli.Deps(
            sitemap_fetch=self.sitemap_fetch,
            fetch_runner=self.fake_fetch,
            scorer=scorer or FakeScorer(),
            translator=translator or FakeTranslator(),
            image_transport=self.image_transport(),
            sleep=lambda s: None,
        )

    def statuses(self) -> dict[str, str]:
        conn = store.connect(self.db)
        try:
            rows = conn.execute("SELECT url, status FROM articles").fetchall()
        finally:
            conn.close()
        return {urlsplit(r["url"]).path: r["status"] for r in rows}

    def snapshot(self) -> dict[str, tuple[bytes, int]]:
        return {
            p.relative_to(self.out).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(self.out.rglob("*"))
            if p.is_file()
        }


def md_name(path: str) -> str:
    """文件名 = 日期_slug.md；slug 由 discover 生成（可读部分 + hash）。"""
    from qbkb.discover import make_slug

    return f"{KEPT[path]}_{make_slug(path.removeprefix('/insights'))}.md"


def frontmatter(text: str) -> dict[str, object]:
    _, head, _ = text.split("---\n", 2)
    data = yaml.safe_load(head)
    assert isinstance(data, dict)
    return data


def test_full_pipeline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = Env(tmp_path)
    assert cli.main(env.argv(), deps=env.deps()) == cli.EXIT_OK
    assert env.statuses() == {
        "/insights/carry-basics": Status.PUBLISHED,
        "/insights/momentum-method": Status.PUBLISHED,
        "/insights/climate-note": Status.SKIPPED,  # L2：ESG/气候
        "/insights/a-moment-in-macro/rates-week": Status.SKIPPED,  # L1：排除栏目
        "/insights/short-clip": Status.SKIPPED,  # L1：太短
    }
    out = capsys.readouterr().out
    assert "[discover] local: 新 5" in out and "[export] local: 发布 2" in out

    for path, date in KEPT.items():
        main = env.out / SITE / md_name(path)
        text = main.read_text(encoding="utf-8")
        fm = frontmatter(text)
        assert fm["source_url"] == BASE + path
        assert fm["published"] == date
        assert fm["firm"] == "Example Capital" and fm["author"] == "Test Author"
        assert fm["translator"] == "fake" and fm["rubric_version"] == "v1@fake"
        assert str(fm["title_zh"]).startswith("【译】")
        assert fm["value_score"] == 8.0 and fm["category"]
        assert "> 来源：Example Capital" in text
        assert "趋势跟踪（trend following）" in text  # 术语第一次出现附英文
        assert "](images/" in text  # 图片本地化
        assert "---\n\n## 【译】Important information" in text  # 版权声明（译文）保留
        bilingual = (env.out / "bilingual" / SITE / md_name(path)).read_text(encoding="utf-8")
        assert "Paragraph 0 about" in bilingual and "【译】Paragraph 0 about" in bilingual
        assert f"](../../{SITE}/images/" in bilingual

    images = sorted(p.name for p in (env.out / SITE / "images").iterdir())
    assert len(images) == 2
    assert all((env.out / SITE / "images" / n).read_bytes() == PNG for n in images)

    index = (env.out / "index.md").read_text(encoding="utf-8")
    assert "共 2 篇" in index
    for path in KEPT:
        assert f"({SITE}/{md_name(path)})" in index
        assert f"(bilingual/{SITE}/{md_name(path)})" in index

    assert cli.main(["status", "--db", str(env.db)]) == 0
    status = capsys.readouterr().out
    assert "published         2" in status and "skipped           3" in status


def test_idempotent(tmp_path: Path) -> None:
    env = Env(tmp_path)
    assert cli.main(env.argv(), deps=env.deps()) == 0
    first_db, first_files = env.statuses(), env.snapshot()
    scorer, translator = FakeScorer(), FakeTranslator()
    env.image_hits.clear()
    assert cli.main(env.argv(), deps=env.deps(scorer, translator)) == 0
    assert env.statuses() == first_db
    assert env.snapshot() == first_files  # 内容和修改时间都没变
    assert scorer.calls == [] and translator.calls == [] and env.image_hits == []
    # 强制重新生成：内容一样，文件不重写
    assert cli.main(env.argv("--rebuild"), deps=env.deps()) == 0
    assert env.snapshot() == first_files


def test_resume_after_translation_quota(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reference = Env(tmp_path / "ref")
    full = FakeTranslator()
    assert cli.main(reference.argv(), deps=reference.deps(translator=full)) == 0
    capsys.readouterr()

    env = Env(tmp_path / "run")
    # 够翻完第一篇（约 53%），不够第二篇
    first = FakeTranslator(quota_chars=full.chars_used * 2 // 3)
    assert cli.main(env.argv(), deps=env.deps(translator=first)) == cli.EXIT_QUOTA
    out = capsys.readouterr().out
    assert "额度用完" in out and "下次运行同一条命令会从中断处接着跑" in out
    states = env.statuses()
    assert Status.PUBLISHED in states.values()  # 翻完的那篇照样导出
    assert Status.FILTERED in states.values()

    second = FakeTranslator()
    assert cli.main(env.argv(), deps=env.deps(translator=second)) == cli.EXIT_OK
    assert env.statuses() == reference.statuses()
    # 断点续跑不重复计费：两次加起来正好等于一次跑完
    assert first.chars_used + second.chars_used == full.chars_used
    ref_files = {k: v[0] for k, v in reference.snapshot().items()}
    got_files = {k: v[0] for k, v in env.snapshot().items()}
    # translated_at 不同，比较去掉这一行后的内容
    strip = lambda b: b"\n".join(x for x in b.split(b"\n") if not x.startswith(b"translated_at"))  # noqa: E731
    assert {k: strip(v) for k, v in got_files.items()} == {
        k: strip(v) for k, v in ref_files.items()
    }


def test_resume_after_scoring_quota(tmp_path: Path) -> None:
    env = Env(tmp_path)
    assert cli.main(env.argv(), deps=env.deps(FakeScorer(quota_calls=1))) == cli.EXIT_QUOTA
    assert list(env.statuses().values()).count(Status.PUBLISHED) == 1
    assert Status.EXTRACTED in env.statuses().values()
    scorer = FakeScorer()
    assert cli.main(env.argv(), deps=env.deps(scorer)) == cli.EXIT_OK
    assert len(scorer.calls) == 2  # 只打剩下的两篇（另两篇被 L1 筛掉，不打分）
    assert list(env.statuses().values()).count(Status.PUBLISHED) == 2


def _db_rows(db: Path) -> list[sqlite3.Row]:
    conn = store.connect(db)
    try:
        return conn.execute("SELECT * FROM articles ORDER BY url").fetchall()
    finally:
        conn.close()


def test_real_fetch_against_local_server(tmp_path: Path) -> None:
    """不注入 fetch：run 在子进程里跑 Scrapy，抓本机 HTTP 服务；图片也从本机下载。"""
    with serve() as (base, hits):
        env = Env(tmp_path, base)
        code = cli.main(
            env.argv("--scorer", "fake", "--translator", "fake", "--fetch-delay", "0"),
        )
        assert code == cli.EXIT_OK
    rows = _db_rows(env.db)
    assert {r["status"] for r in rows} == {Status.PUBLISHED, Status.SKIPPED}
    assert sum(r["status"] == Status.PUBLISHED for r in rows) == 2
    assert all(Path(r["raw_path"]).is_file() for r in rows)
    for p in PAGES:
        assert p.path in hits.paths
    assert "/robots.txt" in hits.paths and "/sitemap.xml" in hits.paths
    images = [p for p in hits.paths if p.startswith("/files/")]
    assert sorted(images) == ["/files/carry-fig1.png", "/files/momentum-fig1.png"]
    assert all(a.startswith("qbkb/") for a in hits.agents)
    assert USER_AGENT.startswith("qbkb/")
    assert len(list((env.out / SITE / "images").glob("*.png"))) == 2
