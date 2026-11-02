"""Offline setup/import utilities. Never send SMS or open a door."""
import argparse
import getpass
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from werkzeug.security import generate_password_hash
from storage import Store


def import_session(directory, store):
    root = Path(directory)
    login = json.loads((root / "qinlin_login.json").read_text(encoding="utf-8-sig"))
    community = json.loads((root / "qinlin_community.json").read_text(encoding="utf-8-sig"))
    doors = json.loads((root / "qinlin_doors.json").read_text(encoding="utf-8-sig"))
    token = login.get("data", {}).get("sessionId")
    if str(login.get("code")) != "0" or not isinstance(token, str) or not token.strip():
        raise ValueError("登录文件没有有效的 sessionId 字段")
    if str(community.get("code")) != "0" or not isinstance(community.get("data"), list):
        raise ValueError("小区文件格式异常")
    if str(doors.get("code")) != "0" or not isinstance(doors.get("data"), dict):
        raise ValueError("门列表文件格式异常")
    with store.connect() as db:
        db.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)", [("token", json.dumps(token)), ("preferred_community_id", "null")])


def configure(path):
    if path.exists():
        raise ValueError("配置文件已存在；请编辑现有文件，避免覆盖凭据")
    origin = input("HTTPS Origin（例如 https://door.example.com）：").strip().rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username or any(c in origin for c in "'\n\r"):
        raise ValueError("请输入有效的 HTTPS Origin，不包含路径")
    username = input("管理员账号 [admin]：").strip() or "admin"
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
        raise ValueError("账号只支持英文字母、数字、下划线、点和连字符")
    password = getpass.getpass("管理员密码（至少 12 位）：")
    if len(password) < 12 or password != getpass.getpass("再次输入密码："):
        raise ValueError("密码过短或两次输入不一致")
    content = (f"PUBLIC_ORIGIN='{origin}'\nADMIN_USERNAME='{username}'\n"
               f"ADMIN_PASSWORD_HASH='{generate_password_hash(password)}'\n"
               f"API_TOKEN='{secrets.token_urlsafe(32)}'\n"
               "SESSION_TTL_HOURS=720\nHOST_PORT=8088\n")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
        file.write(content)
    print("配置已生成。API Token 位于 .env；请妥善保管，不要提交此文件。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("configure")
    setup.add_argument("--output", default=".env")
    imp = commands.add_parser("import-session")
    imp.add_argument("--session-dir", required=True)
    imp.add_argument("--data-dir", default=os.getenv("DATA_DIR", "data"))
    args = parser.parse_args()
    try:
        if args.command == "configure":
            configure(Path(args.output))
        else:
            import_session(args.session_dir, Store(args.data_dir))
            print("会话已导入；未联网验证、未发送短信、未开门。请重启服务后刷新状态。")
    except (ValueError, KeyError, TypeError, OSError):
        # File contents and credentials must not appear in CLI tracebacks.
        parser.exit(1, "操作失败：请检查文件格式、输入值、文件是否已存在及目录读写权限。\n")


if __name__ == "__main__":
    main()
