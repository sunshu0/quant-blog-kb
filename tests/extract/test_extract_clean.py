from __future__ import annotations

import pytest
from lxml.html import fragment_fromstring

from qbkb.extract.clean import finish, prepare, referenced_ids, split_blocks

URL = "https://example.test/blog/post"


def _run(html: str, remove: tuple[str, ...] = ("script", "button")) -> list[str]:
    root = prepare(fragment_fromstring(html), URL, remove)
    return split_blocks(finish(root, referenced_ids([root])))


def test_adjacent_divs_do_not_merge() -> None:
    assert _run("<div><div>first</div><div>second</div></div>") == [
        "<p>first</p>",
        "<p>second</p>",
    ]


def test_space_between_inline_elements_is_kept() -> None:
    assert _run("<div><em>a</em> <strong>b</strong></div>") == [
        "<p><em>a</em> <strong>b</strong></p>"
    ]


def test_loose_text_around_blocks_becomes_paragraphs() -> None:
    out = _run("<div>lead <b>in</b><h2>Head</h2>tail text<br></div>")
    assert out == ["<p>lead <b>in</b></p>", "<h2>Head</h2>", "<p>tail text</p>"]


def test_div_inside_list_item_is_unwrapped_without_gluing() -> None:
    out = _run("<div><ul><li><div>one</div><div>two</div></li></ul></div>")
    assert out == ["<ul><li>one two</li></ul>"]


def test_noise_removed_and_tail_text_kept() -> None:
    out = _run("<div><p>keep <button>Share</button>this</p><script>x()</script></div>")
    assert out == ["<p>keep this</p>"]


def test_links_and_images_become_absolute() -> None:
    out = _run(
        '<div><p><a href="../other">o</a> <a href="/root">r</a> <a href="#fn1">1</a></p>'
        '<img src="img/a.png"><p id="fn1">note</p></div>'
    )
    assert out == [
        '<p><a href="https://example.test/other">o</a> <a href="https://example.test/root">r</a>'
        ' <a href="#fn1">1</a></p>',
        '<img src="https://example.test/blog/img/a.png">',
        '<p id="fn1">note</p>',
    ]


def test_dead_links_are_unwrapped() -> None:
    out = _run('<div><p><a href="#">share</a> <a href="javascript:void(0)">js</a></p></div>')
    assert out == ["<p>share js</p>"]


def test_unreferenced_ids_classes_and_styles_are_stripped() -> None:
    out = _run('<div><h2 id="x" class="big" style="color:red" data-k="v">T</h2></div>')
    assert out == ["<h2>T</h2>"]


def test_lazy_image_uses_data_src() -> None:
    out = _run('<div><img src="data:image/gif;base64,AA" data-src="/real.png" alt="a"></div>')
    assert out == ['<img src="https://example.test/real.png" alt="a">']


def test_noscript_image_is_kept() -> None:
    out = _run(
        '<div><div class="embed"><script></script>'
        '<noscript><img src="/t.png"></noscript></div></div>'
    )
    assert out == ['<img src="https://example.test/t.png">']


def test_empty_elements_removed() -> None:
    assert _run("<div><p></p><p>&nbsp;</p><p><span> </span></p><p>x</p></div>") == ["<p>x</p>"]


def test_mathml_attributes_are_kept() -> None:
    out = _run('<div><p>see <math display="inline"><mi mathvariant="bold">x</mi></math></p></div>')
    assert out == ['<p>see <math display="inline"><mi mathvariant="bold">x</mi></math></p>']


def test_pre_whitespace_is_kept() -> None:
    out = _run("<div><pre>a\n    b</pre></div>")
    assert out == ["<pre>a\n    b</pre>"]


def test_block_root_is_one_segment() -> None:
    assert _run('<h2 class="x"><span>Important</span> <span>notes</span></h2>') == [
        "<h2>Important notes</h2>"
    ]


def test_original_tree_is_not_modified() -> None:
    src = fragment_fromstring("<div><script>x</script><p>a</p></div>")
    root = prepare(src, URL, ("script",))
    split_blocks(finish(root, set()))
    assert len(src.findall("script")) == 1


def test_mathml_event_attributes_are_stripped() -> None:
    out = _run(
        '<div><p><math display="block" onclick="x()" style="color:red">'
        '<mtext onmouseover="y()" mathvariant="bold">t</mtext>'
        '<annotation-xml encoding="text/html"><b>evil</b></annotation-xml>'
        '<mi href="javascript:z()">x</mi></math></p></div>'
    )
    assert out == [
        '<p><math display="block"><mtext mathvariant="bold">t</mtext><mi>x</mi></math></p>'
    ]


def test_inline_image_and_math_stay_in_paragraph() -> None:
    out = _run('<div>The value <img src="/a.png"> is high and <math><mi>x</mi></math> low</div>')
    assert out == [
        '<p>The value <img src="https://example.test/a.png"> is high and'
        " <math><mi>x</mi></math> low</p>"
    ]


def test_lone_image_between_blocks_is_its_own_segment() -> None:
    out = _run('<div><h3>Fig</h3>\n<img src="/a.png">\n<p>Source</p></div>')
    assert out == ["<h3>Fig</h3>", '<img src="https://example.test/a.png">', "<p>Source</p>"]


def test_br_only_paragraphs_are_dropped() -> None:
    out = _run("<div><p><br></p><p> <br> <br> </p><div><br></div><p>a<br>b</p></div>")
    assert out == ["<p>a<br>b</p>"]


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "java\tscript:alert(1)",
        " javascript:alert(1)",
        "vbscript:msgbox(1)",
        "data:text/html,<b>x</b>",
        "ftp://example.test/file",
        "#",
        "",
    ],
)
def test_unsafe_link_schemes_lose_href(href: str) -> None:
    root = fragment_fromstring("<div><p>a <a>link</a> b</p></div>")
    root.find(".//a").set("href", href)
    out = split_blocks(finish(prepare(root, URL, ()), set()))
    assert out == ["<p>a link b</p>"]


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("https://x.test/p", "https://x.test/p"),
        ("http://x.test/p", "http://x.test/p"),
        ("mailto:a@x.test", "mailto:a@x.test"),
        ("rel/p", "https://example.test/blog/rel/p"),
        ("//cdn.test/p", "https://cdn.test/p"),
    ],
)
def test_safe_link_schemes_are_kept(href: str, expected: str) -> None:
    root = fragment_fromstring("<div><p><a>link</a></p></div>")
    root.find(".//a").set("href", href)
    out = split_blocks(finish(prepare(root, URL, ()), set()))
    assert out == [f'<p><a href="{expected}">link</a></p>']


@pytest.mark.parametrize(
    "src", ["javascript:alert(1)", "data:image/png;base64,AA", "mailto:a@x.test", "file:///etc"]
)
def test_unsafe_image_sources_are_dropped(src: str) -> None:
    root = fragment_fromstring("<div><p>a <img> b</p></div>")
    root.find(".//img").set("src", src)
    assert split_blocks(finish(prepare(root, URL, ()), set())) == ["<p>a b</p>"]
