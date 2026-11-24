# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

"""Read-only attribution of qB traffic. Never change a torrent's scheduling or tags."""
from __future__ import annotations

import json
import time
from collections import defaultdict

from .qb import hash_keys, hashes, tags

SITE = 'mteam'
FILE_DELIVERY = """(target<>'' OR reason IN (
    '种子文件已保存到目标目录','目标目录已存在相同种子文件',
    '已恢复种子文件保存','已恢复种子文件保存记录'))"""
LEGACY_IMPORT_WINDOW = 120


def init_file_receipts(db):
    """Backfill existing file deliveries once; preserve identity across upgrades/restarts."""
    with db.connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS freeseed_file_receipts (
            site_id TEXT NOT NULL, torrent_id TEXT NOT NULL, infohash TEXT NOT NULL,
            issued REAL NOT NULL, qb_url TEXT NOT NULL DEFAULT '',
            qb_hash TEXT NOT NULL DEFAULT '', added_on REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (site_id,torrent_id,infohash))''')
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute("SELECT 1 FROM kv WHERE key='freeseed_file_receipts_v1'").fetchone():
            return
        saved = conn.execute("SELECT value FROM kv WHERE key='background_config'").fetchone()
        url = json.loads(saved[0]).get('qb_url', '') if saved else ''
        # 2.0.3 recorded completed just after publishing (and updated before
        # publishing). Use a small compatibility allowance for old records only.
        rows = conn.execute(f"""SELECT id,hashes,completed,updated,state FROM torrents
            WHERE state IN ('done','publishing') AND hashes<>'' AND {FILE_DELIVERY}""").fetchall()
        for row in rows:
            stamp = row['completed'] if row['state'] == 'done' else row['updated']
            if stamp and stamp > 0:
                _insert_receipt(conn, row['id'], row['hashes'].split(','), stamp - 120, url)
        conn.execute("INSERT INTO kv VALUES ('freeseed_file_receipts_v1','true')")


def _insert_receipt(conn, tid, values, issued, url):
    conn.executemany('''INSERT OR IGNORE INTO freeseed_file_receipts
        (site_id,torrent_id,infohash,issued,qb_url) VALUES (?,?,?,?,?)''',
        ((SITE, str(tid), key, issued, url) for key in hash_keys(values)))


def record_file_receipt(db, tid, values, issued=None):
    # The worker may run independently of the web server, e.g. recovery tests.
    init_file_receipts(db)
    url = db.get('background_config', {}).get('qb_url', '')
    with db.connect() as conn:
        _insert_receipt(conn, tid, values, time.time() if issued is None else issued, url)


def identity(row):
    added = row.get('added_on')
    key = row.get('hash')
    if not isinstance(key, str) or not hash_keys([key]) or type(added) not in (int, float) or not added > 0:
        return None
    return key.lower(), added


def counters(row):
    values = tuple(row.get(key) for key in ('downloaded', 'uploaded'))
    return values if all(type(value) is int and value >= 0 for value in values) else None


def recover_legacy_receipts(conn, url, live, recorded_keys):
    """Use the name, total size and import time of a recorded file delivery."""
    legacy = conn.execute(f"""SELECT id, name, size, completed FROM torrents
        WHERE state='done' AND COALESCE(hashes,'')='' AND completed>0 AND {FILE_DELIVERY}
        AND NOT EXISTS (SELECT 1 FROM freeseed_file_receipts
                        WHERE site_id=? AND torrent_id=torrents.id)""", (SITE,)).fetchall()
    if not legacy:
        return {}
    by_name_size = defaultdict(list)
    for row in live.values():
        name, size = row.get('name'), row.get('total_size')
        if isinstance(name, str) and name and type(size) is int and size > 0 and identity(row):
            by_name_size[name, size].append(row)
    matches = defaultdict(list)
    for delivery in legacy:
        matching = [row for row in by_name_size.get((delivery['name'], delivery['size']), ())
                    if abs(row['added_on'] - delivery['completed']) <= LEGACY_IMPORT_WINDOW]
        for row in matching:
            matches[identity(row)].append((delivery, row, len(matching)))
    recovered = {}
    for (key, added), candidates in matches.items():
        if len(candidates) != 1:
            continue
        delivery, row, count = candidates[0]
        if count != 1 or hash_keys(hashes(row)) & recorded_keys:
            continue
        changed = conn.execute('''INSERT OR IGNORE INTO freeseed_file_receipts
            (site_id,torrent_id,infohash,issued,qb_url,qb_hash,added_on)
            SELECT ?,?,?,?,?,?,? WHERE NOT EXISTS (
                SELECT 1 FROM freeseed_file_receipts WHERE site_id=? AND torrent_id=?)''',
            (SITE, delivery['id'], key[:40], delivery['completed'], url, key, added, SITE, delivery['id'])).rowcount
        if changed:
            recovered[key, added] = row
    return recovered


def collect_foreground(db, url, live):
    """Find both file-watch and direct API deliveries, including completed seeds.

    A direct delivery uses its run tag before confirmation, then the recorded
    qB instance identity. A file delivery uses the recorded hash and publication
    time, then permanently binds to that qB instance (not a subsequent re-add).
    """
    by_key = {key: row for row in live.values() for key in hash_keys(hashes(row)) if identity(row)}
    result = {}
    with db.connect() as conn:
        managed = [json.loads(row[0]) for row in conn.execute('SELECT data FROM freeseed_managed')]
        recorded_keys = set()
        for rec in managed:
            if rec.get('url') != url:
                continue
            keys = hash_keys(rec.get('hashes', ()))
            recorded_keys.update(keys)
            for key in keys:
                row = by_key.get(key)
                if not row:
                    continue
                if rec.get('added_on'):
                    if row['added_on'] != rec['added_on']:
                        continue
                elif rec.get('run_tag') not in tags(row) or row['added_on'] < rec.get('issued', 0) - 2:
                    continue
                result[identity(row)] = row
        receipts = conn.execute('''SELECT * FROM freeseed_file_receipts
            WHERE qb_url='' OR qb_url=?''', (url,)).fetchall()
        for rec in receipts:
            recorded_keys.add(rec['infohash'])
            row = by_key.get(rec['infohash'])
            if not row:
                continue
            key, added = identity(row)
            if rec['qb_hash']:
                if key != rec['qb_hash'] or added != rec['added_on']:
                    continue
            else:
                if added < rec['issued'] - 2:
                    continue
                # Bind once. Do not count a pre-existing manual torrent or a
                # later torrent with the same hash after an observed removal.
                changed = conn.execute('''UPDATE freeseed_file_receipts SET qb_url=?,qb_hash=?,added_on=?
                    WHERE site_id=? AND torrent_id=? AND infohash=? AND qb_hash='' AND (qb_url='' OR qb_url=?)''',
                    (url, key, added, rec['site_id'], rec['torrent_id'], rec['infohash'], url)).rowcount
                if not changed:
                    continue
            result[(key, added)] = row
        result.update(recover_legacy_receipts(conn, url, live, recorded_keys))
    return result


def delta(key, values, previous, sampled_at):
    old = previous.get(key)
    # Only count from zero for a genuinely new qB instance that was added
    # within the observed interval. Existing jobs start with a baseline.
    if old is None:
        if key[1] < int(sampled_at):
            return (0, 0)
        old = (0, 0)
    # Counter resets are new baselines, not negative traffic or a lifetime spike.
    return tuple(n - before if n >= before else 0 for n, before in zip(values, old))


def interval_rows(start, end, *, wan, job, latency, extra=False):
    """Split measured bytes across minutes, including delayed qB samples."""
    seconds = end - start
    if not seconds > 0 or (seconds > 45 and job is None):
        return
    if seconds > 45:
        wan, latency = None, None
    cursor = start
    recorded = (0, 0)
    while cursor < end:
        minute = int(cursor // 60) * 60
        segment_end = min(end, minute + 60)
        duration = segment_end - cursor
        data = [0, 0, 0, 0, duration if wan is not None else 0,
                duration if job is not None and not extra else 0, 0, 0]
        if wan is not None:
            data[:2] = [round(n * duration / seconds) for n in wan]
        if job is not None:
            through = job if segment_end == end else tuple(round(n * ((segment_end - start) / seconds)) for n in job)
            data[2:4] = [n - before for n, before in zip(through, recorded)]
            recorded = through
        if latency is not None and segment_end == end:
            data[6:] = [latency, 1]
        yield minute, data
        cursor = segment_end
