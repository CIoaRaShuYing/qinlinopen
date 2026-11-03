import json
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from manage import import_session
from qinlin import QinlinClient, ServiceError, encrypt_phone, normalize_doors, sign
from storage import Store

ORIGIN = "https://door.example.test"
TOKEN = "test-api-token-" + "x" * 32


class FakeQinlin:
    def __init__(self):
        self.opens = []
        self.sms = []
        self.failure = None
        self.open_failure = None
        self.login_failure = False

    def communities(self, token):
        if self.failure:
            raise self.failure
        return [{"id": 1001, "name": "测试小区"}]

    def doors(self, token, community_id):
        return [{"id": 2001, "name": "示例楼栋门", "online": "online", "kind": "楼栋门"}]

    def open_door(self, token, community_id, door_id):
        self.opens.append((community_id, door_id))
        if self.open_failure:
            raise self.open_failure

    def send_sms(self, phone):
        self.sms.append(phone)

    def login(self, phone, code):
        if self.login_failure:
            raise ServiceError("QINLIN_REJECTED", "验证码无效", 422)
        return "new-fake-session"


@pytest.fixture(autouse=True)
def prevent_real_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Real network is forbidden in tests")
    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture
def env(tmp_path):
    fake = FakeQinlin()
    cfg = {"TESTING": True, "PUBLIC_ORIGIN": ORIGIN, "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "test-password-12345", "ADMIN_PASSWORD_HASH": "", "API_TOKEN": TOKEN, "DATA_DIR": str(tmp_path)}
    app = create_app(cfg, fake)
    app.extensions['store'].door_access({'state':'valid','communityId':1001,'communities':fake.communities('fixture'),'doors':fake.doors('fixture',1001)})
    return app, app.test_client(), fake, cfg


def post(client, url, data):
    return client.post(url, json=data, headers={"Origin": ORIGIN}, base_url=ORIGIN)


def login(client):
    return post(client, "/api/auth/login", {"username": "admin", "password": "test-password-12345"})


def external(client, data=None, token=None):
    if token is None:
        token=client.application.extensions['store'].door_access({'state':'unknown'})[0]['token']
    return client.post("/api/v1/doors/open", json=data or {"communityId": 1001, "doorId": 2001}, headers={"Authorization": "Bearer " + token})


def test_login_cookie_logout_and_protected_routes(env):
    app, client, fake, _ = env
    assert client.get("/api/status").status_code == 401
    assert post(client, "/api/qinlin/sms", {"mobile": "13800000000"}).status_code == 401
    response = login(client)
    assert response.status_code == 200
    assert all(attr in response.headers["Set-Cookie"] for attr in ("HttpOnly", "Secure", "SameSite=Strict"))
    assert client.get("/api/status", base_url=ORIGIN).json["data"]["state"] == "unconfigured"
    assert post(client, "/api/auth/logout", {}).status_code == 200
    assert client.get("/api/status", base_url=ORIGIN).status_code == 401
    assert fake.sms == [] and fake.opens == []


def test_password_and_origin_rejected(env):
    _, client, _, _ = env
    assert post(client, "/api/auth/login", {"username": "admin", "password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={}, headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.post("/api/auth/login", json={}).status_code == 403


def test_token_is_header_only_and_get_cannot_open(env):
    app, client, fake, _ = env
    app.extensions["store"].put("token", "secret-session")
    login(client)
    assert external(client, token="wrong").status_code == 401
    assert client.post(f"/api/v1/doors/open?token={TOKEN}", json={}).status_code == 401
    assert client.get("/api/v1/doors/open").status_code == 405
    assert fake.opens == []


def test_status_does_not_leak_session_and_open_checks_membership(env):
    app, client, fake, _ = env
    app.extensions["store"].put("token", "secret-session")
    login(client)
    response = client.get("/api/status", base_url=ORIGIN)
    assert response.json["data"]["state"] == "valid"
    assert b"secret-session" not in response.data
    assert external(client, {"communityId": 999, "doorId": 2001}).status_code == 401
    assert external(client, {"communityId": 1001, "doorId": 999}).status_code == 401
    assert fake.opens == []
    assert external(client).json["data"]["code"] == "OPEN_ACCEPTED"
    assert external(client).status_code == 429
    assert len(fake.opens) == 1


@pytest.mark.parametrize("failure,state,status", [
    (ServiceError("QINLIN_SESSION_EXPIRED", "凭证过期", 409), "expired", 409),
    (ServiceError("UPSTREAM_UNAVAILABLE", "网络不可用", 503), "unknown", 503),
])
def test_expiry_and_network_failure_are_distinct(env, failure, state, status):
    app, client, fake, _ = env
    app.extensions["store"].put("token", "existing-token")
    fake.failure = failure
    login(client)
    assert client.get("/api/status", base_url=ORIGIN).json["data"]["state"] == state
    response = external(client)
    assert response.status_code == status
    assert response.json["error"]["code"] == failure.code
    assert app.extensions["store"].get("token") == "existing-token"
    assert fake.sms == [] and fake.opens == []


def test_failed_reauthentication_preserves_credential(env):
    app, client, fake, _ = env
    app.extensions["store"].put("token", "existing-token")
    login(client)
    fake.login_failure = True
    assert post(client, "/api/qinlin/login", {"mobile": "13800000000", "code": "123456"}).status_code == 422
    assert app.extensions["store"].get("token") == "existing-token"
    fake.login_failure = False
    assert post(client, "/api/qinlin/login", {"mobile": "13800000000", "code": "123456"}).json["data"]["state"] == "valid"
    assert app.extensions["store"].get("token") == "new-fake-session"


def test_sms_is_explicit_and_rate_limited(env):
    _, client, fake, _ = env
    login(client)
    assert fake.sms == []
    assert post(client, "/api/qinlin/sms", {"mobile": "bad"}).status_code == 400
    assert post(client, "/api/qinlin/sms", {"mobile": "13800000000"}).status_code == 200
    assert post(client, "/api/qinlin/sms", {"mobile": "13800000000"}).status_code == 429
    assert fake.sms == ["13800000000"]


def test_open_timeout_does_not_retry(env):
    app, client, fake, _ = env
    app.extensions["store"].put("token", "token")
    fake.open_failure = ServiceError("OPEN_RESULT_UNKNOWN", "结果未知", 504)
    assert external(client).status_code == 504
    assert len(fake.opens) == 1
    assert external(client).status_code == 429
    assert len(fake.opens) == 1


def test_cookie_survives_restart_but_password_change_revokes(env):
    app, client, fake, cfg = env
    app.extensions["store"].put("token", "persisted")
    login(client)
    cookie = client.get_cookie("qinlin_admin", domain="door.example.test").value
    second = create_app(cfg, fake)
    other = second.test_client()
    other.set_cookie("qinlin_admin", cookie, domain="door.example.test")
    assert other.get("/api/status", base_url=ORIGIN).status_code == 200
    assert second.extensions["store"].get("token") == "persisted"
    changed = create_app({**cfg, "ADMIN_PASSWORD": "new-password-12345"}, fake).test_client()
    changed.set_cookie("qinlin_admin", cookie, domain="door.example.test")
    assert changed.get("/api/status", base_url=ORIGIN).status_code == 401


@pytest.mark.parametrize("data", [{"communityId": True, "doorId": 1}, {"communityId": 1001, "doorId": "2001"}, {"communityId": -1, "doorId": 1}, {}])
def test_invalid_ids(env, data):
    _, client, _, _ = env
    response = client.post("/api/v1/doors/open", json=data, headers={"Authorization": "Bearer " + TOKEN})
    assert response.status_code == 400


def test_limits_persist_and_cleanup(tmp_path):
    first = Store(tmp_path)
    assert first.limit("sms", 1, 60) == 0
    assert Store(tmp_path).limit("sms", 1, 60) > 0


def test_import_validates_every_file_before_writing(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "qinlin_login.json").write_text(json.dumps({"code": 0, "data": {"sessionId": "imported-secret"}}))
    (source / "qinlin_community.json").write_text(json.dumps({"code": 0, "data": [{"communityId": 1001}]}))
    (source / "qinlin_doors.json").write_text(json.dumps({"code": 1, "data": {}}))
    store = Store(tmp_path / "database")
    with pytest.raises(ValueError):
        import_session(source, store)
    assert store.get("token") is None
    (source / "qinlin_doors.json").write_text(json.dumps({"code": 0, "data": {}}))
    import_session(source, store)
    assert store.get("token") == "imported-secret"


class Response:
    status_code = 200
    def __init__(self, payload):
        self.payload = payload
    def json(self):
        return self.payload


class Transport:
    def __init__(self, response=None, exception=None):
        self.response = response or Response({"code": 0, "data": []})
        self.exception = exception
        self.calls = []
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exception:
            raise self.exception
        return self.response


def test_wire_signing_uses_token_but_query_uses_sessionid():
    transport = Transport()
    QinlinClient(transport=transport).communities("demo-session")
    _, kwargs = transport.calls[0]
    assert kwargs["params"] == {"sessionId": "demo-session"}
    payload = kwargs["json"].copy()
    signature = payload.pop("sign")
    assert signature == sign({"token": "demo-session", **payload})
    assert "token" not in payload and "sessionId" not in payload
    assert payload["version"] == "4.9.3"
    assert isinstance(payload["timestamp"], int)
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize("payload,expected", [
    ({"code": 401}, "QINLIN_SESSION_EXPIRED"),
    ({"code": 999, "message": "请重新登录"}, "QINLIN_SESSION_EXPIRED"),
    ({"code": 403, "message": "没有开门权限"}, "QINLIN_REJECTED"),
    ({"code": 10001, "message": "请求参数错误"}, "QINLIN_REJECTED"),
])
def test_expiry_classification_is_conservative(payload, expected):
    with pytest.raises(ServiceError) as result:
        QinlinClient(transport=Transport(Response(payload))).communities("token")
    assert result.value.code == expected


def test_timeout_at_wire_level_is_one_request():
    transport = Transport(exception=requests.Timeout())
    with pytest.raises(ServiceError) as result:
        QinlinClient(transport=transport).open_door("token", 1001, 2001)
    assert result.value.code == "OPEN_RESULT_UNKNOWN"
    assert len(transport.calls) == 1


def test_transport_sends_login_encrypted_and_sms_separately():
    transport = Transport(Response({"code": 0, "data": {"sessionId": "new-token"}}))
    client = QinlinClient(transport=transport)
    assert client.login("13800000000", "123456") == "new-token"
    assert transport.calls[0][1]["json"]["mobile"] == encrypt_phone("13800000000")
    assert transport.calls[0][1]["params"] == {}
    client.send_sms("13800000000")
    url, kwargs = transport.calls[1]
    assert "gateway2.qinlinkeji.com" in url
    assert kwargs["headers"]["version"] == "v2"
    assert kwargs["headers"]["content-type"] == "application/json; charset=utf-8"
    prepared = requests.Request('POST', url, headers=kwargs['headers'], json=kwargs['json']).prepare()
    assert prepared.headers['Content-Type'] == 'application/json; charset=utf-8'
    # Case-insensitive lookup alone previously missed the actual wire casing bug.
    assert {'appid', 'nonce', 'timestamp', 'version', 'sign'} <= set(prepared.headers)
    assert kwargs["json"] == {"mobile": "13800000000"}


def test_deduplicate_actual_door_shapes():
    item = {"doorControlId": 1, "doorControlName": "门", "online": "online"}
    assert len(normalize_doors({"buildingDoorList": [item], "userDoorDTOS": [item]})) == 1


def test_security_headers_and_bad_requests(env):
    _, client, _, _ = env
    response = client.get("/")
    assert response.status_code == 200
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get("/api/not-exist").json["error"]["code"] == "HTTP_404"
    assert client.post("/api/v1/doors/open", data="x" * 9000, content_type="application/json", headers={"Authorization": "Bearer " + TOKEN}).status_code == 413


def test_admin_limit_and_expired_cookie(env):
    app, client, _, _ = env
    app.extensions["store"].create_session("expired-cookie", 1, "verifier")
    client.set_cookie("qinlin_admin", "expired-cookie", domain="door.example.test")
    assert client.get("/api/status", base_url=ORIGIN).status_code == 401
    for _ in range(20):
        assert post(client, "/api/auth/login", {}).status_code == 400
    assert post(client, "/api/auth/login", {}).status_code == 429


def test_insecure_public_deployment_refused(env):
    _, _, fake, cfg = env
    with pytest.raises(ValueError):
        create_app({**cfg, "PUBLIC_ORIGIN": "http://public.example.com"}, fake)
    with pytest.raises(ValueError):
        create_app({**cfg, "API_TOKEN": "short"}, fake)


def test_community_switch_and_explicit_default_survive_restart(env):
    app, client, fake, cfg = env
    fake.communities = lambda token: [{"id": 1001, "name": "示例小区 A"}, {"id": 1002, "name": "示例小区 B"}]
    app.extensions["store"].put("token", "fake-token")
    login(client)
    initial = client.get("/api/status", base_url=ORIGIN).json["data"]
    assert initial["communityId"] == 1001 and initial["defaultCommunityId"] is None
    assert post(client, "/api/refresh", {"communityId": 1002}).json["data"]["communityId"] == 1002
    assert app.extensions["store"].get("preferred_community_id") is None
    result = post(client, "/api/preferences/community", {"communityId": 1002})
    assert result.json["data"]["defaultCommunityId"] == 1002
    assert post(client, "/api/preferences/community", {"communityId": 9999}).status_code == 403
    restarted = create_app(cfg, fake)
    assert restarted.extensions["doors"].refresh()["communityId"] == 1002
    post(client, "/api/refresh", {"communityId": 1001})
    post(client, "/api/auth/logout", {})
    login(client)
    assert client.get("/api/status", base_url=ORIGIN).json["data"]["communityId"] == 1002
    fake.communities = lambda token: [{"id": 1001, "name": "示例小区 A"}]
    result = post(client, "/api/refresh", {}).json["data"]
    assert result["communityId"] == 1001 and result["defaultCommunityId"] is None


def test_no_community_disclosure_before_login_or_after_expiry(env):
    app, client, fake, _ = env
    app.extensions["store"].put("token", "fake-token")
    assert "测试小区" not in client.get("/").get_data(as_text=True)
    assert client.get("/api/status").status_code == 401
    assert post(client, "/api/preferences/community", {"communityId": 1001}).status_code == 401
    login(client)
    assert client.get("/api/status", base_url=ORIGIN).json["data"]["communities"]
    fake.failure = ServiceError("QINLIN_SESSION_EXPIRED", "凭证过期", 409)
    result = post(client, "/api/refresh", {}).json["data"]
    assert result["communities"] == [] and result["doors"] == []


def test_reauthentication_resets_account_preference(env):
    app, client, _, _ = env
    app.extensions["store"].put("preferred_community_id", 1002)
    login(client)
    post(client, "/api/qinlin/login", {"mobile": "13800000000", "code": "123456"})
    assert app.extensions["store"].get("preferred_community_id") is None


def test_expiry_at_open_response_clears_cached_communities(env):
    app, client, fake, _ = env
    app.extensions["store"].put("token", "fake-token")
    fake.open_failure = ServiceError("QINLIN_SESSION_EXPIRED", "登录失效", 409)
    assert external(client).status_code == 409
    snapshot = app.extensions["doors"].snapshot
    assert snapshot["state"] == "expired"
    assert snapshot["communities"] == [] and snapshot["doors"] == []


def scoped(client, token, cid=1001, did=2001):
    return client.post(f'/api/v1/communities/{cid}/doors/{did}/open',headers={'Authorization':'Bearer '+token})


def access(app):
    return {(x['communityId'],x['doorId']):x['token'] for x in app.extensions['store'].door_access({'state':'unknown'})}


def test_scoped_url_needs_no_body_and_has_door_isolation(env):
    app, client, fake, _=env
    app.extensions['store'].put('token','fake-session')
    token=access(app)[1001,2001]
    assert scoped(client,token,did=2002).status_code==401
    assert scoped(client,token,cid=1002).status_code==401
    assert scoped(client,TOKEN).status_code==401
    assert external(client,token=TOKEN).status_code==401
    assert client.get('/api/v1/communities/1001/doors/2001/open').status_code==405
    assert client.post('/api/v1/communities/1001/doors/2001/open?token='+token).status_code==401
    assert fake.opens==[]
    assert scoped(client,token).status_code==200
    assert fake.opens==[(1001,2001)]


def test_admin_discovery_only_and_persistent_tokens(env):
    app, client, fake, cfg=env
    app.extensions['store'].put('token','fake-session')
    login(client)
    data=client.get('/api/status',base_url=ORIGIN).json['data']
    entry=data['apiDoors'][0]
    assert entry['url']==ORIGIN+'/api/v1/communities/1001/doors/2001/open'
    assert len(entry['token'])>=40
    public=client.get('/api/v1/status',headers={'Authorization':'Bearer '+TOKEN})
    assert 'apiDoors' not in public.json['data']
    assert entry['token'].encode() not in public.data
    restarted=create_app(cfg,fake)
    assert access(restarted)==access(app)
    post(client,'/api/qinlin/login',{'mobile':'13800000000','code':'123456'})
    assert access(app)[1001,2001]==entry['token']


def test_single_reset_requires_confirmation_and_preserves_others(env):
    app,client,fake,_=env
    store=app.extensions['store']
    store.put('token','fake-session')
    store.door_access({'state':'valid','communityId':1002,'communities':[{'id':1002,'name':'示例二'}],'doors':[{'id':2002,'name':'示例门二'}]})
    before=access(app)
    data={'communityId':1001,'doorId':2001}
    assert post(client,'/api/door-tokens/reset',{**data,'confirm':True}).status_code==401
    login(client)
    for confirm in (None,False,'true',1):
        assert post(client,'/api/door-tokens/reset',{**data,'confirm':confirm}).status_code==400
    assert access(app)==before
    assert client.post('/api/door-tokens/reset',json={**data,'confirm':True},headers={'Origin':'https://evil.test'},base_url=ORIGIN).status_code==403
    assert post(client,'/api/door-tokens/reset',{**data,'confirm':True}).json['data']['count']==1
    after=access(app)
    assert after[1001,2001]!=before[1001,2001]
    assert after[1002,2002]==before[1002,2002]
    assert scoped(client,before[1001,2001]).status_code==401
    assert scoped(client,after[1001,2001]).status_code==200
    assert len(fake.opens)==1


def test_reset_all_across_communities_works_while_expired(env):
    app,client,fake,_=env
    store=app.extensions['store']
    store.put('token','expired-session')
    store.door_access({'state':'valid','communityId':1002,'communities':[{'id':1002,'name':'示例二'}],'doors':[{'id':2002,'name':'示例门二'}]})
    before=access(app)
    login(client)
    fake.failure=ServiceError('QINLIN_SESSION_EXPIRED','过期',409)
    assert client.get('/api/status',base_url=ORIGIN).json['data']['state']=='expired'
    assert post(client,'/api/door-tokens/reset',{'all':True}).status_code==400
    result=post(client,'/api/door-tokens/reset',{'all':True,'confirm':True})
    assert result.json['data']['count']==2
    after=access(app)
    for (cid,did),token in before.items():
        assert token!=after[cid,did]
        assert scoped(client,token,cid,did).status_code==401
    assert scoped(client,after[1001,2001]).status_code==409
    assert fake.opens==[] and fake.sms==[]


def test_revoked_upstream_permission_blocks_still_valid_door_token(env):
    app,client,fake,_=env
    app.extensions['store'].put('token','fake-session')
    token=access(app)[1001,2001]
    fake.doors=lambda *args: []
    assert scoped(client,token).status_code==403
    assert fake.opens==[]


def test_inventory_rename_keeps_url_token_and_silent(env):
    app,client,fake,_=env
    app.extensions['store'].put('token','session')
    login(client)
    first=client.get('/api/status',base_url=ORIGIN).json['data']
    fake.communities=lambda _: [{'id':1001,'name':'改名小区'}]
    fake.doors=lambda *_: [{'id':2001,'name':'改名门','online':'online','kind':'楼栋门'}]
    changed=post(client,'/api/refresh',{}).json['data']
    assert changed['apiDoors'][0]['url']==first['apiDoors'][0]['url']
    assert changed['apiDoors'][0]['token']==first['apiDoors'][0]['token']
    assert changed['apiDoors'][0]['doorName']=='改名门'
    assert changed['inventoryEvents']==[]


def test_changed_door_id_invalidates_old_and_persists_notice(env):
    app,client,fake,cfg=env
    app.extensions['store'].put('token','session')
    login(client)
    first=client.get('/api/status',base_url=ORIGIN).json['data']['apiDoors'][0]
    fake.doors=lambda *_:[{'id':2002,'name':'示例楼栋门','online':'online','kind':'楼栋门'}]
    changed=post(client,'/api/refresh',{}).json['data']
    old,new=changed['apiDoors']
    assert old['active']==0 and new['active']==1
    assert first['url']!=new['url'] and first['token']!=new['token']
    assert scoped(client,first['token']).status_code==410
    events=changed['inventoryEvents']
    assert len(events)==1 and events[0]['removed'][0]['id']==2001 and events[0]['added'][0]['id']==2002
    restarted=create_app(cfg,fake)
    assert restarted.extensions['store'].inventory_events()==events
    assert post(client,f"/api/inventory-events/{events[0]['id']}/acknowledge",{}).status_code==200
    assert app.extensions['store'].inventory_events()==[]
    assert post(client,'/api/refresh',{}).json['data']['inventoryEvents']==[]


def test_changed_community_id_never_reuses_token_even_same_name(env):
    app,client,fake,_=env
    app.extensions['store'].put('token','session')
    login(client)
    first=client.get('/api/status',base_url=ORIGIN).json['data']['apiDoors'][0]
    fake.communities=lambda _: [{'id':1002,'name':'测试小区'}]
    changed=post(client,'/api/refresh',{}).json['data']
    assert changed['communityId']==1002
    old,new=changed['apiDoors']
    assert old['active']==0 and new['active']==1
    assert old['token']!=new['token']
    assert scoped(client,first['token']).status_code==410
    assert changed['inventoryEvents'][0]['kind']=='communities'


def test_reappearance_rotates_token_and_notifies(env):
    app,client,fake,_=env
    app.extensions['store'].put('token','session')
    login(client)
    original=fake.doors
    first=client.get('/api/status',base_url=ORIGIN).json['data']['apiDoors'][0]
    fake.doors=lambda *_:[]
    post(client,'/api/refresh',{})
    fake.doors=original
    returned=post(client,'/api/refresh',{}).json['data']
    assert returned['apiDoors'][0]['active']==1
    assert returned['apiDoors'][0]['token']!=first['token']
    assert scoped(client,first['token']).status_code==401
    assert any(x['kind']=='reactivated' for x in returned['inventoryEvents'])


def test_network_failure_preserves_persistent_inventory_and_no_false_notice(env):
    app,client,fake,cfg=env
    app.extensions['store'].put('token','session')
    login(client)
    first=client.get('/api/status',base_url=ORIGIN).json['data']
    fake.failure=ServiceError('UPSTREAM_UNAVAILABLE','网络异常',503)
    restarted=create_app(cfg,fake)
    other=restarted.test_client()
    login(other)
    cached=other.get('/api/status',base_url=ORIGIN).json['data']
    assert cached['state']=='unknown'
    assert cached['cachedInventory']['doors']==first['doors']
    assert cached['inventoryEvents']==[]
    assert cached['apiDoors'][0]['token']==first['apiDoors'][0]['token']
    assert cached['apiDoors'][0]['active']==1


def test_inventory_notice_ack_requires_login_and_origin(env):
    _,client,_,_=env
    assert post(client,'/api/inventory-events/1/acknowledge',{}).status_code==401
    login(client)
    assert client.post('/api/inventory-events/1/acknowledge',json={},base_url=ORIGIN,headers={'Origin':'https://evil.test'}).status_code==403


def test_existing_token_schema_migration_preserves_credentials(tmp_path):
    import sqlite3
    with sqlite3.connect(tmp_path/'qinlin.sqlite3') as db:
        db.execute('CREATE TABLE door_tokens (community_id INTEGER, door_id INTEGER, community_name TEXT, door_name TEXT, token TEXT, updated REAL, PRIMARY KEY (community_id,door_id))')
        db.execute("INSERT INTO door_tokens VALUES(1001,2001,'示例小区','示例门','old-persistent-token',1)")
    store=Store(tmp_path)
    assert store.valid_door_token(1001,2001,'Bearer old-persistent-token')
    store.sync_inventory([{'id':1001,'name':'示例小区'}])
    store.sync_inventory([{'id':1001,'name':'示例小区'}],1001,[{'id':2001,'name':'示例门'}])
    assert store.inventory_events()==[]
    assert store.valid_door_token(1001,2001,'Bearer old-persistent-token')
