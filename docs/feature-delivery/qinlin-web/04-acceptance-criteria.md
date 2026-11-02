# 验收标准

| Requirement | Acceptance criteria | Verification method | Risk | Status |
| --- | --- | --- | --- | --- |
| REQ-001 | 错误密码/未登录被拒绝；Cookie 防护；退出、配置变更撤销会话 | HTTP 集成测试 | 鉴权绕过 | passed |
| REQ-002 | 过期/网络错误分开；会话重启持久化；不向网页返回 token | mock 和重建应用测试 | 误判与泄漏 | passed |
| REQ-003 | 无预置社区；多个社区切换；默认偏好持久化；无权限社区拒绝/回退 | 虚构多社区 fixture 与 UI | 错误展示 | passed |
| REQ-004 | 单次点击单次请求；超时无重试；明确指令接受含义 | mock 调用计数及浏览器 | 重复真实操作 | mock passed |
| REQ-005 | 主动点击才发短信；限流；登录失败不覆盖 | mock 测试 | 意外短信 | mock passed |
| REQ-006 | Token 仅允许请求头；GET 不开门；过期固定错误码 | HTTP 集成测试 | 未鉴权调用 | passed |
| REQ-007 | Compose 检查，镜像构建，HTTPS 运行 | Compose 通过；Docker Hub 拉取超时 | 部署环境差异 | partial |
| REQ-008 | 同源控制、限流、源码/截图/文档隐私清理 | 自动化与交付目录扫描 | 隐私泄漏 | passed |

真实短信、验证码登录和物理开门未测试。远程部署未执行，需目标服务器连接信息及域名。
