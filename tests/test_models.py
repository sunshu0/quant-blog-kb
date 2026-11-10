from qbkb.models import ArticleMeta, ExtractedArticle, Segment, body_hash, segment_hash


def test_segment_hash_ignores_whitespace_differences() -> None:
    assert segment_hash("<p>Hello   world</p>") == segment_hash("<p>Hello\n  world</p>\n")


def test_segment_hash_changes_with_content() -> None:
    assert segment_hash("<p>Hello world</p>") != segment_hash("<p>Hello world!</p>")


def test_body_hash_depends_on_order() -> None:
    a = Segment(0, "<p>A</p>")
    b = Segment(1, "<p>B</p>")
    assert body_hash([a, b]) != body_hash([b, a])


def test_extracted_article_body_hash_matches_helper() -> None:
    segs = (Segment(0, "<h2>T</h2>"), Segment(1, "<p>x</p>"))
    art = ExtractedArticle(url="https://example.test/a", meta=ArticleMeta(title="T"), segments=segs)
    assert art.body_hash == body_hash(segs)
