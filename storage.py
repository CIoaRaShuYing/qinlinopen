import hashlib
import hmac
import json
import os
import secrets
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
                CREATE TABLE IF NOT EXISTS door_tokens (community_id INTEGER NOT NULL, door_id INTEGER NOT NULL, community_name TEXT NOT NULL, door_name TEXT NOT NULL, token TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY (community_id, door_id));
                CREATE TABLE IF NOT EXISTS inventory_events (id INTEGER PRIMARY KEY, time REAL NOT NULL, detail TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0);
            ''')
            if 'active' not in {row[1] for row in db.execute('PRAGMA table_info(door_tokens)')}:
                db.execute('ALTER TABLE door_tokens ADD COLUMN active INTEGER NOT NULL DEFAULT 1')
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

    def door_access(self, snapshot):
        with self.connect() as db:
            if snapshot['state'] == 'valid' and snapshot.get('communityId') is not None:
                community_id = snapshot['communityId']
                community_name = next(c['name'] for c in snapshot['communities'] if c['id'] == community_id)
                for door in snapshot['doors']:
                    db.execute('''INSERT INTO door_tokens(community_id,door_id,community_name,door_name,token,updated) VALUES (?,?,?,?,?,?)
                        ON CONFLICT(community_id,door_id) DO UPDATE SET community_name=excluded.community_name,door_name=excluded.door_name''',
                        (community_id, door['id'], community_name, door['name'], secrets.token_urlsafe(32), time.time()))
            return [dict(zip(('communityId','doorId','communityName','doorName','token','updatedAt','active'), row))
                    for row in db.execute('SELECT community_id,door_id,community_name,door_name,token,updated,active FROM door_tokens ORDER BY community_id,door_id')]

    def valid_door_token(self, community_id, door_id, supplied):
        with self.connect() as db:
            row = db.execute('SELECT token FROM door_tokens WHERE community_id=? AND door_id=? AND active=1', (community_id, door_id)).fetchone()
            return bool(row and hmac.compare_digest(supplied.encode(), ('Bearer ' + row[0]).encode()))

    def retired_door_token(self, community_id, door_id, supplied):
        with self.connect() as db:
            row = db.execute('SELECT token FROM door_tokens WHERE community_id=? AND door_id=? AND active=0',(community_id,door_id)).fetchone()
            return bool(row and hmac.compare_digest(supplied.encode(),('Bearer '+row[0]).encode()))

    def reset_door_tokens(self, community_id=None, door_id=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT community_id,door_id FROM door_tokens' +
                              (' WHERE community_id=? AND door_id=?' if community_id is not None else ''),
                              (community_id, door_id) if community_id is not None else ()).fetchall()
            for cid, did in rows:
                db.execute('UPDATE door_tokens SET token=?,updated=? WHERE community_id=? AND door_id=?',
                           (secrets.token_urlsafe(32), time.time(), cid, did))
            return len(rows)

    def sync_inventory(self, communities, community_id=None, doors=None):
        """Only call for a successful authoritative response; never infer identity from names."""
        key = 'inventory_communities' if community_id is None else f'inventory_doors:{community_id}'
        items = communities if community_id is None else doors
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cached = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            if cached:
                old = json.loads(cached[0])
            elif community_id is None:
                old = [{'id':row[0],'name':row[1]} for row in db.execute('SELECT DISTINCT community_id,community_name FROM door_tokens WHERE active=1')]
            else:
                old = [{'id':row[0],'name':row[1]} for row in db.execute('SELECT door_id,door_name FROM door_tokens WHERE community_id=? AND active=1',(community_id,))]
            previous = {item['id']:item for item in old}
            latest = {item['id']:item for item in items}
            removed = [previous[i] for i in previous.keys()-latest.keys()]
            added = [latest[i] for i in latest.keys()-previous.keys()]
            if community_id is None and not cached:
                # Tokens from the older version only prove previously visited communities.
                added = []
            # An entirely new installation is a baseline, not an alarming change.
            if (cached or old) and (removed or added):
                detail = {'kind':'communities' if community_id is None else 'doors','communityId':community_id,
                          'removed':[{'id':x['id'],'name':x['name']} for x in removed],
                          'added':[{'id':x['id'],'name':x['name']} for x in added]}
                db.execute('INSERT INTO inventory_events(time,detail) VALUES (?,?)',(time.time(),json.dumps(detail,ensure_ascii=False)))
            for item in removed:
                if community_id is None:
                    db.execute('UPDATE door_tokens SET active=0 WHERE community_id=?',(item['id'],))
                else:
                    db.execute('UPDATE door_tokens SET active=0 WHERE community_id=? AND door_id=?',(community_id,item['id']))
            if community_id is not None:
                for door in doors:
                    row = db.execute('SELECT active FROM door_tokens WHERE community_id=? AND door_id=?',(community_id,door['id'])).fetchone()
                    if row and not row[0]:
                        db.execute('UPDATE door_tokens SET active=1,token=?,updated=? WHERE community_id=? AND door_id=?',
                                   (secrets.token_urlsafe(32),time.time(),community_id,door['id']))
                        db.execute('INSERT INTO inventory_events(time,detail) VALUES (?,?)',(time.time(),json.dumps({'kind':'reactivated','communityId':community_id,'doorId':door['id'],'doorName':door['name']})))
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(items,ensure_ascii=False)))

    def inventory_events(self):
        with self.connect() as db:
            return [{'id':row[0],'time':row[1],**json.loads(row[2])} for row in db.execute('SELECT id,time,detail FROM inventory_events WHERE acknowledged=0 ORDER BY id DESC')]

    def acknowledge_event(self, event_id):
        with self.connect() as db:
            db.execute('UPDATE inventory_events SET acknowledged=1 WHERE id=?',(event_id,))
