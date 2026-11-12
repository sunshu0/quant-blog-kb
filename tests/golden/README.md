# 黄金样本（extract 回归测试）

仓库是公开的，这里**只放构造的 HTML**（design.md D5、第 7 节）：

- 照着真实页面的 DOM 结构写：导航、弹窗、正文容器、图片、脚注、表格、公式、版权声明都保留；
- 正文文字全部自己写，**不得出现任何原文句子**；
- 每个样本一对文件：`<site>/<case>.html`（输入）和 `<site>/<case>.json`（人工确认过的期望输出）。

期望输出是 extract 的结果，不是 Markdown（转 Markdown 在 Phase 2 的 export）：

```json
{
  "url": "抽取时用的文章地址（相对链接按它转绝对）",
  "method": "rule:<模板名> 或 trafilatura",
  "meta": {"title": "...", "author": "...", "published": "YYYY-MM-DD", "tags": ["..."]},
  "n_body": 16,
  "issues": ["质检问题，没有就是空列表"],
  "segments": ["<p>清洗后的段落 HTML</p>", "..."]
}
```

`segments` 前 `n_body` 段是正文，后面是附在文末的版权声明（站点配置 `extract.keep`）。

## man.com 样本

| 文件 | 覆盖的情况 |
|---|---|
| `article-figures` | 标准文章：JSON-LD 作者（含 @id 引用）、h2/h3、相对链接和图片、图注、脚注锚点（id 保留）、侧栏/相关文章/弹窗被删；版权声明在响应式布局里重复一份，只附一次 |
| `article-table-math` | 表格（colspan、scope）、MathML 和 TeX 公式（MathML 的 onclick 被删）、段内的图片和公式不拆段、嵌套列表、引用、`<pre>` 代码缩进、`<picture>` + 懒加载图片、JSON-LD keywords 作标签 |
| `video-short` | 视频页：播放器被删、作者为空、质检"正文太短" |
| `podcast-transcript` | 播客页：iframe 被删、坏 JSON-LD 被跳过、div 里的零散文字成段、`<br>` 保留在段内、"cookie jar" 不算噪音（关键词按整词匹配） |
| `weekly-flourish` | 周报：h5 小标题、Flourish 图表留下 noscript 缩略图、"Problems loading this infographic?" 按文字删除、作者来自页面 |
| `meta-fallback-noise` | 没有 JSON-LD：标题来自 og:title（去掉站名后缀）、日期来自页面文字；注释、空段、只有 `<br>` 的段、死链接、javascript:/vbscript: 链接（只留文字）、data: 图片、`<hr>` 被清掉；正文里混进的"You are leaving"被标出 |
| `fallback-trafilatura` | 不认识的旧版式：trafilatura 兜底，元数据来自 meta 标签，版权声明照样附上 |
| `missing-meta` | 缺标题、缺日期、缺版权声明，全部进质检 |

## cfm.com 样本

CFM 是 WordPress 站，全站一个文章模板（`.content-blocks-container .col-80` 是正文，`.col-20.sidebar` 放作者、日期、PDF）。

| 文件 | 覆盖的情况 |
|---|---|
| `edge-blog` | The Edge 博客：JSON-LD headline 带 HTML 实体（`&#8220;`）被解码；JSON-LD / meta 作者是机构名 "CFM"、另一个 meta author 是网址，都跳过，作者取侧栏 `li` 里 `<br>` 前的姓名（空 `li` 跳过）；栏目来自 JSON-LD articleSection；h1、朗读音频、分享按钮、页底相关文章、cookie 提示、页脚被删；`&nbsp;` 空段被清掉；图注（纯文字 div）成段；`<sup>` 脚注；免责声明随正文保留 |
| `paper-pdf` | 论文摘要页：作者带单位、空白 `li`；栏目 + JSON-LD keywords 作标签；侧栏 "Download PDF" 相对链接转绝对、附在正文后；质检"正文太短" |
| `video-commentary` | 视频页：播放按钮和 YouTube iframe 被删、封面图保留；articleSection 里的 `&amp;` 被解码；没有作者；质检"正文太短" |
| `missing-jsonld` | 没有 JSON-LD：标题来自页面 h1（不是带后缀的 `<title>`）、日期来自 article:published_time、标签来自页面上的栏目名和 `.tags`；作者 `li` 全空 |

## 改了抽取规则之后

```bash
QBKB_UPDATE_GOLDEN=1 uv run pytest -q tests/extract -k golden
git diff tests/golden/
```

逐个确认 diff 里的变化都是有意的，再提交。

用真实页面做的验证放在 `tests/local_samples/`（已被 .gitignore 忽略），不提交；
`tests/extract/test_extract_local_samples.py` 标了 `network`，只在本地跑：
`uv run pytest -q -m network tests/extract`。
