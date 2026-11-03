import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qinlin import APPID, APP_SECRET, QinlinClient, encrypt_phone


@pytest.mark.parametrize('operation', ['sms', 'login', 'communities', 'doors', 'open'])
def test_all_gateway_requests_preserve_lowercase_headers_and_payload_protocol(monkeypatch, operation):
    captured = []
    def intercept(adapter, request, **kwargs):
        captured.append(request)
        data = {'login':{'sessionId':'demo-login-session'},'communities':[],'doors':{},'sms':None,'open':None}[operation]
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({'code':0,'data':data}).encode()
        response.request = request
        return response
    # Exercise Requests' real preparation and session header merge, but never access a network.
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', intercept)
    session = requests.Session()
    session.trust_env = False
    client = QinlinClient(transport=session)
    calls = {
        'sms': lambda: client.send_sms('13800000000'),
        'login': lambda: client.login('13800000000','123456'),
        'communities': lambda: client.communities('demo-session'),
        'doors': lambda: client.doors('demo-session',1001),
        'open': lambda: client.open_door('demo-session',1001,2001),
    }
    calls[operation]()
    assert len(captured) == 1
    request = captured[0]
    keys = set(request.headers)
    required = {'qvendor','openid','qversioncode','qversionname','qchannel','qplatform','user-agent','content-type'}
    assert required <= keys
    assert all(key == key.lower() for key in keys if key.lower() in required | {'appid','nonce','timestamp','version','sign'})
    query = {key:values[0] for key,values in parse_qs(urlsplit(request.url).query).items()}
    if operation == 'sms':
        assert {'appid','nonce','timestamp','version','sign'} <= keys
        assert json.loads(request.body) == {'mobile':'13800000000'}
        assert query == {}
        fields = {'appid':APPID,'mobile':'13800000000','nonce':request.headers['nonce'],
                  'timestamp':request.headers['timestamp'],'version':'v2'}
        expected = hashlib.md5(('&'.join(f'{k}={v}' for k,v in sorted(fields.items()))+'&appsecret='+APP_SECRET).encode()).hexdigest().upper()
        assert request.headers['sign'] == expected
    else:
        if operation == 'open':
            assert request.body is None
            fields = query.copy()
            assert fields.pop('sessionId') == 'demo-session'
            assert fields['doorControlId'] == '2001' and fields['appChannel'] == '1'
        elif operation == 'doors':
            fields = {key:values[0] for key,values in parse_qs(request.body).items()}
            assert fields['communityId'] == '1001'
            assert request.headers['content-type'] == 'application/x-www-form-urlencoded'
        else:
            fields = json.loads(request.body)
        signature = fields.pop('sign')
        fields['token'] = '' if operation == 'login' else 'demo-session'
        expected = hashlib.md5(('&'.join(f'{k}={v}' for k,v in sorted(fields.items()))+'&key=qiAnlPinP').encode()).hexdigest().upper()
        assert signature == expected
        if operation == 'login':
            assert query == {}
            assert fields['mobile'] == encrypt_phone('13800000000')
            assert fields['smsCode'] == '123456' and fields['appChannel'] == 1
        elif operation != 'open':
            assert query == {'sessionId':'demo-session'}
    session.close()


def test_request_boundary_normalizes_mixed_header_names(monkeypatch):
    sent = []
    def intercept(adapter, request, **kwargs):
        sent.append(request)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"code":0}'
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter,'send',intercept)
    session = requests.Session()
    session.trust_env = False
    QinlinClient(transport=session)._request('https://example.test/api',headers={'Appid':'example','SIGN':'dummy','Content-Type':'application/json'},json={})
    assert {'appid','sign','content-type'} <= set(sent[0].headers)
    assert not {'Appid','SIGN','Content-Type'} & set(sent[0].headers)
    session.close()
