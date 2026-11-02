# 每门独立接口与 Token

已确认需求：管理页提供每扇门的独立 POST 地址、长期 Token 和复制请求示例；单门及全部重置都需要二次确认。全部重置包含所有小区已生成的 Token。

实现：新增 SQLite door_tokens 表，复合主键为 community_id + door_id；保存随机 256 位 Token、名称和更新时间。无到期时间，重启和亲邻短信续期均保持不变。仅已认证的管理页面可以获取完整 Token。列表不写入服务的公共 snapshot，公开只读状态接口不包含 Token。

接口：POST /api/v1/communities/{communityId}/doors/{doorId}/open；请求头 Authorization: Bearer TOKEN，无请求体。每门 Token 仅匹配该复合主键，随后实时核验上游权限。原共享 API_TOKEN 只保留只读状态查询能力，不能绕过单门重置。

重置：POST /api/door-tokens/reset，管理员会话与 Origin 校验，confirm 必须是布尔 true。单门传 communityId、doorId；全部传 all=true。SQLite 事务原子更换 Token，与开门鉴权使用同一服务锁；重置完成后旧 Token 不再接受新的请求，已发送的指令不能撤回。UI 默认聚焦取消按钮；取消和 Escape 不会执行重置。

迁移：启动时 CREATE TABLE IF NOT EXISTS，保持原凭据、登录会话和默认小区。部署前备份数据库及镜像。回滚旧版本会恢复旧共享 Token 的开门权限，不应在 Token 被泄漏的场景直接回滚。

验证：36 项自动化测试通过。新增覆盖独立地址、跨门/跨社区隔离、永久存储、续期不换 Token、单门/全部重置、二次确认、未登录/跨站重置拒绝、公开状态不泄漏 Token、上游权限撤销和过期行为。真实开门不参与验收。

## 门卡详情与缓存更新

API 信息移入各门卡的详情层，保留复制、查看和重置操作。主页只列门卡和全部重置入口。SQLite 新增 inventory_events 及 door_tokens.active，缓存列表保存在 settings。启动迁移保留现有 Token。

成功的小区查询和门列表查询分别更新各自完整列表；错误响应不删除缓存。按 ID 集合比较，改名只更新标签；删除停用、添加新建；同 ID 恢复时旋转 Token 防止旧权限恢复。旧地址配原 Token 返回 410 DOOR_INTERFACE_CHANGED，告知重新配置。提醒按事件持久化并可确认，不会重复生成相同事件。无后台定时查询；未访问小区的门列表变化在下次查询该小区时检测。

新增测试覆盖 ID 变更、改名、同 ID 恢复、旧接口失效、错误响应不产生虚假变更、缓存及提醒重启保留、提醒鉴权、旧 schema Token 保留。
