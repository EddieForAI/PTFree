# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import contextlib
import json
import sqlite3
import time

from .common import AppError, REQUEST_LABELS, new_id


class Store:
    def __init__(self, home, local_limits=True, background=False):
        self.path = home / 'state.sqlite3'
        self.local_limits = local_limits
        self.background = background

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def init(self):
        with self.connect() as d:
            d.execute('PRAGMA journal_mode=WAL')
            d.executescript('''
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS torrents(
              id TEXT PRIMARY KEY, name TEXT, size INTEGER, meta TEXT,
              state TEXT NOT NULL DEFAULT 'seen', reason TEXT DEFAULT '',
              attempts INTEGER DEFAULT 0, retry_at REAL DEFAULT 0,
              first_seen REAL, updated REAL, completed REAL DEFAULT 0,
              hashes TEXT DEFAULT '', target TEXT DEFAULT '', temp TEXT DEFAULT '');
            CREATE INDEX IF NOT EXISTS torrent_state ON torrents(state,retry_at);
            CREATE TABLE IF NOT EXISTS freeseed_managed (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS foreground_intents (hash TEXT PRIMARY KEY, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, level TEXT, message TEXT);
            CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, ts REAL, kind TEXT);
            CREATE INDEX IF NOT EXISTS request_ts ON requests(ts);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, kind TEXT, state TEXT,
              created REAL, started REAL DEFAULT 0, finished REAL DEFAULT 0, result TEXT DEFAULT '{}');
            ''')
            d.execute("UPDATE torrents SET state='pending',reason='等待保存种子文件',retry_at=0 "
                      "WHERE state='submitting'")
            d.execute("UPDATE torrents SET state='waiting_quota' WHERE state='failed' AND temp='' "
                      "AND reason='已达到本程序的站点请求配额，等待窗口恢复'")
            d.execute("""UPDATE torrents
                         SET state='pending', reason='等待重新下载种子文件', attempts=0, retry_at=0
                         WHERE state IN ('failed','exhausted') AND (
                           reason='种子下载地址不属于 M-Team HTTPS 域名，已停止请求'
                           OR reason LIKE '种子下载链接不是受信任的 M-Team 域名，已拦截%'
                           OR reason LIKE '种子下载跳转不是受信任的 M-Team 域名，已拦截%'
                           OR reason LIKE '种子下载链接使用了不支持的协议或端口，已拦截%'
                           OR reason LIKE '种子下载跳转使用了不支持的协议或端口，已拦截%'
                         )""")
            d.execute("UPDATE jobs SET state='cancelled',finished=?,result=? "
                      "WHERE kind='test-qb' AND state IN ('queued','running')",
                      (time.time(), json.dumps({'message':'旧版操作已取消'}, ensure_ascii=False)))

    def get(self, key, default=None):
        with self.connect() as d:
            row = d.execute('SELECT value FROM kv WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.connect() as d:
            d.execute('INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(value)))

    def event(self, message, level='info'):
        with self.connect() as d:
            d.execute('INSERT INTO events(ts,level,message) VALUES(?,?,?)',(time.time(),level,message[:2000]))
            d.execute('DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0)-2000 FROM events)')

    def queue_job(self, kind):
        jid = new_id()
        with self.connect() as d:
            d.execute('BEGIN IMMEDIATE')
            if d.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= 5:
                raise AppError('操作队列已满，请等待当前任务结束', 'busy')
            d.execute('INSERT INTO jobs(id,kind,state,created) VALUES(?,?,?,?)',(jid,kind,'queued',time.time()))
        return jid

    def finish_job(self, jid, result, state='done'):
        with self.connect() as d:
            d.execute('UPDATE jobs SET state=?,finished=?,result=? WHERE id=?',(state,time.time(),json.dumps(result,ensure_ascii=False),jid))

    def job(self, jid):
        with self.connect() as d:
            row = d.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
        if row:
            v = dict(row); v['result'] = json.loads(v['result']); return v

    def set_state(self, tid, state, reason='', **fields):
        allowed = {'attempts','retry_at','completed','hashes','target','temp'}
        if set(fields)-allowed:
            raise ValueError('bad field')
        fields.update(state=state, reason=reason, updated=time.time())
        with self.connect() as d:
            d.execute('UPDATE torrents SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),tid))

    def observe(self, t, state, reason):
        n = time.time()
        with self.connect() as d:
            d.execute('''INSERT INTO torrents(id,name,size,meta,state,reason,first_seen,updated)
              VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
              name=excluded.name,size=excluded.size,meta=excluded.meta,updated=excluded.updated,
              state=CASE WHEN torrents.state IN ('done','publishing','failed','exhausted','site_existing','waiting_quota') THEN torrents.state ELSE excluded.state END,
              reason=CASE WHEN torrents.state IN ('done','publishing','failed','exhausted','site_existing','waiting_quota') THEN torrents.reason ELSE excluded.reason END''',
                      (t['id'],t['name'],t['size'],json.dumps(t,ensure_ascii=False),state,reason,n,n))

    def sync_history_account(self, fingerprint):
        if self.get('history_account') == fingerprint:
            return
        with self.connect() as d:
            d.execute("UPDATE torrents SET state='pending',reason='等待核对新令牌的站点历史',"
                      "attempts=0,retry_at=0 WHERE state='site_existing'")
            d.execute("INSERT INTO kv VALUES('history_account',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (json.dumps(fingerprint),))

    def reserve_request(self, kind):
        n = time.time()
        limits = {'search': [(86400,900)], 'detail': [(3600,80)], 'history': [(3600,60),(86400,900)],
                  'token': [(3600,80),(86400,800)], 'download': [(3600,80),(86400,800)], 'profile': [(3600,30)]}
        with self.connect() as d:
            d.execute('BEGIN IMMEDIATE')
            def kv(k, default):
                r=d.execute('SELECT value FROM kv WHERE key=?',(k,)).fetchone()
                return json.loads(r[0]) if r else default
            blocked = kv('blocked_until',0)
            if blocked > n:
                raise AppError('站点请求处于冷却期', 'rate', blocked-n, source='active')
            if self.background and d.execute(
                "SELECT 1 FROM jobs WHERE state IN ('queued','running') LIMIT 1"
            ).fetchone():
                raise AppError('Freeseed 正在访问站点，后台任务让道', 'rate', 10, source='priority')
            available_at = n
            for window, limit in limits.get(kind, []) if self.local_limits or self.background else ():
                if self.background:
                    limit = limit * 3 // 4
                rows = d.execute('SELECT ts FROM requests WHERE kind=? AND ts>? ORDER BY ts',(kind,n-window)).fetchall()
                if len(rows) >= limit:
                    available_at = max(available_at, rows[-limit][0]+window)
            if available_at > n:
                raise AppError(f'等待本地配额（{REQUEST_LABELS[kind]}）', 'rate', available_at-n,
                               stage=kind, source='quota')
            # Torrent work is paced by the worker before the detail request.
            pacing_key = None if kind in ('detail','token','download') else 'last_request'
            planned = n
            if pacing_key:
                planned = max(n, kv(pacing_key,0)+3)
                d.execute('INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                          (pacing_key,json.dumps(planned)))
            d.execute('INSERT INTO requests(ts,kind) VALUES(?,?)',(planned,kind))
            d.execute('DELETE FROM requests WHERE ts<?',(n-172800,))
        if planned > n:
            time.sleep(planned-n)

    def cooldown(self, seconds, source='site', stage=None):
        with self.connect() as d:
            d.execute('BEGIN IMMEDIATE')
            r=d.execute("SELECT value FROM kv WHERE key='blocked_until'").fetchone()
            current = float(json.loads(r[0])) if r else 0
            end = time.time()+seconds
            if end <= current:
                return current
            d.executemany("INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          [('blocked_until', json.dumps(end)),
                           ('cooldown_reason', json.dumps({'source': source, 'stage': stage}))])
            return end

    def pace_torrent(self):
        with self.connect() as d:
            d.execute('BEGIN IMMEDIATE')
            n = time.time()
            row = d.execute("SELECT value FROM kv WHERE key='last_torrent_start'").fetchone()
            planned = max(n, json.loads(row[0])+3) if row else n
            d.execute("INSERT INTO kv VALUES('last_torrent_start',?) ON CONFLICT(key) "
                      "DO UPDATE SET value=excluded.value",(json.dumps(planned),))
        if planned > n:
            time.sleep(planned-n)

    def defer_rate(self, error):
        if error.source in ('http_429', 'site_rate', 'active'):
            return self.get('blocked_until',0)
        return self.cooldown(error.retry_after or 900, error.source or 'site_rate', error.stage)

    def release_local_wait(self):
        with self.connect() as d:
            d.execute('BEGIN IMMEDIATE')
            queued = d.execute("""UPDATE torrents SET state='pending', retry_at=0, reason='等待处理'
                                  WHERE state='waiting_quota'""").rowcount
            reason = d.execute("SELECT value FROM kv WHERE key='cooldown_reason'").fetchone()
            if reason:
                is_local = json.loads(reason[0]).get('source') == 'quota'
            else:
                # Old releases logged the cause but only saved the deadline.
                event = d.execute("""SELECT message FROM events WHERE
                    message LIKE '%已达到本程序的站点请求配额，等待窗口恢复' OR
                    message LIKE '%M-Team 请求限流，已进入冷却期' OR
                    message LIKE '%下载接口限流' OR
                    message LIKE '%API 请求过于频繁，冷却 15 分钟'
                    ORDER BY id DESC LIMIT 1""").fetchone()
                is_local = bool(event and event[0].endswith('已达到本程序的站点请求配额，等待窗口恢复'))
            cleared = d.execute("DELETE FROM kv WHERE key IN ('blocked_until','cooldown_reason')").rowcount if is_local else 0
            return bool(queued or cleared)

    def history(self, page=1, state=''):
        where=' WHERE state=?' if state else ''
        args=(state,) if state else ()
        with self.connect() as d:
            total=d.execute('SELECT COUNT(*) FROM torrents'+where,args).fetchone()[0]
            rows=d.execute('''SELECT id,name,size,state,reason,attempts,first_seen,updated,completed
                FROM torrents'''+where+'''
                ORDER BY CASE state WHEN 'done' THEN COALESCE(completed,0)
                                    ELSE COALESCE(first_seen,0) END DESC, id DESC
                LIMIT 50 OFFSET ?''',(*args,(page-1)*50)).fetchall()
        return {'total':total,'page':page,'items':[dict(r) for r in rows]}

    def stats(self):
        with self.connect() as d:
            counts={r[0]:r[1] for r in d.execute('SELECT state,COUNT(*) FROM torrents GROUP BY state')}
            requests={r[0]:r[1] for r in d.execute('SELECT kind,COUNT(*) FROM requests WHERE ts>? GROUP BY kind',(time.time()-86400,))}
            events=[dict(r) for r in d.execute('SELECT * FROM events ORDER BY id DESC LIMIT 100')]
        return {'counts':counts,'requests_24h':requests,'events':events}
