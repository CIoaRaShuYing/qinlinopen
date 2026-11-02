# 调研

参考 https://github.com/burgesswu/ha-qinlin/blob/main/custom_components/qinlin/api.py 。业务 query 使用 sessionId；签名数据包含 token；版本 4.9.3；短信使用独立网关和签名规则。

已有会话的只读社区和门查询曾验证成功；不记录账户、小区、门禁或设备标识。源码、截图和测试使用虚构数据。

参考集成的过期错误码分类较宽。本实现仅接受明确过期文案和配置的业务码，不将 HTTP 403 或未知业务码一律判为凭证失效。
