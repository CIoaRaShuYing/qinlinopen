import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, directory):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "qinlin.sqlite3"
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, expires REAL NOT NULL, verifier TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS limits (key TEXT PRIMARY KEY, start REAL NOT NULL, count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, time REAL NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, result TEXT NOT NULL);
            ''')
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))

    def create_session(self, token, expires, verifier):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires < ? OR verifier != ?", (time.time(), verifier))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (digest(token), expires, verifier))

    def authenticated(self, token, verifier):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM sessions WHERE token_hash=? AND expires>? AND verifier=?", (digest(token), time.time(), verifier)).fetchone())

    def revoke(self, token):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(token),))

    def limit(self, key, maximum, window):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM limits WHERE start < ?", (now - 86400,))
            row = db.execute("SELECT start, count FROM limits WHERE key=?", (key,)).fetchone()
            if row and now - row[0] < window:
                if row[1] >= maximum:
                    return max(1, int(window - (now - row[0])) + 1)
                db.execute("UPDATE limits SET count=count+1 WHERE key=?", (key,))
            else:
                db.execute("INSERT OR REPLACE INTO limits VALUES (?, ?, 1)", (key, now))
        return 0

    def audit(self, action, target, result):
        with self.connect() as db:
            db.execute("INSERT INTO audit(time,action,target,result) VALUES (?,?,?,?)", (time.time(), action, str(target), result))
            db.execute("DELETE FROM audit WHERE id NOT IN (SELECT id FROM audit ORDER BY id DESC LIMIT 500)")

    def recent(self):
        with self.connect() as db:
            return [dict(zip(("time", "action", "target", "result"), row)) for row in db.execute("SELECT time,action,target,result FROM audit ORDER BY id DESC LIMIT 12")]
