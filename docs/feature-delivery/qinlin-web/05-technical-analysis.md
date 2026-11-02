# 技术分析

选择 Flask + Waitress + requests + cryptography，静态 HTML/CSS/JavaScript，SQLite。单服务部署，无前端构建和独立数据库。

参考 API 已通过真实只读查询核验。开门与验证码链路依据参考代码和模拟测试，未声称现场验证。

由主 Agent 完成分析和实现，未使用 subagent。源码公开交付采用虚构 fixture，真实会话仅在独立工具内存中用于只读验证。
