"""PR #8 审查后的导出修正：缩进代码块、全角空格、行内代码后面的图片。"""

from qbkb.export.markdown import split_image_rows, tidy_zh_spacing


def test_tidy_keeps_indented_code_blocks() -> None:
    md = "说明。 如下：\n\n    x = 1，  y = 2\n    print（a。 b）\n\n\tz， w\n\n完。 好"
    assert tidy_zh_spacing(md) == (
        "说明。如下：\n\n    x = 1，  y = 2\n    print（a。 b）\n\n\tz， w\n\n完。好"
    )


def test_tidy_keeps_fenced_blocks_of_both_kinds() -> None:
    md = "前。 后\n\n~~~\na， b\n~~~\n\n```python\nc。 d\n```\n\n完， 了"
    assert tidy_zh_spacing(md) == "前。后\n\n~~~\na， b\n~~~\n\n```python\nc。 d\n```\n\n完，了"
    # 围栏里出现另一种围栏记号，不会提前结束
    md = "```\na， b\n~~~\nc， d\n```\n外。 面"
    assert tidy_zh_spacing(md) == "```\na， b\n~~~\nc， d\n```\n外。面"


def test_tidy_keeps_fullwidth_spaces() -> None:
    assert tidy_zh_spacing("第一条：　内容。　　下一句") == "第一条：　内容。　　下一句"
    # 半角空格和制表符照常去掉
    assert tidy_zh_spacing("内容。 \t下一句") == "内容。下一句"


def test_tidy_inline_code_in_the_middle_of_a_line() -> None:
    assert tidy_zh_spacing("先， 看 `a， b`， 再。 看") == "先，看 `a， b`，再。看"


def test_images_after_inline_code_are_not_split() -> None:
    for line in (
        "`code`![](a.jpg)![](b.jpg)",
        "见 `x`： ![](a.jpg)![](b.jpg)",
        "![](a.jpg)![](b.jpg) `code`",
    ):
        assert split_image_rows(line) == line


def test_image_rows_in_code_blocks_are_not_split() -> None:
    for md in (
        "    ![](a.jpg)![](b.jpg)",
        "~~~\n![](a.jpg)![](b.jpg)\n~~~",
        "```md\n![](a.jpg)![](b.jpg)\n```",
    ):
        assert split_image_rows(md) == md
    # 代码块前后的图片行照常拆
    md = "![](a.jpg)![](b.jpg)\n\n```\n![](c.jpg)![](d.jpg)\n```\n\n![](e.jpg) ![](f.jpg)"
    assert split_image_rows(md) == (
        "![](a.jpg)\n\n![](b.jpg)\n\n```\n![](c.jpg)![](d.jpg)\n```\n\n![](e.jpg)\n\n![](f.jpg)"
    )
