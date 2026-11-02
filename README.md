# Qinlin Open · 亲邻门禁 Web

独立部署的亲邻门禁管理页与 HTTPS API，无需 Home Assistant。管理员使用配置的账号密码登录，随后连接自己的亲邻账户，查看凭证状态、选择小区和门禁。

支持多个小区切换、显式设置默认展示小区、凭证过期后通过短信重新登录、POST + API Token 调用。源码没有预置小区、门禁、手机号或个人凭证。小区和门列表仅在管理登录后通过服务端查询；首次没有偏好时显示账户返回的第一个小区。切换小区不会改变默认偏好，点击“设为默认展示小区”才保存。默认小区不再属于账户时自动回退。重新认证或导入账户会话会清除原偏好，避免账户切换残留。

## 快速部署

需要 Docker Compose 和一个已配置 HTTPS 的反向代理。

```bash
git clone https://github.com/CIoaRaShuYing/qinlinopen.git
cd qinlinopen
docker build -t local/qinlin-web:1.0.0 .
# 交互生成账号密码哈希和随机 API Token；不在终端打印密码或 Token
docker run --rm -it --user "$(id -u):$(id -g)" \
  -v "$PWD:/config" -w /config local/qinlin-web:1.0.0 \
  python /app/manage.py configure
docker compose config --quiet
docker compose up -d
```

也可以复制 `.env.example` 为 `.env` 手动设置 `ADMIN_USERNAME`、`ADMIN_PASSWORD`、`API_TOKEN` 和 `PUBLIC_ORIGIN`。密码至少 12 位，随机 API Token 至少 32 位。推荐 `manage.py configure`：它保存密码哈希，不保存明文密码。哈希和 Token 均用单引号包围，避免 Compose 将哈希中的 `$` 当作变量。

应用端口只绑定 `127.0.0.1:8088`。将反向代理指向此端口，域名必须与 `PUBLIC_ORIGIN` 一致。宿主机 Caddy 示例见 `deploy/Caddyfile.example`。已有 Docker 反向代理时，可使用：

```bash
# .env 可设置 PROXY_NETWORK 为已有代理网络名称
docker compose -f compose.yaml -f deploy/compose.proxy.yaml up -d --build
```

此时代理上游为 `qinlin:8080`。示例网络名 `sub_default` 必须按实际环境修改。代理读取超时建议至少 70 秒，以覆盖权限查询和开门请求；禁止代理自动重试 POST。

访问自己的 HTTPS 域名，登录管理员账号，再在“亲邻账户”中主动获取验证码。服务不会启动即发送短信，也不会自动开门。亲邻 `sessionId` 仅保存在服务器数据卷，不返回浏览器。

## 导入已有会话（可选）

准备独立、非源码目录，包含 `qinlin_login.json`、`qinlin_community.json`、`qinlin_doors.json`。导入只读取 `login.data.sessionId`，检查其他两份文件结构，不把其中的门、小区或手机号写入源码；展示内容仍由登录后实时查询得到。

```bash
docker compose stop qinlin
docker compose run --rm -v /secure/session:/import:ro qinlin \
  python manage.py import-session --session-dir /import
docker compose up -d
```

导入命令不会发起网络请求。务必在停止服务时导入，然后重启，防止旧的内存状态与新凭据不一致。

## API

登录管理页面后，点击对应门卡上的 **接口与长期 Token**，进入该门的详情层，查看独立 POST 地址、长期 Token 和完整 cURL。可复制地址、复制 Token、显示/隐藏 Token 或复制完整请求。地址已包含小区与门 ID，无需请求体。

```bash
curl --request POST 'https://door.example.com/api/v1/communities/1001/doors/2001/open' \
  --header "Authorization: Bearer $DOOR_TOKEN"
```

ID 为虚构示例。Token 不设置过期时间，存储在数据卷内，重启服务及重新认证亲邻后保持不变；它只授权对应小区的对应门。上游门禁权限仍会实时校验，亲邻凭证过期仍返回 `QINLIN_SESSION_EXPIRED`。

- **重置此门 Token**：二次确认后仅更换该门 Token。
- **重置全部 Token**：二次确认后更换所有小区已生成的 Token，包括非当前小区。
- 重置后地址不变，旧 Token 立即失效，需更新快捷指令/脚本。取消确认不会发送重置请求。已经发出的开门指令不能通过重置撤回。
- 凭证过期或网络异常时仍可以管理以前生成的接口。该记录不表示账户仍有门禁权限。
- 没有 GET 开门接口，Token 不接受 URL 参数，必须使用 `Authorization: Bearer` 请求头。

升级说明：原 `.env` 中的共享 `API_TOKEN` **现在只用于 GET /api/v1/status，不再授权开门**。原通用 `POST /api/v1/doors/open` 可继续传 `{communityId,doorId}`，但请求头必须改用对应门的 Token。请到管理页复制新请求配置。公开状态接口不会返回门禁 Token。

成功响应表示亲邻平台接受指令，不能证明物理门已经打开：

```json
{"ok":true,"data":{"code":"OPEN_ACCEPTED","message":"平台已接受开门指令，请确认门禁响应","doorId":2001}}
```

| HTTP 状态 | error.code | 含义 |
| --- | --- | --- |
| 401 | API_UNAUTHORIZED | 调用密钥无效或缺失 |
| 409 | QINLIN_SESSION_EXPIRED | 亲邻凭证过期；到 Web 端重新认证 |
| 409 | QINLIN_NOT_CONFIGURED | 尚未连接亲邻账户 |
| 403 | COMMUNITY_NOT_ALLOWED / DOOR_NOT_ALLOWED | 目标不在当前账户授权范围 |
| 410 | DOOR_INTERFACE_CHANGED | 原小区或门 ID 已失效，请在网页重新配置地址和 Token |
| 429 | OPEN_TOO_FREQUENT / RATE_LIMITED | 操作频繁，稍候再操作 |
| 503 | UPSTREAM_UNAVAILABLE | 网络或上游异常，不能判为凭证过期 |
| 502 / 504 | OPEN_RESULT_UNKNOWN | 指令结果未知，请现场确认；服务不自动重试 |
| 422 | QINLIN_REJECTED | 上游拒绝请求，可能是权限或参数问题 |

状态查询本身成功时返回 HTTP 200，业务状态在 `data.state`（`valid`、`expired`、`unknown`、`unconfigured`），不自动发送短信。状态缓存最多 30 秒，刷新按钮强制检查。只有上游明确表示登录失效或匹配已配置的错误码时才标为过期；未知业务错误不猜测。`QINLIN_AUTH_ERROR_CODES` 默认为 `401`，应按真实上游响应调整，不要盲目把权限错误 `403` 当作过期。

## 运维与数据

- SQLite 存放亲邻会话、默认小区偏好、门禁长期 Token、管理员会话哈希、频率计数、最近 500 条脱敏操作记录；持久化到 `qinlin-data` 数据卷。数据库包含敏感凭据，依赖服务器和备份的访问控制，不声称数据库内容已加密。
- 管理会话默认保留 720 小时，Cookie 使用 HttpOnly、SameSite=Strict 和 HTTPS Secure；修改管理员账号或密码配置并重建容器后，旧登录会话失效。
- 单进程 Waitress + 线程锁串行处理凭据变更和开门。同一扇门 5 秒内不重复提交；请勿直接改为多进程或多副本部署。
- `.env`、数据库、会话 JSON、缓存均被 Git 和 Docker 排除。不要将上述文件、真实截图或服务器连接信息提交到公开仓库。
- 停止容器后备份数据卷，再启动。恢复时停止服务并恢复完整数据库目录，保留 UID/GID 10001 的读写权限。不要用 `docker compose down -v` 进行常规升级。
- 门禁 Token 轮换：在管理页执行单门或全部重置并二次确认。状态查询 Token 轮换：修改私有 `.env` 后重建容器。
- 健康检查 `/healthz` 只检查本服务和数据库，不连接亲邻平台。

## 本地开发与验证

```bash
python -m venv .venv
# 激活虚拟环境后
pip install -r requirements.lock
pip install pytest
pytest tests -q
node --check static/app.js
```

本地启动需要向进程提供配置环境变量；`python app.py` 本身不自动读取 `.env`。仅 localhost/127.0.0.1 允许 HTTP，其余 Origin 必须使用 HTTPS。`.env` 主要供 Docker Compose 读取。

测试均采用虚构数据并阻止真实网络请求，不会发送短信或开门。实际验证记录见 `docs/verification.md`；本项目尚未完成真实短信、验证码登录或物理开门验收。

## 协议参考

亲邻协议参考 [ha-qinlin](https://github.com/burgesswu/ha-qinlin)，签名及短信网关分开处理，版本为 `4.9.3`。本服务是非官方实现，上游接口变化可能需要更新。第三方许可见 `THIRD_PARTY_NOTICES.md`。

## 服务器缓存与接口变更提醒

成功查询后将小区列表、各小区门列表、最近展示内容及每门接口保存在 SQLite。服务重启不丢失缓存；网络异常或凭证过期时可查看标注为缓存的卡片和接口，但开门仍需实时验证权限，缓存不授权离线开门。

查询到小区 ID 列表变化，或刷新/访问对应小区时发现门 ID 列表变化，会生成持久提醒。仅名称变化不更换地址和 Token。ID 消失时停用旧接口；新增 ID 使用新地址和独立 Token，提示重新配置。不会用相同名称猜测并自动绑定旧接口。消失的 ID 恢复后地址不变，但会更换 Token 并提醒。首次安装建立基线不提示，网络故障不会被当作门禁删除。

提醒保留到管理员点击“已了解”，重启不消失。主页下方保留“已失效的接口记录”供核对。变化检测发生在成功查询时，不提供后台定时监控或短信通知。
