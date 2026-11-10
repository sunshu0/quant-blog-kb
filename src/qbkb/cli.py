"""命令行入口：各阶段子命令、run（按顺序跑完全部阶段）、status、eval。

退出码：0 正常；1 出错；2 参数错误（argparse）；3 额度用完，进度已保存，下次接着跑；
4 后端暂时不可用（限流 / 网络），稍后再跑。
"""

from __future__ import annotations

import argparse
import io
import logging
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import httpx

from qbkb import config, store
from qbkb.models import Scorer, Status, Translator

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_QUOTA = 3
EXIT_UNAVAILABLE = 4

STAGES = ("discover", "fetch", "extract", "filter", "translate", "export")
RESUME_HINT = "进度已保存在状态库里，下次运行同一条命令会从中断处接着跑。"

log = logging.getLogger("qbkb")


@dataclass(slots=True)
class Deps:
    """可替换的外部依赖（测试注入离线的假实现；正常运行全部为 None）。

    - sitemap_fetch：discover 拉 sitemap 的函数（URL → 字节）；
    - fetch_runner：fetch 阶段的执行方式，参数是 (args, 站点名)，返回退出码。默认在子进程里
      跑 `qbkb fetch`（Twisted reactor 一个进程只能启动一次，run 在同一进程里要串多个站点、
      测试里还要反复调用 main）；
    - scorer / translator：直接用这个后端，忽略 --scorer / --translator；
    - image_transport / sleep：图片下载用的 httpx transport 和等待函数。
    """

    sitemap_fetch: Callable[[str], bytes] | None = None
    fetch_runner: Callable[[argparse.Namespace, str], int] | None = None
    scorer: Scorer | None = None
    translator: Translator | None = None
    image_transport: httpx.BaseTransport | None = None
    sleep: Callable[[float], None] | None = None


def _utf8(stream: TextIO) -> None:
    # Windows 控制台默认 GBK，中文会乱码；统一输出 UTF-8。
    if isinstance(stream, io.TextIOWrapper):
        stream.reconfigure(encoding="utf-8")


def _cfg(args: argparse.Namespace) -> Path:
    """配置目录：--config-dir，默认 config.CONFIG_DIR（可用环境变量 QBKB_CONFIG_DIR 改）。"""
    return Path(args.config_dir) if args.config_dir else config.CONFIG_DIR


def _sites(args: argparse.Namespace) -> list[config.SiteConfig]:
    d = _cfg(args)
    names = [args.site] if args.site else config.list_sites(d)
    return [config.load_site(n, d) for n in names]


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--site", help="只处理这个站点（默认全部已配置站点）")
    p.add_argument("--db", default="state.db", help="状态库路径（默认 state.db）")
    p.add_argument("--limit", type=int, help="每个阶段最多处理多少篇")
    p.add_argument("-v", "--verbose", action="store_true", help="输出详细日志")
    p.add_argument("--config-dir", help="配置目录（默认仓库里的 config/）")


def _fetch_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--raw-dir", default="raw", help="原始 HTML 目录（默认 raw）")
    p.add_argument(
        "--fetch-delay",
        type=float,
        default=None,
        help="两次请求的间隔秒数（默认 2；只有对本机测试服务才应该调小）",
    )


def _filter_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--scorer", choices=("deepseek", "fake"), default="deepseek")
    p.add_argument("--refilter", action="store_true", help="已筛过的文章也重新判断（复用打分）")
    p.add_argument("--fake-score-quota", type=int, help="假打分后端最多打几次（测试断点续跑用）")


def _translate_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--translator", choices=("deepl", "fake"), default="deepl")
    p.add_argument(
        "--retry-rejected",
        action="store_true",
        help="重翻结构校验没过的段落（包括 needs_review 的文章）",
    )
    p.add_argument("--fake-quota", type=int, help="假翻译后端的字符额度（测试断点续跑用）")


def _export_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", default="output", help="输出目录（默认 output）")
    p.add_argument("--no-images", action="store_true", help="不下载图片，保留原始链接")
    p.add_argument("--rebuild", action="store_true", help="已发布的文章也重新生成")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qbkb", description="量化机构博客中文知识库")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-db", help="创建或升级状态库")
    p_init.add_argument("--db", default="state.db")
    sub.add_parser("sites", help="列出已配置站点及其合规结论")

    p = sub.add_parser("discover", help="拉 sitemap，把新的 / 更新过的 URL 写进状态库")
    _common(p)
    p = sub.add_parser("fetch", help="抓取 discovered 的文章（Scrapy）")
    _common(p)
    _fetch_opts(p)
    p = sub.add_parser("extract", help="从原始 HTML 抽取正文")
    _common(p)
    p.add_argument("--retry-failed", action="store_true", help="重试抽取阶段失败过的文章")
    p = sub.add_parser("filter", help="价值筛选（L1 规则、L2 打分、L3 人工覆盖）")
    _common(p)
    _filter_opts(p)
    p = sub.add_parser("translate", help="翻译 filtered 的文章")
    _common(p)
    _translate_opts(p)
    p = sub.add_parser("export", help="把 translated 的文章输出成 Markdown")
    _common(p)
    _export_opts(p)
    p = sub.add_parser("run", help="按顺序跑完全部阶段：" + " → ".join(STAGES))
    _common(p)
    _fetch_opts(p)
    _filter_opts(p)
    _translate_opts(p)
    _export_opts(p)
    p.add_argument("--skip", action="append", choices=STAGES, default=[], help="跳过某个阶段")
    p = sub.add_parser("status", help="各状态的文章数，列出 needs_review 和 failed")
    _common(p)
    p = sub.add_parser("eval", help="L2 评测：LLM 判断和人工判断的一致率")
    _common(p)
    p.add_argument("--file", type=Path, help="评测集（默认 config/eval/<site>.yaml）")
    p.add_argument("--rubric-version", help="只比较这个打分版本（例如 v1@deepseek-chat）")
    return parser


class Stopped(Exception):
    """某个阶段因为额度 / 后端不可用停下（code 是退出码）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---- 各阶段 ----------------------------------------------------------------


def stage_discover(args: argparse.Namespace, deps: Deps, site: config.SiteConfig) -> None:
    from qbkb.discover import discover_site

    conn = store.connect(args.db)
    try:
        kwargs = {} if deps.sleep is None else {"sleep": deps.sleep}
        s = discover_site(site, conn, deps.sitemap_fetch, **kwargs)
    finally:
        conn.close()
    print(
        f"[discover] {site.name}: 新 {s.new}、更新 {s.updated}、没变 {s.unchanged}、"
        f"过滤掉 {s.filtered}（sitemap {s.sitemaps_fetched} 份）"
    )
    for e in s.errors:
        print(f"  ! {e}")


def fetch_in_process(args: argparse.Namespace, site_name: str) -> int:
    """在本进程里跑 Scrapy。一个进程只能调用一次（Twisted reactor 不能重启）。"""
    from qbkb.fetch import run_fetch

    site = config.load_site(site_name, _cfg(args))
    overrides: dict[str, object] = {}
    if args.fetch_delay is not None:
        overrides = {
            "DOWNLOAD_DELAY": args.fetch_delay,
            "AUTOTHROTTLE_START_DELAY": args.fetch_delay,
            "AUTOTHROTTLE_ENABLED": args.fetch_delay > 0,
        }
    s = run_fetch(
        site,
        args.db,
        args.raw_dir,
        limit=args.limit,
        log_level="INFO" if args.verbose else "WARNING",
        settings_overrides=overrides,
    )
    print(
        f"[fetch] {site.name}: 抓到 {s.fetched}、没变（304）{s.not_modified}、"
        f"PDF {s.pdf}、失败 {s.failed}"
    )
    return EXIT_OK


def fetch_in_subprocess(args: argparse.Namespace, site_name: str) -> int:
    """在子进程里跑 `qbkb fetch`：run 在一个进程里可能要抓多个站点、被反复调用。"""
    cmd = [sys.executable, "-m", "qbkb.cli", "fetch", "--site", site_name, "--db", args.db]
    cmd += ["--raw-dir", args.raw_dir]
    if args.limit is not None:
        cmd += ["--limit", str(args.limit)]
    if args.fetch_delay is not None:
        cmd += ["--fetch-delay", str(args.fetch_delay)]
    if args.verbose:
        cmd.append("-v")
    if args.config_dir:
        cmd += ["--config-dir", str(args.config_dir)]
    sys.stdout.flush()
    sys.stderr.flush()
    # 不截获输出：抓取可能要很久，子进程的进度直接显示在终端上
    return subprocess.run(cmd, check=False).returncode


def stage_extract(args: argparse.Namespace, site: config.SiteConfig) -> None:
    from qbkb import extract

    conn = store.connect(args.db)
    try:
        s = extract.run(conn, site, retry_failed=getattr(args, "retry_failed", False))
    finally:
        conn.close()
    print(f"[extract] {site.name}: 抽取 {s.extracted}、失败 {s.failed}、质检提示 {len(s.flagged)}")
    for url, issues in s.flagged.items():
        print(f"  ? {url}：{'；'.join(issues)}")


def _make_scorer(args: argparse.Namespace, deps: Deps) -> Scorer:
    from qbkb import filter as flt

    if deps.scorer is not None:
        return deps.scorer
    cfg = flt.load_filter_config(_cfg(args) / "filters.yaml")
    if args.scorer == "fake":
        return flt.FakeScorer(cfg.rubric_version, quota_calls=args.fake_score_quota)
    rubric = flt.load_rubric(cfg.rubric_version, cfg, _cfg(args) / "rubrics")
    return flt.DeepSeekScorer.from_env(rubric, cfg)


def stage_filter(args: argparse.Namespace, deps: Deps, site: config.SiteConfig) -> None:
    from qbkb import filter as flt

    cfg = flt.load_filter_config(_cfg(args) / "filters.yaml")
    overrides = flt.load_overrides(_cfg(args) / "overrides.yaml")
    scorer = _make_scorer(args, deps)
    conn = store.connect(args.db)
    stopped: flt.FilterStopped | None = None
    try:
        try:
            s = flt.run(
                conn, site.name, cfg, scorer, overrides, refilter=args.refilter, limit=args.limit
            )
        except flt.FilterStopped as e:
            stopped, s = e, e.stats
    finally:
        conn.close()
        close = getattr(scorer, "close", None)
        if callable(close):
            close()
    print(
        f"[filter] {site.name}: 收 {s.kept}、L1 筛掉 {s.skipped_l1}、L2 筛掉 {s.skipped_l2}、"
        f"人工覆盖 {s.overridden}（打分 {s.scored} 次，复用 {s.reused} 次）"
    )
    for e in s.errors:
        print(f"  ! {e}")
    if isinstance(stopped, flt.FilterQuotaStopped):
        raise Stopped(EXIT_QUOTA, f"[filter] 打分额度用完，停下：{stopped}")
    if stopped is not None:
        raise Stopped(EXIT_UNAVAILABLE, f"[filter] 打分后端暂时不可用，停下：{stopped}")


def _make_translator(args: argparse.Namespace, deps: Deps) -> Translator:
    from qbkb.translate import DeepLTranslator, FakeTranslator

    if deps.translator is not None:
        return deps.translator
    if args.translator == "fake":
        return FakeTranslator(quota_chars=args.fake_quota)
    return DeepLTranslator.from_env()


def stage_translate(args: argparse.Namespace, deps: Deps, site: config.SiteConfig) -> None:
    from qbkb import translate as tr

    conn = store.connect(args.db)
    try:
        urls = store.list_by_status(conn, Status.FILTERED, site.name)
        if args.retry_rejected:
            urls = sorted(urls + store.list_by_status(conn, Status.NEEDS_REVIEW, site.name))
        if args.limit is not None:
            urls = urls[: args.limit]
        if not urls:
            print(f"[translate] {site.name}: 没有待翻译的文章")
            return
        translator = _make_translator(args, deps)
        glossary = tr.load_glossary(_cfg(args) / "glossary.yaml")
        need = tr.estimate_chars(conn, urls, translator.name, glossary)
        print(
            f"[translate] {site.name}: 待翻译 {len(urls)} 篇，"
            f"预计约 {need} 字符（不含命中翻译记忆的）"
        )
        done = review = failed = chars = 0
        for url in urls:
            try:
                st = tr.translate_article(
                    conn, url, translator, glossary, retry_rejected=args.retry_rejected
                )
            except tr.QuotaStopped as e:
                chars += e.stats.chars
                print(f"[translate] 完成 {done} 篇、需复查 {review} 篇，送出 {chars} 字符")
                raise Stopped(EXIT_QUOTA, f"[translate] 翻译额度用完，停下：{e}") from e
            except tr.BackendStopped as e:
                print(f"[translate] 完成 {done} 篇、需复查 {review} 篇")
                raise Stopped(EXIT_UNAVAILABLE, f"[translate] 翻译后端暂时不可用：{e}") from e
            except ValueError as e:
                store.record_failure(conn, url, f"translate: {e}")
                failed += 1
                continue
            chars += st.chars
            if st.needs_review:
                review += 1
            else:
                done += 1
        print(
            f"[translate] {site.name}: 完成 {done} 篇、需复查 {review} 篇、失败 {failed} 篇，"
            f"送出 {chars} 字符"
        )
    finally:
        conn.close()


def stage_export(args: argparse.Namespace, deps: Deps, site: config.SiteConfig) -> None:
    from qbkb import export
    from qbkb import filter as flt
    from qbkb.translate import load_glossary

    out = Path(args.out)
    cfg = flt.load_filter_config(_cfg(args) / "filters.yaml")
    images = None
    if not args.no_images:
        images = export.ImageFetcher(
            site,
            out / site.name / export.IMAGES_DIR,
            transport=deps.image_transport,
            sleep=deps.sleep or time.sleep,
        )
    conn = store.connect(args.db)
    try:
        s = export.run(
            conn,
            site,
            load_glossary(_cfg(args) / "glossary.yaml"),
            out,
            images=images,
            categories=cfg.categories,
            rebuild=args.rebuild,
            limit=args.limit,
        )
    finally:
        conn.close()
        if images is not None:
            images.close()
    print(
        f"[export] {site.name}: 发布 {s.published}（内容没变 {s.unchanged}）、"
        f"退回重翻 {len(s.requeued)}、失败 {len(s.failed)}、"
        f"下载图片 {s.images_downloaded}、图片失败 {len(s.image_failures)}"
    )
    for f in s.image_failures:
        print(f"  ! 图片 {f.image_url}：{f.reason}（保留原链接）")
    for e in s.failed:
        print(f"  ! {e}")
    print(f"  索引：{out / export.INDEX_NAME}")


# ---- 汇总命令 ----------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    try:
        counts = store.count_by_status(conn, args.site)
        total = sum(counts.values())
        print(f"共 {total} 篇" + (f"（站点 {args.site}）" if args.site else ""))
        for st in Status:
            print(f"  {st.value:<13}{counts.get(st, 0):>6}")
        for st, label in ((Status.NEEDS_REVIEW, "需要人工复查"), (Status.FAILED, "失败")):
            rows = store.list_rows(conn, [st], args.site)
            if not rows:
                continue
            print(f"{label}（{st.value}，{len(rows)} 篇）：")
            for r in rows:
                print(f"  {r['url']}\n    {r['error'] or ''}")
    finally:
        conn.close()
    return EXIT_OK


def cmd_eval(args: argparse.Namespace) -> int:
    from qbkb import filter as flt

    cfg = flt.load_filter_config(_cfg(args) / "filters.yaml")
    code = EXIT_OK
    conn = store.connect(args.db)
    try:
        for site in _sites(args):
            path = args.file or _cfg(args) / "eval" / f"{site.name}.yaml"
            if not path.is_file():
                print(f"[eval] {site.name}: 没有评测集 {path}")
                continue
            r = flt.evaluate(conn, flt.load_eval_set(path), cfg, rubric_version=args.rubric_version)
            rate = "—" if r.agreement is None else f"{r.agreement:.0%}"
            versions = "、".join(sorted(r.rubric_versions)) or "—"
            print(
                f"[eval] {site.name}: 评测集 {r.total} 条，可比较 {r.compared} 条，"
                f"一致 {r.agree} 条，一致率 {rate}（打分版本：{versions}）"
            )
            print(
                f"  收录为正类：TP {r.true_pos}、FP {r.false_pos}、"
                f"FN {r.false_neg}、TN {r.true_neg}"
            )
            for d in r.disagreements:
                print(
                    f"  ✗ {d.url}：人工 {d.human.value}，LLM {d.llm.value}"
                    f"（{d.score:g} 分，{d.category}）{d.reason}"
                )
            if r.missing:
                print(f"  还没打分 / 不在状态库：{len(r.missing)} 条")
    finally:
        conn.close()
    return code


def _run_stage(name: str, args: argparse.Namespace, deps: Deps, site: config.SiteConfig) -> int:
    if name == "discover":
        stage_discover(args, deps, site)
    elif name == "fetch":
        runner = deps.fetch_runner or (
            fetch_in_process if args.command == "fetch" else fetch_in_subprocess
        )
        code = runner(args, site.name)
        if code != EXIT_OK:
            print(f"[fetch] {site.name}: 抓取进程退出码 {code}")
            return EXIT_ERROR
    elif name == "extract":
        stage_extract(args, site)
    elif name == "filter":
        stage_filter(args, deps, site)
    elif name == "translate":
        stage_translate(args, deps, site)
    elif name == "export":
        stage_export(args, deps, site)
    return EXIT_OK


def cmd_stages(args: argparse.Namespace, deps: Deps) -> int:
    """单个阶段，或 run（按顺序跑全部阶段）。

    run 里某个阶段因为额度 / 后端不可用停下时，后面的阶段照常处理已经就绪的文章
    （比如打分额度用完，已经 filtered 的文章照样翻译、导出），最后用对应的退出码结束。
    """
    stages = [s for s in STAGES if s not in args.skip] if args.command == "run" else [args.command]
    final = EXIT_OK
    for site in _sites(args):
        for name in stages:
            try:
                code = _run_stage(name, args, deps, site)
            except Stopped as e:
                print(e)
                final = max(final, e.code)
                continue
            if code != EXIT_OK:
                return code
    if final == EXIT_QUOTA:
        print(f"额度用完。{RESUME_HINT}")
    elif final == EXIT_UNAVAILABLE:
        print(f"后端暂时不可用。{RESUME_HINT}")
    return final


def main(argv: Sequence[str] | None = None, *, deps: Deps | None = None) -> int:
    _utf8(sys.stdout)
    _utf8(sys.stderr)
    args = build_parser().parse_args(argv)
    if getattr(args, "verbose", False):
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    deps = deps or Deps()
    try:
        if args.command == "init-db":
            store.connect(args.db).close()
            print(f"状态库就绪: {args.db}")
            return EXIT_OK
        if args.command == "sites":
            for name in config.list_sites():
                site = config.load_site(name)
                print(f"{name}\t{site.compliance_decision}")
            return EXIT_OK
        if args.command == "status":
            return cmd_status(args)
        if args.command == "eval":
            return cmd_eval(args)
        return cmd_stages(args, deps)
    except Exception as e:  # 顶层兜底：给出中文错误信息和退出码，-v 时附带堆栈
        if getattr(args, "verbose", False):
            log.exception("出错")
        print(f"错误：{e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
