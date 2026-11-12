"""演示模式（D12）：仓库自带的构造示例站点 + 自带中文的演示翻译后端。不联网、不需要 API key。

- 示例站点的页面照 man.com 的模板结构写（抽取规则直接复用 config/sites/man.yaml），
  正文全部是为演示自己写的英文，**不含任何真实文章的内容**；每段配一份自己写的中文，
  DemoTranslator 按段落原文查这份中文，所以演示时看到的是像样的中文译文；
- 站点在本机 127.0.0.1 起 HTTP 服务，端口记在 .qbkb/demo/site.json 里，下次尽量用同一个端口
  （文章 URL 里带端口，换端口就是另一批文章）；端口被占用时清空演示数据、换一个端口；
- 打分用 FakeScorer（按关键词），翻译用 DemoTranslator，抓取间隔 0（本机站点允许）；
- 为了让「待复查」页有内容可演示：momentum 那篇的第一段前两次翻译会"丢掉脚注上标"，
  结构校验拦下 → needs_review；在控制台点「重翻」（第三次）就好了。计数记在 .qbkb/demo/ 里。
"""

from __future__ import annotations

import contextlib
import html
import os
import shutil
import struct
import threading
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import yaml

from qbkb.config import CONFIG_DIR
from qbkb.console.net import LocalServer
from qbkb.console.workspace import DEMO_SITE, Workspace, read_json, write_json
from qbkb.translate.base import count_chars
from qbkb.translate.structure import plain_text

DEMO_STATE_ENV = "QBKB_DEMO_STATE"
FLAKY_FILE = "demo_flaky.json"
SITE_FILE = "site.json"
FIRM = "示例资本（演示）"
# 前几次翻译 momentum 那篇第一段时丢掉脚注（让它进 needs_review），之后正常
FLAKY_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class Block:
    tag: str  # p / h2 / img
    en: str  # 英文 HTML（标签里面的部分）；img 时是 src
    zh: str  # 对应的中文；img 时是 alt


def p(en: str, zh: str) -> Block:
    return Block("p", en, zh)


def h2(en: str, zh: str) -> Block:
    return Block("h2", en, zh)


@dataclass(frozen=True, slots=True)
class DemoPage:
    path: str
    title: str
    title_zh: str
    date: str  # 页面上显示的日期，例如 "7 May 2024"
    blocks: tuple[Block, ...]
    tagline: str = "ARTICLE | 4 MIN"


CHART_PATH = "/files/demo-trend-chart.png"
DISCLAIMER = (
    "This constructed page is part of the qbkb demo site and is not investment material.",
    "本页是 qbkb 演示站点的构造页面，不构成投资材料。",
)
IMPORTANT = ("Important information", "重要信息")

PAGES: tuple[DemoPage, ...] = (
    DemoPage(
        "/insights/trend-following-basics",
        "Trend Following Basics",
        "趋势跟踪基础",
        "7 May 2024",
        (
            p(
                "Trend following is one of the oldest ideas in systematic investing. The rule "
                "is simple: buy markets whose prices have been rising and sell markets whose "
                "prices have been falling, then hold those positions until the trend turns.",
                "趋势跟踪是系统化投资中最古老的思路之一。规则很简单：买入价格一直在上涨的市场，"
                "卖出价格一直在下跌的市场，然后持有这些头寸，直到趋势反转。",
            ),
            h2("Why trends can persist", "趋势为什么会延续"),
            p(
                "Prices often move gradually because investors react slowly to news. Some wait "
                "for confirmation, others are forced to adjust positions later, and these delays "
                "can stretch a move over weeks or months instead of a single day.",
                "价格往往是逐步变动的，因为投资者对新闻的反应比较慢。有人要等确认信号，"
                "有人要晚些时候才被迫调整头寸，这些延迟会让一段行情持续几周甚至几个月，"
                "而不是在一天内完成。",
            ),
            Block("img", CHART_PATH, "Constructed chart of a rising price series"),
            p(
                "The price of this approach is that it loses money when markets chop sideways. "
                "A trend follower will often buy near the top of a short rally and sell near the "
                "bottom, so long flat periods and drawdowns are part of the strategy.",
                "这种方法的代价是，在市场横盘震荡时会亏钱。趋势跟踪者常常在短暂反弹的高点附近买入、"
                "在低点附近卖出，所以长时间不赚钱和回撤都是这个策略的一部分。",
            ),
            h2("Sizing positions by volatility", "按波动率确定头寸规模"),
            p(
                "Most trend models scale each position by its recent volatility. A calm market "
                "gets a larger position and a wild market gets a smaller one, so that no single "
                "market dominates the risk of the whole portfolio.",
                "大多数趋势模型会按近期波动率调整每个头寸的规模。平静的市场头寸大一些，"
                "剧烈波动的市场头寸小一些，这样就不会有单个市场主导整个组合的风险。",
            ),
            p(
                "This short article is a constructed example written for the demo mode of this "
                "tool. The numbers and the chart are invented and do not describe any real fund "
                "or market.",
                "这篇短文是为本工具的演示模式编写的构造示例。文中的数字和图表都是虚构的，"
                "不代表任何真实的基金或市场。",
            ),
        ),
    ),
    DemoPage(
        "/insights/simple-momentum-signal",
        "Building a Simple Momentum Signal",
        "构建一个简单的动量信号",
        "12 June 2023",
        (
            p(
                "A momentum signal ranks assets by how much they have gained over a lookback "
                "window. In this made-up example we use twelve months of returns and skip the "
                "most recent month, a common choice in academic studies.<sup>1</sup>",
                "动量信号按资产在回看窗口内的涨幅给资产排序。在这个虚构的例子里，"
                "我们用过去十二个月的收益率，并跳过最近一个月，这是学术研究里常见的做法。"
                "<sup>1</sup>",
            ),
            h2("Step one: measure the return", "第一步：计算收益率"),
            p(
                "For each asset we divide the price one month ago by the price thirteen months "
                "ago and subtract one. The result is a single number per asset that says how "
                "strong its recent run has been.",
                "对每个资产，用一个月前的价格除以十三个月前的价格再减一。"
                "结果是每个资产一个数字，表示它最近这段行情有多强。",
            ),
            h2("Step two: rank and combine", "第二步：排序并组合"),
            p(
                "We then rank the assets from strongest to weakest, hold the top third and avoid "
                "the bottom third. Rebalancing once a month keeps trading costs modest while "
                "still following the signal closely.",
                "然后把资产从强到弱排序，持有前三分之一，回避后三分之一。"
                "每月调仓一次，既能紧跟信号，又能把交易成本控制在较低水平。",
            ),
            p(
                "Before trusting the signal, a careful researcher runs a backtest on data the "
                "rule has never seen. If the momentum effect only appears in one decade or one "
                "market, it is probably not robust.",
                "在相信这个信号之前，严谨的研究者会在规则从未见过的数据上做回测。"
                "如果动量效应只在某一个十年或某一个市场里出现，它很可能并不稳健。",
            ),
            p(
                "<sup>1</sup> The skipped month is meant to avoid short-term reversal; this note "
                "belongs to a constructed demo article.",
                "<sup>1</sup> 跳过最近一个月是为了避开短期反转；这条注释属于构造的演示文章。",
            ),
        ),
    ),
    DemoPage(
        "/insights/machine-learning-volatility",
        "Machine Learning and Volatility Forecasts",
        "机器学习与波动率预测",
        "18 March 2025",
        (
            p(
                "Machine learning models are increasingly used to forecast volatility. Instead "
                "of a single formula, a model learns from many inputs at once, such as past "
                "returns, trading volume and the shape of the options market.",
                "机器学习模型越来越多地被用来预测波动率。模型不再依赖单一公式，"
                "而是同时从许多输入中学习，比如过去的收益率、成交量以及期权市场的形态。",
            ),
            h2("Implied and realised volatility", "隐含波动率与已实现波动率"),
            p(
                "Two measures matter here. Implied volatility is read from option prices and "
                "reflects what the market expects, while realised volatility is measured from "
                "past price moves. The gap between them is a useful input.",
                "这里有两个重要的指标。隐含波动率从期权价格中推算，反映市场的预期；"
                "已实现波动率则根据过去的价格变动计算。两者之间的差距是一个有用的输入。",
            ),
            p(
                "A model that fits the past perfectly is usually overfitted. We hold back the "
                "most recent years as a test set and only look at them once, after every "
                "modelling choice has been made.",
                "一个对历史数据拟合得完美无缺的模型，通常是过拟合了。我们把最近几年的数据留作测试集，"
                "并且只在所有建模选择都确定之后看一次。",
            ),
            p(
                "In this invented example the model improves forecasts slightly in calm periods "
                "but adds little during sudden shocks, which is a reminder that no method "
                "removes tail risk.",
                "在这个虚构的例子里，模型在平静时期略微提高了预测精度，但在突发冲击时帮助不大，"
                "这提醒我们：没有任何方法能消除尾部风险。",
            ),
            p(
                "All figures in this article are made up for the demo and should not be used "
                "for any investment decision.",
                "本文所有数字均为演示而虚构，不应用于任何投资决策。",
            ),
        ),
    ),
    DemoPage(
        "/insights/short-history-of-bubbles",
        "A Short History of Market Bubbles",
        "市场泡沫简史",
        "2 November 2022",
        (
            p(
                "Market bubbles appear again and again in financial history. Prices rise far "
                "above any reasonable estimate of value, new buyers arrive because prices are "
                "rising, and the story ends when there is nobody left to buy.",
                "市场泡沫在金融史上一再出现。价格涨到远高于任何合理的价值估计，"
                "新的买家因为价格在涨而入场，直到再也没有人接盘，故事才结束。",
            ),
            h2("A familiar pattern", "熟悉的模式"),
            p(
                "Many episodes share the same stages: a new idea that seems to change "
                "everything, easy credit, stories of quick fortunes, and finally a sharp fall "
                "when confidence breaks.",
                "许多次泡沫都有相同的几个阶段：一个似乎能改变一切的新想法、宽松的信贷、"
                "一夜暴富的故事，最后在信心崩溃时急剧下跌。",
            ),
            p(
                "Behavioural finance offers several explanations. People extrapolate recent "
                "gains, copy what their neighbours do and feel more regret about missing a rally "
                "than about joining it late.",
                "行为金融学给出了几种解释。人们会把最近的上涨外推到未来，模仿身边人的做法，"
                "而且错过一波上涨带来的懊悔，比晚些加入更让人难受。",
            ),
            p(
                "For a systematic investor, the lesson is humility. Bubbles are easy to name "
                "afterwards and hard to time while they happen, so position limits matter more "
                "than predictions.",
                "对系统化投资者来说，教训是保持谦逊。泡沫事后很容易指认，身处其中时却很难把握时机，"
                "所以头寸上限比预测更重要。",
            ),
            p(
                "This history is a short constructed summary written for the demo mode; it does "
                "not quote any published article.",
                "这段历史是为演示模式编写的简短构造概述，没有引用任何已发表的文章。",
            ),
        ),
    ),
    # 下面两篇演示"筛掉"：一篇 L2 判成 ESG/气候，一篇 L1 排除栏目。不会被翻译，所以不配中文
    DemoPage(
        "/insights/climate-note-for-portfolios",
        "A Climate Note for Portfolios",
        "",
        "1 March 2024",
        tuple(
            p(
                f"Paragraph {i} of a constructed climate note: it talks about carbon prices, "
                "weather patterns and how a made-up portfolio could report its emissions. It "
                "exists only so that the demo shows an article being filtered out by topic.",
                "",
            )
            for i in range(4)
        ),
    ),
    DemoPage(
        "/insights/a-moment-in-macro/rates-this-week",
        "Rates This Week",
        "",
        "2 February 2025",
        tuple(
            p(
                f"Paragraph {i} of a constructed weekly macro note about interest rates. The "
                "column it belongs to is excluded by a rule, so the demo never translates it.",
                "",
            )
            for i in range(4)
        ),
    ),
)
# 用来认出"这个端口上是 qbkb 的示例站点"（DemoSite.start）
MARKER_PATH = "/qbkb-demo-site"
MARKER_BODY = b"qbkb-demo-site"
# sitemap 里列出、但页面不存在（404）：演示「待复查」页里的"失败"
MISSING_PATH = "/insights/page-that-was-removed"


def _key(html_text: str) -> str:
    return plain_text(html_text)


def _units() -> dict[str, str]:
    """段落原文的纯文本 → 中文 HTML（标签里面的部分）。标题也算一个单元。"""
    out: dict[str, str] = {}
    for page in PAGES:
        if page.title_zh:
            out[_key(html.escape(page.title))] = page.title_zh
        for b in page.blocks:
            if b.tag != "img" and b.zh:
                out[_key(b.en)] = b.zh
    out[_key(IMPORTANT[0])] = IMPORTANT[1]
    out[_key(DISCLAIMER[0])] = DISCLAIMER[1]
    return out


UNITS = _units()
FLAKY_KEY = _key(PAGES[1].blocks[0].en)


def _split_outer(segment: str) -> tuple[str, str, str] | None:
    """`<p class="x">内容</p>` → ("<p class=\"x\">", "内容", "</p>")；不是这种形状返回 None。"""
    s = segment.strip()
    if not s.startswith("<") or not s.endswith(">"):
        return None
    open_end = s.find(">")
    close_start = s.rfind("</")
    if open_end < 0 or close_start <= open_end:
        return None
    return s[: open_end + 1], s[open_end + 1 : close_start], s[close_start:]


class DemoTranslator:
    """演示用的翻译后端：示例文章的段落换成自带的中文，别的文字加"【演示译文】"前缀。

    不联网、不计费（chars_used 只是统计）。state_dir 不为空时启用"前两次丢脚注"的演示。
    """

    name = "demo"

    def __init__(self, state_dir: Path | None = None) -> None:
        self.state_dir = state_dir
        self.chars_used = 0

    @classmethod
    def from_env(cls) -> DemoTranslator:
        d = os.environ.get(DEMO_STATE_ENV)
        return cls(Path(d) if d else None)

    def translate(
        self, segments: Sequence[str], glossary: Mapping[str, str], context: str = ""
    ) -> list[str]:
        self.chars_used += count_chars(segments)
        return [self._one(s) for s in segments]

    def _one(self, segment: str) -> str:
        key = _key(segment)
        if not key:
            return segment  # 只有图片之类：原样返回
        zh = UNITS.get(key)
        parts = _split_outer(segment)
        if zh is None or parts is None:
            return _prefix_text(segment)
        if key == FLAKY_KEY and self._flaky():
            zh = zh.replace("<sup>1</sup>", "")
        return parts[0] + zh + parts[2]

    def _flaky(self) -> bool:
        if self.state_dir is None:
            return False
        path = self.state_dir / FLAKY_FILE
        data = read_json(path) or {}
        n = data.get("attempts", 0)
        n = n if isinstance(n, int) else 0
        write_json(path, {"attempts": n + 1})
        return n < FLAKY_ATTEMPTS


def _prefix_text(segment: str) -> str:
    """不认识的段落：第一个文字节点前加"【演示译文】"，标签不动（结构校验能过）。"""
    out: list[str] = []
    done = False
    i = 0
    while i < len(segment):
        if segment[i] == "<":
            j = segment.find(">", i)
            j = len(segment) if j < 0 else j + 1
            out.append(segment[i:j])
            i = j
            continue
        j = segment.find("<", i)
        j = len(segment) if j < 0 else j
        text = segment[i:j]
        if not done and text.strip():
            text = "【演示译文】" + text
            done = True
        out.append(text)
        i = j
    return "".join(out)


# ---- 示例站点 ----------------------------------------------------------------


def chart_png(width: int = 360, height: int = 160) -> bytes:
    """一张自己画的柱状图（逐渐升高的蓝色柱子），用 zlib 手写 PNG，不依赖图像库。"""
    bars = 9
    rows: list[bytes] = []
    for y in range(height):
        row = bytearray([0])  # 每行的过滤类型
        for x in range(width):
            col = x * bars // width
            in_bar = (x * bars) % width > width // 6  # 柱子之间留空
            top = height - 12 - (col + 1) * (height - 30) // bars
            if in_bar and top <= y < height - 12:
                row += bytes((44, 110, 190))
            elif y == height - 12:
                row += bytes((90, 90, 90))
            else:
                row += bytes((255, 255, 255))
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )


def page_html(page: DemoPage) -> str:
    """照 man.com 文章模板的结构写的页面（抽取规则和 man.yaml 一样）。"""
    body: list[str] = []
    for b in page.blocks:
        if b.tag == "img":
            body.append(f'<p><img src="{b.en}" alt="{html.escape(b.zh)}"></p>')
        else:
            body.append(f"<{b.tag}>{b.en}</{b.tag}>")
    title = html.escape(page.title)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{title} | qbkb demo</title>
<meta property="og:title" content="{title} | Man Group">
</head><body>
<header><nav><a href="/insights">Insights</a></nav></header>
<main>
<div id="article-tagline-text"><p>{page.tagline}</p></div>
<div id="article-title-text"><h1>{title}</h1></div>
<div id="article-date-text"><p>{page.date}</p></div>
<div id="article-authors"><a href="/people/demo"><span class="fw-600">Demo Author</span></a></div>
<div id="article-body-text"><div class="digital-article">
{chr(10).join(body)}
</div></div>
<div id="insights-important-information-section">
<h2 id="insights-important-information-section-title">{IMPORTANT[0]}</h2>
<div class="accordion-body">
<p>{DISCLAIMER[0]}</p></div>
</div>
</main></body></html>
"""


def sitemap_xml(base: str) -> str:
    paths = [pg.path for pg in PAGES] + [MISSING_PATH]
    urls = "".join(
        f"<url><loc>{base}{path}</loc><lastmod>2026-11-01</lastmod></url>" for path in paths
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    )


def responses(base: str) -> dict[str, tuple[str, bytes]]:
    """路径 → (Content-Type, 内容)。不在表里的路径返回 404。"""
    out: dict[str, tuple[str, bytes]] = {
        "/robots.txt": ("text/plain", b"User-agent: *\nDisallow:\n"),
        "/sitemap.xml": ("application/xml", sitemap_xml(base).encode("utf-8")),
        CHART_PATH: ("image/png", chart_png()),
        MARKER_PATH: ("text/plain", MARKER_BODY),
    }
    for pg in PAGES:
        out[pg.path] = ("text/html; charset=utf-8", page_html(pg).encode("utf-8"))
    return out


def is_demo_site(port: int) -> bool:
    """127.0.0.1:port 上是不是 qbkb 的示例站点（看 MARKER_PATH 的内容）。"""
    try:
        r = httpx.get(f"http://127.0.0.1:{port}{MARKER_PATH}", timeout=2.0, trust_env=False)
    except httpx.HTTPError:
        return False
    return r.status_code == 200 and r.content == MARKER_BODY


class DemoSite:
    """示例站点的本机 HTTP 服务（只监听 127.0.0.1）。start 可以重复调用。"""

    def __init__(self, ws: Workspace) -> None:
        self.ws = ws
        self._srv: ThreadingHTTPServer | None = None
        self._lock = threading.Lock()
        self.hits: list[str] = []

    @property
    def base_url(self) -> str | None:
        if self._srv is None:
            return None
        return f"http://127.0.0.1:{self._srv.server_address[1]}"

    def start(self) -> str:
        """起服务、写好演示配置，返回站点地址。

        保存的端口被占用时，先看占着它的是不是另一个进程里的示例站点（控制台开着的时候，
        每天的定时任务也会来跑演示模式）：是就直接用它，不换端口；不是才清空演示数据、换端口。
        """
        with self._lock:
            if self._srv is None:
                saved = read_json(self.ws.base / SITE_FILE) or {}
                port = saved.get("port")
                srv = None
                if isinstance(port, int) and 0 < port < 65536:
                    try:
                        srv = self._bind(port)
                    except OSError:
                        if is_demo_site(port):
                            base = f"http://127.0.0.1:{port}"
                            ensure_config(self.ws, base)
                            return base
                if srv is None:
                    if port is not None:
                        reset_data(self.ws)  # 换端口 = 换一批 URL：旧数据对不上了
                    srv = self._bind(0)
                    write_json(self.ws.base / SITE_FILE, {"port": srv.server_address[1]})
                self._srv = srv
                threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = self.base_url
            assert base is not None
            ensure_config(self.ws, base)
            return base

    def _bind(self, port: int) -> ThreadingHTTPServer:
        table: dict[str, tuple[str, bytes]] = {}
        hits = self.hits

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                hits.append(self.path)
                found = table.get(self.path.split("?")[0])
                status, (ctype, body) = (200, found) if found else (404, ("text/plain", b"nope"))
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        srv = LocalServer(("127.0.0.1", port), Handler)
        table.update(responses(f"http://127.0.0.1:{srv.server_address[1]}"))
        return srv

    def stop(self) -> None:
        with self._lock:
            if self._srv is not None:
                self._srv.shutdown()
                self._srv.server_close()
                self._srv = None


def _write_if_changed(path: Path, text: str) -> None:
    data = text.encode("utf-8")
    if path.is_file() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def ensure_config(ws: Workspace, base: str, source: Path = CONFIG_DIR) -> Path:
    """演示模式的配置目录：仓库 config/ 的副本（不带 man 站点、评测集和本机的固定译文），
    加一个指向本机示例站点的站点 demo（抽取规则照抄 man.yaml）。"""
    cfg = ws.config_dir
    assert cfg is not None
    for name in ("filters.yaml", "glossary.yaml", "overrides.yaml"):
        _write_if_changed(cfg / name, (source / name).read_text(encoding="utf-8"))
    rubrics = source / "rubrics"
    for f in sorted(rubrics.glob("*.md")):
        _write_if_changed(cfg / "rubrics" / f.name, f.read_text(encoding="utf-8"))
    man: dict[str, Any] = yaml.safe_load((source / "sites" / "man.yaml").read_text("utf-8"))
    man["base_url"] = base
    man["firm"] = FIRM
    man["extract"]["min_chars"] = 400  # 示例文章短，不要每篇都报"正文太短"
    man["discover"]["sitemap"] = f"{base}/sitemap.xml"
    man["compliance"] = {
        "robots_txt": "演示站点：本机构造，全部放行",
        "terms_summary": "仓库自带的构造内容，没有任何真实文章",
        "checked_at": date(2026, 11, 12),
        "decision": "allowed",
    }
    sites = cfg / "sites"
    sites.mkdir(parents=True, exist_ok=True)
    for old in sites.glob("*.yaml"):
        if old.stem != DEMO_SITE:
            old.unlink()
    _write_if_changed(sites / f"{DEMO_SITE}.yaml", yaml.safe_dump(man, allow_unicode=True))
    filters: dict[str, Any] = yaml.safe_load((cfg / "filters.yaml").read_text("utf-8"))
    # 示例文章比真实文章短：最少单词数调低，排除栏目照抄 man
    filters["sites"] = {
        DEMO_SITE: {"exclude_prefixes": ["/insights/a-moment-in-macro/"], "min_words": 100}
    }
    _write_if_changed(cfg / "filters.yaml", yaml.safe_dump(filters, allow_unicode=True))
    return cfg


def reset_data(ws: Workspace) -> None:
    """清空演示数据（状态库、原始 HTML、输出、脱脚注计数）。只动 .qbkb/demo/ 下面的东西。"""
    if not ws.demo:
        raise ValueError("只能清空演示模式的数据")
    for name in ("state.db", "state.db-wal", "state.db-shm", "state.db-journal", FLAKY_FILE):
        with contextlib.suppress(OSError):
            (ws.base / name).unlink(missing_ok=True)
    for d in (ws.raw, ws.out):
        if d.is_dir():
            shutil.rmtree(d)


def demo_env(ws: Workspace, env: Mapping[str, str]) -> dict[str, str]:
    """演示模式子进程的环境变量：去掉 API key（演示模式不需要、也不该用到），带上演示状态目录。"""
    out = {k: v for k, v in env.items() if k not in ("DEEPL_API_KEY", "LLM_API_KEY")}
    out[DEMO_STATE_ENV] = str(ws.base)
    return out
