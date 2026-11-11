"""输出 Markdown（design.md 4.6）：frontmatter、纯中文 + 对照版、图片本地化、index.md。

用法：
    images = ImageFetcher(site, out_dir / site.name / "images")    # 不下载图片就传 None
    stats = run(conn, site, load_glossary(), out_dir, images=images, categories=cfg.categories)
"""

from qbkb.export.images import ImageError, ImageFetcher, image_stem
from qbkb.export.markdown import (
    TermAnnotator,
    md_filename,
    rewrite_images,
    to_markdown,
)
from qbkb.export.pipeline import (
    BILINGUAL_DIR,
    FAIL_PREFIX,
    IMAGES_DIR,
    INDEX_NAME,
    ExportStats,
    ImageFailure,
    article_paths,
    build_index,
    export_article,
    run,
    source_line,
    write_index,
)

__all__ = [
    "BILINGUAL_DIR",
    "FAIL_PREFIX",
    "IMAGES_DIR",
    "INDEX_NAME",
    "ExportStats",
    "ImageError",
    "ImageFailure",
    "ImageFetcher",
    "TermAnnotator",
    "article_paths",
    "build_index",
    "export_article",
    "image_stem",
    "md_filename",
    "rewrite_images",
    "run",
    "source_line",
    "to_markdown",
    "write_index",
]
