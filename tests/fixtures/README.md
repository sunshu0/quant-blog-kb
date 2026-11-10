# 测试用本地文件

sitemap、RSS 等发现阶段的输入样本（Phase 1 A 线）。同样只放构造的内容，不放原文。

| 文件 | 用途 |
|---|---|
| `sitemap_index.xml` | sitemap index：本站两份子 sitemap、一份外站（不跟随）、一条重复 |
| `sitemap_insights.xml` | urlset：前缀过滤、`/it/` `/es/` 语言版本、外站、列表页、重复 URL、多级路径 |
| `sitemap_pages_no_ns.xml` | 不带命名空间的 urlset，含一个 PDF 链接 |

域名统一用 `example.test`（保留的测试顶级域，不会解析到真实站点）。
