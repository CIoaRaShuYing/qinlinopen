import hmac
import os
import re
import secrets
import threading
import time
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from qinlin import QinlinClient, ServiceError
from gateway_log import GatewayLog
from storage import Store, digest

COOKIE = "qinlin_admin"


class DoorService:
    def __init__(self, store, client):
        self.store, self.client = store, client
        self.lock = threading.RLock()
        self.snapshot = {"state": "unconfigured", "checkedAt": None, "communities": [], "doors": [], "communityId": None}

    def invalidate(self):
        self.snapshot = {"state": "unknown", "checkedAt": None, "communities": [], "doors": [], "communityId": None}

    def refresh(self, force=False, community_id=None):
        with self.lock:
            token = self.store.get("token")
            if not token:
                self.snapshot = {"state": "unconfigured", "checkedAt": None, "communities": [], "doors": [], "communityId": None}
                return self.snapshot.copy()
            previous = self.snapshot
            if not force and community_id is None and previous.get("checkedAt") and time.time() - previous["checkedAt"] < 30:
                return previous.copy()
            try:
                communities = self.client.communities(token)
                self.store.sync_inventory(communities)
                allowed = {c["id"] for c in communities}
                preferred = self.store.get("preferred_community_id")
                if preferred not in allowed:
                    preferred = None
                selected = community_id or previous.get("communityId") or preferred
                if community_id is not None and selected not in allowed:
                    raise ServiceError("COMMUNITY_NOT_ALLOWED", "该小区不在当前账户权限内", 403)
                if selected not in allowed:
                    selected = communities[0]["id"] if communities else None
                doors = self.client.doors(token, selected) if selected is not None else []
                if selected is not None:
                    self.store.sync_inventory(communities, selected, doors)
                self.snapshot = {"state": "valid", "checkedAt": time.time(), "communities": communities, "communityId": selected, "defaultCommunityId": preferred, "doors": doors}
                self.store.put('last_inventory_view',self.snapshot)
            except ServiceError as error:
                if error.code == "COMMUNITY_NOT_ALLOWED":
                    raise
                base = {"communities": [], "doors": [], "communityId": None, "defaultCommunityId": None} if error.code == "QINLIN_SESSION_EXPIRED" else previous
                self.snapshot = {**base, "state": "expired" if error.code == "QINLIN_SESSION_EXPIRED" else "unknown", "checkedAt": time.time(), "error": {"code": error.code, "message": error.message}}
            return self.snapshot.copy()

    def open(self, community_id, door_id):
        with self.lock:
            snapshot = self.refresh(force=True, community_id=community_id)
            if snapshot["state"] == "unconfigured":
                raise ServiceError("QINLIN_NOT_CONFIGURED", "请先登录亲邻账户", 409)
            if snapshot["state"] != "valid":
                error = snapshot.get("error", {"code": "UPSTREAM_UNAVAILABLE", "message": "暂时无法验证凭证"})
                raise ServiceError(error["code"], error["message"], 409 if snapshot["state"] == "expired" else 503)
            if door_id not in {door["id"] for door in snapshot["doors"]}:
                raise ServiceError("DOOR_NOT_ALLOWED", "该门不在当前小区的授权列表中", 403)
            retry = self.store.limit(f"open:{community_id}:{door_id}", 1, 5)
            if retry:
                raise ServiceError("OPEN_TOO_FREQUENT", "刚刚已提交，请稍候再操作", 429)
            try:
                self.client.open_door(self.store.get("token"), community_id, door_id)
            except ServiceError as error:
                if error.code == "QINLIN_SESSION_EXPIRED":
                    self.snapshot = {"state": "expired", "checkedAt": time.time(), "communities": [], "doors": [], "communityId": None, "defaultCommunityId": None, "error": {"code": error.code, "message": error.message}}
                self.store.audit("open", door_id, error.code)
                raise
            self.store.audit("open", door_id, "accepted")
            return {"code": "OPEN_ACCEPTED", "message": "平台已接受开门指令，请确认门禁响应", "doorId": door_id}


def create_app(config=None, client=None):
    app = Flask(__name__, static_folder=None)
    app.config.update(
        ADMIN_USERNAME=os.getenv("ADMIN_USERNAME", "admin"),
        ADMIN_PASSWORD=os.getenv("ADMIN_PASSWORD", ""),
        ADMIN_PASSWORD_HASH=os.getenv("ADMIN_PASSWORD_HASH", ""),
        API_TOKEN=os.getenv("API_TOKEN", ""),
        PUBLIC_ORIGIN=os.getenv("PUBLIC_ORIGIN", ""),
        DATA_DIR=os.getenv("DATA_DIR", str(Path(__file__).parent / "data")),
        SESSION_TTL_HOURS=int(os.getenv("SESSION_TTL_HOURS", "720")),
        QINLIN_AUTH_ERROR_CODES=os.getenv("QINLIN_AUTH_ERROR_CODES", "401"),
        MAX_CONTENT_LENGTH=8192,
    )
    if config:
        app.config.update(config)
    origin = app.config["PUBLIC_ORIGIN"].rstrip("/")
    parts = urlsplit(origin)
    if parts.scheme not in ("https", "http") or not parts.netloc or parts.path or parts.query or parts.fragment or parts.username:
        raise ValueError("PUBLIC_ORIGIN 必须为完整 Origin，例如 https://door.example.com")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("公网部署必须使用 HTTPS；HTTP 仅用于本机测试")
    secure = parts.scheme == "https"
    if len(app.config["API_TOKEN"]) < 32:
        raise ValueError("API_TOKEN 至少 32 个字符，请使用随机 Token")
    password = app.config["ADMIN_PASSWORD"]
    password_hash = app.config["ADMIN_PASSWORD_HASH"]
    if not app.config["ADMIN_USERNAME"]:
        raise ValueError("ADMIN_USERNAME 不能为空")
    if password_hash:
        try:
            check_password_hash(password_hash, "startup-validation")
        except ValueError:
            raise ValueError("ADMIN_PASSWORD_HASH 格式无效") from None
        if not password_hash.startswith(("scrypt:", "pbkdf2:")) or password_hash.count("$") != 2:
            raise ValueError("ADMIN_PASSWORD_HASH 格式无效")
        verifier = digest(app.config["ADMIN_USERNAME"] + ":" + password_hash)
    else:
        if len(password) < 12:
            raise ValueError("ADMIN_PASSWORD 至少 12 个字符，或配置 ADMIN_PASSWORD_HASH")
        verifier = digest(app.config["ADMIN_USERNAME"] + ":" + password)
        password_hash = generate_password_hash(password)
    app.config["ADMIN_PASSWORD"] = ""
    store = Store(app.config["DATA_DIR"])
    client = client or QinlinClient(app.config["QINLIN_AUTH_ERROR_CODES"].split(","), response_log=GatewayLog(app.config["DATA_DIR"]))
    service = DoorService(store, client)
    app.extensions.update(store=store, doors=service)

    def ok(data=None, status=200):
        return jsonify({"ok": True, "data": data}), status

    def admin_status(snapshot):
        # Secrets are added only to authenticated admin responses, never service snapshots.
        access = store.door_access(snapshot)
        for item in access:
            item['url'] = f"{origin}/api/v1/communities/{item['communityId']}/doors/{item['doorId']}/open"
        return {**snapshot, 'apiDoors': access, 'recent': store.recent(),
                'inventoryEvents':store.inventory_events(), 'cachedInventory':store.get('last_inventory_view')}

    def require_admin(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not store.authenticated(request.cookies.get(COOKIE, ""), verifier):
                raise ServiceError("ADMIN_UNAUTHORIZED", "请先登录管理页面", 401)
            return fn(*args, **kwargs)
        return wrapped

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ServiceError("INVALID_REQUEST", "请提交 JSON 对象", 400)
        return data

    def text_field(data, name, maximum=256):
        value = data.get(name)
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise ServiceError("INVALID_REQUEST", f"{name} 格式不正确", 400)
        return value

    def positive_id(data, name):
        value = data.get(name)
        if type(value) is not int or not 0 < value <= 2147483647:
            raise ServiceError("INVALID_REQUEST", f"{name} 必须是正整数", 400)
        return value

    def rate_limit(key, maximum, window):
        retry = store.limit(key, maximum, window)
        if retry:
            raise ServiceError("RATE_LIMITED", f"操作频繁，请在 {retry} 秒后重试", 429)

    @app.post('/api/inventory-events/<int:event_id>/acknowledge')
    @require_admin
    def acknowledge_inventory_event(event_id):
        store.acknowledge_event(event_id)
        return ok({'inventoryEvents':store.inventory_events()})

    @app.before_request
    def protect_writes():
        if request.path.startswith("/api/") and request.method in ("POST", "PUT", "PATCH", "DELETE"):
            # External API authenticates with a header, never with ambient cookies.
            if request.path.startswith("/api/v1/"):
                return
            if request.headers.get("Origin") != origin:
                raise ServiceError("ORIGIN_REJECTED", "请求来源不被允许，请通过配置的域名访问", 403)
            if not request.is_json:
                raise ServiceError("INVALID_REQUEST", "请使用 application/json", 415)

    @app.after_request
    def response_headers(response):
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        if secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.errorhandler(ServiceError)
    def service_error(error):
        return jsonify({"ok": False, "error": {"code": error.code, "message": error.message}}), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify({"ok": False, "error": {"code": f"HTTP_{error.code}", "message": "请求路径、方法或数据不正确"}}), error.code

    @app.errorhandler(Exception)
    def internal_error(error):
        # No request URL/body or exception text: they may contain credentials.
        app.logger.error("Unhandled error type=%s", type(error).__name__)
        return jsonify({"ok": False, "error": {"code": "INTERNAL_ERROR", "message": "服务暂时异常，请稍后重试"}}), 500

    @app.get("/")
    def index():
        return send_from_directory(Path(__file__).parent / "static", "index.html")

    @app.get("/assets/<path:name>")
    def assets(name):
        return send_from_directory(Path(__file__).parent / "static", name)

    @app.get("/healthz")
    def health():
        store.get("health_check")
        return ok({"status": "ok"})

    @app.get("/api/auth/session")
    def session():
        return ok({"authenticated": store.authenticated(request.cookies.get(COOKIE, ""), verifier)})

    @app.post("/api/auth/login")
    def login():
        # Deliberately ignore untrusted X-Forwarded-For. Global cap works behind any proxy.
        rate_limit("admin-login", 20, 300)
        data = body()
        username = text_field(data, "username", 100)
        supplied_password = text_field(data, "password", 1024)
        valid_password = check_password_hash(password_hash, supplied_password)
        if not (hmac.compare_digest(username.encode(), app.config["ADMIN_USERNAME"].encode()) and valid_password):
            raise ServiceError("LOGIN_FAILED", "账号或密码不正确", 401)
        store.revoke(request.cookies.get(COOKIE, ""))
        token = secrets.token_urlsafe(32)
        ttl = app.config["SESSION_TTL_HOURS"] * 3600
        store.create_session(token, time.time() + ttl, verifier)
        with service.lock:
            service.invalidate()
        response = jsonify({"ok": True, "data": {"authenticated": True}})
        response.set_cookie(COOKIE, token, max_age=ttl, secure=secure, httponly=True, samesite="Strict")
        return response

    @app.post("/api/auth/logout")
    @require_admin
    def logout():
        store.revoke(request.cookies.get(COOKIE, ""))
        response = jsonify({"ok": True, "data": None})
        response.delete_cookie(COOKIE, secure=secure, httponly=True, samesite="Strict")
        return response

    @app.get("/api/status")
    @require_admin
    def status():
        with service.lock:
            return ok(admin_status(service.refresh()))

    @app.post("/api/refresh")
    @require_admin
    def refresh():
        rate_limit("refresh", 10, 60)
        data = body()
        community_id = positive_id(data, "communityId") if "communityId" in data else None
        with service.lock:
            return ok(admin_status(service.refresh(True, community_id)))

    @app.post("/api/qinlin/sms")
    @require_admin
    def sms():
        phone = text_field(body(), "mobile", 11)
        if not re.fullmatch(r"1[3-9]\d{9}", phone, re.ASCII):
            raise ServiceError("INVALID_REQUEST", "请输入正确的 11 位手机号", 400)
        rate_limit("sms-minute", 1, 60)
        rate_limit("sms-hour", 5, 3600)
        try:
            client.send_sms(phone)
        except ServiceError as error:
            store.audit("sms", "", error.code)
            raise
        store.audit("sms", "", "sent")
        return ok({"message": "验证码已发送", "retryAfter": 60})

    @app.post("/api/preferences/community")
    @require_admin
    def default_community():
        rate_limit("refresh", 10, 60)
        community_id = positive_id(body(), "communityId")
        with service.lock:
            snapshot = service.refresh(True, community_id)
            if snapshot["state"] != "valid":
                error = snapshot.get("error", {"code": "QINLIN_NOT_CONFIGURED", "message": "请先连接亲邻账户"})
                raise ServiceError(error["code"], error["message"], 409 if snapshot["state"] in ("expired", "unconfigured") else 503)
            store.put("preferred_community_id", community_id)
            service.snapshot["defaultCommunityId"] = community_id
            return ok(admin_status(service.snapshot))

    @app.post("/api/qinlin/login")
    @require_admin
    def qinlin_login():
        rate_limit("qinlin-login", 10, 600)
        data = body()
        phone, code = text_field(data, "mobile", 11), text_field(data, "code", 8)
        if not re.fullmatch(r"1[3-9]\d{9}", phone, re.ASCII) or not re.fullmatch(r"\d{4,8}", code, re.ASCII):
            raise ServiceError("INVALID_REQUEST", "手机号或验证码格式不正确", 400)
        with service.lock:
            token = client.login(phone, code)
            # Only a successful login replaces the saved credential.
            store.put("token", token)
            store.put("preferred_community_id", None)
            service.invalidate()
            store.audit("login", "", "authenticated")
            return ok(admin_status(service.refresh(True)))

    @app.post('/api/door-tokens/reset')
    @require_admin
    def reset_tokens():
        data = body()
        if data.get('confirm') is not True:
            raise ServiceError('CONFIRMATION_REQUIRED', '请确认重置，旧 Token 将立即失效', 400)
        reset_all = data.get('all') is True
        if reset_all and ('communityId' in data or 'doorId' in data):
            raise ServiceError('INVALID_REQUEST', '重置范围不明确', 400)
        cid = None if reset_all else positive_id(data, 'communityId')
        did = None if reset_all else positive_id(data, 'doorId')
        rate_limit('token-reset', 20, 60)
        # Shares the lock with authorization/open, so reset can't race an old token check.
        with service.lock:
            count = store.reset_door_tokens(cid, did)
            if not reset_all and not count:
                raise ServiceError('TOKEN_NOT_FOUND', '该门尚未生成 Token，请先刷新门列表', 404)
            store.audit('reset_tokens', 'all' if reset_all else did, 'reset')
            return ok({'count': count, **admin_status(service.snapshot)})

    @app.post("/api/doors/open")
    @require_admin
    def web_open():
        rate_limit("all-opens", 20, 60)
        data = body()
        return ok(service.open(positive_id(data, "communityId"), positive_id(data, "doorId")))

    def require_token():
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + app.config["API_TOKEN"]).encode()):
            raise ServiceError("API_UNAUTHORIZED", "API Token 无效或缺失", 401)

    @app.post("/api/v1/doors/open")
    def api_open():
        if not request.headers.get('Authorization', '').startswith('Bearer '):
            raise ServiceError('API_UNAUTHORIZED', '该门的 Token 无效或缺失', 401)
        data = body()
        return open_with_door_token(positive_id(data, 'communityId'), positive_id(data, 'doorId'))

    def open_with_door_token(community_id, door_id):
        with service.lock:
            if not store.valid_door_token(community_id, door_id, request.headers.get('Authorization', '')):
                if store.retired_door_token(community_id, door_id, request.headers.get('Authorization','')):
                    raise ServiceError('DOOR_INTERFACE_CHANGED','小区或门禁标识已变化，此接口已失效；请登录网页重新配置请求地址和 Token',410)
                raise ServiceError('API_UNAUTHORIZED', '该门的 Token 无效、已重置或缺失', 401)
            rate_limit('all-opens', 20, 60)
            return ok(service.open(community_id, door_id))

    @app.post('/api/v1/communities/<int:community_id>/doors/<int:door_id>/open')
    def scoped_api_open(community_id, door_id):
        return open_with_door_token(community_id, door_id)

    @app.get("/api/v1/status")
    def api_status():
        require_token()
        return ok(service.refresh())

    return app


if __name__ == "__main__":
    from waitress import serve
    # One process; the service lock serializes credential changes and physical commands.
    serve(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8080")), threads=8, ident="qinlin-web")
