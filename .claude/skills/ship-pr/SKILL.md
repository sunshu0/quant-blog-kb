---
name: ship-pr
description: qbkb 的提交流程。完成一块工作、准备提交或开 PR 时使用：跑 verify.sh → 自查 diff → 提交 → 推送 → 开 PR → 停下，不合并。
---

# ship-pr

按顺序执行，任何一步失败就停下修，不要跳步。

1. **验证**：`bash scripts/verify.sh`，退出码必须是 0。不是 0 就修，修完重跑。
2. **自查 diff**：`git diff main...HEAD` 加上未提交的改动，逐项检查：
   - 只改了自己这条线负责的目录（见 CLAUDE.md「分工」）；要改共用文件（`models.py`、`store/`、`pyproject.toml`、`uv.lock`）就停下，告诉用户；
   - **没有原文内容**：`raw/`、`output/`、`state.db`、`.env` 没被加入；`tests/` 里没有真实文章的句子；
   - 没有调试残留（print、注释掉的代码、临时文件）；
   - 新行为都有离线测试。
3. **提交**：一个 PR 可以有多个提交；提交信息用中文写清"做了什么、为什么"，结尾带 Co-Authored-By 行。
4. **推送**：`git push -u origin <分支>`。不要推 main，不要 force push 别人的分支。
5. **开 PR**：`gh pr create --base main`，正文包括：改动摘要、verify.sh 结果、没覆盖到的地方，以及 `docs/ai-retro.md` 度量表要填的数（开工时间、返工次数）。
6. **停下**：不合并、不开 auto-merge。把 PR 链接告诉用户（或派你来的会话）。
