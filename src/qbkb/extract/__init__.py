"""正文抽取与清洗（站点规则优先，trafilatura 兜底，自动质检）。Phase 1 B 线负责。

用法：
    rules = load_rules(config.load_site("man"))
    article = extract(raw_html, url, rules)          # -> ExtractedArticle
    result = extract_full(raw_html, url, rules)      # 带质检需要的信息
    issues = check_quality(result, rules)
    stats = run(conn, site)                          # 处理状态库里全部 fetched 文章
"""

from qbkb.extract.core import ExtractError, Extraction, extract, extract_full
from qbkb.extract.pipeline import QC_PREFIX, RunStats, extract_one, run
from qbkb.extract.quality import check_quality
from qbkb.extract.rules import ExtractRules, load_rules

__all__ = [
    "QC_PREFIX",
    "ExtractError",
    "ExtractRules",
    "Extraction",
    "RunStats",
    "check_quality",
    "extract",
    "extract_full",
    "extract_one",
    "load_rules",
    "run",
]
