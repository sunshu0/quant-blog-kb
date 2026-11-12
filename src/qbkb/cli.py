"""命令行入口：各阶段子命令、run（按顺序跑完默认流程）、status、sections、eval。

退出码见 EXIT_CODES_HELP。
"""

from __future__ import annotations

import argparse
import io
import logging
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, cast

import httpx

from qbkb import config, store
from qbkb.fetch.settings import DOWNLOAD_DELAY_SECONDS as DEFAULT_FETCH_DELAY
from qbkb.models import Scorer, Status, Translator

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_QUOTA = 3
EXIT_UNAVAILABLE = 4
EXIT_ATTENTION = 5
# 同时出现多种情况时，按这个顺序取第一个（先告诉用户"要等一会儿 / 等额度再跑"）
_STOP_PRIORITY = (EXIT_UNAVAILABLE, EXIT_QUOTA, EXIT_ATTENTION)
# 连续这么多篇被后端以同样原因（ArticleTranslationError）拒绝，就当是系统性的配置问题
# （术语表被删、目标语言配置错等），停下让用户查配置，而不是把整个队列逐篇标成 needs_review。
MAX_CONSECUTIVE_ARTICLE_ERRORS = 3

EXIT_CODES_HELP = """退出码：
  0  正常完成
  1  出错（配置错误、缺 key、网络错误等），看 stderr
  2  参数错误
  3  额度用完（翻译；单独运行 filter 时也包括打分）：进度已保存，额度恢复后运行同一条命令接着跑
  4  后端暂时不可用（限流 / 网络）：稍后运行同一条命令接着跑
  5  跑完了，但有文章需要人工处理：抓取 / 抽取 / 翻译 / 导出失败、翻译需复查、
     因缺译文退回重翻、sitemap 部分失败、发现了配置里没有的新栏目（qbkb status 查看）
同时出现多种情况时按 4、3、5 的顺序取第一个。"""

# 默认流程（v2，design.md 2）：采集阶段只有确定性规则，不调用 LLM。
# filter（L2 打分）保留为可选的加工命令 `qbkb filter`，不在 run / 控制台 / 每天自动更新里。
STAGES = ("discover", "fetch", "extract", "translate", "export")
# 查翻译额度（只用来显示）最多等这么久
USAGE_TIMEOUT_S = 5.0
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
        "--url-file",
        type=Path,
        help="只抓这个文件里列出的网址（每行一个，须属于该站点；qbkb sections 抽样时用）",
    )
    p.add_argument(
        "--fetch-delay",
        type=float,
        default=None,
        help=(
            f"两次请求的间隔秒数（默认 {DEFAULT_FETCH_DELAY:g}）。只有本机测试站点"
            "（localhost、127.0.0.1、*.test）可以调小，真实站点只能调大"
        ),
    )


def _filter_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--scorer", choices=("deepseek", "fake"), default="deepseek")
    p.add_argument("--refilter", action="store_true", help="已筛过的文章也重新判断（复用打分）")
    p.add_argument("--fake-score-quota", type=int, help="假打分后端最多打几次（测试断点续跑用）")


def _translate_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--translator",
        choices=("deepl", "fake", "demo"),
        default="deepl",
        help="翻译后端（demo：控制台演示模式用，示例文章自带中文，不联网）",
    )
    p.add_argument(
        "--url",
        action="append",
        default=[],
        help="只翻译这篇文章（可以重复；控制台的「重翻」按钮用）",
    )
    p.add_argument(
        "--retry-rejected",
        action="store_true",
        help="重翻结构校验没过的段落（包括 needs_review 的文章）",
    )
    p.add_argument(
        "--recheck",
        action="store_true",
        help=(
            "已翻译 / 已发布 / 需复查的文章也按现在的校验规则、术语表和固定译文重新过一遍："
            "术语没变、校验合格的段落直接复用（不花额度）；含改过的术语的段落会重翻（花额度，"
            "开始前打印预计字符数）；校验不合格的进 needs_review（配合 --retry-rejected 重翻）。"
            "没有段落变化的文章保持原状态"
        ),
    )
    p.add_argument("--fake-quota", type=int, help="假翻译后端的字符额度（测试断点续跑用）")
    p.add_argument(
        "--titles",
        action="store_true",
        help="只补翻标题：已翻译 / 已发布但还没有中文标题的文章（走翻译记忆），补完重新导出",
    )


def _export_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", default="output", help="输出目录（默认 output）")
    p.add_argument("--no-images", action="store_true", help="不下载图片，保留原始链接")
    p.add_argument("--rebuild", action="store_true", help="已发布的文章也重新生成")


def build_parser() -> argparse.ArgumentParser:
    fmt = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(
        prog="qbkb",
        description="量化机构博客中文知识库",
        epilog=EXIT_CODES_HELP,
        formatter_class=fmt,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-db", help="创建或升级状态库")
    p_init.add_argument("--db", default="state.db")
    sub.add_parser("sites", help="列出已配置站点及其合规结论")

    def stage(name: str, help: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help, epilog=EXIT_CODES_HELP, formatter_class=fmt)

    p = stage("discover", "拉 sitemap，把新的 / 更新过的 URL 写进状态库")
    _common(p)
    p = stage("fetch", "抓取 discovered 的文章（Scrapy）")
    _common(p)
    _fetch_opts(p)
    p = stage("extract", "从原始 HTML 抽取正文")
    _common(p)
    p.add_argument("--retry-failed", action="store_true", help="重试抽取阶段失败过的文章")
    p = stage(
        "filter",
        "可选的加工步骤：L2 LLM 打分（不在 run 的默认流程里；会把文章标成 filtered / skipped）",
    )
    _common(p)
    _filter_opts(p)
    p = stage("translate", "翻译 extracted 的文章（只翻采集栏目里的）")
    _common(p)
    _translate_opts(p)
    p = stage("export", "把 translated 的文章输出成 Markdown")
    _common(p)
    _export_opts(p)
    p = stage("run", "按顺序跑完全部阶段：" + " → ".join(STAGES))
    _common(p)
    _fetch_opts(p)
    _translate_opts(p)
    _export_opts(p)
    p.add_argument("--skip", action="append", choices=STAGES, default=[], help="跳过某个阶段")
    p = stage("summarize", "独立生成中文摘要（调用 LLM，不在默认run流程中）")
    _common(p)
    p.add_argument(
        "--input-chars", type=int, default=24000, help="正文输入字符上限，超出会截取并提示"
    )
    p = stage("table", "从现有状态库输出 CSV 和 JSONL（不调用外部服务）")
    _common(p)
    p.add_argument("--out", default="output", help="输出目录（默认 output）")
    p = stage("status", "各状态的文章数、各栏目的篇数、新栏目提醒，列出 needs_review 和 failed")
    _common(p)
    p = stage(
        "sections",
        "栏目调研：每个栏目的篇数、固定种子随机抽样的标题（CSV），标好要 / 不要后再运行一次算占比",
    )
    _common(p)
    _fetch_opts(p)
    p.add_argument("--sample", type=int, default=20, help="每个栏目抽几篇（默认 20）")
    p.add_argument(
        "--seed", type=int, default=2026, help="随机种子（默认 2026，同一种子结果可复现）"
    )
    p.add_argument(
        "--csv",
        type=Path,
        help="抽样表路径（默认 output/sections/<站点>.csv；output/ 不进仓库）",
    )
    p.add_argument(
        "--resample",
        action="store_true",
        help="重新抽样（已经标过的按网址保留）；不加时抽样表已存在就只读标记、算占比",
    )
    p.add_argument(
        "--fetch",
        type=int,
        default=0,
        metavar="N",
        help="先随机抓 N 篇还没判定栏目的页面再统计（走正常的限速和合规闸门）",
    )
    p = stage("eval", "L2 评测：LLM 判断和人工判断的一致率")
    _common(p)
    p.add_argument("--file", type=Path, help="评测集（默认 config/eval/<site>.yaml）")
    p.add_argument("--rubric-version", help="只比较这个打分版本（例如 v2@deepseek-flash）")
    p = sub.add_parser("console", help="启动本地网页控制台（只监听 127.0.0.1），并打开浏览器")
    p.add_argument("--port", type=int, default=None, help="端口（默认 8790，被占用时自动换）")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.add_argument("--root", type=Path, help="项目目录（默认这份代码所在的仓库）")
    return parser


class Stopped(Exception):
    """某个阶段因为额度 / 后端不可用停下（code 是退出码）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---- 各阶段 ----------------------------------------------------------------


def stage_discover(args: argparse.Namespace, deps: Deps, site: config.SiteConfig) -> int:
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
    _print_sections(args, site, "discover")
    return EXIT_ATTENTION if s.errors else EXIT_OK


def _print_sections(args: argparse.Namespace, site: config.SiteConfig, stage: str) -> None:
    """按栏目配置推进 / 退回状态，打印一行（没有变化不打印）。"""
    from qbkb import sections

    conn = store.connect(args.db)
    try:
        st = sections.apply_sections(conn, site)
    finally:
        conn.close()
    if st.unselected or st.reselected:
        print(
            f"[{stage}] {site.name}: 标为未选栏目 {st.unselected} 篇、"
            f"栏目改成采集后回到流程 {st.reselected} 篇"
        )


def fetch_delay_overrides(delay: float | None, site: config.SiteConfig) -> dict[str, object]:
    """--fetch-delay 对应的 Scrapy 设置。真实站点不能低于默认间隔（调小时提示并用默认值）。"""
    if delay is None:
        return {}
    if delay < DEFAULT_FETCH_DELAY and not config.is_local_site(site):
        print(
            f"[fetch] {site.name} 不是本机测试站点，间隔不能低于 {DEFAULT_FETCH_DELAY:g} 秒，"
            f"忽略 --fetch-delay {delay:g}"
        )
        return {}
    return {
        "DOWNLOAD_DELAY": delay,
        "AUTOTHROTTLE_START_DELAY": delay,
        "AUTOTHROTTLE_ENABLED": delay > 0,
    }


def fetch_in_process(args: argparse.Namespace, site_name: str) -> int:
    """在本进程里跑 Scrapy。一个进程只能调用一次（Twisted reactor 不能重启）。"""
    from qbkb.fetch import run_fetch

    site = config.load_site(site_name, _cfg(args))
    overrides = fetch_delay_overrides(args.fetch_delay, site)
    url_file: Path | None = getattr(args, "url_file", None)
    urls = None
    if url_file is not None:
        urls = [u.strip() for u in url_file.read_text(encoding="utf-8").splitlines() if u.strip()]
    s = run_fetch(
        site,
        args.db,
        args.raw_dir,
        urls=urls,
        limit=args.limit,
        log_level="INFO" if args.verbose else "WARNING",
        settings_overrides=overrides,
    )
    print(
        f"[fetch] {site.name}: 抓到 {s.fetched}、没变（304）{s.not_modified}、"
        f"PDF {s.pdf}、失败 {s.failed}"
    )
    return EXIT_ATTENTION if s.failed else EXIT_OK


def fetch_in_subprocess(args: argparse.Namespace, site_name: str) -> int:
    """在子进程里跑 `qbkb fetch`：run 在一个进程里可能要抓多个站点、被反复调用。"""
    cmd = [sys.executable, "-m", "qbkb.cli", "fetch", "--site", site_name, "--db", args.db]
    cmd += ["--raw-dir", args.raw_dir]
    if args.limit is not None:
        cmd += ["--limit", str(args.limit)]
    if args.fetch_delay is not None:
        cmd += ["--fetch-delay", str(args.fetch_delay)]
    if getattr(args, "url_file", None) is not None:
        cmd += ["--url-file", str(args.url_file)]
    if args.verbose:
        cmd.append("-v")
    if args.config_dir:
        cmd += ["--config-dir", str(args.config_dir)]
    sys.stdout.flush()
    sys.stderr.flush()
    # 不截获输出：抓取可能要很久，子进程的进度直接显示在终端上
    return subprocess.run(cmd, check=False).returncode


def stage_extract(args: argparse.Namespace, site: config.SiteConfig) -> int:
    from qbkb import extract

    conn = store.connect(args.db)
    try:
        s = extract.run(conn, site, retry_failed=getattr(args, "retry_failed", False))
    finally:
        conn.close()
    print(f"[extract] {site.name}: 抽取 {s.extracted}、失败 {s.failed}、质检提示 {len(s.flagged)}")
    if s.legacy or s.backfilled:
        print(
            f"  v1 老数据：重新抽取 {s.legacy} 篇（按 v2 规则判定栏目后进入流程），"
            f"已翻译 / 已发布的补记栏目 {s.backfilled} 篇"
        )
    for url, issues in s.flagged.items():
        print(f"  ? {url}：{'；'.join(issues)}")
    if s.sections.unselected or s.sections.reselected:
        print(
            f"  栏目：标为未选栏目 {s.sections.unselected} 篇、"
            f"栏目改成采集后回到流程 {s.sections.reselected} 篇"
        )
    _print_new_sections(s.new, site)
    # 质检提示只是提示，不算；新栏目要人确认，算"需要处理"
    return EXIT_ATTENTION if s.failed or s.new else EXIT_OK


def _print_new_sections(new: Sequence[object], site: config.SiteConfig) -> None:
    from qbkb.sections import NewSection

    items = [n for n in new if isinstance(n, NewSection)]
    if not items:
        return
    print(
        f"  ! 发现 {len(items)} 个配置里没有的栏目（只记录，不采集；确认后加进 "
        f"config/sites/{site.name}.yaml 的 sections）："
    )
    for n in items:
        print(f"    {n.label}：{n.count} 篇，例如 {n.example}")


def _make_scorer(args: argparse.Namespace, deps: Deps) -> Scorer:
    from qbkb import filter as flt

    if deps.scorer is not None:
        return deps.scorer
    cfg = flt.load_filter_config(_cfg(args) / "filters.yaml")
    if args.scorer == "fake":
        return flt.FakeScorer(cfg.rubric_version, quota_calls=args.fake_score_quota)
    rubric = flt.load_rubric(cfg.rubric_version, cfg, _cfg(args) / "rubrics")
    return flt.DeepSeekScorer.from_env(rubric, cfg)


def scorer_usage_line(scorer: object) -> str | None:
    """打分后端这次运行的用量（请求数、token、格式重问次数）；后端不统计就返回 None。"""
    requests = getattr(scorer, "requests", None)
    tokens = getattr(scorer, "tokens", None)
    if not isinstance(requests, int) or not isinstance(tokens, int):
        return None
    retries = getattr(scorer, "format_retries", 0)
    truncated = getattr(scorer, "truncated_retries", 0)
    return (
        f"  LLM 用量：请求 {requests} 次（其中格式重问 {retries} 次、"
        f"截断重问 {truncated} 次），共 {tokens} token"
    )


def stage_filter(args: argparse.Namespace, deps: Deps, site: config.SiteConfig) -> int:
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
    usage = scorer_usage_line(scorer)
    if usage:
        print(usage)
    for e in s.errors:
        print(f"  ! {e}")
    if isinstance(stopped, flt.FilterQuotaStopped):
        raise Stopped(EXIT_QUOTA, f"[filter] 打分额度用完，停下：{stopped}")
    if stopped is not None:
        raise Stopped(EXIT_UNAVAILABLE, f"[filter] 打分后端暂时不可用，停下：{stopped}")
    return EXIT_ATTENTION if s.errors else EXIT_OK


def _make_translator(args: argparse.Namespace, deps: Deps) -> Translator:
    from qbkb.translate import DeepLTranslator, FakeTranslator

    if deps.translator is not None:
        return deps.translator
    if args.translator == "fake":
        return FakeTranslator(quota_chars=args.fake_quota)
    if args.translator == "demo":
        from qbkb.console.demo import DemoTranslator

        return DemoTranslator.from_env()
    return DeepLTranslator.from_env()


def translator_quota(translator: object) -> tuple[int | None, int, bool] | None:
    """翻译后端本月的 (已用字符, 上限, 上限是不是实测的)；后端没有额度概念就返回 None。

    问后端的 usage()（DeepL 的这个接口不消耗额度；只问一次，最多等 USAGE_TIMEOUT_S 秒）。
    取不到用量时已用为 None；
    取不到上限时退回 FREE_MONTHLY_CHARS。
    """
    from qbkb.translate import FREE_MONTHLY_CHARS

    usage = getattr(translator, "usage", None)
    if not callable(usage):
        return None
    box: list[object] = []

    def ask() -> None:
        try:
            box.append(usage())
        except Exception as e:  # 只是显示用：取不到不影响翻译
            log.info("取不到翻译额度：%s", e)

    # 放在守护线程里问，最多等 USAGE_TIMEOUT_S 秒：接口卡住时跳过，不拖住翻译也不拖住退出
    worker = threading.Thread(target=ask, daemon=True)
    worker.start()
    worker.join(USAGE_TIMEOUT_S)
    if worker.is_alive():
        log.info("取不到翻译额度：%s 秒内没有返回", USAGE_TIMEOUT_S)
    try:
        used, limit = cast("tuple[object, object]", box[0])
    except (IndexError, TypeError, ValueError):
        return None, FREE_MONTHLY_CHARS, False
    if not isinstance(used, int):
        used = None
    if not isinstance(limit, int) or limit <= 0:
        return used, FREE_MONTHLY_CHARS, False
    return used, limit, True


def quota_line(translator: object, when: str) -> str | None:
    """ "本月额度：已用 / 上限"这一行；后端没有额度概念就返回 None。"""
    quota = translator_quota(translator)
    if quota is None:
        return None
    used, limit, measured = quota
    source = "" if measured else "（取不到真实上限，按 API Free 的默认值）"
    if used is None:
        return f"[translate] {when}本月额度：已用未知 / 上限 {limit} 字符{source}"
    return (
        f"[translate] {when}本月额度：已用 {used} / 上限 {limit} 字符"
        f"（剩余 {max(limit - used, 0)}）{source}"
    )


def _print_quota(translator: object, when: str) -> None:
    line = quota_line(translator, when)
    if line:
        print(line)


def stage_titles(args: argparse.Namespace, deps: Deps, site: config.SiteConfig) -> int:
    """translate --titles：只补翻缺中文标题的文章。"""
    from qbkb import translate as tr

    conn = store.connect(args.db)
    try:
        urls = tr.missing_titles(conn, site.name)
        if args.limit is not None:
            urls = urls[: args.limit]
        if not urls:
            print(f"[translate] {site.name}: 没有缺中文标题的文章")
            return EXIT_OK
        translator = _make_translator(args, deps)
        glossary = tr.load_glossary(_cfg(args) / "glossary.yaml")
        try:
            n = tr.backfill_titles(conn, urls, translator, glossary)
        except tr.QuotaStopped as e:
            raise Stopped(EXIT_QUOTA, f"[translate] 补翻标题时额度用完，停下：{e}") from e
        except tr.BackendStopped as e:
            raise Stopped(EXIT_UNAVAILABLE, f"[translate] 翻译后端暂时不可用：{e}") from e
        print(
            f"[translate] {site.name}: 补翻标题 {n} / {len(urls)} 篇"
            "（已发布的退回 translated，下次 export 重新生成）"
        )
        return EXIT_OK if n == len(urls) else EXIT_ATTENTION
    finally:
        conn.close()


def stage_translate(args: argparse.Namespace, deps: Deps, site: config.SiteConfig) -> int:
    from qbkb import translate as tr

    if getattr(args, "titles", False):
        return stage_titles(args, deps, site)
    from qbkb import sections

    conn = store.connect(args.db)
    try:
        rules = sections.load_section_rules(site)
        sections.apply_sections(conn, site, rules)
        urls = sections.translatable(conn, site, rules)
        if rules.enabled and not any(s.collect for s in rules.sections):
            print(
                f"[translate] {site.name}: 还没有勾选任何采集栏目"
                "（sections 的 collect 都是 false），"
                f"不翻译新文章。用 qbkb sections --site {site.name} 调研后在 "
                f"config/sites/{site.name}.yaml 里勾选"
            )
        if args.retry_rejected or getattr(args, "recheck", False):
            urls = sorted(urls + store.list_by_status(conn, Status.NEEDS_REVIEW, site.name))
        if getattr(args, "recheck", False):
            for st in (Status.TRANSLATED, Status.PUBLISHED):
                urls = sorted(urls + store.list_by_status(conn, st, site.name))
        only = getattr(args, "url", None)
        if only:
            urls = [u for u in urls if u in only]
        if args.limit is not None:
            urls = urls[: args.limit]
        if not urls:
            print(f"[translate] {site.name}: 没有待翻译的文章")
            return EXIT_OK
        translator = _make_translator(args, deps)
        glossary = tr.load_glossary(_cfg(args) / "glossary.yaml")
        need = tr.estimate_chars(
            conn, urls, translator.name, glossary, retry_rejected=args.retry_rejected
        )
        print(
            f"[translate] {site.name}: 待翻译 {len(urls)} 篇，"
            f"预计约 {need} 字符（不含命中翻译记忆的）"
        )
        if getattr(args, "recheck", False):
            print(
                f"[translate] --recheck：上面的 {need} 字符是要重新送 DeepL 的部分"
                "（术语表或匹配规则变了的段落、没有译文的段落；"
                + ("含以前没通过校验的段落" if args.retry_rejected else "不含没通过校验的段落")
                + "），其余按新规则复用，不花额度"
            )
        _print_quota(translator, "翻译前")
        problems = tr.fixed_translation_errors(conn, urls, glossary)
        if problems:
            for p in problems:
                print(f"  ! {p}", file=sys.stderr)
            print(f"错误：固定译文和原文结构不一致，请检查 config/{tr.FIXED_FILE}", file=sys.stderr)
            return EXIT_ERROR
        done = review = chars = free = 0
        consecutive: list[
            tuple[str, Status, str | None]
        ] = []  # 连续同类请求错误的 (url, 原状态, 原 error)
        last_error: str | None = None
        for url in urls:
            before = store.get_article(conn, url)
            orig_status = Status(before["status"]) if before is not None else Status.EXTRACTED
            orig_error = before["error"] if before is not None else None
            try:
                st = tr.translate_article(
                    conn, url, translator, glossary, retry_rejected=args.retry_rejected
                )
            except tr.QuotaStopped as e:
                chars += e.stats.chars
                print(f"[translate] 完成 {done} 篇、需复查 {review} 篇，送出 {chars} 字符")
                _print_quota(translator, "翻译后")
                raise Stopped(EXIT_QUOTA, f"[translate] 翻译额度用完，停下：{e}") from e
            except tr.BackendStopped as e:
                print(f"[translate] 完成 {done} 篇、需复查 {review} 篇")
                raise Stopped(EXIT_UNAVAILABLE, f"[translate] 翻译后端暂时不可用：{e}") from e
            except tr.BackendAuthError as e:
                raise Stopped(EXIT_ERROR, f"[translate] 翻译后端鉴权失败，停下：{e}") from e
            except tr.ArticleTranslationError as e:
                # 这一篇的请求有问题：标 needs_review（写明原因），继续下一篇，别拖垮整轮
                msg = str(e)
                store.set_status(conn, url, Status.NEEDS_REVIEW, error=f"translate: {e}")
                review += 1
                if msg == last_error:
                    consecutive.append((url, orig_status, orig_error))
                else:
                    consecutive = [(url, orig_status, orig_error)]
                last_error = msg
                if len(consecutive) >= MAX_CONSECUTIVE_ARTICLE_ERRORS:
                    # 连续 N 篇被后端以同样原因拒绝：多半是系统性的配置问题（术语表被删、目标语言
                    # 配置错），不是这几篇自己的问题。把这批文章恢复原状态、不留一堆 needs_review，
                    # 停下让用户查配置。4xx 不是"过一会儿就好"（那才是退出码 4），按配置错误退出 1。
                    for u, st0, err0 in consecutive:
                        store.set_status(conn, u, st0, error=err0)
                    raise Stopped(
                        EXIT_ERROR,
                        f"[translate] 连续 {len(consecutive)} 篇被后端以同样原因拒绝（{msg}），"
                        "疑似配置问题（术语表被删、目标语言写错等）。已把这批文章恢复原状态，"
                        "请检查配置后重跑",
                    ) from e
                continue
            except tr.FixedTranslationError as e:
                # 理论上预检已经拦下；这里再兜一次底，同样整体报错而不是逐篇标 failed
                raise Stopped(EXIT_ERROR, f"[translate] 配置错误：{e}") from e
            except ValueError as e:
                # 没有段落、单段超长等这一篇的问题：标 needs_review，继续下一篇
                store.set_status(conn, url, Status.NEEDS_REVIEW, error=f"translate: {e}")
                review += 1
                consecutive = []
                last_error = None
                continue
            # 这一篇正常翻完：连续同类错误从头数
            consecutive = []
            last_error = None
            chars += st.chars
            free += st.fixed + st.speaker_labels + st.revalidated
            if st.needs_review:
                review += 1
            else:
                done += 1
        print(
            f"[translate] {site.name}: 完成 {done} 篇、需复查 {review} 篇，"
            f"送出 {chars} 字符；固定译文 / 说话人标签 / 按新规则复用 {free} 段（不计费）"
        )
        _print_quota(translator, "翻译后")
        return EXIT_ATTENTION if review else EXIT_OK
    finally:
        conn.close()


def stage_export(args: argparse.Namespace, deps: Deps, site: config.SiteConfig) -> int:
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
        f"下载图片 {s.images_downloaded}、图片失败 {len(s.image_failures)}、"
        f"外站图片（保留原链接）{len(s.external_images)}"
    )
    for f in s.image_failures:
        print(f"  ! 图片 {f.image_url}：{f.reason}（保留原链接）")
    for f in s.external_images:
        print(f"  · 外站图片 {f.image_url}（保留原链接）")
    for e in s.failed:
        print(f"  ! {e}")
    for url in s.requeued:
        print(f"  ! {url}：缺译文，已退回 extracted，下次 translate 重翻")
    print(f"  索引：{out / export.INDEX_NAME}")
    from qbkb.export.tables import write_tables

    conn = store.connect(args.db)
    try:
        table = write_tables(conn, site, load_glossary(_cfg(args) / "glossary.yaml"), out)
    finally:
        conn.close()
    print(f"  表格：{table.rows} 篇，{table.csv}、{table.jsonl}")

    # 图片失败不算：原链接保留了，外站图片按规则本来就不下载
    return EXIT_ATTENTION if s.failed or s.requeued else EXIT_OK


# ---- 汇总命令 ----------------------------------------------------------------


def cmd_summarize(args: argparse.Namespace) -> int:
    from qbkb.summarize import DeepSeekSummarizer, run

    backend = DeepSeekSummarizer.from_env(max_input_chars=args.input_chars)
    conn = store.connect(args.db)
    attention = False
    try:
        for site in _sites(args):
            st = run(conn, site, backend, limit=args.limit)
            print(
                f"[summarize] {site.name}: 新生成 {st.generated}、复用 {st.reused}、"
                f"输入截取 {st.truncated}"
            )
            for error in st.errors:
                print(f"  ! {error}")
            if st.stopped == "quota":
                return EXIT_QUOTA
            if st.stopped == "unavailable":
                return EXIT_UNAVAILABLE
            attention = attention or bool(st.errors)
    finally:
        conn.close()
        backend.close()
    return EXIT_ATTENTION if attention else EXIT_OK


def cmd_table(args: argparse.Namespace) -> int:
    from qbkb.export.tables import write_tables
    from qbkb.translate import load_glossary

    conn = store.connect(args.db)
    try:
        glossary = load_glossary(_cfg(args) / "glossary.yaml")
        for site in _sites(args):
            table = write_tables(conn, site, glossary, Path(args.out))
            print(f"[table] {site.name}: {table.rows} 篇，{table.csv}、{table.jsonl}")
    finally:
        conn.close()
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    try:
        counts = store.count_by_status(conn, args.site)
        total = sum(counts.values())
        print(f"共 {total} 篇" + (f"（站点 {args.site}）" if args.site else ""))
        for st in Status:
            print(f"  {st.value:<13}{counts.get(st, 0):>6}")
        _status_sections(conn, args)
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


def _status_sections(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """status 里每个站点的栏目统计和新栏目提醒。"""
    from qbkb import sections

    for site in _sites(args):
        rules = sections.load_section_rules(site)
        if not rules.enabled:
            continue
        rows = store.site_rows(conn, site.name)
        per: dict[str | None, int] = {}
        pending = 0
        for r in rows:
            if r["section_key"] is None and rules.by_url(r["url"]) is None:
                pending += 1
                continue
            sec = rules.classify(str(r["url"]), r["section_key"])
            per[None if sec is None else sec.id] = per.get(None if sec is None else sec.id, 0) + 1
        print(f"栏目（站点 {site.name}；还没判定栏目的 {pending} 篇不计）：")
        for sec in rules.sections:
            mark = "采集" if sec.collect else "不采集"
            note = f"，{sec.status}" if sec.status else ""
            print(f"  {sec.name}（{sec.id}）：{per.get(sec.id, 0)} 篇，{mark}{note}")
        new = sections.new_sections(conn, site, rules)
        if new:
            print(f"  ! 新栏目（配置里没有，只记录不采集，{len(new)} 个）：")
            for n in new:
                print(f"    {n.label}：{n.count} 篇，例如 {n.example}")


def cmd_sections(args: argparse.Namespace, deps: Deps) -> int:
    """栏目调研（design.md 4.9）：数篇数、抽样、读人工标记、算占比。不调用 LLM。"""
    from qbkb import sections
    from qbkb.sections import survey

    if not args.site:
        print("错误：qbkb sections 要用 --site 指定站点", file=sys.stderr)
        return EXIT_ERROR
    site = config.load_site(args.site, _cfg(args))
    rules = sections.load_section_rules(site)
    if not rules.enabled:
        print(f"站点 {site.name} 的配置里没有 sections，不分栏目（全部采集）")
        return EXIT_OK
    code = EXIT_OK
    if args.fetch > 0:
        config.ensure_allowed(site)
        conn = store.connect(args.db)
        try:
            urls = survey.pick_unclassified(conn, site.name, rules, args.fetch, args.seed)
        finally:
            conn.close()
        if urls:
            csv_dir = (args.csv or Path("output") / "sections" / f"{site.name}.csv").parent
            csv_dir.mkdir(parents=True, exist_ok=True)
            url_file = csv_dir / f"{site.name}-fetch-urls.txt"
            url_file.write_text("\n".join(urls) + "\n", encoding="utf-8")
            print(
                f"[sections] {site.name}: 随机抓 {len(urls)} 篇还没判定栏目的页面"
                f"（种子 {args.seed}）"
            )
            args.url_file = url_file
            args.limit = None
            for name in ("fetch", "extract"):
                c = _run_stage(name, args, deps, site)
                if c == EXIT_ERROR:
                    return c
                code = max(code, c)
    conn = store.connect(args.db)
    try:
        tallies = survey.tally(conn, site.name, rules)
    finally:
        conn.close()
    path: Path = args.csv or Path("output") / "sections" / f"{site.name}.csv"
    old = survey.read_csv(path) if path.is_file() else []
    if old and not args.resample:
        rows = old
        print(f"[sections] 读取已有的抽样表 {path}（重新抽样加 --resample，已标的会保留）")
    else:
        n = survey.write_samples(path, tallies, args.sample, args.seed, old)
        rows = survey.read_csv(path)
        print(
            f"[sections] 抽样表写到 {path}（{n} 篇，每栏 {args.sample} 篇、"
            f"不超过 {survey.FULL_BELOW} 篇的栏目列全部，种子 {args.seed}）。"
            "在「要不要」一列填 要 / 不要，再运行一次同一条命令算占比"
        )
    sampled: dict[str, int] = {}
    for r in rows:
        sampled[r.get("栏目id", "")] = sampled.get(r.get("栏目id", ""), 0) + 1
    vs, bad = survey.verdicts(rows)
    summary = survey.summary_rows(tallies, vs, sampled)
    print("栏目 | 采集 | 已判定 | 估计（未抓取） | 抽样 | 已标 | 要 | 占比 | 结论")
    for r in summary:
        sid, name, collect, known, est, smp, marked, yes, _no, ratio, verdict = r
        print(
            f"  {name}（{sid}）| {collect} | {known} | {est} | {smp} | {marked} | {yes} | "
            f"{ratio or '—'} | {verdict}"
        )
    print(
        "  已判定 = 抓到页面、按规则定了栏目（或网址前缀就能定）的实际篇数；"
        "估计 = 没抓页面、按网址猜的，只供参考"
    )
    for b in bad:
        print(f"  ? 看不懂的标记：{b}（填 要 / 不要）")
    out = path.with_name(path.stem + "-summary.csv")
    survey.write_summary(out, summary)
    print(f"[sections] 占比汇总写到 {out}")
    done = [v for v in vs.values() if v.marked and v.section in {s.id for s in rules.sections}]
    if done:
        print("调研记录（确认后抄进站点配置对应栏目的 survey，并填上判断人）：")
        today = time.strftime("%Y-%m-%d")
        for v in done:
            ratio = "null" if v.ratio is None else f"{v.ratio:.2f}"
            print(
                f"  {v.section}: survey: {{sampled: {v.marked}, wanted: {v.yes}, "
                f"ratio: {ratio}, date: {today}, by: 填判断人}}"
            )
    return code


def cmd_eval(args: argparse.Namespace) -> int:
    from qbkb import filter as flt

    cfg = flt.load_filter_config(_cfg(args) / "filters.yaml")
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
    return EXIT_OK


def _run_stage(name: str, args: argparse.Namespace, deps: Deps, site: config.SiteConfig) -> int:
    """跑一个阶段，返回 EXIT_OK / EXIT_ATTENTION / EXIT_ERROR；额度 / 后端问题抛 Stopped。"""
    if name == "discover":
        return stage_discover(args, deps, site)
    if name == "fetch":
        runner = deps.fetch_runner or (
            fetch_in_process if args.command == "fetch" else fetch_in_subprocess
        )
        code = runner(args, site.name)
        if code not in (EXIT_OK, EXIT_ATTENTION):
            print(f"[fetch] {site.name}: 抓取进程退出码 {code}")
            return EXIT_ERROR
        return code
    if name == "extract":
        return stage_extract(args, site)
    if name == "filter":
        return stage_filter(args, deps, site)
    if name == "translate":
        return stage_translate(args, deps, site)
    if name == "export":
        return stage_export(args, deps, site)
    raise ValueError(f"未知阶段 {name}")


def cmd_stages(args: argparse.Namespace, deps: Deps) -> int:
    """单个阶段，或 run（按顺序跑全部阶段）。

    run 里某个阶段因为额度 / 后端不可用停下时，后面的阶段照常处理已经就绪的文章
    （比如翻译额度用完，已经翻好的文章照样导出），最后用对应的退出码结束。
    """
    stages = [s for s in STAGES if s not in args.skip] if args.command == "run" else [args.command]
    seen: set[int] = set()
    for site in _sites(args):
        for name in stages:
            try:
                code = _run_stage(name, args, deps, site)
            except Stopped as e:
                print(e)
                if e.code == EXIT_ERROR:
                    return EXIT_ERROR  # 鉴权失败等硬错误：立刻停下，不再跑后面的站点/阶段
                seen.add(e.code)
                continue
            if code == EXIT_ERROR:
                return code
            seen.add(code)
    final = next((c for c in _STOP_PRIORITY if c in seen), EXIT_OK)
    if EXIT_QUOTA in seen:
        print(f"额度用完。{RESUME_HINT}")
    if EXIT_UNAVAILABLE in seen:
        print(f"后端暂时不可用。{RESUME_HINT}")
    if EXIT_ATTENTION in seen:
        print("有文章需要人工处理（失败、需复查、退回重翻或新栏目），用 qbkb status 查看。")
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
        if args.command == "summarize":
            return cmd_summarize(args)
        if args.command == "table":
            return cmd_table(args)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "eval":
            return cmd_eval(args)
        if args.command == "sections":
            return cmd_sections(args, deps)
        if args.command == "console":
            from qbkb.console.server import run_console

            return run_console(root=args.root, port=args.port, open_browser=not args.no_browser)
        return cmd_stages(args, deps)
    except Exception as e:  # 顶层兜底：给出中文错误信息和退出码，-v 时附带堆栈
        if getattr(args, "verbose", False):
            log.exception("出错")
        print(f"错误：{e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
