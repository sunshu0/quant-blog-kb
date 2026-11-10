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
```

需要真实翻译时，复制 `.env.example` 为 `.env` 并填写 API key。

## 版权说明

本仓库只包含代码和文档。抓取的原文和译文只在本地生成（`raw/`、`output/`，已被忽略），**不在仓库中公开**。原文版权归各机构所有。

## 进度

- [x] Phase 0：骨架、共享模型、状态库、验证脚本
- [ ] Phase 1：发现+抓取 / 抽取 / 翻译（并行）
- [ ] Phase 2：筛选、输出、CLI 串联
