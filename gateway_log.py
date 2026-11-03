"""Private, rotating diagnostics for every Qinlin upstream response."""
import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote, urlsplit

MASK = '[REDACTED]'
SENSITIVE = re.compile(r'token|session|password|passwd|secret|authorization|cookie|mobile|phone|smscode|securitycode|verifycode|verificationcode|openid|^sign$|验证码|手机号', re.I)
ASSIGNMENT = re.compile(r'''(?i)(?<![\w-])((?:[\w-]*(?:token|session|password|secret|mobile|phone|smscode|securitycode|verifycode)[\w-]*|sign|authorization|cookie)["']?\s*[:=]\s*["']?)([^\s"'<>&,;]+)''')


def sensitive(key):
    return bool(SENSITIVE.search(re.sub(r'[-_\s]', '', str(key))))


def secret_values(value):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if sensitive(key) and isinstance(item, (str, int)) and str(item):
                found.add(str(item))
            found.update(secret_values(item))
    elif isinstance(value, list):
        for item in value:
            found.update(secret_values(item))
    return found


def clean_text(value, secrets):
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, MASK).replace(quote(secret, safe=''), MASK)
    value = re.sub(r'(?i)\bBearer\s+[^\s"<>,;]+', 'Bearer '+MASK, value)
    value = ASSIGNMENT.sub(lambda match: match[1]+MASK, value)
    return re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', MASK, value)


def clean(value, secrets):
    if isinstance(value, dict):
        return {key: MASK if sensitive(key) else clean(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item, secrets) for item in value]
    return clean_text(value, secrets) if isinstance(value, str) else value


class PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        descriptor = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        if os.name != 'nt':
            os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, self.mode, encoding=self.encoding)

    def handleError(self, record):
        # Never let logging print response content in an exception traceback.
        print('Qinlin gateway diagnostic log write failed', file=sys.stderr)


class GatewayLog:
    def __init__(self, data_dir, max_bytes=10*1024*1024, backups=5):
        directory = Path(data_dir) / 'logs'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != 'nt':
            directory.chmod(0o700)
        self.path = directory / 'qinlin-gateway.log'
        self.handler = PrivateRotatingHandler(self.path, maxBytes=max_bytes, backupCount=backups, encoding='utf-8', delay=True)
        self.handler.setFormatter(logging.Formatter('%(message)s'))
        self.logger = logging.Logger('qinlin.gateway.private', logging.INFO)
        self.logger.addHandler(self.handler)

    def record(self, url, started, request_fields, response=None, error=None):
        try:
            parts = urlsplit(url)
            event = {
                'time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'method': 'POST', 'endpoint': parts.scheme+'://'+parts.netloc+parts.path,
                'durationMs': round((time.monotonic()-started)*1000),
                'status': response.status_code if response is not None else None,
            }
            if error is not None:
                event['exceptionType'] = type(error).__name__
            if response is not None:
                headers = dict(response.headers)
                body = response.text
                try:
                    parsed = json.loads(body)
                except ValueError:
                    parsed = None
                secrets = secret_values(request_fields) | secret_values(headers) | secret_values(parsed)
                event['responseBytes'] = len(response.content)
                event['headers'] = clean(headers, secrets)
                event['bodyFormat'] = 'json' if parsed is not None else 'text'
                event['body'] = clean(parsed, secrets) if parsed is not None else clean_text(body, secrets)
            self.logger.info(json.dumps(event, ensure_ascii=False))
            # Docker logs provide a safe overview; full responses stay in the private file.
            print(json.dumps({key:value for key,value in event.items() if key not in ('headers','body')}, ensure_ascii=False), flush=True)
        except Exception:
            # A logging failure must not change an accepted door command into a retryable failure.
            print('Qinlin gateway diagnostic log write failed', file=sys.stderr)
