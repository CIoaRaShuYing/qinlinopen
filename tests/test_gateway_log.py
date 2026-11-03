import json
import os
from pathlib import Path
import sys

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway_log import GatewayLog
from qinlin import QinlinClient, ServiceError


class Transport:
    def __init__(self, body='', status=200, error=None, headers=None):
        self.calls = 0
        self.error = error
        self.response = requests.Response()
        self.response.status_code = status
        self.response._content = body.encode('utf-8')
        self.response.encoding = 'utf-8'
        self.response.headers.update(headers or {})

    def post(self, *args, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('Real network is forbidden in tests')
    monkeypatch.setattr(requests.Session, 'request', blocked)


@pytest.mark.parametrize('status,body,expected', [
    (403, '<html>gateway blocked</html>', 'UPSTREAM_UNAVAILABLE'),
    (503, 'maintenance', 'UPSTREAM_UNAVAILABLE'),
    (302, 'redirected', 'UPSTREAM_UNAVAILABLE'),
    (200, '{broken json', 'UPSTREAM_FORMAT'),
    (200, '{"code":403,"message":"denied"}', 'QINLIN_REJECTED'),
])
def test_every_error_response_is_logged_before_classification(tmp_path, status, body, expected):
    log = GatewayLog(tmp_path)
    transport = Transport(body, status)
    client = QinlinClient(transport=transport, response_log=log)
    with pytest.raises(ServiceError) as error:
        client.communities('test-session')
    assert error.value.code == expected
    event = json.loads(log.path.read_text(encoding='utf-8'))
    assert event['status'] == status
    assert event['body'] == (json.loads(body) if body.startswith('{"') else body)
    assert '?' not in event['endpoint']
    assert event['durationMs'] >= 0
    assert transport.calls == 1
    log.handler.close()


def test_success_records_full_body_and_redacts_nested_echoed_credentials(tmp_path, capsys):
    token = 'response-secret-token'
    body = {'code':0,'data':{'sessionId':token,'nested':[{'phone':'13800000000','sms_code':'123456'}],
                          'message':'request-secret echoed '+token,'large':'x'*30000,'doors':[{'id':2001}]}}
    log = GatewayLog(tmp_path)
    transport = Transport(json.dumps(body), headers={'Set-Cookie':'private-cookie','X-Request-ID':'trace-123'})
    result = QinlinClient(transport=transport,response_log=log)._request(
        'https://example.test/login?sessionId=request-secret',params={'sessionId':'request-secret'})
    assert result == body
    raw = log.path.read_text(encoding='utf-8')
    event = json.loads(raw)
    for secret in (token,'request-secret','13800000000','123456','private-cookie'):
        assert secret not in raw
    assert event['body']['data']['large'] == 'x'*30000
    assert event['body']['data']['doors'] == [{'id':2001}]
    assert event['headers']['X-Request-ID'] == 'trace-123'
    assert event['headers']['Set-Cookie'] == '[REDACTED]'
    stdout = capsys.readouterr().out
    assert token not in stdout and 'large' not in stdout
    log.handler.close()


def test_non_json_redaction_and_timeout_details(tmp_path):
    log = GatewayLog(tmp_path)
    transport = Transport('phone=13800000000 sessionId=opaque-secret Authorization: Bearer abcde',403)
    client = QinlinClient(transport=transport,response_log=log)
    with pytest.raises(ServiceError):
        client.send_sms('13800000000')
    raw = log.path.read_text(encoding='utf-8')
    assert all(secret not in raw for secret in ('13800000000','opaque-secret','abcde'))
    timeout = Transport(error=requests.Timeout('secret-url?sessionId=never-log-this'))
    client = QinlinClient(transport=timeout,response_log=log)
    with pytest.raises(ServiceError) as error:
        client.open_door('session-secret',1001,2001)
    assert error.value.code == 'OPEN_RESULT_UNKNOWN'
    event = json.loads(log.path.read_text(encoding='utf-8').splitlines()[-1])
    assert event['exceptionType'] == 'Timeout' and event['status'] is None
    assert 'never-log-this' not in str(event)
    assert timeout.calls == 1
    log.handler.close()


def test_rotation_persistence_permissions_and_logging_failure(tmp_path, monkeypatch):
    log = GatewayLog(tmp_path,max_bytes=450,backups=2)
    client = QinlinClient(transport=Transport('{"code":0}'),response_log=log)
    for _ in range(8):
        assert client._request('https://example.test/open',opening=True) == {'code':0}
    files = list(log.path.parent.glob('qinlin-gateway.log*'))
    assert len(files) == 3
    if os.name != 'nt':
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in files)
        assert log.path.parent.stat().st_mode & 0o777 == 0o700
    log.handler.close()
    restarted = GatewayLog(tmp_path,max_bytes=450,backups=2)
    assert restarted.path.read_text(encoding='utf-8')
    def fail(*args, **kwargs):
        raise OSError('full disk')
    monkeypatch.setattr(restarted.logger,'info',fail)
    transport = Transport('{"code":0}')
    result = QinlinClient(transport=transport,response_log=restarted)._request('https://example.test/open',opening=True)
    assert result == {'code':0} and transport.calls == 1
    restarted.handler.close()
