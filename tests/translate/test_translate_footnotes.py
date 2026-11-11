"""脚注上标（真实冒烟发现 DeepL 会丢 <sup>）：占位符往返、请求参数。句子都是自己编的。"""

from typing import Any

import deepl

from qbkb.translate import DeepLTranslator
from qbkb.translate.footnotes import protect_sups, restore_sups
from qbkb.translate.structure import same_structure

SRC = "<p>A firm at a zero discount rate<sup>3</sup> has no upper limit on its value.</p>"
PLACEHOLDER = '<a href="#qbkb-sup-3">3</a>'


def test_protect_and_restore_round_trip() -> None:
    sent = protect_sups(SRC)
    assert "<sup>" not in sent and PLACEHOLDER in sent
    assert restore_sups(sent) == SRC
    two = "<p>x<sup>1</sup> and y<sup>12</sup></p>"
    assert restore_sups(protect_sups(two)) == two


def test_protect_leaves_other_sups_alone() -> None:
    for html in (
        "<p>the 20<sup>th</sup> century</p>",  # 不是纯数字
        '<p>see <a href="#fn1"><sup>1</sup></a></p>',  # 已经在链接里
        '<p>x<sup class="n">1</sup></p>',  # 带属性
        "<p>x<sup>n+1</sup> and y<sup>1,2</sup></p>",
    ):
        assert protect_sups(html) == html


def test_protect_after_a_closed_link() -> None:
    html = '<p><a href="https://example.test/">link</a> then<sup>2</sup></p>'
    assert '<a href="#qbkb-sup-2">2</a>' in protect_sups(html)
    assert restore_sups(protect_sups(html)) == html


def test_restore_tolerates_text_pulled_into_the_placeholder() -> None:
    # 真实 DeepL 的返回形状：把前一个字一起包进了链接
    got = '<p>在零折现<a href="#qbkb-sup-3">率3</a>的情况下，价值没有上限。</p>'
    out = restore_sups(got)
    assert out == "<p>在零折现率<sup>3</sup>的情况下，价值没有上限。</p>"
    assert same_structure(SRC, out)
    # 后面的字被包进来、数字在中间、数字不见了
    assert restore_sups('<a href="#qbkb-sup-3">3的</a>') == "<sup>3</sup>的"
    assert restore_sups('<a href="#qbkb-sup-3">率3的</a>') == "率<sup>3</sup>的"
    assert restore_sups('<a href="#qbkb-sup-3">率</a>') == "率<sup>3</sup>"
    assert restore_sups("<a href='#qbkb-sup-3'>3</a>") == "<sup>3</sup>"


def test_restore_leaves_real_links_alone() -> None:
    html = '<p><a href="#fn3">3</a> <a href="https://example.test/#qbkb-sup-3">x</a></p>'
    assert restore_sups(html) == html


class EchoClient:
    """假的 deepl 客户端：记录请求，按 reply 改写送来的文本。"""

    def __init__(self, drift: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.drift = drift

    def translate_text(self, text: Any, **kwargs: Any) -> Any:
        self.calls.append({"text": list(text), **kwargs})
        out = []
        for t in text:
            if self.drift:
                t = t.replace("rate" + PLACEHOLDER, '<a href="#qbkb-sup-3">率3</a>')
            out.append(deepl.TextResult(t, "EN", None))
        return out

    def list_glossaries(self) -> list[deepl.GlossaryInfo]:
        return []

    def create_glossary(
        self, name: str, source_lang: Any, target_lang: Any, entries: dict[str, str]
    ) -> deepl.GlossaryInfo:
        raise AssertionError("空术语表不应该创建")

    def get_usage(self) -> deepl.Usage:
        return deepl.Usage({"character_count": 0, "character_limit": 1_000_000})


def test_deepl_request_uses_placeholder_and_ignore_tags() -> None:
    client = EchoClient()
    t = DeepLTranslator(client)
    assert t.translate([SRC], {}) == [SRC]
    (call,) = client.calls
    assert call["ignore_tags"] == ["sup"]
    assert call["tag_handling"] == "html"
    assert call["text"] == [SRC.replace("<sup>3</sup>", PLACEHOLDER)]


def test_deepl_output_is_restored_even_when_the_boundary_drifts() -> None:
    t = DeepLTranslator(EchoClient(drift=True))
    (out,) = t.translate([SRC], {})
    assert "qbkb-sup" not in out and "率<sup>3</sup>" in out
    assert same_structure(SRC, out)


def test_ignore_tags_can_be_turned_off() -> None:
    client = EchoClient()
    DeepLTranslator(client, ignore_tags=()).translate(["<p>a</p>"], {})
    assert client.calls[0]["ignore_tags"] is None
