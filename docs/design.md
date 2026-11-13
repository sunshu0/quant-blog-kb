# qbkb：量化机构博客中文知识库 设计文档

- 版本：v0.1 初稿（2026-11-11）
- 状态：已实现（Phase 0–2 和冒烟修复已合并；本地控制台见第 10 节）；第 0 节列出仍待决定的事项。
- **v2（2026-11-12）**：按栏目采集、采集与筛选分开（老板反馈）。默认流程不再调用 LLM 打分，筛选改为人工选择栏目，见 D13 和 4.9。
- **v2 分层翻译（2026-11-13）**：入库与全文翻译分开。默认流程变成 discover → fetch → extract → 翻译标题（titles）→ 生成摘要（summarize）→ export，**不翻全文**；全文按清单用 `qbkb translate --url-file` 翻，或用 `qbkb run --full-translate` 恢复旧行为。见 4.5、4.6 和 4.10。
- 性质：面试小项目，同时作为"工程化 AI 工作流"的试点。

---

## 0. 待决事项（审阅时请先看这里）

| # | 事项 | 影响 | 当前状态 |
|---|---|---|---|
| D1 | **数据来源合规**：man.com 的条款要求自动抓取、建库需经书面许可（见第 3 节） | 决定抓哪里 | **已定**（2026-11-11/12）：本项目由公司安排，用户确认已获许可，man.com 可以使用；许可凭证由用户保管，不放进仓库。他人从公开仓库拷贝代码时不在该许可内（见 D12 演示模式） |
| D2 | **L2 价值打分要用 LLM**，但翻译已定为只用免费机翻 | 筛选的第二层没有可用后端 | **已定**：用便宜的 LLM API **只做打分**（默认 DeepSeek，后端可插拔），只读标题和前 500 个单词。API key 由用户注册，放进 `.env`（见 4.4）。**v2（2026-11-12）**：打分移出默认流程（爬虫里不用 LLM 筛选），代码保留为可选的 `qbkb filter` |
| D3 | GitHub 仓库 | PR 协作流程的载体 | **已定**：`sunshu0/quant-blog-kb`，**公开（PUBLIC）**，目前是空仓库。因为是公开仓库，任何原文内容都不能提交（见 3.3） |
| D5 | 黄金样本用什么 HTML | 公开仓库不能放原文 | **已定**：用**构造的 HTML** 提交进仓库（照抄页面结构，正文换成自己写的文字）；真实页面只在本地的 `tests/local_samples/` 里验证（见 7） |
| D4 | 知识库的形式（飞书 / Notion / Git / 向量库） | 只影响最后一步"上传" | **待定**：用户向公司确认（2026-11-12）；在此之前输出 md 文件夹 |
| D6 | **"有价值文章"的具体标准**：现行标准（`config/rubrics/v2.md`）没有偏向股票，债券配置、私募股权对冲、通胀资产配置类文章也被归进"量化方法论"收录；只看前 500 个单词；evergreen 不参与决策 | 决定收哪些文章、花多少翻译额度 | **待定**（2026-11-12）：用户向公司确认想要的文章标准，之后写评分标准 v3，并用人工标注的评测集（`config/eval/man.yaml`）校准。**v2**：不再用评分标准决定收哪些文章，改为人工选择栏目（D13） |
| D7 | 结构校验分级：第一批 50 篇里 6 篇共 13 段被拦，其中 7 段只是 `<em>` 数量变了、3 段是链接被拆分；另有脚注上标重复 / 错位（译文验收发现） | needs_review 的比例（本批 6/10） | **已定**（2026-11-12）：**严格**——sup（编号唯一、不重复、不丢失）、img、math、code、table；**按地址**——a 比较 href 集合；**放宽**——em/i/strong/b 不比数量。同时查清脚注重复的来源。**已实现**（PR A 译文质量修复，见 4.5）：脚注重复的原因是占位链接的文字就是编号本身，DeepL 把它当正文数字处理 |
| D8 | 翻译额度与全量规模：实测平均约 2.3 万字符/篇，man.com 全站约 200 篇收录 ≈ 460 万字符，约 5 个月的 DeepL Free 额度 | 全量需要多久、要不要付费或换翻译后端 | **已定**（2026-11-12）：先做出 MVP（30–50 篇），扩大规模时再议 |
| D9 | 自动定时更新 | 新文章能否自动入库 | **已定**（2026-11-12）：Windows 任务计划，**每天一次**（默认 09:00，错过后开机补跑，每次最多 5 篇，均可在设置页修改）；网页控制台保留**手动运行**按钮和**自动更新开关**。**已实现**（PR B 控制台，见第 10 节） |
| D10 | 其他机构的站点（AQR、Two Sigma 等） | 知识库覆盖面 | **已定**（2026-11-12）：暂不考虑，MVP 只做 man.com；以后要加再按"合规检查 → 发现方式 → 抽取规则 → 真实冒烟"逐站添加 |
| D11 | Man 许可的范围：是否覆盖在公司内部知识库共享译文 | 能否把译文放进团队知识库 | 随 D1：由公司安排、用户确认有许可；公开仓库只放代码 |
| D12 | 使用与演示方式 | 日常使用、面试演示 | **已定**（2026-11-12）：一个**本地网页控制台**——浏览文章（中文 / 中英对照）、点击运行（选篇数、实时进度、额度）、待复查列表与重翻、设置（API key 存本机 `.env`、自动更新开关和时间）；桌面快捷方式双击启动。**演示模式**（默认，无 key 时）：抓取仓库自带的构造示例站点，本机服务、不联网、假后端；**真实模式**：用户自己的 key。给面试官演示时可单独建一个限额 key，私下提供，用完停用。另写 `docs/usage.md`。**已实现**（PR B 控制台，见第 10 节） |
| D13 | **按栏目采集**（v2，老板 2026-11-12 的反馈）：爬虫里不用 LLM 筛选，采集和筛选分开；筛选就是选择栏目，某栏目需要的文章超过七成就整栏采集，入库后再筛 | 采集哪些文章、翻译额度 | **已定**：栏目写在站点配置的 `sections` 里，`collect` 决定是否采集；新栏目只提醒不采集；`qbkb sections` 做抽样调研（见 4.9）。Man 的栏目已列出，**用户还没勾选**，现在全部 `collect: false`（待调研） |

---

## 1. 目标与范围

**目标**：自动抓取国外量化机构博客中**有价值的文章**，翻译成中文 Markdown，放进团队知识库，用来补充量化和股票方面的基础知识；新文章发布后能**自动增量入库**。

**MVP 范围**：
- 1 个来源站点，约 **30 篇**。DeepL API Free 的月额度以账户实际显示的为准（2026-11 实测上限 100 万字符；最初按 50 万估算），30 篇约 45 万字符，一个月内能翻完。
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

**v2（2026-11-12，见 4.9）**：默认流程是 discover → fetch → extract（顺带判定栏目）→ translate → export，**不再有 filter**；translate 只处理采集栏目里的文章。上图的 filter 保留为可选的加工命令 `qbkb filter`，不在 `qbkb run`、控制台和每天自动更新里。

**v2 分层翻译（2026-11-13）**：默认流程改为 discover → fetch → extract → **翻译标题（titles）** → **生成摘要（summarize）** → export，**不翻全文**。全文翻译变成按需的加工步骤：`qbkb translate --url-file 清单`（或 `--url`）只翻列出的文章；`qbkb run --full-translate` 恢复旧的"入库即翻全文"。见 4.10。

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
- 去噪：弹窗、分享按钮、订阅框；**保留**版权声明。man.com 的摘句（pull quote，`.digital-quote` / `.digital-mi-quote` 里的 `<blockquote>`）把正文里的一句话放大重复一遍，译文里读起来是重复的，也去掉（2026-11-12 验收；只对以后重新抽取的文章生效，已抽取的文章段落不变）。
- 保留结构：标题层级、列表、表格、脚注、图片和图注、公式；相对链接转成绝对链接。
- 元数据优先从 JSON-LD 和 meta 标签提取。
- 输出**清洗后的 HTML**，按块级元素切成段落，交给 translate。转 Markdown 放到 export 阶段做（见 4.5）。
- 自动质检：正文太短、出现噪音关键词、缺少元数据的，标记出来给人看。

### 4.4 filter：价值筛选

> **v2（2026-11-12）**：本节描述的打分只在单独运行 `qbkb filter` 时发生（加工阶段，可选），不在默认流程里。L1 的"正文太短"等改为 extract 的质量提示（`qc_issues`），文章照常往下走；按系列排除（`exclude_prefixes: /insights/a-moment-in-macro/`）已从 `filters.yaml` 删掉，改为"不选这个栏目"（见 4.9）。

- 收：量化方法论 / 研究、基础概念讲解、行为金融 / 金融史、与量化相关的 AI。
- 不收：宏观周评、ESG / 气候、公司文化 / 软文。
- 顺序：**L3 人工覆盖 → L1 规则 → L2 打分**，处理 `extracted` 的文章，结果是 `filtered`（收）或 `skipped`（不收，原因写在 `score_reason`）。
- L1：`filters.yaml` 里按站点写的规则：排除栏目（`exclude_prefixes`，按路径段匹配）和最少单词数（`min_words`）。**只数正文的单词**，文末附的版权声明和重要信息（extract 的 keep 块，见 `body_segments`）不算，否则视频页会因为长长的免责声明被误收。
- L2：LLM 读标题和正文前 `excerpt_words`（500）个单词，按评分标准打分，输出 JSON（score 0–10、category、evergreen、reason）。
  - 评分标准是独立文件 `config/rubrics/<版本>.md`（文件头写版本号），`filters.yaml` 的 `l2.rubric_version` 选用哪份（现在是 v2）；category 只能从 `include_categories` + `exclude_categories` 里选。改了评分标准内容或发给模型的消息格式，都要新建一份、递增版本号。
  - 防 prompt 注入：标题和摘录分别包在 `<article_title>`、`<article_excerpt>` 里（文本里的尖括号换成全角，伪造不了结束标签），评分标准写明标签内是待评文本、不是指令。
  - 写进 `rubric_version` 的是"评分标准版本@后端"，例如 `v2@deepseek-flash`、`v1@fake`：换模型或换评分标准都会重新打分，假后端的分数也不会被真后端误用。
  - **换模型的迁移策略**：`rubric_version` 带模型名，所以改 `l2.model`（或 `LLM_MODEL`）等于让所有文章重新打分——这是预期行为，不同模型的分数不能混用，没有"沿用旧分数"的开关。只有再跑 filter 的文章才会重打：已经翻译 / 发布的文章不受影响，`--refilter` 才会把已筛过的也重新判断。换之前先估成本：要重打的篇数 ≈ 通过 L1 的文章数（`qbkb status` 里 extracted + filtered + skipped 的上限），每篇 1 个请求、约 800–900 token（2026-11 `deepseek-flash` 实测，含思考），再乘以新模型的单价。2026-11-11 默认模型从已下线的 `deepseek-chat` 改成 `deepseek-flash` 就属于这种情况：用旧默认值打过分的库会全部重打（冒烟库当时已经用 `LLM_MODEL=deepseek-flash` 打分，版本串一致，可以复用）。
  - 判定：score ≥ `threshold` 且 category 在收录列表里 → `filtered`，否则 `skipped`；两种情况都写入 value_score、category、evergreen、score_reason、rubric_version、scored_body_hash。
  - **复用打分**：`body_hash == scored_body_hash` 且 rubric_version 相同就不再调用后端。所以改阈值后 `qbkb filter --refilter` 不花钱。
  - 后端：`DeepSeekScorer`（httpx 调 OpenAI 兼容的 `/chat/completions`，`response_format=json_object`、temperature 0；`LLM_API_KEY` 放 `.env`，base_url / model 在 `filters.yaml`，环境变量 `LLM_BASE_URL` / `LLM_MODEL` 优先）。输出不合格会带着错误说明重问（默认 2 次），还不行文章留在 `extracted`、error 写原因，下次再试。默认模型 `deepseek-flash` 是推理模型，思考过程也算在 `max_tokens` 里：上限给到 2000（原来的 300 会被思考用完，输出被截断只能重问）；`finish_reason=length` 时把 `max_tokens` 翻倍再问（最多 8000，到上限还截断就放弃；temperature 是 0，原样再问大概率还是截断），截断的输出不带回去。解析时接受代码块围栏和前后带说明文字的输出：取第一个带 `score` 字段的**顶层** JSON 对象，外层不完整时不去取里面嵌套的对象。filter 结束时打印请求数、格式重问次数、截断重问次数（两者分开计数，合起来最多重问 2 次）和 token 数；429 / 5xx / 网络错误重试，等待时间取 Retry-After 和指数退避里较大的一个（Retry-After: 0 也不会立刻重打）；402（余额不足）或 `insufficient_quota` 抛 `QuotaExceededError`，流程停下、下次接着跑。测试用确定性的 `FakeScorer`（按关键词给类别和分数）。
- L3：`config/overrides.yaml` 按 URL 指定 include / exclude（可带 category、note），**优先级最高**，压过 L1 和 L2；每次跑 filter 都重新套用到还没翻译的文章上（改了覆盖不用加参数）。覆盖不动打分列，去掉覆盖后旧打分还能复用。
- 评测集：`config/eval/<site>.yaml`，URL → 人工判断。`qbkb eval` 用状态库里已有的打分（不重新打分、不花钱）算 LLM 和人工判断的一致率和混淆矩阵，列出不一致的文章；改评分标准前先在评测集上验证。现在只有框架和一个小样例，样例的判断还没有人工逐篇确认。
- 被筛掉的文章标记为 `skipped`，不删除，以后放宽标准时不用重新爬取。

### 4.5 translate：翻译
- 后端可插拔，接口为 `translate(segments, glossary) -> translations`。MVP 用 DeepL API Free，Azure 作为备选。
- 使用 DeepL 的 **HTML 模式**（`tag_handling=html`），标签原样保留；`glossary.yaml` 同步成 DeepL 的术语表。
- **段落级翻译记忆**：key 是段落 HTML 规范化后的 hash。命中就复用，不命中才翻译。重复的段落（比如版权声明）只翻译一次。
- **脚注上标**：真实冒烟发现 DeepL 在 HTML 模式下会丢掉 `<sup>2</sup>`，或者只丢标签、把数字并进正文；加 `ignore_tags=["sup"]` 后第二种情况仍然出现。所以送出去之前把纯数字的 `<sup>N</sup>` 换成占位链接 `<a href="#qbkb-sup-N">†</a>`（DeepL 对带 href 的链接保留得好；链接文字不放编号，原因见下面"脚注重复 / 错位"），译文回来后还原；DeepL 把旁边的字一起包进链接时，以 href 里的数字为准把字放回外面（`translate/footnotes.py`）。`ignore_tags=["sup"]` 仍然带着，管不是纯数字的上标。占位链接也被丢掉时脚注不会悄悄消失：结构校验除了比标签指纹，还单独比较**脚注编号**（纯数字 `<sup>` 和 Unicode 上标数字的个数和数字，`structure.footnote_marks`），不一致就重翻一次，还不一致标 rejected、文章进 `needs_review`。
- **脚注重复 / 错位**（2026-11-12 第一批 50 篇的人工验收：重复 10 处、错位 3 处）：原因在占位链接本身——PR #8 的写法 `<a href="#qbkb-sup-13">13</a>` 把编号放在链接文字里，DeepL 把它当成正文里的数：另写一个 Unicode 上标（"指标³" + 还原出的 `<sup>3</sup>`）、和前面的年份连读（"2008" + "13" 译成"2008 至 2013 年"）、在小标题里复制一份放到句末。抽取和导出没有问题（段落原文里每个脚注只出现一次）；当时的校验不认识 Unicode 上标，所以没拦住。修法：
  - 占位链接的文字改成不含数字的记号 `†`，编号只放在 href 里；还原时去掉记号（DeepL 挪到链接外的零散记号也去掉，原文本来有 `†` 时除外），兼容旧写法。
  - 结构校验把 Unicode 上标数字也算作脚注编号（`¹<sup>1</sup>` 算两个 1），并检查"紧挨着脚注或在段末、和脚注编号相同的裸数字"（小标题那种形状）。
  - `footnotes.repair_marks` 只修两种确定无误的形状：Unicode 上标和同编号的 `<sup>` 紧挨着（中间至多几个标点），留一个；原文有 `<sup>N</sup>`、译文没有但有一个 Unicode 上标 N，就换成 `<sup>N</sup>`。离得远的（年份那种）不猜，交给校验拦下、进 needs_review。DeepL 后端每次返回后都过一遍，翻译记忆里的旧译文在重新校验时也过一遍（不花额度）。
- **结构校验分级（D7）**：严格——sup（编号的集合和次数）、img（src）、math、code / pre、table 及其子结构，以及其他没单独列出的标签（标题、列表、cite、br）；按地址——a 只比较 href 的集合（一个链接被拆成两段、指向同一地址不算错）；放宽——em、i、strong、b 不比数量和位置，只要求正确闭合。第一批被拦的 13 段里，em 数量变化 7 段、链接拆分 3 段属于放宽 / 按地址的范围。
- **翻译记忆按现在的规则重新判断**：translate 查到翻译记忆时，不管以前是 ok 还是 rejected，都按现在的校验规则重新判断：合格就复用（以前 rejected 的改成 ok），能用 `repair_marks` 修好的就地修好，都不花额度；不合格的进 needs_review（`--retry-rejected` 才重翻）。`qbkb translate --recheck` 把已翻译、已发布、需复查的文章也过一遍，校验规则、术语表、固定译文改了之后用它：**含改过的术语的段落会重新送 DeepL（花额度）**，开始前打印预计字符数（`estimate_chars`；不加 `--retry-rejected` 时不算 rejected 的段落）。没有任何段落变化的文章保持原状态和 `translated_at`（已发布的不会被降回 translated）；有段落新译、修好或换成固定译文的回到 translated，下次 export 重新生成。超长判断（oversized）用脚注换成占位链接以后的长度（`footnotes.sent_length`），和 DeepL 后端的检查一致。
- **固定译文**：文末的"重要信息"和版权声明每篇都一样，人工校对一份放在 `config/fixed_translations.yaml`，按原文段落的 hash 对应（格式见 `config/fixed_translations.example.yaml`）。命中时不送 DeepL、不计费，术语表改了也不重翻；export 优先用它（翻译记忆里可能还是以前的 DeepL 译文）。固定译文和原文结构不一致时 translate 报错。这个文件是 man.com 免责声明的译文，属于原文的衍生内容，**不提交**（`.gitignore`）。最后一段带年份（© Man 2026），每年原文变了要补一条。
- **说话人标签**：只在访谈类文章（抽取的 tags 里有 PODCAST / VIDEO / WEBINAR / INTERVIEW）里启用。单独成段、加粗、"人名:" 形状的段落（`<p><b>Ann Smith:</b></p>`）原样保留英文、不送翻译。名字要像人名：1–3 个首字母大写的词，不含 Pros、Example、Summary、Key 这类常见小标题词（`fixed.HEADING_WORDS`）；同一个名字（规范化后）在一篇里出现两次以上，这个名字的所有标签都保留，同一篇里处理一致。逐段翻译时同一个人会有好几种译名（验收发现一人三译）。
- **按行翻译**：诗歌、脚注列表这种用 `<br>` 分行的段落，整段送出去时 DeepL 会把引号、书名号挪过换行（"《……⏎》7."）。只拆两种：脚注列表（每行都以 "1. " 这样的编号开头，至少两行）；诗歌（至少三行，每行纯文字不超过 80 个字，或者每行都以引号开头）。普通段落里的 `<br>`（"Source: …<br>Note: …"、长句换行）不拆。DeepL 后端拆成一行一个单元送出、译完拼回去，拼回时检查行首编号，DeepL 改成 "3。" 或丢掉时按原文补回。翻译记忆和结构校验看到的仍是整段。
- **术语表的匹配**：DeepL 的术语表不看上下文，单个常用词一定误命中（"carry over some capital" 被译成"套息"、一般意义的 factors 被译成"因子"），所以有歧义的词只收完整短语（carry trade、value factor、factor investing……）。`terms_in`（决定段落的术语版本和首现注释）改成最长匹配：被更长的术语覆盖的出现不算（"implied volatility" 里的 volatility 不单独算，首现注释不再出现"隐含波动率（volatility）"）；缩写（全大写、≤6 个字符：CLO、ROE、EV/S）区分大小写、要求右边界，否则 "CLO" 会匹配 "close"。匹配规则变了，规则版本 `SEGMENT_RULES` 从 g1 升到 g2：所有含术语的段落都会重翻；不含术语的段落版本固定是 `g1-none`，不受影响。`Exhibit N` 逐个列出 1–30（DeepL 术语表不支持模式），带编号的词条导出时不附英文注释。2026-11-12 用户确认的决定：multiples→估值倍数（只收复数）；只收 breakeven inflation；private equity→私募股权（缩写 PE 不收，交给人工）；Exhibit N→图表N；collar、Introduction、Cambridge Associates、Summer Haven 不收（后两个保留英文）。
- **术语表升级的成本**（2026-11-12，v1 → v2 + 规则 g1 → g2，在两个库的副本上模拟，按送出的 HTML 长度计，是上界）：正式库（10 篇已翻译）`glossary_upgrade_cost` 101,551 字符；`translate --recheck --retry-rejected` 全部重跑约 121,605 字符（不加 `--retry-rejected` 约 119,316，剩 5 段进 needs_review）。冒烟库（3 篇）20,739 字符。其中规则版本 g1 → g2 本身约占 4.6 万（正式库）/ 4 千（冒烟库）：不升规则版本时分别约 76,014 / 16,589。免责声明 12 段走固定译文，不再计入。
- **术语表只保留一份**：DeepL Free 账户同时只能保留 1 份术语表（2026-11-12 实测撞到）。换术语表版本时，`sync_glossary` 先删掉我们自己的旧版本术语表（名字精确匹配 `qbkb-v<版本>-<十六进制指纹>`、且不是当前名字的），再建新的；别的名字的术语表不动；删除时遇到 404（已经被删了）跳过，不中止翻译。DeepL 对"术语表太多"也返回 456，SDK 报成"额度用完"：这种情况抛 `GlossaryLimitError`（退出码 1），提示"术语表数量到上限，不是额度用完"和处理建议，不再说"下个月再跑"。
- 额度用完就停下，记录进度，下个月接着翻。`qbkb translate` 在翻译前后各调一次 DeepL 的 usage 接口（不消耗额度），显示真实的"已用 / 上限"。查额度只问一次、不走退避重试，最多等 5 秒，取不到就跳过、不影响翻译；这时上限退回保守的常量 `FREE_MONTHLY_CHARS`（50 万）。真实上限各账户不一样（我们的账户实测 100 万），以 usage 接口为准。
- 校验：翻译前后按 D7 分级比较结构（见上面"结构校验分级"），不一致就重新翻译一次。
- 术语第一次出现时附上英文（在导出阶段处理）。
- **标题**作为额外的一个翻译单元（`<p>标题</p>`），在正文之后翻译，同样走翻译记忆，结果写进 `title_zh`；标题结构校验没过只是 `title_zh` 留空（导出时用英文标题），不影响文章状态。额度估算（`estimate_chars`）把标题算在内。
- 这个改动之前翻的文章没有 `title_zh`：`qbkb translate --titles` 只补翻标题（命中翻译记忆就不花额度），已发布的文章补上后退回 `translated`，下次 export 用中文标题重新生成。

### 4.6 export：输出
- 输入是 `translated` 的文章：按段落从翻译记忆取中文 HTML（backend 用文章的 `translator`，术语版本按段落计算，和 translate 用同一个函数 `translate.lookup_translation`），再用 markdownify 转成 Markdown。取不到译文（比如术语表改过）就把文章退回 `filtered`，下次 translate 只重翻缺的段落。
- `output/<site>/<发布日期>_<slug>.md`（没有日期用 `undated`），完成后标 `published`，`md_path` 写绝对路径。发布日期变了会删掉旧文件。
- frontmatter 字段：title、title_zh、firm（站点配置的 `firm`）、author、published、source_url、category、value_score、rubric_version、translator、translated_at。
- 标题下面一行来源和版权说明；正文之后用分隔线隔开，附上原文的版权声明和重要信息（keep 块的译文）。
- **输出里不留原始 HTML**：markdownify 会把原文里转义过的 `&lt;script&gt;` 还原成 `<script>`，所以转换后把正文里剩下的 `<` 一律转成 `&lt;`，只放行 `<sup>` / `<sub>` 和自动链接，代码里不动；标题、来源行、index 里的标题同样转义；frontmatter 里含 `< > &` 的值加双引号。链接和图片地址里的括号、空格转成 `%28` `%29` `%20`，Markdown 链接不会断。
- **链接协议白名单**（纵深防御，extract 已经挡过一次）：链接只留 http、https、mailto、`#` 锚点和相对路径，图片只留 http、https 和相对路径；判断前先还原 HTML 实体、去掉控制字符和空白（`JaVaScRiPt:`、`java\tscript:`（中间夹制表符） 都算）。不合格的链接去掉地址、保留文字，图片只留 alt；对照版同样处理，frontmatter 和来源行里的 `source_url` 只放行 http / https。
- 排版清理：中文段落里中文标点（。！？；：，、）后面紧跟的空格去掉（DeepL 的译文常有"影响。 如果"），代码（围栏、缩进、行内）、链接地址、英文段落、全角空格、行尾硬换行不动；整行只有几张图片时每张拆成单独一段（前面有行内代码或文字的行不拆）。
- 术语第一次出现时附上英文："趋势跟踪（trend following）"。短译文落在更长的术语译文里面时不标（"隐含波动率"里的"波动率"不会变成"隐含波动率（volatility）"），所以长短语要整条收进术语表（v3 加了 realized / realised volatility；测试检查"译文是包含关系的词条，英文也必须是包含关系"）。只标原文这段确实含该术语的段落；译文和原文相同的词条（alpha、CTA）、代码、公式、链接文字里不标。
- 主文件用**纯中文**；另外从翻译记忆生成 `output/bilingual/<site>/` 对照版（每段英文后面跟中文，只有图片的段落只出现一次），不额外消耗额度。
- 图片下载到 `output/<site>/images/`（文件名是地址的 hash），Markdown 里改成相对路径（对照版是 `../../<site>/images/`）。礼貌规则：先过合规闸门；只下载站点本身的 host（以及站点配置 `export.image_hosts` 列出的）；串行，两次请求间隔 ≥ 1 秒（真实站点调不低于 1 秒，只有本机测试站点可以）；带项目 UA；单张不超过 10 MB；已下载过的不再请求。下载失败保留原始链接，并追加记进 `output/<site>/images/failed.tsv`。外站图片（例如 Flourish 的图表缩略图）按规则本来就不下载：保留原链接，单独统计为"外站图片（保留原链接）"，不算图片失败、不写 failed.tsv。`--no-images` 关掉下载。
- 每次运行后按状态库里全部 `published` 文章重新生成 `output/index.md`：按类别分组（先按 `filters.yaml` 的顺序），列出中文标题、日期、分数和对照版链接。
- 幂等：内容没变的文件不重写（修改时间也不变）；`--rebuild` 重新生成已发布的文章。发布日期变了要删旧文件时，只删本次 `--out` 目录里的。

### 4.7 增量与定时
- 每天跑一次：拉 sitemap → 只处理新 URL 和 `lastmod` 变化的 URL → 整篇 hash 没变就跳过。
- 每月全量复查一次（使用条件请求），用来发现 lastmod 没更新的修改。
- 原文更新后直接覆盖译文（方案 A）。
- 定时方式：Windows 任务计划，每天一次（D9，见 10.4）。

### 4.8 CLI 串联
- 子命令：`discover`、`fetch`、`extract`、`filter`、`translate`、`export`，`run`（按这个顺序跑完全部阶段，`--skip` 跳过某些阶段），`status`（每个状态的文章数，列出 needs_review 和 failed 的文章及原因），`eval`（见 4.4），`console`（本地网页控制台，见第 10 节），以及 `init-db`、`sites`。`translate --url` 只翻这几篇（控制台的「重翻」用）；`--translator demo` 是演示模式的翻译后端。
- **v2**：`run` 跑 discover → fetch → extract → translate → export（`--skip` 只能选这五个），没有打分，也没有 `--scorer` / `--refilter`；`filter` 仍可单独运行。新增 `sections`（栏目调研，见 4.9）；`status` 另外列出每个栏目的篇数、是否采集和新栏目提醒；`fetch --url-file` 只抓文件里列出的网址（`sections --fetch` 用）。发现新栏目时 extract 的退出码是 5（需要人确认）。
- 通用参数：`--site`（默认全部站点）、`--db`、`--limit`（每个阶段最多处理多少篇）、`--config-dir`、`-v`。后端开关：`--scorer deepseek|fake`、`--translator deepl|fake`；其他：`--no-images`、`--refilter`、`--retry-rejected`、`--recheck`（见 4.5）、`--rebuild`、`--fetch-delay`（只对本机测试服务调小）。
- 退出码（`--help` 里也有）：0 正常；1 出错；2 参数错误；**3 额度用完**（打分或翻译），进度已保存，下次运行同一条命令从中断处接着跑；4 后端暂时不可用（限流、网络）；**5 跑完了但有文章需要人工处理**（抓取 / 抽取 / 翻译 / 导出失败、翻译需复查、打分输出不合格、缺译文退回重翻、sitemap 部分失败；extract 的质检提示和图片下载失败不算）。同时出现时按 4、3、5 的顺序取。`run` 里某个阶段因为额度停下，后面的阶段照样处理已就绪的文章（打分额度用完，已经 filtered 的文章照样翻译、导出）。
- 抓取间隔：`--fetch-delay` 只有本机测试站点（localhost、127.0.0.1、`*.localhost`、`*.test`）可以调到默认的 2 秒以下；真实站点只能调大，调小会提示并用默认值。
- Twisted reactor 一个进程只能启动一次：单独的 `qbkb fetch` 在本进程里跑 Scrapy；`run` 把 fetch 放进子进程（`python -m qbkb.cli fetch …`），所以一个进程里可以跑多个站点、反复调用。
- 缺 key 时只有真的要调用后端才报错（全部复用旧打分、没有待翻译文章时不需要 key）。
- Windows 控制台的 stdout / stderr 统一用 UTF-8。


### 4.9 栏目（v2，PR C）

**为什么**：老板要求爬虫里不用 LLM 筛选（自动化运行时出问题难查），采集和筛选分成两步；筛选就是**人工选择栏目**，
某栏目需要的文章超过七成就整栏采集，入库后再筛。采集阶段的每一步都是确定性规则，同样的输入得到同样的结果。

**配置**：站点配置的 `sections` 节（例子见 `config/sites/man.yaml`）：
- `list`：每个栏目一项——`id`（状态库里记的就是它，不要改）、`name`、识别规则 `url_prefix` 或 `key`（二选一）、
  `collect: true/false`（必须写明）、`status`（如"待调研 / 已调研"）、`survey`（调研记录：抽样数、要的篇数、
  占比、日期、判断人）、`estimate`（没抓页面时按网址最后一段估计篇数的正则，**只用于调研**，不用于采集判定）；
- `page_key`：从页面上读栏目标识——`selector` 第一个匹配的文字，`pattern` 的命名分组按 `parts` 的顺序用 " | " 拼起来
  （统一大写、折叠空白）；
- `cross_check`（可选）：原始 HTML 里再按一个正则取值，和栏目标识的某一部分比较，不一致记一条质量提示，
  **以 page_key 为准**；`equivalent` 列出算作一致的组合。
- 没有 `sections` 节的站点不分栏目，全部采集（老行为）。

**判定顺序**：
1. **网址前缀**（discover 阶段就能定）：前缀栏目不采集时直接标 `unselected`，**不抓取**；
2. **页面元数据**（抓取并抽取后）：extract 写 `section_key` / `section`，栏目不在采集列表里（包括配置里没有的
   新栏目、页面上读不到栏目信息的）→ `unselected`，不翻译，但保留记录。
3. 每次 discover / extract / translate 都按当前配置重新对一遍（`sections.apply_sections`，不联网、幂等）：
   取消勾选 → extracted 退回 unselected；勾选 → 抽取过的回到 extracted（不重抓），没抓过的回到 discovered。
   已翻译 / 已发布 / 需复查 / 失败的文章**从不降级**，只补记栏目。

**质量提示只标记、不跳过（C2）**：正文太短、噪音关键词、缺标题 / 日期、用了 trafilatura 兜底、
栏目核对不一致、读不到栏目信息……都写进 `qc_issues`，文章照常往下走。

**新栏目提醒（C5）**：判定出的栏目标识在配置里没有 → 只记录（`section` 为 NULL）、不采集；extract 的输出和
`qbkb status` 都列出"标识、篇数、一个例子"，extract 退出码 5。确认后把它加进 `sections.list`。

**栏目调研 `qbkb sections --site <站点>`**（不调用任何 LLM）：
- 每个栏目的篇数：**已判定**（抓到页面按规则定了的，或者网址前缀就能定的）是实际篇数；没抓的按 `estimate`
  估计，标明"约"；都对不上的归入"未分类"；
- 抽样：每个栏目的成员按网址排序后用 `random.Random("<种子>:<栏目 id>")` 洗牌取前 N 篇（默认 20，种子默认 2026），
  **同一种子可复现**；把 N 调大时原来抽到的仍在里面（补抽不浪费已标的）；不超过 30 篇的栏目列全部；
- 输出 `output/sections/<站点>.csv`（UTF-8 带 BOM，Excel 直接打开；`output/` 不进仓库）：栏目、是否采集、网址、
  英文标题、日期、**要不要**（人工填"要 / 不要"）、备注；抽样表已存在时只读标记、不重抽（`--resample` 重抽，
  已标的按网址保留）；
- 再运行一次：按栏目算占比，结论分两档——**占比超过 70%：超过七成**（可以整栏采集）；
  **不到七成（≤ 70%）：不到七成**（请老板决定，补抽或不采）。汇总写到 `<站点>-summary.csv`，并打印可以抄进配置的 `survey` 行；
- `--fetch N`：先按固定种子随机抓 N 篇还没判定栏目的页面（走正常的 fetch 阶段：合规闸门、robots、限速、重试），
  抽取判定后再统计。中文标题暂不做。

**Man 的栏目**（依据：只读调研，58 页样本 + sitemap 网址计数，2026-11-12）：网址是平铺的 `/insights/<slug>`，
栏目 = tagline（`#article-tagline-text`，"类型 | 时长 | 系列名"）的**类型 + 系列名**；A Moment in Macro 按网址前缀
`/insights/a-moment-in-macro` 单独成栏（模板不同）。已列出：A Moment in Macro、独立文章（ARTICLE 无系列）、
Views From the Floor、The Road Ahead、The Early View、A Sustainable Future（播客）、其他播客、视频。dataLayer 的
contentType 和 tagline 不一致时记质量提示（播客页的 contentType 多数是 Article，列为一致）。

**`collect` 的默认值**：Man 的栏目现在**全部 `false`、标"待调研"**。理由：全部 `true` 时每天的自动更新会把约 1000 篇
全部送去翻译（估计 1500 万字符以上，远超免费额度），而且送出去就收不回；全部 `false` 时最坏只是"抓了、没翻译"，
勾选后下一次运行直接翻译已抽取的文章，不用重抓。translate 发现一个采集栏目都没有时会打印提示。

**v1 老数据的迁移策略**（状态库 v2 → v3 自动加列；状态由 extract 在下次运行时处理，只读本地原始 HTML，不联网、不花额度）：
- extracted / filtered / skipped（v1 的打分结果）：**重新抽取一次**，按 v2 的栏目规则判定，采集栏目里的进入
  extracted 等翻译，其余标 unselected。打分列（value_score、category 等）保留但不再起作用；
  也就是说 v1 被 L1 / L2 筛掉的文章，只要栏目被选中，就会被翻译；
- translated / needs_review / published：**不改状态**，只读原始 HTML 补记栏目（已花的翻译额度不浪费，
  也不会因为栏目没选就被撤下）；
- 原始 HTML 不在了的老数据保持原样（分栏目的站点里，它们没有栏目信息，不会被翻译）；
- 还没重新抽取之前，老数据不会被翻译（translate 只翻栏目在采集列表里的）。

### 4.10 分层翻译（v2，2026-11-13）

入库和全文翻译分开，省下全文翻译额度：

- **默认流程**：`qbkb run`（含控制台「运行」和每天自动更新）跑 discover → fetch → extract → **titles**（只翻标题）→ **summarize**（DeepSeek 摘要，不占翻译额度）→ export。文章的 `translator` 保持为空，状态是 extracted（有中文标题）→ 导出后 published；`title_zh` + `summary_zh` 有值但不等于"全文翻了"。
- **恢复旧行为**：`qbkb run --full-translate` 把 titles 换成 translate（翻全文）。
- **按需翻全文**：`qbkb translate --url-file 清单`（或 `--url`）只翻列出的文章全文，开始前打印预计字符数；`--url` / `--url-file` 直接按清单选文章，不管它当前是 extracted 还是只翻过标题的 published。
- **区分**：有没有全文译文看 `translator` 字段（空 = 只有标题 + 摘要）；状态 `translated` 只表示"全文翻过"。translate（翻全文）还会把"已导出但 translator 为空"的文章重新选进来，所以 `--full-translate` 续跑 / 先入库再补翻都能接上。
- **导出**：export / table 对没有全文译文的文章正常输出（英文原文 + 中文标题 + 中文摘要，中文正文留空并注明"尚未翻译"），不报错、不退回重翻。

---

## 5. 数据模型（SQLite：`state.db`）

表结构以 [`src/qbkb/store/schema.sql`](../src/qbkb/store/schema.sql) 为准，这里只写要点：

- `articles`：每篇文章一行，`url` 为主键。记录状态、抓取信息（lastmod、etag、last_modified、http_status、raw_path 绝对路径）、正文 hash、正文段落数（body_segments：前多少段是正文，其后是版权声明等附录）、元数据（含 tags）、打分（value_score、evergreen、score_reason、rubric_version、scored_body_hash）、翻译（translator、translated_at）、抽取质检问题（qc_issues，JSON 数组，和失败原因 error 分开）、输出路径、失败原因和重试次数。
- 状态（v2）：discovered → fetched → extracted → translated → published；**unselected**（未选栏目：保留记录、不翻译，栏目改成采集后自动回到流程）。filtered / skipped 只由可选的 `qbkb filter` 产生，不再是必经状态。另有 failed（失败，error 写原因）、pdf_pending、**needs_review**（翻译结果有问题，等人工复查）。v1 的说法：discovered → fetched → extracted → filtered / skipped → translated → published；另有 failed（失败，error 写原因）、pdf_pending、**needs_review**（翻译结果有问题，等人工复查）。
- `segments`：文章由哪些段落组成，**同时存段落 HTML**，translate 直接从这里取要翻的正文。
- `translation_memory`：段落级翻译记忆，主键 `(src_hash, backend)`，backend 带目标语言（如 `deepl:ZH-HANS`）；**术语表版本不同视为未命中**（版本按段落里实际出现的术语计算）；`quality` 为 `ok` / `rejected`，`tm_get` 只返回 ok，结构校验没过的译文以 rejected 保存、不会被复用。
- 老库升级：`store.connect()` 按 `PRAGMA user_version` 自动补新列（只加不删；v2 加了 body_segments），并把 PR #3 时期 `#rejected` 后缀的隔离译文转成 `quality = rejected`。
- 栏目（v3，PR C）：`section_key`（页面上读到的栏目标识，`url:` 开头表示按网址前缀判定；`""` 表示读不到；NULL 表示还没按 v2 规则判定过）、`section`（站点配置里的栏目 id，NULL 表示配置里没有这个栏目）。老库自动加列；老数据的状态怎么迁移见 4.9。

增量规则（`upsert_discovered`）：lastmod 变化时文章退回 `discovered`，同时清掉 etag、error、retries；打分保留；filter 比较当前 body_hash 和 scored_body_hash，相同就复用打分；translate 靠段落翻译记忆复用。

---

## 6. 仓库结构

```
quant-blog-kb/
├── pyproject.toml            # uv 管理依赖
├── CLAUDE.md                 # 简短：命令、约定、完成标准
├── config/
│   ├── sites/<site>.yaml     # 发现方式、选择器、合规结论
│   ├── filters.yaml          # L1 规则、L2 阈值和后端
│   ├── rubrics/v1.md         # L2 评分标准（带版本号）
│   ├── overrides.yaml        # L3 人工覆盖
│   ├── eval/<site>.yaml      # L2 评测集
│   └── glossary.yaml
├── src/qbkb/
│   ├── models.py             # Article / Segment 等数据类和模块间接口
│   ├── store/  discover/  fetch/  extract/  filter/  translate/  export/
│   ├── console/              # 本地网页控制台（第 10 节）：服务、演示模式、定时任务、static/
│   └── cli.py
├── tests/
│   ├── golden/               # 黄金样本：输入 HTML + 期望输出
│   ├── fixtures/             # sitemap 等本地文件
│   └── e2e/                  # 端到端：构造的站点 + 本机 HTTP 服务
├── scripts/verify.sh
├── docs/  design.md  ai-retro.md
├── .env                      # DEEPL_API_KEY、LLM_API_KEY（不提交）
└── .gitignore                # .env raw/ output/ state.db .scrapy/ tests/local_samples/
```

---

## 7. 质量与测试

- **完成标准**：`scripts/verify.sh` 退出码为 0。它会依次运行 `ruff check`、`ruff format --check`、`pyright`、`pytest -m "not network"`。
- **测试完全离线**：不访问网站，不消耗 DeepL 额度。翻译测试用假后端；真实 DeepL 只做一次手动冒烟测试。
- **端到端测试**（`tests/e2e/`）：构造的 sitemap 和页面，FakeScorer + FakeTranslator，从 discover 一路跑到 export，检查 md、frontmatter、index、对照版；覆盖额度用完后断点续跑（两次加起来的计费字符等于一次跑完）和幂等（跑两次文件不变）。其中一个用例起本机 HTTP 服务、在子进程里真跑 Scrapy。
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
- **iframe / 脚本嵌入的交互图表会被丢掉**：抽取阶段把 `<iframe>`、`<script>` 整个去掉（不抓第三方图表服务），译文里这个位置没有任何提示；有 `<noscript>` 静态缩略图的会保留缩略图。要看图表请打开每篇开头的原文链接。例：通胀那篇（when-inflation-hits）的"图表 1"是 Infogram 的 `<script>` 嵌入，页面里没有静态图片，只剩图注（"Problems loading this infographic?" 那段按 `drop_text` 去掉了）。
- 脚注上标靠占位链接保住，只覆盖纯数字的 `<sup>N</sup>`；其他上标仍可能被 DeepL 改动，由结构校验拦下后人工复查。新的占位写法（`†`）还没有用真实 DeepL 批量验证过，下一次重翻时看 needs_review 的比例。
- 意思被改的错译（PE 被当成市盈率、realised yield 译成"实际收益率"等，验收约 15 处）只能人工修；打分对访谈类降权等 D6 的文章标准定了再做。
- 机翻质量受限：术语一致性靠术语表，长难句可能生硬；以后可以换成 LLM 后端。
- 需要 JS 渲染或有反爬挑战的站点暂不支持。
- 合规检查是人工完成的，代码只负责强制执行检查结论。
- 控制台的定时更新只支持 Windows；本机其他程序能读控制台的只读接口（见 10.5）。

---

## 10. 本地网页控制台（D9、D12）

用户不是程序员，命令行用起来不方便，还要拿去面试演示。`qbkb console`（或双击 `scripts/start-console.bat`）启动一个**只监听 127.0.0.1** 的网页控制台，并用默认浏览器打开。代码在 `src/qbkb/console/`，使用说明见 [docs/usage.md](usage.md)。

### 10.1 选型
- **服务端只用标准库** `http.server.ThreadingHTTPServer`：接口不到 20 个、只有一个本机用户，不值得引入 Web 框架。端口独占（关掉 `SO_REUSEADDR`、Windows 上 `SO_EXCLUSIVEADDRUSE`：标准库默认的 reuse 在 Windows 上会让第二个进程悄悄绑到同一端口）。
- **前端是纯 HTML + 原生 JS + CSS**（`console/static/`），没有构建步骤，不从 CDN 加载任何东西，离线可用。
- **Markdown 渲染在服务端**，新增依赖 `markdown-it-py`（纯 Python，只带 `mdurl`）：读的是 export 的产物本身（`output/` 下的 md），控制台显示的和用户拿去用的是同一份东西（术语注释、本地图片、版权说明都在里面）。目录和文章元数据（分数、类别、翻译时间）从状态库读。

### 10.2 四个页面
- **浏览文章**：左边按类别分组（`filters.yaml` 的顺序）列出已发布的文章（中文标题、日期、分数），右边是渲染好的文章，可切换"中文 / 中英对照"；顶部显示原文链接、分数、类别、翻译时间。图片走 `/out/` 接口，只允许 output 目录里的图片文件。
  - v2：按**栏目**分组（站点配置的栏目名；没有栏目的老数据按 v1 的类别），不再显示分数；顶部显示栏目。真实模式只需要 DeepL 的 key（DeepSeek 的 key 留给以后的加工步骤，可不填）。
- **运行**：选"这次最多处理 N 篇"（`--limit`）→ 后台子进程跑 `qbkb run`（Twisted reactor 一个进程只能启动一次，控制台从不在自己进程里跑流程）。页面每秒轮询一次增量日志，按 `[stage]` 行显示当前阶段和进度条；结束后按退出码说明（0 / 3 / 4 / 5，以及"已停止"），并显示 DeepL 剩余额度（从 translate 打印的 usage 行解析，存进 `.qbkb/quota.json`）。可以停止（Windows 上 `taskkill /T` 连同 Scrapy 子进程一起结束）。
- **待复查**：列出 needs_review 和 failed 的文章及原因；needs_review 的有「重翻」按钮：`translate --retry-rejected --url <这篇>`，然后 `export` 把它发布出来。
- **设置**：DeepL / DeepSeek 的 key（写入本机 `.env`，页面只显示是否已配置和后 4 位；「测试连接」查 DeepL `/v2/usage`、DeepSeek `/models`，都不计费）；模式（自动 / 演示 / 真实）；每天自动更新（开关、时间、篇数，见 10.4）；清空演示数据。

### 10.3 演示模式
- 没有配置两个 key 时默认是演示模式（`auto`），页面顶部明确提示；也可以在设置里手动切换。
- 用仓库自带的**构造示例站点**（`console/demo.py`）：6 篇照 man.com 模板结构写的页面（抽取规则直接复用 `man.yaml`），正文都是为演示自己写的英文，**没有任何真实原文**；在本机 127.0.0.1 起 HTTP 服务提供，配一份 `allowed` 的站点配置 `demo`。打分用 `FakeScorer`；翻译用 `DemoTranslator`：每段示例原文配一份自己写的中文，按段落原文查表，所以演示时看到的是像样的译文、术语注释和本地图片。
- 特意安排的情况：一篇 ESG（L2 筛掉）、一篇排除栏目（L1 筛掉）、一篇 404（failed）、一篇前两次翻译"丢脚注"（结构校验拦下 → needs_review，点「重翻」第三次就好），四个页面都有东西可看。
  - v2：演示配置只采集"独立文章"栏目；原来"L2 筛掉"的那篇改成视频栏目、A Moment in Macro 那篇按网址前缀判定，两篇都是"未选栏目"。
- 数据完全隔离：状态库、`raw/`、`output/`、日志、生成的配置都在 `.qbkb/demo/` 下（已被忽略）；演示模式的子进程去掉 API key 环境变量。
- 文章 URL 里带端口，所以示例站点的端口记在 `.qbkb/demo/site.json`，下次尽量用同一个；端口被别的程序占了才清空演示数据、换端口。被另一个进程里的示例站点占着（控制台开着时定时任务也来跑）就直接用它——这一条是实测发现的：最初的写法让定时任务清空了控制台正在用的演示数据。

### 10.4 每天自动更新（D9）
- 设置页打开开关时，用任务的 XML 定义创建 Windows 任务计划（`schtasks /Create /XML`）：命令行参数设不了"错过后尽快补跑"（`StartWhenAvailable`）和工作目录。每天一次，只在用户登录时运行（`InteractiveToken`，不保存密码），不管是否用电池，同一时间只跑一个实例，执行时限 2 小时。
- 任务执行 `.venv\Scripts\pythonw.exe -m qbkb.console.auto --root <项目目录>`，工作目录是项目目录：没有黑窗口，也不依赖 uv 在不在 PATH 上。找不到 `.venv` 里的 pythonw.exe 时不建任务，设置页提示先运行 `uv sync`（不回退到 python.exe：每天会弹黑窗口，别的解释器也可能没装 qbkb）。自动更新和控制台用同一套子进程环境规则（`jobs.job_env`）：key 以 `.env` 为准；访问本机的请求不走代理（`NO_PROXY` / `no_proxy` 追加 127.0.0.1、localhost，配了系统代理的电脑上演示模式也能抓本机示例站点）。`auto` 读设置里的篇数、用当前模式跑一次 `qbkb run`，日志追加到数据目录的 `logs/auto-update.log`，结果（时间、退出码、新增篇数）写进 `.qbkb/last_auto.json`，设置页显示"上次自动更新"。
- 任务名固定为 `qbkb-daily-update`，描述和作者里带 `qbkb-console` 标记；关闭开关或重建前先查询，不是我们建的同名任务不覆盖、不删除。
- 控制台和定时任务共用一把锁（`.qbkb/run.lock`，O_EXCL 创建）。锁里记下持有者进程的 pid 和**进程创建时间**：检查时 pid 对应的进程还活着、创建时间也对得上（Windows 会复用 pid）才算有效，进程被硬杀后马上就能重新运行，不用等；运行中定期刷新锁文件的修改时间。拿到锁之后启动失败（建不了日志目录、起不了线程）会立刻释放。「停止」只杀还在运行、而且是我们启动的子进程：每条命令结束立刻清掉记录的进程，杀之前再用 `poll()` 确认没退出（重翻的两条命令之间点停止，什么都不杀，第二条也不会再启动）：控制台在跑任务时定时任务跳过这一次并记下原因；反过来控制台会提示"已经有任务在运行"。

### 10.5 安全
- 只绑定 127.0.0.1；每个请求检查 `Host` 头必须是 `127.0.0.1:<端口>` 或 `localhost:<端口>`（防 DNS 重绑定）。
- 会改状态的接口（运行、停止、重翻、保存 key、测试连接、切换模式、开关定时任务、清空演示数据、退出）都是 POST，必须带每次启动随机生成的 token（`X-QBKB-Token` 头；token 放在 index.html 的 meta 里，别的网站读不到），`Content-Type` 必须是 `application/json`（表单 / text/plain 这类不触发预检的跨站请求一律拒绝），有 `Origin` 头时必须是本服务自己。
- 响应带 CSP（`script-src 'self'`，不允许内联脚本）、`nosniff`、`frame-ancestors 'none'`。
- 文章渲染：markdown-it 关掉原始 HTML（export 已经转义过，这里再挡一层），只把转义过的纯文字 `<sup>` / `<sub>` 还原（行内代码和代码块里的不动）；外部链接（含 `//host`）在新标签页打开，页内锚点保留，其他站内相对链接去掉地址，点了不会离开控制台；链接协议用 markdown-it 自带的检查（挡 javascript: / vbscript: / file: / data:）；图片相对路径解析后必须在 output 目录里。页面上除了文章 HTML，其他文字一律用 `textContent` 写入。
- key：只写进 `.env`（只接受字母、数字和 `: . _ -`，防止注入别的变量），接口和页面只返回"是否已配置 + 后 4 位"；任务日志写入前把 key 换成 `***`；测试连接的错误信息只给异常类型。
- 已知的边界：同一台电脑上的其他程序能访问 127.0.0.1，但拿不到 token，只能读（文章、状态、日志）；这对单用户的个人电脑可以接受。

### 10.6 没做的
- 只支持 Windows 的任务计划（D9 的结论）；macOS / Linux 上设置页提示不可用，其他功能正常。
- 运行时只能限制"每个阶段最多 N 篇"（沿用 `--limit` 的语义），不是"最终收录 N 篇"。
- 页面不显示 `qbkb eval`、`--recheck`、`--titles` 这类维护用的命令，仍然用命令行。
