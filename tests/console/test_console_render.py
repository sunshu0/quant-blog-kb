"""文章渲染：服务端把 export 的 Markdown 渲染成 HTML 时防 XSS、改写图片路径、还原上下标。"""

from pathlib import Path

import pytest

from qbkb.console.render import image_url, render_markdown, strip_frontmatter


@pytest.fixture
def out(tmp_path: Path) -> Path:
    o = tmp_path / "output"
    (o / "man" / "images").mkdir(parents=True)
    return o


def _render(text: str, out: Path, bilingual: bool = False) -> str:
    md = out / ("bilingual/man/a.md" if bilingual else "man/a.md")
    return render_markdown(text, md, out)


@pytest.mark.parametrize(
    "evil",
    [
        "<script>alert(1)</script>",
        '<img src=x onerror="alert(1)">',
        "<iframe src=//evil.test></iframe>",
        '<a href="javascript:alert(1)">x</a>',
        "&lt;script&gt;alert(1)&lt;/script&gt;",
    ],
)
def test_raw_html_is_never_rendered(out: Path, evil: str) -> None:
    html = _render(f"段落 {evil} 结束", out)
    assert "<script" not in html and "<img" not in html and "<iframe" not in html
    assert "<a " not in html


@pytest.mark.parametrize(
    "link",
    [
        "[点我](javascript:alert(1))",
        "[点我](JaVaScRiPt:alert(1))",
        "[点我](vbscript:msgbox)",
        "[点我](data:text/html;base64,PHNjcmlwdD4=)",
        "![x](javascript:alert(1))",
    ],
)
def test_dangerous_link_schemes_are_dropped(out: Path, link: str) -> None:
    html = _render(link, out).lower()
    # 不合格的链接按普通文字显示（不生成 <a> / <img>）
    assert "<a " not in html and "<img" not in html and "href=" not in html


def test_footnote_sup_is_restored_but_nothing_else(out: Path) -> None:
    html = _render("动量<sup>1</sup>和<sub>t</sub>，<sup onclick=x>2</sup><sup><b>3</b></sup>", out)
    assert "动量<sup>1</sup>和<sub>t</sub>" in html
    assert "onclick" not in html.replace("&lt;sup onclick=x&gt;", "")
    assert "<b>" not in html


def test_images_are_rewritten_into_output_and_traversal_is_blocked(out: Path) -> None:
    html = _render(
        "![图](images/abc.png)\n\n![坏](../../state.db)\n\n![外](https://x.test/a.png)", out
    )
    assert 'src="/out/man/images/abc.png"' in html
    assert "state.db" not in html and 'class="img-missing">坏<' in html
    assert 'src="https://x.test/a.png"' in html
    bi = _render("![图](../../man/images/abc.png)", out, bilingual=True)
    assert 'src="/out/man/images/abc.png"' in bi


def test_image_url_rules(out: Path) -> None:
    md_dir = out / "man"
    assert image_url("images/a b.png", md_dir, out, "/out/") == "/out/man/images/a%20b.png"
    assert image_url("/etc/passwd", md_dir, out, "/out/") is None
    assert image_url("file:///c:/x.png", md_dir, out, "/out/") is None
    assert image_url("//evil.test/x.png", md_dir, out, "/out/") is None


def test_external_links_open_in_new_tab(out: Path) -> None:
    html = _render("[原文](https://www.example.test/a) 和 <https://b.test/x>", out)
    assert 'href="https://www.example.test/a" target="_blank" rel="noopener noreferrer"' in html
    assert 'href="https://b.test/x"' in html


def test_frontmatter_is_stripped(out: Path) -> None:
    text = "---\ntitle: X\n---\n\n# 标题\n\n正文"
    assert strip_frontmatter(text).strip().startswith("# 标题")
    html = _render(text, out)
    assert "<h1>标题</h1>" in html and "title: X" not in html


def test_tables_render(out: Path) -> None:
    html = _render("| a | b |\n|---|---|\n| 1 | 2 |", out)
    assert "<table>" in html and "<td>1</td>" in html
