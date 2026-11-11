# qbkb：量化机构博客中文知识库 设计文档

- 版本：v0.1 初稿（2026-11-11）
- 状态：已实现（Phase 0–2 和冒烟修复已合并）；第 0 节列出仍待决定的事项。
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
| D6 | **"有价值文章"的具体标准**：现行标准（`config/rubrics/v2.md`）没有偏向股票，债券配置、私募股权对冲、通胀资产配置类文章也被归进"量化方法论"收录；只看前 500 个单词；evergreen 不参与决策 | 决定收哪些文章、花多少翻译额度 | **待定**（2026-11-12）：用户向公司确认想要的文章标准，之后写评分标准 v3，并用人工标注的评测集（`config/eval/man.yaml`）校准 |
| D7 | 结构校验对纯格式标签是否放宽：第一批 50 篇里 6 篇共 13 段被拦，其中 7 段只是 `<em>` 数量变了、3 段是链接被拆分 | needs_review 的比例（本批 6/10） | 待定：建议斜体/粗体放宽、链接按 href 集合比较、sup/img/math/code/table 保持严格 |
| D8 | 翻译额度与全量规模：实测平均约 2.3 万字符/篇，man.com 全站约 200 篇收录 ≈ 460 万字符，约 5 个月的 DeepL Free 额度 | 全量需要多久、要不要付费或换翻译后端 | 待定：MVP 先做 30–50 篇 |
| D9 | 自动定时更新的方式（Windows 任务计划 / 云服务器 / GitHub Actions） | 新文章能否自动入库 | 待定 |
| D10 | 其他机构的站点（AQR、Two Sigma 等）：逐个做合规检查、写抽取规则 | 知识库覆盖面 | 待定：MVP 只做 man.com |
| D11 | Man 许可的范围：是否覆盖在公司内部知识库共享译文（公开仓库只放代码，这一点已定） | 能否把译文放进团队知识库 | 待用户确认 |
| D12 | 演示方式：是否需要本地预览网页（`qbkb serve`）、`docs/usage.md` 使用说明 | 面试演示效果 | 待定 |

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
- **脚注上标**：真实冒烟发现 DeepL 在 HTML 模式下会丢掉 `<sup>2</sup>`，或者只丢标签、把数字并进正文；加 `ignore_tags=["sup"]` 后第二种情况仍然出现。所以送出去之前把纯数字的 `<sup>N</sup>` 换成占位链接 `<a href="#qbkb-sup-N">N</a>`（DeepL 对带 href 的链接保留得好），译文回来后还原；DeepL 把旁边的字一起包进链接时，以 href 里的数字为准把字放回外面（`translate/footnotes.py`）。`ignore_tags=["sup"]` 仍然带着，管不是纯数字的上标。占位链接也被丢掉时脚注不会悄悄消失：结构校验除了比标签指纹，还单独比较**脚注编号**（纯数字 `<sup>` 的个数和数字，`structure.footnote_marks`），不一致就重翻一次，还不一致标 rejected、文章进 `needs_review`。
- 额度用完就停下，记录进度，下个月接着翻。`qbkb translate` 在翻译前后各调一次 DeepL 的 usage 接口（不消耗额度），显示真实的"已用 / 上限"。查额度只问一次、不走退避重试，最多等 5 秒，取不到就跳过、不影响翻译；这时上限退回保守的常量 `FREE_MONTHLY_CHARS`（50 万）。真实上限各账户不一样（我们的账户实测 100 万），以 usage 接口为准。
- 校验：翻译前后标签、链接、图片的数量必须一致，不一致就重新翻译。
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
- 术语第一次出现时附上英文："趋势跟踪（trend following）"。只标原文这段确实含该术语的段落；译文和原文相同的词条（alpha、CTA）、代码、公式、链接文字里不标。
- 主文件用**纯中文**；另外从翻译记忆生成 `output/bilingual/<site>/` 对照版（每段英文后面跟中文，只有图片的段落只出现一次），不额外消耗额度。
- 图片下载到 `output/<site>/images/`（文件名是地址的 hash），Markdown 里改成相对路径（对照版是 `../../<site>/images/`）。礼貌规则：先过合规闸门；只下载站点本身的 host（以及站点配置 `export.image_hosts` 列出的）；串行，两次请求间隔 ≥ 1 秒（真实站点调不低于 1 秒，只有本机测试站点可以）；带项目 UA；单张不超过 10 MB；已下载过的不再请求。下载失败保留原始链接，并追加记进 `output/<site>/images/failed.tsv`。`--no-images` 关掉下载。
- 每次运行后按状态库里全部 `published` 文章重新生成 `output/index.md`：按类别分组（先按 `filters.yaml` 的顺序），列出中文标题、日期、分数和对照版链接。
- 幂等：内容没变的文件不重写（修改时间也不变）；`--rebuild` 重新生成已发布的文章。发布日期变了要删旧文件时，只删本次 `--out` 目录里的。

### 4.7 增量与定时
- 每天跑一次：拉 sitemap → 只处理新 URL 和 `lastmod` 变化的 URL → 整篇 hash 没变就跳过。
- 每月全量复查一次（使用条件请求），用来发现 lastmod 没更新的修改。
- 原文更新后直接覆盖译文（方案 A）。
- 定时方式（本机任务计划、云服务器、GitHub Actions）等 MVP 完成后再定。

### 4.8 CLI 串联
- 子命令：`discover`、`fetch`、`extract`、`filter`、`translate`、`export`，`run`（按这个顺序跑完全部阶段，`--skip` 跳过某些阶段），`status`（每个状态的文章数，列出 needs_review 和 failed 的文章及原因），`eval`（见 4.4），以及 `init-db`、`sites`。
- 通用参数：`--site`（默认全部站点）、`--db`、`--limit`（每个阶段最多处理多少篇）、`--config-dir`、`-v`。后端开关：`--scorer deepseek|fake`、`--translator deepl|fake`；其他：`--no-images`、`--refilter`、`--retry-rejected`、`--rebuild`、`--fetch-delay`（只对本机测试服务调小）。
- 退出码（`--help` 里也有）：0 正常；1 出错；2 参数错误；**3 额度用完**（打分或翻译），进度已保存，下次运行同一条命令从中断处接着跑；4 后端暂时不可用（限流、网络）；**5 跑完了但有文章需要人工处理**（抓取 / 抽取 / 翻译 / 导出失败、翻译需复查、打分输出不合格、缺译文退回重翻、sitemap 部分失败；extract 的质检提示和图片下载失败不算）。同时出现时按 4、3、5 的顺序取。`run` 里某个阶段因为额度停下，后面的阶段照样处理已就绪的文章（打分额度用完，已经 filtered 的文章照样翻译、导出）。
- 抓取间隔：`--fetch-delay` 只有本机测试站点（localhost、127.0.0.1、`*.localhost`、`*.test`）可以调到默认的 2 秒以下；真实站点只能调大，调小会提示并用默认值。
- Twisted reactor 一个进程只能启动一次：单独的 `qbkb fetch` 在本进程里跑 Scrapy；`run` 把 fetch 放进子进程（`python -m qbkb.cli fetch …`），所以一个进程里可以跑多个站点、反复调用。
- 缺 key 时只有真的要调用后端才报错（全部复用旧打分、没有待翻译文章时不需要 key）。
- Windows 控制台的 stdout / stderr 统一用 UTF-8。

---

## 5. 数据模型（SQLite：`state.db`）

表结构以 [`src/qbkb/store/schema.sql`](../src/qbkb/store/schema.sql) 为准，这里只写要点：

- `articles`：每篇文章一行，`url` 为主键。记录状态、抓取信息（lastmod、etag、last_modified、http_status、raw_path 绝对路径）、正文 hash、正文段落数（body_segments：前多少段是正文，其后是版权声明等附录）、元数据（含 tags）、打分（value_score、evergreen、score_reason、rubric_version、scored_body_hash）、翻译（translator、translated_at）、抽取质检问题（qc_issues，JSON 数组，和失败原因 error 分开）、输出路径、失败原因和重试次数。
- 状态：discovered → fetched → extracted → filtered / skipped → translated → published；另有 failed（失败，error 写原因）、pdf_pending、**needs_review**（翻译结果有问题，等人工复查）。
- `segments`：文章由哪些段落组成，**同时存段落 HTML**，translate 直接从这里取要翻的正文。
- `translation_memory`：段落级翻译记忆，主键 `(src_hash, backend)`，backend 带目标语言（如 `deepl:ZH-HANS`）；**术语表版本不同视为未命中**（版本按段落里实际出现的术语计算）；`quality` 为 `ok` / `rejected`，`tm_get` 只返回 ok，结构校验没过的译文以 rejected 保存、不会被复用。
- 老库升级：`store.connect()` 按 `PRAGMA user_version` 自动补新列（只加不删；v2 加了 body_segments），并把 PR #3 时期 `#rejected` 后缀的隔离译文转成 `quality = rejected`。

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
- **iframe 嵌入的交互图表会被丢掉**：抽取阶段把 `<iframe>` 整个去掉（不抓第三方图表服务），译文里这个位置没有任何提示；有 `<noscript>` 静态缩略图的会保留缩略图。要看图表请打开每篇开头的原文链接。
- 脚注上标靠占位链接保住，只覆盖纯数字的 `<sup>N</sup>`；其他上标仍可能被 DeepL 改动，由结构校验拦下后人工复查。
- 机翻质量受限：术语一致性靠术语表，长难句可能生硬；以后可以换成 LLM 后端。
- 需要 JS 渲染或有反爬挑战的站点暂不支持。
- 合规检查是人工完成的，代码只负责强制执行检查结论。
