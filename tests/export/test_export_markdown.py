"""Markdown 转换、术语标注、图片地址替换、拼装。"""

from qbkb.export import TermAnnotator, md_filename, rewrite_images, to_markdown
from qbkb.export.markdown import ArticleDoc, render, render_bilingual

TERMS = {"trend following": "趋势跟踪", "alpha": "alpha", "Sharpe ratio": "夏普比率"}


def test_annotates_first_occurrence_only() -> None:
    a = TermAnnotator()
    first = a.annotate("<p>趋势跟踪很简单。趋势跟踪也难。</p>", TERMS)
    assert first == "<p>趋势跟踪（trend following）很简单。趋势跟踪也难。</p>"
    assert a.annotate("<p>趋势跟踪</p>", TERMS) == "<p>趋势跟踪</p>"


def test_annotation_skips_same_text_terms_links_and_code() -> None:
    a = TermAnnotator()
    html = '<p><a href="https://e.test/趋势跟踪">趋势跟踪</a>和<code>趋势跟踪</code>，alpha，趋势跟踪</p>'
    out = a.annotate(html, TERMS)
    assert out.count("（trend following）") == 1
    assert out.endswith("趋势跟踪（trend following）</p>")
    assert "alpha（" not in out
    assert 'href="https://e.test/趋势跟踪"' in out


def test_annotation_only_for_terms_in_source() -> None:
    # 原文这段没出现 Sharpe ratio：译文里的"夏普比率"不标
    a = TermAnnotator()
    assert a.annotate("<p>夏普比率</p>", {}) == "<p>夏普比率</p>"
    assert a.annotate("<p>夏普比率（Sharpe ratio）</p>", TERMS) == "<p>夏普比率（Sharpe ratio）</p>"


def test_to_markdown_structure() -> None:
    assert to_markdown("<h2>小标题</h2>") == "## 小标题"
    assert to_markdown("<ul><li>甲</li><li>乙</li></ul>") == "- 甲\n- 乙"
    md = to_markdown(
        '<figure><img src="images/a.png" alt="图"><figcaption>说明</figcaption></figure>'
    )
    assert "![图](images/a.png)" in md and "说明" in md
    assert to_markdown("<p>x<sup>2</sup></p>") == "x<sup>2</sup>"
    table = to_markdown("<table><tr><th>A</th></tr><tr><td>1</td></tr></table>")
    assert "| A |" in table and "| 1 |" in table


def test_rewrite_images() -> None:
    html = '<p><img alt="x" src="https://e.test/a.png?x=1&amp;y=2"><img src=\'https://e.test/b.jpg\'></p>'
    seen: list[str] = []

    def replace(src: str) -> str:
        seen.append(src)
        return f"images/{len(seen)}.png"

    out = rewrite_images(html, replace)
    assert seen == ["https://e.test/a.png?x=1&amp;y=2", "https://e.test/b.jpg"]
    assert 'src="images/1.png"' in out and "src='images/2.png'" in out


def test_md_filename() -> None:
    assert md_filename("2024-05-01T10:00:00Z", "a-1") == "2024-05-01_a-1.md"
    assert md_filename(None, "a-1") == "undated_a-1.md"
    assert md_filename("May 2024", "a-1") == "undated_a-1.md"


DOC = ArticleDoc(
    frontmatter={"title": "T", "title_zh": "标题", "value_score": 8.0},
    title_zh="标题",
    source_line="> 来源：示例",
    zh_blocks=["正文一", "![](images/a.png)", "版权声明"],
    en_blocks=["Body one", "![](images/a.png)", "Copyright"],
    n_body=2,
)


def test_render_main_file() -> None:
    text = render(DOC)
    assert text.startswith(
        "---\ntitle: T\ntitle_zh: 标题\nvalue_score: 8.0\n---\n\n# 标题\n\n> 来源"
    )
    assert text.endswith("正文一\n\n![](images/a.png)\n\n---\n\n版权声明\n")
    assert "Body one" not in text


def test_render_bilingual_pairs_and_dedupes_images() -> None:
    text = render_bilingual(DOC, "T")
    assert "Body one\n\n正文一" in text
    assert text.count("![](images/a.png)") == 1
    assert "---\n\nCopyright\n\n版权声明" in text
    assert "*T*" in text
