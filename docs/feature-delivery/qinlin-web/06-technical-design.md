# 技术设计

- app.py：配置校验、服务端登录、同源校验、业务状态、门权限核验、社区偏好 API、限流。
- qinlin.py：固定上游地址、签名、加密、社区/门/短信/登录/开门；禁用重定向和重试。
- storage.py：SQLite settings、sessions、limits、audit；参数化 SQL，凭据仅存在服务端数据卷。
- manage.py：交互生成配置、离线导入会话，不连接上游。
- static/：手机优先页面；状态条、动态社区选择、默认展示按钮、门卡、短信表单。

管理员会话为随机 Cookie，数据库只存摘要和过期时间，凭据配置指纹变化即撤销旧会话。API Token 独立于 Cookie。Cookie 写请求校验 Origin；API 只认 Authorization，拒绝 URL Token。无跨域。

社区数据不进入 HTML 模板或前端源码；无固定社区 ID 配置。默认偏好只保存用户选择的已授权社区 ID。重新认证清空原偏好。权限刷新后若偏好失效，回退到可用社区。外部开门每次重新查询社区及门列表。

上游状态区分 valid/expired/unknown/unconfigured；明确过期才删除展示数据，网络异常不删除凭据。开门超时返回 OPEN_RESULT_UNKNOWN，不重试。单进程锁确保同一进程内凭据更换和开门串行。

默认不需要业务数据库迁移，新建四张内部表，无旧业务数据。升级回滚依靠停止服务后备份的数据卷及旧镜像。Docker 非 root、只读根文件系统、持久化数据卷；HTTPS 由现有反向代理终结。

验证：pytest tests -q；node --check static/app.js；docker compose config --quiet；docker build；浏览器检查手机及桌面布局。远程部署和真实有副作用操作单独验收。

视觉：蓝灰 #244e60、纸白 #f2f5f6、文字 #213c48、线条 #dce5e9、状态绿 #267566；标题使用系统中文 UI 字体，正文 Segoe UI，门形标记与实体门卡构成识别元素，避免额外图片依赖。
