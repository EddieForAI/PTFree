# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import time

from .freeseed_options import FreeseedOptions
from .storage_paths import GIB, cleanup_empty_root, owned_path, qpath
from .common import AppError, new_id
from .qb import QB, connection_args, hash_keys, hashes, tags
from .torrent import inspect_torrent

TAG = 'MTFreeFreeseed'


def client(db, settings, config):
    connection = FreeseedOptions(db, settings).config()
    if not connection['qb_url']:
        raise AppError('请在全局配置中填写 qB Web API 地址', 'config')
    root = qpath(config['managed_path'])
    qb = QB(*connection_args(connection))
    qb.connect()
    if qb.preferences.get('temp_path_enabled') and qb.version < (5, 1):
        raise AppError('本 qB 版本开启了全局临时下载目录，无法安全启用容量回收', 'config')
    return qb, root, connection['qb_url']


def put(db, rec):
    with db.connect() as conn:
        conn.execute('INSERT OR REPLACE INTO freeseed_managed(id,data) VALUES (?,?)',
                     (rec['id'], json.dumps(rec)))


def managed_tasks(db, url, root):
    with db.connect() as conn:
        items = [json.loads(row[0]) for row in conn.execute('SELECT data FROM freeseed_managed')]
    return sorted((r for r in items if r['url'] == url and qpath(r['path']).parent == root),
                  key=lambda r: r['issued'])


def attributed(rec, live, qb):
    row = next((r for r in live.values() if rec['run_tag'] in tags(r) and TAG in tags(r)
                and hash_keys(rec['hashes']) & hash_keys(hashes(r))
                and (not rec.get('added_on') or r.get('added_on') == rec['added_on'])), None)
    temp = bool(qb.preferences.get('temp_path_enabled')) and row and float(row.get('progress', 0)) < 1
    return row if row and owned_path(rec['path'], row, temp) else None


def release(db, qb, record, row):
    qb.action('delete', row['hash'], deleteFiles='true')
    if qb.torrent_info({row['hash']}):
        raise AppError('qB 正在清理最旧任务，等释放容量后继续', 'capacity')
    cleanup_empty_root(record['path'], record['url'])
    with db.connect() as conn:
        conn.execute('DELETE FROM freeseed_managed WHERE id=?', (record['id'],))
    db.event('Freeseed 已清理最旧托管种子 #' + record['id'])


def deliver(db, settings, config, tid, data):
    values, size = inspect_torrent(data, include_size=True)
    keys = hash_keys(values)
    limit = config['managed_gib'] * GIB
    if size <= 0 or size > limit:
        raise AppError('本种子内容大于 Freeseed 的总容量配额', 'size')
    qb, root, url = client(db, settings, config)
    records = managed_tasks(db, url, root)
    all_keys = keys | {key for rec in records for key in hash_keys(rec['hashes'])}
    live = {}
    sorted_keys = sorted(all_keys)
    for offset in range(0, len(sorted_keys), 100):
        live.update(qb.torrent_info(set(sorted_keys[offset:offset+100])))
    current_keys = {key for row in live.values() for key in hash_keys(hashes(row))}
    by_run_tag = {tag: row for row in live.values() for tag in tags(row) if tag.startswith('mtfree-fs-')}
    reserved = 0
    safe_old = []
    recovered = None
    gone = []
    for rec in records:
        present = bool(hash_keys(rec['hashes']) & current_keys)
        candidate = by_run_tag.get(rec['run_tag'])
        row = attributed(rec, {candidate['hash']: candidate}, qb) if candidate else None
        if rec['id'] == tid and row:
            recovered = rec, row
        if not present and time.time() - rec['issued'] >= 30:
            gone.append((rec['id'],))
            continue
        reserved += rec['size']
        if row and rec['id'] != tid:
            safe_old.append((rec, row))
    if gone:
        with db.connect() as conn:
            conn.executemany('DELETE FROM freeseed_managed WHERE id=?', gone)
    if not recovered:
        if keys & current_keys:
            return 'qB 已存在同一任务，其归属与文件保持原样'
        if any(rec['id'] == tid and time.time() - rec['issued'] < 30 for rec in records):
            raise AppError('等待 qB 确认加入种子', 'capacity')
        need = reserved + size - limit
        if need > 0:
            if config['managed_full'] == 'wait':
                raise AppError('Freeseed 容量已满，暂缓新增', 'capacity')
            chosen = []
            for rec, row in safe_old:
                chosen.append((rec, row))
                need -= rec['size']
                if need <= 0:
                    break
            if need > 0:
                raise AppError('可安全清理的 Freeseed 任务不足，暂缓新增', 'capacity')
            for rec, row in chosen:
                refreshed = qb.torrent_info({row['hash']})
                verified = attributed({**rec, 'added_on': row.get('added_on')}, refreshed, qb)
                if not verified:
                    raise AppError('清理前归属发生变化，重新评估容量', 'capacity')
                release(db, qb, rec, verified)
        run_tag = 'mtfree-fs-' + new_id()
        rec = {'id': tid, 'run_tag': run_tag, 'path': str(root / run_tag), 'size': size,
               'hashes': values, 'url': url, 'issued': time.time()}
        put(db, rec)
        qb.add(data, rec['path'], run_tag, tag=TAG)
        row = attributed(rec, qb.torrent_info(keys), qb)
        if not row:
            raise AppError('等待 qB 确认加入种子', 'capacity')
    else:
        rec, row = recovered
    rec['added_on'] = row.get('added_on')
    put(db, rec)
    qb.action('start', row['hash'])
    return '已加入 qB 的 Freeseed 托管目录'
