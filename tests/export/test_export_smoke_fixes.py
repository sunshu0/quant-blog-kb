"""真实冒烟后的导出修正：中文标点后的空格、相邻图片各占一行。"""

from qbkb.export.markdown import split_image_rows, tidy_zh_spacing, to_markdown


def test_tidy_removes_space_after_chinese_punctuation() -> None:
    assert tidy_zh_spacing("有影响。 如果利率上升， 那么；  结果：\t如下、 等等") == (
        "有影响。如果利率上升，那么；结果：如下、等等"
    )
    assert tidy_zh_spacing("真的吗？ 是的！ 好") == "真的吗？是的！好"


def test_tidy_keeps_english_code_links_and_hard_breaks() -> None:
    # 英文句子之间的空格
    assert tidy_zh_spacing("First one. Second one, third.") == "First one. Second one, third."
    # 代码
    assert tidy_zh_spacing("见 `a。 b` 和\n\n```\nx， y\n```") == "见 `a。 b` 和\n\n```\nx， y\n```"
    # 链接地址不动，链接文字照常清理
    assert tidy_zh_spacing("看[这，  里](https://example.test/a，%20b)。 好") == (
        "看[这，里](https://example.test/a，%20b)。好"
    )
    # 行尾两个空格是硬换行；换行本身不动；表格的分隔线不动
    assert tidy_zh_spacing("第一行。  \n第二行") == "第一行。  \n第二行"
    assert tidy_zh_spacing("| 好。 | 行， |") == "| 好。 | 行， |"


def test_adjacent_block_images_get_their_own_lines() -> None:
    md = to_markdown('<figure><img src="a.jpg" alt="x"><img src="b.jpg"></figure>')
    assert md == "![x](a.jpg)\n\n![](b.jpg)"
    md = to_markdown('<p><img src="a.jpg"><img src="b.jpg"> <img src="c.jpg"></p>')
    assert md.split("\n\n") == ["![](a.jpg)", "![](b.jpg)", "![](c.jpg)"]
    md = to_markdown(
        '<figure><img src="a.jpg"><img src="b.jpg"><figcaption>cap</figcaption></figure>'
    )
    assert md.split("\n\n") == ["![](a.jpg)", "![](b.jpg)", "cap"]


def test_inline_images_and_code_are_not_split() -> None:
    inline = "text ![](a.jpg)![](b.jpg) more"
    assert split_image_rows(inline) == inline
    assert split_image_rows("![](a.jpg)") == "![](a.jpg)"
    code = "```\n![](a.jpg)![](b.jpg)\n```"
    assert split_image_rows(code) == code
    lst = "- ![](a.jpg)![](b.jpg)"
    assert split_image_rows(lst) == lst
