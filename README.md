# qbkb：量化机构博客中文知识库

自动发现、抓取、筛选国外量化机构博客中有价值的文章，翻译成中文 Markdown，用于补充量化与股票方面的基础知识；新文章发布后增量入库。

- 设计文档：[docs/design.md](docs/design.md)
- AI 协作复盘：[docs/ai-retro.md](docs/ai-retro.md)

## 快速开始

```bash
uv sync
bash scripts/verify.sh      # 离线检查：lint、类型、测试
uv run qbkb init-db
uv run qbkb sites
uv run qbkb run --site man --limit 5   # 发现 → 抓取 → 抽取 → 筛选 → 翻译 → 输出
uv run qbkb status
```

不想花额度时加 `--scorer fake --translator fake`（假后端，跑通流程用）。退出码：0 正常，1 出错，3 额度用完（进度已保存，下个月再跑同一条命令即可接着翻），4 后端暂时不可用（稍后再跑），5 跑完了但有文章需要人工处理（`qbkb status` 查看）。完整说明见 `uv run qbkb --help`。

需要真实翻译时，复制 `.env.example` 为 `.env` 并填写 API key。filter 结束时会打印 LLM 的请求数和 token 数，translate 前后会打印 DeepL 本月的"已用 / 上限"字符数。

## 已知局限

- **原文里 iframe 嵌入的交互图表不会出现在译文里**，位置上也没有提示；需要看图表请打开每篇开头的原文链接。
- 不处理 PDF；图表图片里的文字不翻译。
- 机器翻译，结构校验没过的段落会进 `needs_review` 等人工复查。
- 完整列表见 [docs/design.md](docs/design.md) 第 9 节。

## 版权说明

本仓库只包含代码和文档。抓取的原文和译文只在本地生成（`raw/`、`output/`，已被忽略），**不在仓库中公开**。原文版权归各机构所有。

## 进度

- [x] Phase 0：骨架、共享模型、状态库、验证脚本
- [x] Phase 1：发现+抓取 / 抽取 / 翻译（并行）
- [x] Phase 2：筛选、输出、CLI 串联
