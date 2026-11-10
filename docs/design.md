# qbkb：量化机构博客中文知识库 设计文档

- 版本：v0.1 初稿（2026-11-11）
- 状态：**待审阅**。审阅通过之前不写代码。
- 性质：面试小项目，同时作为"工程化 AI 工作流"的试点。

---

## 0. 待决事项（审阅时请先看这里）

| # | 事项 | 影响 | 当前状态 |
|---|---|---|---|
| D1 | **数据来源合规**：man.com 的条款禁止未经书面许可的自动抓取和建库（见第 3 节） | 决定抓哪里 | **已定**（2026-11-11）：用户已获得许可，man.com 可以使用。许可凭证（邮件等）由用户自己保管，不放进仓库 |
| D2 | **L2 价值打分要用 LLM**，但翻译已定为只用免费机翻 | 筛选的第二层没有可用后端 | **已定**：用便宜的 LLM API **只做打分**（默认 DeepSeek，后端可插拔），只读标题和前 500 个单词。API key 由用户注册，放进 `.env`（见 4.4） |
| D3 | GitHub 仓库 | PR 协作流程的载体 | **已定**：`sunshu0/quant-blog-kb`，**公开（PUBLIC）**，目前是空仓库。因为是公开仓库，任何原文内容都不能提交（见 3.3） |
| D5 | 黄金样本用什么 HTML | 公开仓库不能放原文 | **已定**：用**构造的 HTML** 提交进仓库（照抄页面结构，正文换成自己写的文字）；真实页面只在本地的 `tests/local_samples/` 里验证（见 7） |
| D4 | 知识库的形式（飞书 / Notion / Git / 向量库） | 只影响最后一步"上传" | 待定，先输出成 md 文件夹 |

---

## 1. 目标与范围

**目标**：自动抓取国外量化机构博客中**有价值的文章**，翻译成中文 Markdown，放进团队知识库，用来补充量化和股票方面的基础知识；新文章发布后能**自动增量入库**。

**MVP 范围**：
- 1 个来源站点，约 **30 篇**。DeepL Free 每月 50 万字符，30 篇约 45 万字符，一个月内能翻完。
- 能跑通完整流程：发现 → 抓取 → 抽取 → 筛选 → 翻译 → 输出。
- 有设计文档、自动测试和局限性说明。

**不做（非目标）**：
- PDF 白皮书：标记为 `pdf_pending`，只记录链接。
- 需要渲染 JS 或有反爬挑战的站点：不绕过，标记为需要人工处理。
- 图表内的文字翻译：只翻译图注和 alt 文字。
- 公开发布译文。

---

## 2. 流程总览

```
            ┌──────────── 合规检查（每个站点加入前，人工完成，结论写进站点配置）
            ▼
 discover ─▶ fetch ─▶ extract ─▶ filter ─▶ translate ─▶ export ─▶（以后：上传知识库）
 sitemap     Scrapy    规则优先     L1 规则    DeepL         md + frontmatter
 → URL 清单  限速重试   trafilatura  (L2 LLM)  HTML 模式      纯中文 + 对照版
             存原始HTML 兜底+质检              术语表 + TM    index.md
     │          │          │          │           │            │
     └──────────┴──────────┴────┬─────┴───────────┴────────────┘
                                ▼
                          store（SQLite）
                 文章状态 · 整篇 hash · 段落翻译记忆
```

每个阶段只处理状态库里"轮到自己"的文章，所以整个流程可以**断点续跑**，而且是**幂等**的：同一条命令跑一次和跑十次，结果一样。

---

## 3. 合规

### 3.1 规则
**robots.txt 允许，不等于条款允许。** 每个站点加入之前，都要人工检查以下内容，并把结论写进 `sites/<site>.yaml` 的 `compliance` 字段：

```yaml
compliance:
  robots_txt: allows /insights/          # robots.txt 的结论
  terms_url: https://.../terms           # 条款页面地址
  terms_summary: "..."                   # 条款对抓取、存储、再分发的规定
  checked_at: 2026-11-11
  decision: allowed | permission_needed | blocked
```

`decision` 不是 `allowed` 的站点，爬虫**拒绝运行**。这道闸由代码强制执行，不靠人记得。

### 3.2 man.com 的检查结果
- robots.txt：只禁止 `/core/` 和 `/profiles/`，文章区放行。
- 条款（`/terms-and-conditions` 第 6 节，以下是转述）：
  - 未经书面许可，不得用爬虫等自动化手段复制、存储、存档、挖掘、抓取内容用于公开或商业目的；
  - 特别禁止：以图书馆或档案的形式再分发、去掉版权声明、通过系统化下载存储内容来建立数据库。
- 原结论为 `permission_needed`；用户已获得许可（2026-11-11），改为 **`allowed`**。配置里记录许可依据和日期。
- 许可默认只覆盖本项目的抓取和内部使用，**不包括公开再分发**，所以公开仓库里仍然不放原文（见 3.3、7）。

### 3.3 对输出的影响
- 译文保留原文的版权声明，**不删除**（这条推翻了讨论时第 8 点"去掉免责声明"的建议）。每篇开头另加一行来源说明。
- 仓库是**公开**的，所以 `.env`、`raw/`、`output/`、`state.db`、Scrapy 的缓存目录（`.scrapy/`）都必须在 `.gitignore` 里，**第一次提交前要逐项核对**。
- 黄金样本不放真实原文（见 7）。

---

## 4. 各阶段设计

### 4.1 discover：发现 URL
- 按顺序尝试：sitemap → RSS → 列表页 API → 无头浏览器，能用前面的就不用后面的。
- 按站点配置过滤：路径前缀白名单、排除路径、多语言去重（hreflang）。
- 自己解析 sitemap XML，不用 Scrapy 的 `SitemapSpider`。这样能脱离 Scrapy 单独测试（用本地 sitemap 文件），新 URL 直接写进状态库。
- 记录每个 URL 的 `lastmod`。

### 4.2 fetch：抓取
- 用 Scrapy，从状态库读取待抓取的 URL。
- 配置：`ROBOTSTXT_OBEY=True`、`AutoThrottle`（同一站点并发 1，间隔约 2 秒）、超时 30 秒、指数退避重试 3 次、明确的 User-Agent。
- **原始 HTML 由我们自己存到** `raw/<site>/<slug>.html`。这是对讨论时"用 HTTPCACHE"的细化：HTTPCACHE 的存储格式是 Scrapy 内部的，extract 阶段不方便直接读，所以只在开发调试时打开它。
- 支持条件请求（ETag / If-Modified-Since），对方返回 304 就跳过。
- 遇到 403 或人机验证页面：标记 `failed`，写明原因，**不绕过**。

### 4.3 extract：正文抽取与清洗
- 站点规则（CSS 选择器）优先，trafilatura 兜底。规则可以按页面模板分多条。
- 去噪：弹窗、分享按钮、订阅框；**保留**版权声明。
- 保留结构：标题层级、列表、表格、脚注、图片和图注、公式；相对链接转成绝对链接。
- 元数据优先从 JSON-LD 和 meta 标签提取。
- 输出**清洗后的 HTML**，按块级元素切成段落，交给 translate。转 Markdown 放到 export 阶段做（见 4.5）。
- 自动质检：正文太短、出现噪音关键词、缺少元数据的，标记出来给人看。

### 4.4 filter：价值筛选
- 收：量化方法论 / 研究、基础概念讲解、行为金融 / 金融史、与量化相关的 AI。
- 不收：宏观周评、ESG / 气候、公司文化 / 软文。
- L1：`filters.yaml` 里的规则（路径、栏目、长度）。
- L2：LLM 读标题和前 500 个单词，按评分标准打分，输出 JSON（score、category、evergreen、reason），记录 `rubric_version`。后端用便宜的 LLM API（默认 DeepSeek，`LLM_API_KEY` 放 `.env`），接口可插拔；测试时用假后端，不花钱。
- L3：人工抽检；人工判断过的文章保存为评测集，以后改提示词时先在评测集上验证。
- 被筛掉的文章标记为 `skipped`，不删除，以后放宽标准时不用重新爬取。

### 4.5 translate：翻译
- 后端可插拔，接口为 `translate(segments, glossary) -> translations`。MVP 用 DeepL API Free，Azure 作为备选。
- 使用 DeepL 的 **HTML 模式**（`tag_handling=html`），标签原样保留；`glossary.yaml` 同步成 DeepL 的术语表。
- **段落级翻译记忆**：key 是段落 HTML 规范化后的 hash。命中就复用，不命中才翻译。重复的段落（比如版权声明）只翻译一次。
- 额度用完就停下，记录进度，下个月接着翻。
- 校验：翻译前后标签、链接、图片的数量必须一致，不一致就重新翻译。
- 术语第一次出现时附上英文（在导出阶段处理）。

### 4.6 export：输出
- `output/<site>/<日期>_<slug>.md`，图片下载到 `output/<site>/images/`。
- frontmatter 字段：title、title_zh、firm、author、published、source_url、category、value_score、rubric_version、translator、translated_at。
- 开头加一行来源和版权说明；保留原文的版权声明（译文）。
- 主文件用**纯中文**；另外从翻译记忆生成 `output/bilingual/` 对照版，不额外消耗额度。
- 每次运行后重新生成 `output/index.md`，按类别分组列出。

### 4.7 增量与定时
- 每天跑一次：拉 sitemap → 只处理新 URL 和 `lastmod` 变化的 URL → 整篇 hash 没变就跳过。
- 每月全量复查一次（使用条件请求），用来发现 lastmod 没更新的修改。
- 原文更新后直接覆盖译文（方案 A）。
- 定时方式（本机任务计划、云服务器、GitHub Actions）等 MVP 完成后再定。

---

## 5. 数据模型（SQLite：`state.db`）

```sql
CREATE TABLE articles (
  url            TEXT PRIMARY KEY,
  site           TEXT NOT NULL,
  slug           TEXT NOT NULL,
  status         TEXT NOT NULL,  -- discovered|fetched|extracted|filtered|skipped|
                                 -- translated|published|failed|pdf_pending
  lastmod        TEXT,
  etag           TEXT,
  http_status    INTEGER,
  raw_path       TEXT,
  body_hash      TEXT,           -- 整篇正文 hash
  title          TEXT,
  title_zh       TEXT,
  author         TEXT,
  published      TEXT,
  category       TEXT,
  value_score    REAL,
  rubric_version TEXT,
  md_path        TEXT,
  error          TEXT,
  retries        INTEGER NOT NULL DEFAULT 0,
  updated_at     TEXT NOT NULL
);

CREATE TABLE segments (            -- 文章由哪些段落组成、顺序如何
  article_url TEXT NOT NULL REFERENCES articles(url),
  idx         INTEGER NOT NULL,
  src_hash    TEXT NOT NULL,
  PRIMARY KEY (article_url, idx)
);

CREATE TABLE translation_memory (  -- 段落级翻译记忆
  src_hash         TEXT NOT NULL,
  backend          TEXT NOT NULL,  -- deepl | azure | ...
  src_html         TEXT NOT NULL,
  tgt_html         TEXT NOT NULL,
  glossary_version TEXT,
  created_at       TEXT NOT NULL,
  PRIMARY KEY (src_hash, backend)
);
```

---

## 6. 仓库结构

```
quant-blog-kb/
├── pyproject.toml            # uv 管理依赖
├── CLAUDE.md                 # 简短：命令、约定、完成标准
├── config/
│   ├── sites/<site>.yaml     # 发现方式、选择器、合规结论
│   ├── filters.yaml
│   └── glossary.yaml
├── src/qbkb/
│   ├── models.py             # Article / Segment 等数据类和模块间接口
│   ├── store/  discover/  fetch/  extract/  filter/  translate/  export/
│   └── cli.py
├── tests/
│   ├── golden/               # 黄金样本：输入 HTML + 期望输出
│   └── fixtures/             # sitemap 等本地文件
├── scripts/verify.sh
├── docs/  design.md  ai-retro.md
├── .env                      # DEEPL_API_KEY、LLM_API_KEY（不提交）
└── .gitignore                # .env raw/ output/ state.db .scrapy/ tests/local_samples/
```

---

## 7. 质量与测试

- **完成标准**：`scripts/verify.sh` 退出码为 0。它会依次运行 `ruff check`、`ruff format --check`、`pyright`、`pytest -m "not network"`。
- **测试完全离线**：不访问网站，不消耗 DeepL 额度。翻译测试用假后端；真实 DeepL 只做一次手动冒烟测试。
- **黄金样本回归**：每个站点 5–10 篇。仓库公开，所以样本用**构造的 HTML**：照着真实页面的 DOM 结构写（导航、弹窗、正文容器、图片、脚注、版权声明都保留），正文换成自己写的文字。版式和结构本身不受版权保护，这样既能测试抽取规则，又不会公开原文。
- 用真实页面做的验证只在本地进行，文件放在被忽略的目录 `tests/local_samples/` 里，不提交。

---

## 8. 开发流程（"工程化 AI 工作流"试点）

> 这一节来自"顶级工程师的 Claude 工作流"会话转达的建议，审阅时一并确认。

| 阶段 | 内容 | 方式 |
|---|---|---|
| Phase 0 | 骨架：pyproject、models.py、store/、verify.sh、CLAUDE.md、ruff 格式化 hook、ship-pr skill、ai-retro.md | 串行，1 个 PR |
| Phase 1 | A：discover + fetch；B：extract + 黄金样本；C：translate | 3 条线并行，每条线一个 worktree、一个会话、一个 PR，只改自己的目录 |
| Phase 2 | filter + export + CLI 串联 | 串行 |

- 审查：开一个干净的新会话跑 `/code-review`。
- 复盘：`docs/ai-retro.md` 记录"问题 / 处理 / 落到哪一层"，以及每个 PR 的 token 消耗、开工到合并时长、返工次数。
- 项目配置（**用户已批准，2026-11-11**）：`CLAUDE.md`、`.claude/settings.json`（PostToolUse hook：Edit/Write 修改 `.py` 文件后运行 `ruff format`）、`.claude/skills/ship-pr/SKILL.md`。这三个文件都在 Phase 0 创建。

### 和已定方案的差异（以已定方案为准）
1. 黄金样本原计划直接用 man.com 的原始 HTML，但条款不允许，所以样本来源跟着 D1 一起定。
2. discover 不用 Scrapy 的 SitemapSpider，改为自己解析 sitemap，以便和 fetch 分开、各自测试（见 4.1）。
3. 原始 HTML 由我们自己存，HTTPCACHE 只在开发时使用（见 4.2）。

---

## 9. 局限性（MVP）

- 不处理 PDF。
- 图表里的文字不翻译。
- 机翻质量受限：术语一致性靠术语表，长难句可能生硬；以后可以换成 LLM 后端。
- 需要 JS 渲染或有反爬挑战的站点暂不支持。
- 合规检查是人工完成的，代码只负责强制执行检查结论。
