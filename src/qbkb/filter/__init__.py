"""价值筛选（design.md 4.4）：L1 规则、L2 LLM 打分、L3 人工覆盖，以及评测。

用法：
    cfg = load_filter_config()
    scorer = FakeScorer(cfg.rubric_version)                       # 测试 / 串联
    scorer = DeepSeekScorer.from_env(load_rubric(cfg.rubric_version, cfg), cfg)
    stats = run(conn, "man", cfg, scorer, load_overrides())
    report = evaluate(conn, load_eval_set(path), cfg)
"""

from qbkb.filter.evaluate import Disagreement, EvalReport, evaluate
from qbkb.filter.pipeline import (
    FAIL_PREFIX,
    FilterBackendStopped,
    FilterQuotaStopped,
    FilterStats,
    FilterStopped,
    apply_override,
    body_text,
    check_l1,
    excerpt,
    filter_article,
    reusable_score,
    run,
    word_count,
)
from qbkb.filter.rules import (
    Decision,
    FilterConfig,
    Override,
    Rubric,
    SiteRules,
    load_eval_set,
    load_filter_config,
    load_overrides,
    load_rubric,
)
from qbkb.filter.scorers import (
    DeepSeekScorer,
    FakeScorer,
    ScoreFormatError,
    ScorerUnavailableError,
    parse_score,
)

__all__ = [
    "FAIL_PREFIX",
    "Decision",
    "DeepSeekScorer",
    "Disagreement",
    "EvalReport",
    "FakeScorer",
    "FilterBackendStopped",
    "FilterConfig",
    "FilterQuotaStopped",
    "FilterStats",
    "FilterStopped",
    "Override",
    "Rubric",
    "ScoreFormatError",
    "ScorerUnavailableError",
    "SiteRules",
    "apply_override",
    "body_text",
    "check_l1",
    "evaluate",
    "excerpt",
    "filter_article",
    "load_eval_set",
    "load_filter_config",
    "load_overrides",
    "load_rubric",
    "parse_score",
    "reusable_score",
    "run",
    "word_count",
]
