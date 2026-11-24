# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import math
import re
import time
from collections import deque
from pathlib import PurePosixPath, PureWindowsPath

from .common import AppError, new_id
from .policy import timestamp
from .qb import local_endpoint, validate_connection
from .qb_yield import LEASE
from .traffic import init_file_receipts, interval_rows

GIB = 1024**3
DEFAULTS = {
    'enabled': False, 'mteam_enabled': True, 'automatic': True, 'normal': True, 'adult': False,
    'scan_minutes': 10, 'recheck_minutes': 20, 'min_seeders': 1,
    'min_size_gib': 0, 'max_size_gib': 0, 'categories': '', 'max_active': 8,
    'slow_minutes': 10, 'slow_mbps': 1, 'qb_poll_seconds': 15, 'qb_hook': False,
    'site_free_start': '', 'site_free_end': '',
    'storage': 'disk', 'capacity_gib': 100, 'min_free_gib': 10,
    'ram_capacity_gib': 30, 'ram_reserve_gib': 4, 'save_path': '',
    'ssd_path': '', 'ram_path': '', 'retain_path': '', 'ram_create': True,
    'bandwidth_mode': 'shared', 'auto_line': True,
    'line_mbps': 650, 'reserve_mbps': 100, 'max_mbps': 650,
    'line_up_mbps': 70, 'reserve_up_mbps': 15, 'max_up_mbps': 15,
    'latency_peer': '', 'latency_extra_ms': 30, 'latency_interval': 15,
    'latency_action': 'reserve', 'latency_reserve_mbps': 100, 'latency_reserve_up_mbps': 10,
    'qb_yield_enabled': False,
    'qb_url': '', 'qb_auth': 'password', 'qb_user': '', 'qb_password': '', 'qb_api_key': '',
    'ros_host': '', 'ros_port': 8729, 'ros_user': '', 'ros_password': '', 'ros_fingerprint': '',
    'ros_wan': 'pppoe-CT', 'qos_ipv6': False,  # Used when rolling back older rules.
}
SECRETS = {'qb_password', 'qb_api_key', 'ros_password'}
STORAGE_FIELDS = {'storage', 'save_path', 'qb_url', 'ram_create'}
ACTIVE = ('adding', 'active', 'deleting', 'moving')


def qpath(value):
    return PureWindowsPath(value) if re.match(r'^[A-Za-z]:|^\\\\', value) else PurePosixPath(value)


def validate(c):
    for key, default in DEFAULTS.items():
        value = c[key]
        if isinstance(default, bool):
            valid = isinstance(value, bool)
        elif isinstance(default, int):
            valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        else:
            valid = isinstance(value, str) and len(value) <= 2048 and not any(ord(char) < 32 for char in value)
        if not valid:
            raise AppError(f'{key} 格式不正确', 'config')
    ranges = {
        'scan_minutes': (2, 1440), 'recheck_minutes': (2, 1440), 'min_seeders': (0, 10000000),
        'min_size_gib': (0, 100000), 'max_size_gib': (0, 100000), 'max_active': (1, 50),
        'slow_minutes': (0, 1440), 'slow_mbps': (0, 10000), 'qb_poll_seconds': (15, 35),
        'latency_interval': (15, 3600), 'latency_reserve_mbps': (0, 100000), 'latency_reserve_up_mbps': (0, 100000),
        'capacity_gib': (1, 100000), 'min_free_gib': (0, 100000), 'ram_capacity_gib': (1, 100000),
        'ram_reserve_gib': (1, 100000),
        'line_mbps': (1, 100000), 'reserve_mbps': (0, 100000), 'max_mbps': (0, 100000),
        'line_up_mbps': (1, 100000), 'reserve_up_mbps': (0, 100000), 'max_up_mbps': (0, 100000),
        'latency_extra_ms': (5, 10000), 'ros_port': (1, 65535),
    }
    fractional = {'min_free_gib', 'min_size_gib', 'max_size_gib', 'slow_mbps'}
    for key, (low, high) in ranges.items():
        value = c[key]
        if not low <= value <= high or key not in fractional and int(value) != value:
            raise AppError(f'{key} 必须在 {low}～{high} 范围内', 'config')
        c[key] = value if key in fractional else int(value)
    if c['storage'] not in ('disk', 'ram'):
        raise AppError('存储模式不正确', 'config')
    if c['bandwidth_mode'] not in ('shared', 'upload_priority'):
        raise AppError('带宽调度模式不正确', 'config')
    if c['max_size_gib'] and c['min_size_gib'] > c['max_size_gib']:
        raise AppError('种子体积下限不能超过上限', 'config')
    if c['categories'] and not re.fullmatch(r'[0-9]{1,10}(?:\s*[,，]\s*[0-9]{1,10}){0,19}', c['categories']):
        raise AppError('综合区分类请填写数字 ID，多个用逗号分隔', 'config')
    start, end = timestamp(c['site_free_start']), timestamp(c['site_free_end'])
    if (c['site_free_start'] or c['site_free_end']) and not (start and end and start < end):
        raise AppError('请填写有效的全站免费开始和结束时间（北京时间）', 'config')
    if c['latency_action'] not in ('reserve', 'pause'):
        raise AppError('延迟调度方式不正确', 'config')
    for key in ('ssd_path', 'ram_path', 'retain_path'):
        if c[key]:
            path = qpath(c[key])
            if not path.is_absolute() or not path.name or '..' in path.parts:
                raise AppError('请填写 qB 主机上的专用绝对目录', 'config')
            c[key] = str(path)
    if c['retain_path'] and c['save_path']:
        source, target = map(qpath, (c['save_path'], c['retain_path']))
        if type(source) is not type(target) or target.is_relative_to(source) or source.is_relative_to(target):
            raise AppError('保留目录需与后台下载目录分开', 'config')
    validate_connection(c)
    c['qb_url'] = c['qb_url'].rstrip('/')
    if c['qb_yield_enabled'] and not (c['qb_url'] and c['latency_peer']):
        raise AppError('开启 qB 整体让路前，请保存 qB 连接并填写外网延迟探测地址', 'config')
    if c['enabled']:
        if not c['mteam_enabled']:
            raise AppError('请先在调度与容量中勾选 M-Team', 'config')
        if not c['qb_url']:
            raise AppError('启动前请填写 qB WebUI 地址', 'config')
        if not c['save_path'] and not (c['storage'] == 'ram' and c['ram_create']):
            raise AppError('启动前请填写下载目录', 'config')
        if c['storage'] == 'ram' and not local_endpoint(c['qb_url']):
            raise AppError('内存盘要求 PTFree 和 qB 同机，qB 地址须为回环地址', 'config')
    if c['enabled'] and c['automatic'] and not (c['normal'] or c['adult']):
        raise AppError('自动选种至少需要开启一个站点分区', 'config')
    if c['save_path']:
        path = qpath(c['save_path'])
        if not path.is_absolute() or not path.name or '..' in path.parts:
            raise AppError('下载目录必须是 qB 主机上专用的绝对路径', 'config')
        c['save_path'] = str(path)
    for key in ('ros_host', 'latency_peer'):
        if c[key] and (c[key].startswith('-') or not re.fullmatch(r'[A-Za-z0-9_.:-]+', c[key])):
            raise AppError(f'{key} 应填写 IP 地址或主机名', 'config')
    c['ros_fingerprint'] = c['ros_fingerprint'].replace(':', '').lower()
    if c['ros_fingerprint'] and not re.fullmatch(r'[0-9a-f]{64}', c['ros_fingerprint']):
        raise AppError('证书指纹应为 SHA-256 的 64 位十六进制值', 'config')
    return c


class BackgroundStore:
    def __init__(self, db, settings):
        self.db = db
        self.cipher = settings.cipher
        init_file_receipts(db)
        with db.connect() as conn:
            conn.executescript('''
            CREATE TABLE IF NOT EXISTS background_tasks (
              id TEXT PRIMARY KEY, phase TEXT NOT NULL, data TEXT NOT NULL, updated REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS background_tasks_phase ON background_tasks(phase, updated);
            CREATE TABLE IF NOT EXISTS background_round_ids (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS background_download_days (
                day INTEGER PRIMARY KEY, bytes INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS background_events(ts REAL PRIMARY KEY, message TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS background_minutes (
              minute INTEGER PRIMARY KEY, wan_down INTEGER NOT NULL DEFAULT 0, wan_up INTEGER NOT NULL DEFAULT 0,
              job_down INTEGER NOT NULL DEFAULT 0, job_up INTEGER NOT NULL DEFAULT 0,
              wan_s REAL NOT NULL DEFAULT 0, job_s REAL NOT NULL DEFAULT 0,
              latency REAL NOT NULL DEFAULT 0, latency_n INTEGER NOT NULL DEFAULT 0
            );
            ''')
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute("SELECT 1 FROM kv WHERE key='background_download_days_migrated'").fetchone():
                conn.execute('''INSERT INTO background_download_days SELECT (minute+28800)/86400, SUM(job_down)
                    FROM background_minutes WHERE job_s>0 GROUP BY (minute+28800)/86400''')
                conn.execute("INSERT INTO kv VALUES ('background_download_days_migrated', 'true')")
            row = conn.execute("SELECT value FROM kv WHERE key='background_router_marker'").fetchone()
            self.marker = json.loads(row[0]) if row else ('mtf-' + new_id()[:8])
            if not row:
                conn.execute("INSERT INTO kv VALUES ('background_router_marker',?)", (json.dumps(self.marker),))

    def config(self, public=False):
        saved = self.db.get('background_config', {})
        value = {**DEFAULTS, **{key: saved[key] for key in DEFAULTS.keys() & saved.keys()}}
        path_key = 'ram_path' if value['storage'] == 'ram' else 'ssd_path'
        if path_key not in saved:
            value[path_key] = value['save_path']
        value['save_path'] = value[path_key]
        for key in SECRETS:
            if public:
                value[key + '_set'] = bool(value.pop(key))
            elif value[key]:
                value[key] = self.cipher.decrypt(value[key].encode()).decode()
        return value

    def save(self, patch):
        if set(patch) - set(DEFAULTS):
            raise AppError('后台配置含未知字段', 'config')
        previous = self.config()
        c = {**previous, **{key: value for key, value in patch.items() if key not in SECRETS or value != ''}}
        path_key = 'ram_path' if c['storage'] == 'ram' else 'ssd_path'
        if 'save_path' in patch and path_key not in patch:
            c[path_key] = patch['save_path']
        c['save_path'] = c[path_key]
        c = validate(c)
        changed = {key for key, value in c.items() if previous[key] != value}
        if c['storage'] != 'ram':
            changed.discard('ram_create')
        if changed & STORAGE_FIELDS and self.tasks(*ACTIVE):
            raise AppError('请先移除或完成当前 qB 托管任务，再切换客户端或存储路径', 'config')
        if 'qb_url' in changed and self.db.get(LEASE):
            raise AppError('请关闭 qB 整体让路并等原限速还原，再更换 qB 地址', 'config')
        if changed & {'ros_host', 'ros_port'} and self.db.get('background_qos_applied'):
            raise AppError('请先回滚本实例应用的 QoS 规则，再切换路由器地址', 'config')
        encoded = dict(c)
        for key in SECRETS:
            if c[key]:
                encoded[key] = self.cipher.encrypt(c[key].encode()).decode()
        self.db.put('background_config', encoded)
        return self.config(public=True)

    def tasks(self, *phases):
        with self.db.connect() as conn:
            if phases:
                sql = ' WHERE phase IN (' + ','.join('?' for _ in phases) + ')'
                rows = conn.execute('SELECT data FROM background_tasks' + sql + ' ORDER BY updated', phases).fetchall()
            else:
                rows = conn.execute('SELECT data FROM background_tasks ORDER BY updated DESC LIMIT 300').fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, tid):
        with self.db.connect() as conn:
            row = conn.execute('SELECT data FROM background_tasks WHERE id=?', (tid,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, task, *, round_used=False):
        task['updated'] = time.time()
        with self.db.connect() as conn:
            conn.execute('''INSERT INTO background_tasks VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                         phase=excluded.phase, data=excluded.data, updated=excluded.updated''',
                         (task['id'], task['phase'], json.dumps(task, ensure_ascii=False), task['updated']))
            if round_used:
                conn.execute('INSERT OR IGNORE INTO background_round_ids VALUES (?)', (task['id'],))

    def log(self, text, level='info'):
        message = '流量助手 · ' + text
        self.db.event(message, level)
        if text.startswith('调度：'):
            with self.db.connect() as conn:
                conn.execute('INSERT OR IGNORE INTO background_events VALUES (?,?)', (time.time(), message))

    def foreground_ids(self, ids):
        ids = tuple(ids)
        if not ids:
            return set()
        sql = ','.join('?' for _ in ids)
        with self.db.connect() as conn:
            rows = conn.execute(f"""SELECT id FROM torrents WHERE id IN ({sql})
                AND state IN ('pending','publishing','failed','waiting_quota','done')""", tuple(ids))
            return {row[0] for row in rows}

    def foreground_hashes(self, values):
        values = tuple(values)
        if not values:
            return set()
        sql = ','.join('?' for _ in values)
        with self.db.connect() as conn:
            conn.execute('DELETE FROM foreground_intents WHERE expires<=?', (time.time(),))
            rows = conn.execute(f'SELECT hash FROM foreground_intents WHERE hash IN ({sql})', values)
            return {row[0] for row in rows}

    def round_used(self, ids):
        if not ids:
            return set()
        sql = ','.join('?' for _ in ids)
        with self.db.connect() as conn:
            return {row[0] for row in conn.execute(
                f'SELECT id FROM background_round_ids WHERE id IN ({sql})', tuple(ids))}

    def reset_round(self):
        with self.db.connect() as conn:
            conn.execute('DELETE FROM background_round_ids')
            conn.execute("DELETE FROM kv WHERE key='background_candidate_page'")

    def line_estimate(self, now):
        with self.db.connect() as conn:
            rows = conn.execute("""SELECT minute,wan_down,wan_up,wan_s FROM background_minutes
                WHERE minute>=? AND wan_s>=52 ORDER BY minute""", (int(now) - 7 * 86400,))
            window = deque(maxlen=5)
            peak = [0, 0]
            for row in rows:
                window.append(row)
                if len(window) == 5 and window[-1][0] - window[0][0] == 240:
                    seconds = sum(r[3] for r in window)
                    for i in range(2):
                        peak[i] = max(peak[i], sum(r[i+1] for r in window) * 8 / seconds / 1e6)
        return peak

    def add_interval(self, start, end, *, wan=None, job=None, latency=None, extra=False):
        with self.db.connect() as conn:
            for minute, data in interval_rows(start, end, wan=wan, job=job, latency=latency, extra=extra):
                conn.execute('''INSERT INTO background_minutes VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(minute) DO UPDATE SET wan_down=wan_down+excluded.wan_down, wan_up=wan_up+excluded.wan_up,
                    job_down=job_down+excluded.job_down, job_up=job_up+excluded.job_up,
                    wan_s=wan_s+excluded.wan_s, job_s=job_s+excluded.job_s,
                    latency=latency+excluded.latency, latency_n=latency_n+excluded.latency_n''', (minute, *data))
                if job is not None:
                    conn.execute('''INSERT INTO background_download_days VALUES (?,?)
                        ON CONFLICT(day) DO UPDATE SET bytes=bytes+excluded.bytes''', ((minute+28800)//86400, data[2]))
            if job is not None and not extra and end > start:
                conn.execute("INSERT OR IGNORE INTO kv VALUES ('background_job_accounting_since', ?)", (str(start),))

    def download_totals(self, now):
        # Use the same elapsed windows as the graph; the all-time total is
        # durable even if minute-level history is ever compacted in the future.
        spans = (86400, 7 * 86400, 30 * 86400, 365 * 86400)
        boundaries = tuple(int(now - span) for span in spans)
        with self.db.connect() as conn:
            totals = [0] * 4
            seen = [False] * 4
            for row in conn.execute('SELECT minute,job_down,job_s FROM background_minutes WHERE minute>=? AND minute<?',
                                    (boundaries[-1] // 60 * 60, now)):
                minute, count, covered = row
                if not covered and not count:
                    continue
                for i, boundary in enumerate(boundaries):
                    if minute + 60 <= boundary:
                        continue
                    fraction = min(1, max(0, min(now, minute + 60) - max(boundary, minute)) /
                                   max(1e-9, min(now, minute + 60) - minute))
                    totals[i] += round(count * fraction)
                    seen[i] = True
            all_time = conn.execute('SELECT SUM(bytes) FROM background_download_days').fetchone()[0]
        return dict(zip(('day', 'week', 'month', 'year', 'all'),
                        [v if found else None for v, found in zip(totals, seen)] + [all_time]))

    def history(self, period, offset=0, now=None):
        now = time.time() if now is None else now
        durations = {'day': (86400, 300), 'week': (7 * 86400, 1800),
                     'month': (30 * 86400, 7200), 'year': (365 * 86400, 86400)}
        if period in durations:
            duration, bucket = durations[period]
            end = now - offset * duration
            start = end - duration
        else:
            with self.db.connect() as conn:
                first = conn.execute('SELECT MIN(minute) FROM background_minutes').fetchone()[0]
            start, end = (first if first is not None else now), now
            bucket = max(86400, math.ceil((end - start) / (86400 * 600)) * 86400)
        start, end = float(start), float(end)
        total = [0] * 8
        windows = deque(maxlen=5)
        buckets = {}
        peak, peak_windows = 0, 0
        with self.db.connect() as conn:
            for row in conn.execute('SELECT * FROM background_minutes WHERE minute>=? AND minute<? ORDER BY minute', (start // 60 * 60, end)):
                minute, *values = row
                overlap_start, overlap_end = max(start, minute), min(end, minute + 60)
                if overlap_end <= overlap_start:
                    continue
                # Stored data is aggregated by minute. Estimate only the fractional
                # boundary minute; the current, incomplete minute is already partial.
                recorded_end = min(now, minute + 60)
                fraction = min(1, (overlap_end - overlap_start) / max(1e-9, recorded_end - minute))
                if fraction < 1:
                    values = [round(v * fraction) if i < 4 else v * fraction for i, v in enumerate(values)]
                windows.append((minute, values[0], values[4]))
                if (len(windows) == 5 and windows[-1][0] - windows[0][0] == 240
                        and all(item[2] >= 59.5 for item in windows)):
                    peak_windows += 1
                    peak = max(peak, sum(item[1] for item in windows) * 8 / sum(item[2] for item in windows) / 1e6)
                index = start + (max(start, minute) - start) // bucket * bucket
                group = buckets.setdefault(index, [0] * 8)
                for i, value in enumerate(values):
                    total[i] += value
                    group[i] += value
        span = max(1, end - start)
        wan_down, wan_up, job_down, job_up, wan_s, job_s, *_ = total
        job_mbps = job_down * 8 / span / 1e6 if job_s else None
        def mbps(count, seconds):
            return round(count * 8 / seconds / 1e6, 3) if seconds else None
        points = [{'ts': i, 'wan_rx': mbps(values[0], values[4]), 'wan_tx': mbps(values[1], values[4]),
                   'job_rx': mbps(values[2], values[5]), 'job_tx': mbps(values[3], values[5]),
                   'latency': values[6] / values[7] if values[7] else None}
                  for i, values in buckets.items()]
        with self.db.connect() as conn:
            events = [dict(row) for row in conn.execute("""SELECT ts,message FROM background_events WHERE ts>=? AND ts<? ORDER BY ts DESC LIMIT 200""", (start, end))]
        return {'start': start, 'end': end, 'bucket': bucket, 'peak_mbps': peak or None, 'peak_windows': peak_windows,
                'index': job_mbps / peak * 100 if peak and job_mbps is not None else None,
                'wan_rx': wan_down, 'wan_tx': wan_up, 'job_rx': job_down, 'job_tx': job_up,
                'wan_coverage': min(1, wan_s / span), 'job_coverage': min(1, job_s / span),
                'job_average_mbps': job_mbps, 'points': points, 'events': events,
                'download_totals': self.download_totals(now),
                'job_accounting_since': self.db.get('background_job_accounting_since')}
