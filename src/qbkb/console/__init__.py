"""本地网页控制台（design.md 10）：浏览文章、点击运行、待复查、设置；演示模式和真实模式。

- workspace：两种模式各自的数据目录、控制台设置、.env 里的 key；
- demo：演示模式的构造示例站点（本机 HTTP 服务）和自带中文的演示翻译后端；
- jobs：后台任务（子进程跑 `qbkb run`）、单任务锁、退出码说明；
- schedule：Windows 任务计划（每天自动更新）；auto 是任务计划执行的入口；
- render：把 export 产物（Markdown）安全地渲染成 HTML；
- server：只监听 127.0.0.1 的 HTTP 服务和 JSON 接口。
"""
