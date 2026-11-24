# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0
import unittest
from pathlib import Path
from unittest.mock import patch
from tempfile import TemporaryDirectory
from ptfree.background import Background
from ptfree.settings import Settings
from ptfree.store import Store
from ptfree.traffic import collect_foreground
from ptfree import __version__
from test_background import FakeQB

ROOT = Path(__file__).resolve().parents[1]
URL = 'http://127.0.0.1:8180'
NOW = 1795536300


def legacy_delivery(db, tid, name, size, completed):
    db.observe(dict(id=tid, name=name, size=size), 'done', '')
    db.set_state(tid, 'done', '种子文件已保存到目标目录', completed=completed)


class Release206(unittest.TestCase):
    def test_global_traffic_labels_and_quota_scope(self):
        html = (ROOT / 'ptfree/static/index.html').read_text(encoding='utf-8')
        js = (ROOT / 'ptfree/static/background.js').read_text(encoding='utf-8')
        for label in ('PTFree 下载', 'PTFree 上传', 'PTFree 下 / 上行', '流量助手存储配额', '存储与 PTFree 下载累计'):
            self.assertIn(label, html)
        for label in ('PTFree 下载', 'PTFree 上传', '流量助手存储配额已用', '最近24小时 PTFree 平均下载'):
            self.assertIn(label, js)
        for label in ('PTFree 托管下载', 'PTFree 托管上传', 'PTFree 托管下 / 上行', '托管配额已用', '托管速率 / 线路峰值'):
            self.assertNotIn(label, html + js)
        self.assertIn('monitor.reserved', js)
        self.assertIn('monitor.capacity', js)
        self.assertIn('history.job_rx', js)
        self.assertIn('history.job_tx', js)
        self.assertIn('history.job_accounting_since', js)

    def test_distinct_module_traffic_included_and_duplicate_deduped(self):
        with TemporaryDirectory() as d:
            home = Path(d); db = Store(home); db.init()
            bg = Background(home, Settings(home), db)
            now = 1795536300
            a = dict(hash='a'*40, added_on=now-100, downloaded=10, uploaded=20)
            b = dict(hash='b'*40, added_on=now-200, downloaded=30, uploaded=40)
            foreground = {(a['hash'], a['added_on']): a}
            lookup = {'flow': b, 'duplicate': a}
            tasks = [{'run_tag': 'flow'}, {'run_tag': 'duplicate'}]
            with patch('ptfree.background.time.time', return_value=now):
                bg.measure_jobs(tasks, lookup, foreground)
            a['downloaded'] += 100; a['uploaded'] += 500
            b['downloaded'] += 200; b['uploaded'] += 700
            with patch('ptfree.background.time.time', return_value=now+15):
                bg.measure_jobs(tasks, lookup, foreground)
            result = bg.store.history('day', now=now+15)
            self.assertEqual((result['job_rx'], result['job_tx']), (300, 1200))
            self.assertEqual(result['download_totals']['day'], 300)

    def test_upgraded_watch_seed_counts_after_reconnect_with_long_interval(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            db = Store(home)
            db.init()
            bg = Background(home, Settings(home), db)
            bg.store.save({'qb_url': URL})
            with db.connect() as conn:
                conn.execute('INSERT INTO background_minutes (minute, job_up, job_s) VALUES (?, ?, ?)',
                             (NOW - 120, 91, 15))
            legacy_delivery(db, '1', 'old seed', 100, NOW - 300)
            h, manual = 'a' * 40, 'b' * 40
            qb = FakeQB()
            qb.live = {
                h: dict(hash=h, name='old seed', total_size=100, added_on=NOW - 299,
                        state='uploading', progress=1, downloaded=101, uploaded=102),
                manual: dict(hash=manual, name='old seed', total_size=101, added_on=NOW - 299,
                             state='uploading', progress=1, downloaded=201, uploaded=202),
            }
            with patch('ptfree.background.QB', return_value=qb):
                with patch('ptfree.background.time.time', return_value=NOW + 3):
                    bg.tick()
                bg.qb = None
                qb.live[h]['uploaded'] += 8_001_003
                qb.live[manual]['uploaded'] += 60_000_000
                with patch('ptfree.background.time.time', return_value=NOW + 128):
                    bg.tick()
            result = bg.store.history('day', now=NOW + 128)
            self.assertEqual(result['job_tx'], 91 + 8_001_003)
            self.assertTrue(any(point['job_tx'] > 0 for point in result['points']))
            self.assertEqual(result['job_accounting_since'], NOW + 3)
            self.assertAlmostEqual(result['job_coverage'], (15 + 125) / 86400)
            restarted = Store(home)
            self.assertEqual(set(collect_foreground(restarted, URL, qb.live)), {(h, NOW - 299)})
            qb.live[h]['added_on'] = NOW + 130
            self.assertEqual(collect_foreground(restarted, URL, qb.live), {})

    def test_legacy_recovery_requires_a_unique_delivery_and_import(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            db = Store(home)
            db.init()
            bg = Background(home, Settings(home), db)
            bg.store.save({'qb_url': URL})
            legacy_delivery(db, '1', 'ambiguous in qB', 100, NOW - 300)
            legacy_delivery(db, '2', 'ambiguous in history', 200, NOW - 301)
            legacy_delivery(db, '3', 'ambiguous in history', 200, NOW - 300)
            legacy_delivery(db, '4', 'added at another time', 300, NOW - 300)
            live = {
                h * 40: dict(hash=h * 40, name=name, total_size=size, added_on=added)
                for h, name, size, added in (
                    ('a', 'ambiguous in qB', 100, NOW - 300),
                    ('b', 'ambiguous in qB', 100, NOW - 299),
                    ('c', 'ambiguous in history', 200, NOW - 300),
                    ('d', 'added at another time', 300, NOW - 600),
                )
            }
            self.assertEqual(collect_foreground(db, URL, live), {})

    def test_long_interval_preserves_every_qb_byte_and_keeps_wan_gap(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            db = Store(home)
            db.init()
            bg = Background(home, Settings(home), db)
            bg.store.add_interval(NOW, NOW + 125, job=(1, 113), wan=(301, 403))
            result = bg.store.history('day', now=NOW + 125)
            self.assertEqual((result['job_rx'], result['job_tx'], result['wan_tx']), (1, 113, 0))
            self.assertEqual(result['download_totals']['all'], 1)

    def test_version(self):
        self.assertEqual(__version__, '2.0.6')
        self.assertIn('version = "2.0.6"', (ROOT/'pyproject.toml').read_text())
