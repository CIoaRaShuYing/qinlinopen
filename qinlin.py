"""Qinlin transport. Protocol reference: burgesswu/ha-qinlin (MIT)."""
import hashlib
import secrets
import time

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BASE = "https://mobileapi3.qinlinkeji.com"
SMS_URL = "https://gateway2.qinlinkeji.com/member/sms/sendSecurityCode"
APPID = "gbDjIZQSOpCMX49P"
APP_SECRET = "OKFoQ9MmNXQtcyXROo4PnaFkfDPTHuDg"
PHONE_KEY = bytes.fromhex("FBC213C4C7BEEBD2AA4EDBF0F681C41B")


class ServiceError(Exception):
    def __init__(self, code, message, status=502):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def sign(data, suffix="key=qiAnlPinP"):
    fields = "&".join(f"{k}={v}" for k, v in sorted(data.items()) if v is not None)
    return hashlib.md5(f"{fields}&{suffix}".encode()).hexdigest().upper()


def encrypt_phone(phone):
    padder = padding.PKCS7(128).padder()
    padded = padder.update(phone.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(PHONE_KEY), modes.ECB()).encryptor()
    return (encryptor.update(padded) + encryptor.finalize()).hex().upper()


def normalize_doors(data):
    if not isinstance(data, dict):
        raise ServiceError("UPSTREAM_FORMAT", "门列表格式异常，请稍后刷新")
    result = {}
    for key in ("buildingDoorList", "gateDoorList", "customDoorList", "userDoorDTOS"):
        items = data.get(key, [])
        if not isinstance(items, list):
            raise ServiceError("UPSTREAM_FORMAT", "门列表格式异常，请稍后刷新")
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                door_id = int(item["doorControlId"])
            except (KeyError, TypeError, ValueError):
                continue
            result.setdefault(door_id, {
                "id": door_id, "name": str(item.get("doorControlName") or f"门禁 {door_id}"),
                "online": str(item.get("online", "unknown")),
                "kind": "小区门" if key == "gateDoorList" else "楼栋门",
            })
    return list(result.values())


class QinlinClient:
    def __init__(self, auth_codes=(401,), transport=None):
        self.auth_codes = {str(c) for c in auth_codes}
        self.transport = transport or requests.Session()
        self.headers = {
            "Qvendor": "Xiaomi", "Openid": secrets.token_hex(8),
            "Qversioncode": "3113", "Qversionname": "4.9.3", "Qchannel": "xiaomi",
            "Qplatform": "0", "User-Agent": "Dart/2.18 (dart:io)",
        }

    def _request(self, url, *, opening=False, **kwargs):
        # requests defaults to no retry. Never follow redirects carrying credentials.
        try:
            response = self.transport.post(url, timeout=(5, 15), allow_redirects=False, **kwargs)
        except requests.RequestException:
            if opening:
                raise ServiceError("OPEN_RESULT_UNKNOWN", "请求未收到明确结果，请现场确认；不会自动重试", 504) from None
            raise ServiceError("UPSTREAM_UNAVAILABLE", "暂时无法连接亲邻服务，请稍后重试", 503) from None
        if response.status_code != 200:
            if opening:
                raise ServiceError("OPEN_RESULT_UNKNOWN", "平台响应异常，开门结果未知，请现场确认", 502)
            # HTTP errors alone are not proof of a Qinlin session expiring.
            raise ServiceError("UPSTREAM_UNAVAILABLE", "亲邻服务暂时不可用", 503)
        try:
            data = response.json()
        except ValueError:
            raise ServiceError("OPEN_RESULT_UNKNOWN" if opening else "UPSTREAM_FORMAT",
                               "平台返回格式异常" + ("，开门结果未知，请现场确认" if opening else "")) from None
        if not isinstance(data, dict):
            raise ServiceError("OPEN_RESULT_UNKNOWN" if opening else "UPSTREAM_FORMAT", "平台返回格式异常")
        return data

    def _unwrap(self, payload, *, authenticated=True):
        code = str(payload.get("code"))
        if code == "0":
            return payload.get("data")
        message = str(payload.get("message") or payload.get("msg") or "")
        explicit_expiry = any(s in message.lower() for s in (
            "请重新登录", "请先登录", "未登录", "登录已过期", "登录过期", "登录失效",
            "登录状态失效", "登录状态已失效", "session expired", "invalid session", "token expired",
        ))
        if authenticated and (code in self.auth_codes or explicit_expiry):
            raise ServiceError("QINLIN_SESSION_EXPIRED", "亲邻凭证已失效，请重新获取验证码登录", 409)
        # Don't echo upstream bodies, which can contain tokens or phone numbers.
        raise ServiceError("QINLIN_REJECTED", "亲邻未接受请求，请检查权限或验证码后重试", 422)

    def _post(self, path, fields, token="", mode="json", opening=False):
        body = {**fields, "timestamp": int(time.time() * 1000), "nonce": str(secrets.randbelow(90000) + 10000), "version": "4.9.3"}
        body["sign"] = sign({"token": token, **body})
        params = {"sessionId": token} if token else {}
        kwargs = {"headers": self.headers, "params": params}
        if mode == "query":
            params.update(body)
        elif mode == "form":
            kwargs["data"] = body
        else:
            kwargs["json"] = body
        return self._unwrap(self._request(BASE + path, opening=opening, **kwargs), authenticated=bool(token))

    def communities(self, token):
        data = self._post("/api/app/user/v2/communityInfo", {"method": "communityInfo"}, token)
        if not isinstance(data, list):
            raise ServiceError("UPSTREAM_FORMAT", "小区列表格式异常")
        result = {}
        for item in data:
            try:
                community_id = int(item["communityId"])
                result[community_id] = {"id": community_id, "name": str(item["communityName"])}
            except (KeyError, TypeError, ValueError):
                continue
        return list(result.values())

    def doors(self, token, community_id):
        return normalize_doors(self._post("/api/app/user/v2/queryAllUserDoorByCache", {"communityId": community_id}, token, "form"))

    def open_door(self, token, community_id, door_id):
        self._post("/api/open/doorcontrol/v2/open", {"appChannel": 1, "doorControlId": door_id, "communityId": community_id}, token, "query", opening=True)

    def send_sms(self, phone):
        fields = {"appid": APPID, "mobile": phone, "nonce": str(secrets.randbelow(9000) + 1000), "timestamp": str(int(time.time() * 1000)), "version": "v2"}
        headers = {**self.headers, **{key.title(): value for key, value in fields.items() if key != "mobile"}, "Sign": sign(fields, f"appsecret={APP_SECRET}")}
        data = self._request(SMS_URL, headers=headers, json={"mobile": phone})
        if not (str(data.get("code", data.get("status"))) in ("0", "200") or data.get("success") is True):
            raise ServiceError("SMS_REJECTED", "验证码发送未被确认，请稍后再试", 422)

    def login(self, phone, code):
        data = self._post("/api/app/v1/login", {"mobile": encrypt_phone(phone), "smsCode": code, "appChannel": 1})
        token = data.get("sessionId") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise ServiceError("UPSTREAM_FORMAT", "登录响应缺少凭证")
        return token
