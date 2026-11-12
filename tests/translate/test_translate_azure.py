"""AzureTranslator 的离线测试：httpx.MockTransport 模拟 Azure，不访问网络、不花额度。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from qbkb import store
from qbkb.models import DiscoveredUrl, QuotaExceededError, Segment
from qbkb.translate import (
    AzureTranslator,
    Glossary,
    SegmentTooLargeError,
    TranslatorUnavailableError,
    load_glossary,
    translate_article,
)
from qbkb.translate.azure_backend import (
    GLOBAL_ENDPOINT,
    AzureAuthError,
    AzureRequestError,
    CharRateLimiter,
    TermMarker,
    check_connection,
    strip_dictionary_markup,
)

GLOSSARY = {
    "drawdown": "回撤",
    "trend following": "趋势跟踪",
    "momentum": "动量",
    "momentum factor": "动量因子",
    "alpha": "alpha",
    "CLO": "CLO",
}
GLOSSARY_OBJ = Glossary("t", GLOSSARY)
_DICT = re.compile(r'<mstrans:dictionary translation="([^"]*)">.*?</mstrans:dictionary>')

Reply = Callable[[list[str]], httpx.Response]


def _ok(texts: list[str], metered: int | None = None) -> httpx.Response:
    """像 Azure 一样：动态词典换成指定译名、去掉标记，再加前缀表示"译过了"。"""
    out = [
        {"translations": [{"text": "ZH:" + _DICT.sub(r"\1", t), "to": "zh-Hans"}]} for t in texts
    ]
    headers = {"X-metered-usage": str(metered if metered is not None else sum(map(len, texts)))}
    return httpx.Response(200, json=out, headers=headers)


def _err(status: int, code: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"code": code, "message": f"err {code}"}}, headers=headers
    )


class FakeAzure:
    """记录请求；replies 里依次取出预设的响应（函数），用完以后正常翻译。"""

    def __init__(self, replies: list[Reply] | None = None) -> None:
        self.replies = list(replies or [])
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"translation": {"zh-Hans": {}}})
        texts = [item["Text"] for item in json.loads(request.content)]
        if self.replies:
            return self.replies.pop(0)(texts)
        return _ok(texts)

    def texts(self, i: int = 0) -> list[str]:
        return [item["Text"] for item in json.loads(self.requests[i].content)]

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def _translator(
    fake: FakeAzure, sleeps: list[float] | None = None, **kwargs: Any
) -> AzureTranslator:
    record = sleeps if sleeps is not None else []
    kwargs.setdefault("limiter", CharRateLimiter(10**9, sleep=record.append))
    return AzureTranslator(fake.client(), "test-key", sleep=record.append, **kwargs)


# ---- 请求格式 ----


def test_request_shape_and_headers() -> None:
    fake = FakeAzure()
    t = _translator(fake, region="eastasia")
    assert t.translate(["<p>Hello.</p>"], {}) == ["ZH:<p>Hello.</p>"]
    req = fake.requests[0]
    assert str(req.url).startswith(f"{GLOBAL_ENDPOINT}/translate?")
    assert req.url.params["api-version"] == "3.0"
    assert req.url.params["from"] == "en"  # 动态词典要求写 from
    assert req.url.params["to"] == "zh-Hans"
    assert req.url.params["textType"] == "html"
    assert req.headers["Ocp-Apim-Subscription-Key"] == "test-key"
    assert req.headers["Ocp-Apim-Subscription-Region"] == "eastasia"
    assert t.translate([], GLOSSARY) == []


def test_region_header_only_when_set_and_custom_endpoint() -> None:
    fake = FakeAzure()
    t = _translator(fake, endpoint="https://x.cognitiveservices.azure.com/translator/text/v3.0/")
    t.translate(["<p>Hi.</p>"], {})
    req = fake.requests[0]
    assert "Ocp-Apim-Subscription-Region" not in req.headers
    assert req.url.path == "/translator/text/v3.0/translate"


def test_name_includes_target_language() -> None:
    assert _translator(FakeAzure()).name == "azure:zh-Hans"
    assert _translator(FakeAzure(), target_lang="zh-Hant").name == "azure:zh-Hant"


# ---- 术语：动态词典 ----


def test_terms_wrapped_in_dynamic_dictionary_and_hit() -> None:
    fake = FakeAzure()
    t = _translator(fake)
    src = "<p>Drawdowns hurt <em>trend following</em> and the momentum factor.</p>"
    out = t.translate([src], GLOSSARY)[0]
    sent = fake.texts()[0]
    # 大小写不敏感地匹配，按原文写法包起来；复数词尾一起包进去
    assert '<mstrans:dictionary translation="回撤">Drawdowns</mstrans:dictionary>' in sent
    assert '<mstrans:dictionary translation="趋势跟踪">trend following</mstrans:dictionary>' in sent
    # 最长匹配：momentum factor 整条，不再单独包 momentum
    assert '<mstrans:dictionary translation="动量因子">momentum factor</mstrans:dictionary>' in sent
    assert sent.count("<mstrans:dictionary") == 3
    assert "回撤" in out and "趋势跟踪" in out and "动量因子" in out
    assert "mstrans" not in out
    assert t.term_hits == 3 and t.glossary_misses == []


def test_term_marking_boundaries() -> None:
    m = TermMarker(GLOSSARY)
    # 缩写区分大小写、要右边界："close" 不算 CLO；CLOs 算
    text, hits = m.mark("<p>Close the CLOs, clo.</p>")
    assert hits == ["CLO"] and ">CLOs</mstrans:dictionary>" in text
    # 原文 = 译文的词条按原文写法保留大小写
    text, _ = m.mark("<p>Alpha Architect</p>")
    assert '<mstrans:dictionary translation="Alpha">Alpha</mstrans:dictionary>' in text
    # 词的一部分不算（drawdownish）；属性、code、上标里的不动
    text, hits = m.mark(
        '<p><a href="/drawdown">x</a> <code>drawdown</code> drawdownish<sup>drawdown</sup></p>'
    )
    assert hits == [] and "mstrans" not in text


def test_translation_attribute_is_escaped() -> None:
    text, _ = TermMarker({"risk": 'A"B<C'}).mark("<p>risk</p>")
    assert 'translation="A&quot;B&lt;C"' in text


def test_glossary_miss_is_recorded() -> None:
    fake = FakeAzure([lambda texts: _ok([_DICT.sub("下跌", t) for t in texts])])
    t = _translator(fake)
    t.translate(["<p>drawdown</p>"], GLOSSARY)
    assert t.glossary_misses == [("drawdown", "回撤")]


def test_leftover_dictionary_markup_is_stripped() -> None:
    left = 'a <mstrans:dictionary translation="回撤">drawdown</mstrans:dictionary> b'
    assert strip_dictionary_markup(left) == "a 回撤 b"
    assert strip_dictionary_markup("x</mstrans:dictionary>y") == "xy"
    raw = [lambda texts: httpx.Response(200, json=[{"translations": [{"text": t}]} for t in texts])]
    fake = FakeAzure(raw)
    out = _translator(fake).translate(["<p>drawdown</p>"], GLOSSARY)
    assert out == ["<p>回撤</p>"]


def test_real_glossary_loads_and_marks() -> None:
    g = load_glossary()
    fake = FakeAzure()
    out = _translator(fake).translate(["<p>Implied volatility rose.</p>"], g)[0]
    assert "隐含波动率" in out
    assert fake.texts()[0].count("<mstrans:dictionary") == 1


# ---- 脚注和不翻译的标签 ----


def test_footnote_placeholder_round_trip() -> None:
    fake = FakeAzure()
    out = _translator(fake).translate(["<p>Index<sup>2</sup> rose.</p>"], {})[0]
    assert 'href="#qbkb-sup-2"' in fake.texts()[0]
    assert "<sup>2</sup>" not in fake.texts()[0]
    assert out == "ZH:<p>Index<sup>2</sup> rose.</p>"


def test_non_numeric_sup_is_notranslate_then_restored() -> None:
    fake = FakeAzure()
    out = _translator(fake).translate(["<p>The 20<sup>th</sup> century</p>"], {})[0]
    assert '<sup class="notranslate">th</sup>' in fake.texts()[0]
    assert out == "ZH:<p>The 20<sup>th</sup> century</p>"


def test_pipeline_structure_check_and_separate_memory(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "state.db")
    url = "https://example.test/a"
    store.upsert_discovered(conn, DiscoveredUrl(url=url, site="example", slug="a"))
    segs = [Segment(idx=0, html="<p>Big drawdown<sup>1</sup>.</p>")]
    store.replace_segments(conn, url, segs)
    fake = FakeAzure()
    st = translate_article(conn, url, _translator(fake), GLOSSARY_OBJ)
    assert not st.needs_review and len(fake.requests) == 1
    # 同一个后端第二次命中翻译记忆，不再请求
    translate_article(conn, url, _translator(fake), GLOSSARY_OBJ)
    assert len(fake.requests) == 1
    entry = store.tm_get(
        conn, segs[0].src_hash, "azure:zh-Hans", GLOSSARY_OBJ.version_for(segs[0].html)
    )
    assert entry is not None and "<sup>1</sup>" in entry and "回撤" in entry
    conn.close()


# ---- 分批 ----


def test_chunking_by_elements_and_oversized_segment() -> None:
    fake = FakeAzure()
    t = _translator(fake, max_elements=2)
    out = t.translate(["<p>a</p>", "<p>b</p>", "<p>c</p>"], {})
    assert len(out) == 3 and len(fake.requests) == 2
    with pytest.raises(SegmentTooLargeError):
        _translator(fake, max_chars=10).translate(["<p>" + "x" * 20 + "</p>"], {})
    assert len(fake.requests) == 2  # 超长段落在发请求之前就报错
    with pytest.raises(ValueError):
        _translator(fake, max_chars=60_000)


# ---- 限速、重试、额度 ----


def test_rate_limiter_sliding_window() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    lim = CharRateLimiter(100, 60.0, clock=lambda: now[0], sleep=sleep)
    lim.acquire(60)
    now[0] = 10.0
    lim.acquire(40)  # 正好 100，不等
    assert sleeps == []
    lim.acquire(30)  # 要等第一笔（t=0）滑出窗口
    assert sleeps == [50.0] and lim.waited_s == 50.0  # 等到 t=60
    lim.acquire(500)  # 单次超过上限：等窗口空了整批送
    assert now[0] >= 120.0


def test_translator_paces_requests() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    fake = FakeAzure()
    lim = CharRateLimiter(20, 60.0, clock=lambda: now[0], sleep=sleep)
    t = AzureTranslator(fake.client(), "k", limiter=lim, max_elements=1, sleep=sleep)
    t.translate(["<p>aaaaa</p>", "<p>bbbbb</p>"], {})  # 每条 12 字符，两条超过 20
    assert sleeps == [60.0] and len(fake.requests) == 2


def test_429_backs_off_using_retry_after() -> None:
    sleeps: list[float] = []
    fake = FakeAzure(
        [lambda _: _err(429, 429001, {"Retry-After": "3"}), lambda _: _err(503, 503000)]
    )
    out = _translator(fake, sleeps).translate(["<p>x</p>"], {})
    assert out == ["ZH:<p>x</p>"] and sleeps == [3.0, 4.0]


def test_rate_limit_exhausted_is_unavailable() -> None:
    sleeps: list[float] = []
    fake = FakeAzure([lambda _: _err(429, 429000)] * 3)
    with pytest.raises(TranslatorUnavailableError):
        _translator(fake, sleeps, retries=2).translate(["<p>x</p>"], {})
    assert len(fake.requests) == 3 and sleeps == [2.0, 4.0]


def test_network_error_retried() -> None:
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        if calls[0] == 1:
            raise httpx.ConnectError("boom", request=request)
        return _ok([item["Text"] for item in json.loads(request.content)])

    sleeps: list[float] = []
    t = AzureTranslator(
        httpx.Client(transport=httpx.MockTransport(handler)),
        "k",
        limiter=CharRateLimiter(10**9),
        sleep=sleeps.append,
    )
    assert t.translate(["<p>x</p>"], {}) == ["ZH:<p>x</p>"] and sleeps == [2.0]


def test_free_quota_exhausted_raises_quota_after_one_cooldown() -> None:
    sleeps: list[float] = []
    fake = FakeAzure([lambda _: _err(403, 403001)] * 2)
    with pytest.raises(QuotaExceededError, match="403001"):
        _translator(fake, sleeps, quota_cooldown_s=60).translate(["<p>x</p>"], {})
    assert sleeps == [60] and len(fake.requests) == 2


def test_quota_error_from_burst_recovers_after_cooldown() -> None:
    sleeps: list[float] = []
    fake = FakeAzure([lambda _: _err(403, 403001)])
    t = _translator(fake, sleeps, quota_cooldown_s=5)
    assert t.translate(["<p>x</p>"], {}) == ["ZH:<p>x</p>"] and sleeps == [5]


def test_auth_and_bad_request_are_not_retried() -> None:
    sleeps: list[float] = []
    fake = FakeAzure([lambda _: _err(401, 401000)])
    with pytest.raises(AzureAuthError, match="AZURE_TRANSLATOR_REGION"):
        _translator(fake, sleeps).translate(["<p>x</p>"], {})
    fake = FakeAzure([lambda _: _err(400, 400036)])
    with pytest.raises(AzureRequestError, match="400036"):
        _translator(fake, sleeps).translate(["<p>x</p>"], {})
    assert sleeps == []


def test_quota_stop_in_pipeline(tmp_path: Path) -> None:
    from qbkb.translate import QuotaStopped

    conn = store.connect(tmp_path / "state.db")
    url = "https://example.test/a"
    store.upsert_discovered(conn, DiscoveredUrl(url=url, site="example", slug="a"))
    store.replace_segments(conn, url, [Segment(idx=0, html="<p>Hello.</p>")])
    fake = FakeAzure([lambda _: _err(403, 403001)] * 2)
    with pytest.raises(QuotaStopped):
        translate_article(conn, url, _translator(fake), GLOSSARY_OBJ)
    conn.close()


def test_metered_usage_accumulates() -> None:
    fake = FakeAzure([lambda texts: _ok(texts, metered=7)])
    t = _translator(fake)
    t.translate(["<p>abc</p>"], {})
    # 第二次没有 X-metered-usage 头：按送出的长度算
    fake.replies = [lambda texts: httpx.Response(200, json=[{"translations": [{"text": "x"}]}])]
    t.translate(["<p>abcd</p>"], {})
    assert t.metered_chars == 7 + len("<p>abcd</p>")
    assert t.requests == 2 and t.chars_sent == len("<p>abc</p><p>abcd</p>")


def test_bad_response_shape() -> None:
    fake = FakeAzure([lambda _: httpx.Response(200, json={"oops": 1})])
    with pytest.raises(AzureRequestError):
        _translator(fake).translate(["<p>x</p>"], {})


# ---- from_env、测试连接、CLI ----


def test_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for k in ("AZURE_TRANSLATOR_KEY", "AZURE_TRANSLATOR_REGION", "AZURE_TRANSLATOR_ENDPOINT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("AZURE_TRANSLATOR_CHARS_PER_MINUTE", raising=False)
    with pytest.raises(RuntimeError, match="AZURE_TRANSLATOR_KEY"):
        AzureTranslator.from_env(None)
    env = tmp_path / ".env"
    env.write_text(
        "AZURE_TRANSLATOR_KEY=abc\nAZURE_TRANSLATOR_REGION=westeurope\n"
        "AZURE_TRANSLATOR_CHARS_PER_MINUTE=1234\n",
        encoding="utf-8",
    )
    t = AzureTranslator.from_env(env)
    assert t.region == "westeurope" and t.endpoint == GLOBAL_ENDPOINT
    assert t.limiter.limit == 1234
    t.close()


def test_check_connection_without_and_with_key() -> None:
    fake = FakeAzure([lambda _: _ok(["OK"], metered=2)])
    r = check_connection(fake.client())
    assert r.ok and "没有验证 key" in r.message
    assert (
        fake.requests[0].method == "GET"
        and "Ocp-Apim-Subscription-Key" not in fake.requests[0].headers
    )
    r = check_connection(fake.client(), "k", region="eastasia")
    assert r.ok and r.metered_chars == 2
    assert fake.requests[1].headers["Ocp-Apim-Subscription-Region"] == "eastasia"
    fake.replies = [lambda _: _err(401, 401000)]
    r = check_connection(fake.client(), "bad")
    assert not r.ok and "region" in r.message
    fake.replies = [lambda _: _err(403, 403001)]
    assert "额度用完" in check_connection(fake.client(), "k").message


def test_cli_translator_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    from qbkb import cli

    monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "abc")
    monkeypatch.setattr("qbkb.translate.azure_backend.load_dotenv", lambda *_: None)
    args = argparse.Namespace(translator="azure", fake_quota=None)
    t = cli._make_translator(args, cli.Deps())  # pyright: ignore[reportPrivateUsage]
    assert isinstance(t, AzureTranslator) and t.name == "azure:zh-Hans"
    t.close()
    # 默认仍是 DeepL（真实冒烟通过之前不切换）
    parsed = cli.build_parser().parse_args(["translate", "--site", "man"])
    assert parsed.translator == "deepl"
