# qbkb

把国外量化机构博客抓取、筛选、翻译成中文 Markdown 知识库。设计以 `docs/design.md` 为准。

## 命令

- 安装：`uv sync`
- 完成标准：`bash scripts/verify.sh` 退出码 0（ruff check、ruff format --check、pyright、离线 pytest）
- 单测：`uv run pytest -q -m "not network"`
- CLI：`uv run qbkb --help`

## 约定

- Python 3.13，`src/qbkb/` 布局，类型注解齐全（pyright standard）。
- **测试离线**：不访问网站、不花 DeepL/LLM 额度。外部服务用假后端；要联网的测试标 `@pytest.mark.network`。
- **公开仓库，不放原文**：`raw/`、`output/`、`state.db`、`.env`、`tests/local_samples/` 都被忽略；黄金样本只用构造的 HTML。
- 访问任何站点前调用 `config.ensure_allowed()`（合规闸门）。
- 模块之间只通过 `models.py` 的类型和 `store/` 交换数据。
- 用户可见文字和文档用中文。

## 分工（Phase 1 并行时）

| 线 | 只改这些目录 |
|---|---|
| A | `src/qbkb/discover/` `src/qbkb/fetch/` `tests/discover/` `tests/fetch/` `tests/fixtures/` `config/sites/`（discover 节） |
| B | `src/qbkb/extract/` `tests/extract/` `tests/golden/` `config/sites/`（extract 节） |
| C | `src/qbkb/translate/` `tests/translate/` `config/glossary.yaml` |

共用文件（`models.py`、`store/`、`config.py`、`pyproject.toml`、`uv.lock`、`CLAUDE.md`）要改，先停下说明原因，单独开 PR。

## 完成的定义

verify.sh 通过 → 按 `.claude/skills/ship-pr` 开 PR → 停下，不合并。
