"""export 阶段：translated → output/<site>/<日期>_<slug>.md（+ 对照版、index.md）→ published。"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from qbkb import store
from qbkb.config import SiteConfig
from qbkb.export.images import ImageError, ImageFetcher
from qbkb.export.markdown import (
    ArticleDoc,
    TermAnnotator,
    date_part,
    escape_text,
    md_filename,
    render,
    render_bilingual,
    render_untranslated,
    rewrite_images,
    safe_url,
    tidy_zh_spacing,
    to_markdown,
    url_allowed,
)
from qbkb.models import Status
from qbkb.sections import load_section_rules
from qbkb.summarize import valid_summary
from qbkb.translate import Glossary, lookup_translation, translated_title

log = logging.getLogger(__name__)

FAIL_PREFIX = "export: "
BILINGUAL_DIR = "bilingual"
IMAGES_DIR = "images"
INDEX_NAME = "index.md"
UNCATEGORIZED = "未分类"


@dataclass(frozen=True, slots=True)
class ImageFailure:
    article_url: str
    image_url: str
    reason: str


@dataclass(slots=True)
class ExportStats:
    published: int = 0
    unchanged: int = 0  # 文件内容和上次一样，没有重写
    requeued: list[str] = field(default_factory=list)  # 缺译文，退回 extracted 重新翻译
    failed: list[str] = field(default_factory=list)
    images_downloaded: int = 0
    image_failures: list[ImageFailure] = field(default_factory=list)
    # 外站图片（不在站点 host / export.image_hosts 里）：保留原链接，不算失败、不写 failed.tsv
    external_images: list[ImageFailure] = field(default_factory=list)


def firm_name(site: SiteConfig) -> str:
    firm = site.raw.get("firm")
    return str(firm) if firm else site.name


def checked_source_url(url: str) -> str | None:
    """原文地址也过协议白名单（只放行 http / https）；不合格就不写进输出。"""
    return url if url_allowed(url, "image") and "://" in url else None


def source_line(firm: str, title: str, url: str) -> str:
    """每篇开头的来源和版权说明（design.md 3.3）。"""
    checked = checked_source_url(url)
    where = f"，<{safe_url(checked)}>" if checked else ""
    return (
        f"> 来源：{escape_text(firm)}《{escape_text(title)}》{where}。"
        f"原文版权归 {escape_text(firm)} 所有；本文为机器翻译，"
        "仅供内部学习参考，未经许可不得再分发。文末附原文的重要信息与版权声明（译文）。"
    )


def write_if_changed(path: Path, text: str) -> bool:
    """内容和现有文件一样就不写（保持幂等、不刷新修改时间）。返回是否写了。"""
    data = text.encode("utf-8")
    if path.is_file() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return True


def article_paths(out_dir: Path, site: str, published: str | None, slug: str) -> tuple[Path, Path]:
    """(主文件, 对照版)。"""
    name = md_filename(published, slug)
    return out_dir / site / name, out_dir / BILINGUAL_DIR / site / name


def _localize(
    html: str,
    prefix: str,
    url: str,
    images: ImageFetcher | None,
    stats: ExportStats,
    seen: set[str],
) -> str:
    """把图片地址换成本地相对路径；下载失败就保留原链接并记录（每篇每张只记一次）。"""
    if images is None:
        return html

    def replace(src: str) -> str:
        if not url_allowed(src, "image"):
            return src  # 协议不在白名单里：不下载，to_markdown 会把它去掉
        if images.is_external(src):
            # 外站图片按规则本来就不下载：保留原链接，单独统计，不算"图片失败"
            if src not in seen:
                seen.add(src)
                stats.external_images.append(ImageFailure(url, src, "外站图片，保留原链接"))
            return src
        before = images.requests
        try:
            name = images.get(src)
        except ImageError as e:
            if src not in seen:
                seen.add(src)
                stats.image_failures.append(ImageFailure(url, src, str(e)))
            return src
        if images.requests > before:
            stats.images_downloaded += 1
        return f"{prefix}{IMAGES_DIR}/{name}"

    return rewrite_images(html, replace)


def export_article(
    conn: sqlite3.Connection,
    url: str,
    site: SiteConfig,
    glossary: Glossary,
    out_dir: Path,
    images: ImageFetcher | None,
    stats: ExportStats,
) -> Path | None:
    """导出一篇。返回主文件路径；有全文译文但缺译文时退回 extracted 并返回 None。

    backend（translator）为空 = 还没翻全文：中文正文留空，输出英文原文 + 中文标题 + 摘要，
    不报错、不退回重翻（分层翻译的入库结果）。
    """
    row = store.get_article(conn, url)
    if row is None:
        raise KeyError(url)
    backend = row["translator"]
    segments = store.get_segments(conn, url)
    if not segments:
        raise ValueError("没有段落")

    if backend:
        zh_html: list[str] = []
        missing = 0
        for seg in segments:
            tgt = lookup_translation(conn, seg.html, str(backend), glossary)
            if tgt is None:
                missing += 1
            zh_html.append(tgt or "")
        if missing:
            msg = f"{FAIL_PREFIX}缺 {missing} 段译文（术语表改过？），退回 extracted 重新翻译"
            store.set_status(conn, url, Status.EXTRACTED, error=msg)
            stats.requeued.append(url)
            return None
    else:
        zh_html = ["" for _ in segments]

    title = str(row["title"] or row["slug"])
    title_zh = (
        row["title_zh"]
        or (translated_title(conn, row["title"], str(backend), glossary) if backend else None)
        or title
    )
    n_body = row["body_segments"]
    n_body = len(segments) if n_body is None else min(int(n_body), len(segments))

    seen: set[str] = set()
    annotator = TermAnnotator(glossary)
    # 主文件在 output/<site>/，图片路径 images/x；对照版在 output/bilingual/<site>/，
    # 图片路径 ../../<site>/images/x（同一份图片，不重复下载）
    bi_prefix = f"../../{row['site']}/"
    zh_blocks: list[str] = []
    zh_bi_blocks: list[str] = []
    en_blocks: list[str] = []
    for seg, tgt in zip(segments, zh_html, strict=True):
        if tgt:
            tgt = annotator.annotate(tgt, glossary.terms_in(seg.html))
        zh = to_markdown(_localize(tgt, "", url, images, stats, seen))
        zh_bi = to_markdown(_localize(tgt, bi_prefix, url, images, stats, seen))
        # 中文段落去掉标点后面多余的空格（英文段落不动）
        zh_blocks.append(tidy_zh_spacing(zh))
        zh_bi_blocks.append(tidy_zh_spacing(zh_bi))
        en_blocks.append(to_markdown(_localize(seg.html, bi_prefix, url, images, stats, seen)))

    category = row["category"]
    score = row["value_score"]
    firm = firm_name(site)
    frontmatter: dict[str, object] = {
        "title": title,
        "title_zh": title_zh,
        "firm": firm,
        "author": row["author"],
        "published": row["published"],
        "source_url": checked_source_url(url),
        "section": row["section"],
        "category": category,
        "value_score": None if score is None else float(score),
        "rubric_version": row["rubric_version"],
        "translator": backend,
        "translated_at": row["translated_at"],
    }
    doc = ArticleDoc(
        frontmatter=frontmatter,
        title_zh=title_zh,
        source_line=source_line(firm, title, url),
        zh_blocks=zh_blocks,
        en_blocks=en_blocks,
        n_body=n_body,
    )
    main, bilingual = article_paths(out_dir, str(row["site"]), row["published"], str(row["slug"]))
    if backend:
        wrote = write_if_changed(main, render(doc))
    else:
        wrote = write_if_changed(main, render_untranslated(doc, valid_summary(conn, row)))
    doc_bi = dataclasses.replace(doc, zh_blocks=zh_bi_blocks)
    wrote = write_if_changed(bilingual, render_bilingual(doc_bi, title)) or wrote
    if not wrote:
        stats.unchanged += 1
    # 发布日期变了，文件名跟着变：删掉旧文件，避免同一篇出现两份
    # 只删本次 out_dir 里的：换了 --out 重建时，不能动别的输出目录
    old = row["md_path"]
    root = out_dir.resolve()
    if old and Path(old).resolve() != main.resolve() and Path(old).resolve().is_relative_to(root):
        old_path = Path(old)
        old_bi = out_dir / BILINGUAL_DIR / str(row["site"]) / old_path.name
        for p in (old_path, old_bi):
            with contextlib.suppress(OSError):
                p.unlink(missing_ok=True)
    store.set_status(conn, url, Status.PUBLISHED, md_path=main.resolve().as_posix(), error=None)
    stats.published += 1
    return main


def _record_image_failures(out_dir: Path, site: str, failures: Sequence[ImageFailure]) -> None:
    """失败的图片追加记进 output/<site>/images/failed.tsv（时间、文章、图片、原因）。"""
    if not failures:
        return
    path = out_dir / site / IMAGES_DIR / "failed.tsv"
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8", newline="\n") as f:
        for x in failures:
            reason = x.reason.replace("\t", " ").replace("\n", " ")
            f.write(f"{now}\t{x.article_url}\t{x.image_url}\t{reason}\n")


def run(
    conn: sqlite3.Connection,
    site: SiteConfig,
    glossary: Glossary,
    out_dir: Path,
    *,
    images: ImageFetcher | None = None,
    categories: Sequence[str] = (),
    rebuild: bool = False,
    limit: int | None = None,
) -> ExportStats:
    """导出 translated / published 文章，以及还没翻全文、但已经入库（extracted / filtered）的
    文章（中文正文留空），最后重建 index.md。

    images 为 None 时不下载图片，Markdown 里保留原始链接。
    """
    stats = ExportStats()
    urls = store.list_by_status(conn, Status.TRANSLATED, site.name)
    if rebuild:
        urls = sorted(urls + store.list_by_status(conn, Status.PUBLISHED, site.name))
    # 还没翻全文的文章也导出（分层翻译：入库只翻标题 + 摘要）；按栏目配置过滤
    rules = load_section_rules(site)
    for row in store.list_rows(conn, [Status.EXTRACTED, Status.FILTERED], site.name):
        if rules.enabled and row["section_key"] is None:
            continue
        if rules.collects(str(row["url"]), row["section_key"]):
            urls.append(str(row["url"]))
    urls = sorted(set(urls))
    if limit is not None:
        urls = urls[:limit]
    for url in urls:
        try:
            export_article(conn, url, site, glossary, out_dir, images, stats)
        except Exception as e:  # 一篇出错不影响其他文章
            msg = f"{FAIL_PREFIX}{type(e).__name__}: {e}"
            log.warning("导出失败 %s：%s", url, msg)
            stats.failed.append(f"{url}：{msg}")
            store.record_failure(conn, url, msg)
    _record_image_failures(out_dir, site.name, stats.image_failures)
    sections = load_section_rules(site).sections
    names = {s.id: s.name for s in sections}
    order = [s.name for s in sections] + list(categories)
    write_index(conn, out_dir, order, names)
    return stats


def _index_entry(row: sqlite3.Row) -> str:
    site, published, slug = str(row["site"]), row["published"], str(row["slug"])
    name = md_filename(published, slug)
    title = escape_text(str(row["title_zh"] or row["title"] or slug))
    title = title.replace("[", "［").replace("]", "］")
    score = row["value_score"]
    bits = [date_part(published)]
    if score is not None:
        bits.append(f"{float(score):g} 分")
    return (
        f"- [{title}]({site}/{name}) — {' · '.join(bits)}"
        f"（[对照版]({BILINGUAL_DIR}/{site}/{name})）"
    )


def _group(row: sqlite3.Row, section_names: Mapping[str, str]) -> str:
    """分组：v2 按栏目（站点配置里的栏目名）；没有栏目的老数据按 v1 的打分类别。"""
    section = row["section"]
    if section:
        return section_names.get(str(section), str(section))
    return str(row["category"] or UNCATEGORIZED)


def build_index(
    rows: Sequence[sqlite3.Row],
    categories: Sequence[str] = (),
    section_names: Mapping[str, str] | None = None,
) -> str:
    """index.md 的内容：按栏目分组（老数据按类别；先按给定顺序，再是其他），组内按日期新到旧。"""
    names = section_names or {}
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(_group(row, names), []).append(row)
    order = [c for c in dict.fromkeys(categories) if c in groups]
    order += sorted(c for c in groups if c not in order)
    lines = [
        "# 量化博客中文知识库",
        "",
        f"共 {len(rows)} 篇，按栏目分组。机器翻译，仅供内部学习参考。",
    ]
    for cat in order:
        items = sorted(groups[cat], key=lambda r: (str(r["published"] or ""), str(r["url"])))
        lines += ["", f"## {cat}（{len(items)}）", ""]
        lines += [_index_entry(r) for r in reversed(items)]
    return "\n".join(lines) + "\n"


def write_index(
    conn: sqlite3.Connection,
    out_dir: Path,
    categories: Sequence[str] = (),
    section_names: Mapping[str, str] | None = None,
) -> Path:
    """按状态库里全部已发布的文章（所有站点）重建 output/index.md。"""
    path = out_dir / INDEX_NAME
    rows = store.list_rows(conn, [Status.PUBLISHED])
    write_if_changed(path, build_index(rows, categories, section_names))
    return path
