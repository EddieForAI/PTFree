# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

import datetime as dt
import json
import tempfile
import threading
import time
import unittest
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

from ptfree.background import Background, eligible, lookup_owned
from ptfree.background_selection import classify
from ptfree.background_storage import automatic_ram_path, owned_path
from ptfree.background_store import ACTIVE, DEFAULTS, GIB
from ptfree.common import AppError
from ptfree.completion import configure
from ptfree.freeseed_storage import TAG as FREESEED_TAG, deliver, put
from ptfree.qb import QB, TAG, hash_keys, tags
from ptfree.routeros import remove_rules
from ptfree.qb_yield import LEASE, QBYield
from ptfree.settings import Settings
from ptfree.store import Store
from ptfree.torrent import inspect_torrent
from ptfree.upload_priority import UploadPriority
from test_core import TORRENT


class FakeQB:
    version = (5, 1)
    version_text = 'v5.1.4'
    preferences = {'temp_path_enabled': True}

    def __init__(self):
        self.live = {}
        self.calls = []
        self.unused_tags = set()

    def add(self, data, path, run_tag, limit, upload_limit=None, *, tag=TAG):
        self.calls.append(('add', path, {}))
        hash_value = inspect_torrent(data)[0]
        self.live[hash_value] = {'hash': hash_value, 'tags': tag + ',' + run_tag if run_tag else tag, 'save_path': path,
                                 'content_path': path + '/file', 'download_path': path, 'added_on': time.time(),
                                 'dl_limit': limit, 'up_limit': upload_limit, 'state': 'stoppedDL', 'progress': 0, 'amount_left': 1_000}

    def action(self, action, hash_value, **params):
        self.calls.append((action, hash_value, params))
        row = self.live[hash_value]
        if action == 'delete':
            del self.live[hash_value]
        elif action == 'start':
            row['state'] = 'downloading'
        elif action == 'stop':
            row['state'] = 'stoppedDL'
        elif action == 'up-limit':
            row['up_limit'] = params['limit']
        elif action == 'limit':
            row['dl_limit'] = params['limit']

    def action_many(self, action, values, **params):
        for value in values:
            self.action(action, value, **params)

    def connect(self):
        return self.version_text

    def torrent_info(self, keys):
        return {key: dict(value) for key, value in self.live.items() if hash_keys([key]) & keys}

    def list_tags(self):
        return self.unused_tags | {tag for row in self.live.values() for tag in tags(row) if tag}

    def snapshot(self):
        return self.torrent_info(set(self.live)), {}

    def traffic_snapshot(self):
        return self.snapshot()

    def delete_tags(self, labels):
        self.unused_tags.difference_update(labels)
        for row in self.live.values():
            row['tags'] = ','.join(sorted(tags(row).difference(labels)))

    def transfer_info(self):
        return {key: sum(int(row.get(field) or 0) for row in self.live.values())
                for key, field in (('dl_info_speed', 'dlspeed'), ('up_info_speed', 'upspeed'))}



class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.settings = Settings(self.home)
        self.db = Store(self.home)
        self.db.init()
        self.bg = Background(self.home, self.settings, self.db)
        self.qb = FakeQB()
        (self.home / 'data').mkdir()
        self.bg.store.save({'enabled': True, 'automatic': False, 'save_path': str(self.home / 'data'),
                            'qb_url': 'http://127.0.0.1:8180', 'min_free_gib': 0})
        self.bg.qb = self.qb
        self.bg.qb_key = ('http://127.0.0.1:8180', '', '', '')

    def ready(self, tid='123', size=1000):
        self.bg.manual('add', {'id': tid, 'loop': True})
        task = self.bg.store.get(tid)
        task.update(phase='ready', verified_until=time.time() + 60, hashes=inspect_torrent(TORRENT), size=size)
        self.bg.store.put(task)
        self.bg.cache(tid).parent.mkdir(exist_ok=True)
        self.bg.cache(tid).write_bytes(TORRENT)

    def launch(self):
        self.ready()
        self.bg.tick()
        self.bg.tick()
        return next(iter(self.qb.live.values()))

    def test_upload_priority_clears_managed_upload_cap_and_keeps_transferring_without_router_sample(self):
        row = self.launch()
        unrelated = {'hash': 'a' * 40, 'dlspeed': 50_000_000, 'upspeed': 8_000_000, 'up_limit': 12345}
        self.qb.live[unrelated['hash']] = unrelated
        self.bg.store.save({'bandwidth_mode': 'upload_priority', 'ros_host': '192.0.2.1'})
        start = len(self.qb.calls)
        with patch.object(self.bg, 'router_sample', return_value=(None, None, '等待 RouterOS 采样')):
            self.bg.tick()
        self.assertEqual(50, self.bg.monitor['limit_mbps'])
        self.assertIsNone(self.bg.monitor['upload_limit_mbps'])
        self.assertEqual(64, self.bg.monitor['upload_priority']['qb_upload_mbps'])
        self.assertEqual(0, row['up_limit'])
        self.assertEqual('downloading', row['state'])
        self.assertEqual(12345, unrelated['up_limit'])
        self.assertTrue(all(action in ('limit', 'up-limit') and key == row['hash']
                            for action, key, _ in self.qb.calls[start:]))

    def test_upload_priority_can_admit_and_start_a_new_task_while_qb_is_uploading(self):
        self.bg.store.save({'bandwidth_mode': 'upload_priority', 'ros_host': '192.0.2.1'})
        foreign = 'a' * 40
        self.qb.live[foreign] = {'hash': foreign, 'dlspeed': 20_000_000, 'upspeed': 8_000_000}
        with patch.object(self.bg, 'router_sample', return_value=((160, 69), None, '')):
            self.ready()
            self.bg.tick()
            self.bg.tick()
        self.assertEqual('active', self.bg.store.get('123')['phase'])
        self.assertEqual(50, self.bg.monitor['limit_mbps'])
        managed = next(row for key, row in self.qb.live.items() if key != foreign)
        self.assertEqual('downloading', managed['state'])
        self.assertEqual(0, managed['up_limit'])
        self.assertEqual(TAG, managed['tags'])

    def test_qb_queue_does_not_dilute_bandwidth_or_accumulate_more_work(self):
        self.bg.store.save({'bandwidth_mode': 'upload_priority', 'max_active': 40})
        qb_downloads = []
        now = time.time()
        for index in range(33):
            key = f'{index + 1:040x}'
            marker = f'test-{index}'
            path = str(self.home / 'data' / marker)
            self.bg.store.put({'id': str(index + 200), 'run_tag': marker, 'phase': 'active',
                               'hashes': [key], 'size': 1_000, 'qb_hash': key, 'added_on': now,
                               'path': path, 'qb_url': self.bg.qb_key[0], 'shared_qb_tag': True})
            row = {'hash': key, 'tags': TAG, 'save_path': path, 'download_path': path,
                   'added_on': now, 'state': 'downloading' if index < 3 else 'queuedDL',
                   'amount_left': 1_000, 'dl_limit': 1_325_000}
            self.qb.live[key] = row
            qb_downloads.append(row)
        self.ready()
        self.bg.concurrency = 34
        with patch.object(self.bg, 'upload_priority_allowance',
                          return_value=(350, '上传优先', False, {'qb_upload_mbps': 5})):
            self.bg.tick()
            self.assertEqual((3, 30), (self.bg.monitor['qb_download_runnable'], self.bg.monitor['qb_download_queued']))
            self.assertEqual(0, self.bg.monitor['slots'])
            self.assertEqual(14_573_093, qb_downloads[0]['dl_limit'])
            self.assertEqual(1024, qb_downloads[3]['dl_limit'])
            self.assertEqual('等待 qB 下载队列', self.bg.store.get('203')['note'])
            self.assertEqual('ready', self.bg.store.get('123')['phase'])
            self.assertLessEqual(sum(row['dl_limit'] for row in qb_downloads), 43_750_000)

            qb_downloads[0]['state'] = 'queuedDL'
            qb_downloads[3]['state'] = 'downloading'
            self.bg.tick()
            self.assertEqual(1024, qb_downloads[0]['dl_limit'])
            self.assertEqual(14_573_093, qb_downloads[3]['dl_limit'])
            self.assertLessEqual(sum(row['dl_limit'] for row in qb_downloads), 43_750_000)

    def test_upload_priority_probe_downrates_running_task_and_holds_new_tasks(self):
        row = self.launch()
        self.bg.store.save({'bandwidth_mode': 'upload_priority'})
        calls_before = len(self.qb.calls)
        self.bg.concurrency = 3
        with patch.object(self.bg, 'upload_priority_allowance',
                          return_value=(250, '上传优先 · 验证降速', True, {'qb_upload_mbps': 25})):
            self.bg.tick()
        self.assertEqual(0, self.bg.monitor['slots'])
        self.assertEqual('downloading', row['state'])
        self.assertEqual(0, row['up_limit'])
        self.assertEqual(31_250_000, row['dl_limit'])
        self.assertTrue(all(action not in ('stop', 'start', 'add') for action, *_ in self.qb.calls[calls_before:]))

    def test_existing_settings_keep_shared_mode_until_selected(self):
        stored = self.db.get('background_config')
        stored.pop('bandwidth_mode')
        self.db.put('background_config', stored)
        self.assertEqual('shared', self.bg.store.config()['bandwidth_mode'])
        with self.assertRaisesRegex(AppError, '调度模式'):
            self.bg.store.save({'bandwidth_mode': 'unknown'})

    def test_new_task_has_only_shared_tag_and_requires_expected_path_to_adopt(self):
        self.ready()
        self.bg.tick()
        record = self.bg.store.get('123')
        row = next(iter(self.qb.live.values()))
        self.assertEqual(TAG, row['tags'])
        self.assertEqual(row, lookup_owned([record], self.qb.live)[record['run_tag']])
        wrong = {**row, 'save_path': str(self.home / 'unrelated')}
        self.assertFalse(lookup_owned([record], {row['hash']: wrong}))
        self.bg.tick()
        record = self.bg.store.get('123')
        replacement = {**row, 'added_on': row['added_on'] + 1}
        self.assertFalse(lookup_owned([record], {row['hash']: replacement}))
        self.assertEqual('downloading', row['state'])

    def test_upgrade_retires_confirmed_and_empty_labels_without_touching_unknown_tasks(self):
        row = self.launch()
        task = self.bg.store.get('123')
        task.pop('shared_qb_tag')
        self.bg.store.put(task)
        row['tags'] = f'{TAG},{task["run_tag"]},我的标签'
        empty = 'mtfree-' + 'b' * 24
        unknown = 'mtfree-' + 'c' * 24
        foreign = 'f' * 40
        self.qb.unused_tags.add(empty)
        self.qb.live[foreign] = {'hash': foreign, 'tags': f'{TAG},{unknown}', 'state': 'uploading'}
        self.bg.tag_cleanup_at = 0
        self.bg.tick()
        self.assertEqual({TAG, '我的标签'}, tags(row))
        self.assertNotIn(empty, self.qb.list_tags())
        self.assertIn(unknown, self.qb.list_tags())
        self.assertTrue(self.bg.store.get('123')['shared_qb_tag'])
        self.assertEqual('downloading', row['state'])
        self.assertEqual('active', self.bg.store.get('123')['phase'])

    def test_retiring_legacy_tag_retries_after_qb_failure_without_losing_task(self):
        row = self.launch()
        task = self.bg.store.get('123')
        task.pop('shared_qb_tag')
        self.bg.store.put(task)
        row['tags'] = f'{TAG},{task["run_tag"]}'
        self.bg.tag_cleanup_at = 0
        with patch.object(self.qb, 'delete_tags', side_effect=AppError('连接中断')):
            self.bg.tick()
        self.assertTrue(self.bg.store.get('123')['shared_qb_tag'])
        self.assertIn(task['run_tag'], tags(row))
        self.bg.tag_cleanup_at = 0
        self.bg.tick()
        self.assertEqual(TAG, row['tags'])
        self.assertEqual('active', self.bg.store.get('123')['phase'])

    def test_completion_notification_cleans_immediately_and_advances_loop_after_delete_ack(self):
        row = self.launch()
        self.assertEqual('downloading', row['state'])
        row.update(amount_left=0, progress=1, seeding_time=0, state='uploading')
        self.bg.notify({row['hash']})
        self.assertTrue(self.bg.finish_notified())
        self.assertEqual('deleting', self.bg.store.get('123')['phase'])
        self.assertEqual('true', self.qb.calls[-1][2]['deleteFiles'])
        self.bg.tick()
        task = self.bg.store.get('123')
        self.assertEqual(('queued', True, 1), (task['phase'], task['loop'], task['cycles']))

    def test_manual_removal_and_unreachable_snapshot_never_restart_loop(self):
        self.launch()
        self.qb.live.clear()
        self.bg.tick()
        task = self.bg.store.get('123')
        self.assertEqual('removed', task['phase'])
        self.assertFalse(task['loop'])
        before = len(self.qb.calls)
        self.bg.tick()
        self.assertEqual(before, len(self.qb.calls))

    def test_qb_pause_is_not_overridden_and_manual_resume_works(self):
        row = self.launch()
        row['state'] = 'stoppedDL'
        self.bg.tick()
        self.assertTrue(self.bg.store.get('123')['hold_user'])
        self.bg.tick()
        self.assertEqual('stoppedDL', row['state'])
        self.bg.manual('resume', {'id': '123'})
        self.bg.tick()
        self.assertEqual('downloading', row['state'])

    def test_free_expiry_pauses_and_reverification_can_resume(self):
        row = self.launch()
        task = self.bg.store.get('123')
        task['free_end'] = time.time() - 2
        self.bg.store.put(task)
        self.bg.tick()
        self.assertEqual('stoppedDL', row['state'])
        self.assertTrue(self.bg.store.get('123')['free_hold'])
        site = Mock()
        site.detail.return_value = {'id': '123', 'size': 1000, 'createdDate': time.time()-172800,
                                    'status': {'discount': 'FREE'}}
        self.bg.recheck(site, self.bg.store.config(), self.bg.store.get('123'))
        self.bg.tick()
        self.assertEqual('downloading', row['state'])

    def test_auto_scan_ranks_seeders_and_staging_checks_actual_size(self):
        self.settings.save({'api_key': 'mock-token'}, merge=True)
        self.bg.store.save({'automatic': True, 'adult': True, 'max_active': 1})
        self.bg.tick()
        site = Mock()
        site.c = {'api_key': 'mock-token'}
        def item(tid, seeds):
            return {'id': tid, 'size': 1000, 'createdDate': time.time()-172800,
                    'status': {'discount': 'FREE', 'seeders': seeds}}
        site.search.side_effect = lambda mode, page, **kw: [item('1', 8)] if mode == 'normal' else [item('2', 40)]
        site.detail.return_value = item('2', 40)
        site.download.return_value = TORRENT
        config = self.bg.store.config()
        self.bg.scan(site, config, GIB)
        self.assertEqual('queued', self.bg.store.get('2')['phase'])
        self.assertIsNone(self.bg.store.get('1'))
        self.bg.prepare(site, config, self.bg.store.get('2'), GIB)
        self.assertEqual('ready', self.bg.store.get('2')['phase'])
        self.bg.tick()
        self.bg.tick()
        self.assertEqual('active', self.bg.store.get('2')['phase'])

    def test_page_pair_is_merged_and_restarts_use_indexed_round_ids(self):
        self.bg.store.save({'automatic': True, 'adult': True, 'categories': '419', 'min_seeders': 300})
        self.bg.tick()
        site = Mock()
        site.search.side_effect = lambda mode, **kw: [
            {'id': str(index + (1000 if mode == 'adult' else 0)), 'size': 1000,
             'createdDate': time.time() - 200000,
             'status': {'discount': 'FREE', 'seeders': 600 + index * 2 + int(mode == 'adult')}}
            for index in range(100, 0, -1)]
        config = self.bg.store.config()
        self.bg.scan(site, config, GIB)
        self.assertEqual(200, len(self.bg.candidates))
        self.assertEqual(['1100', '100', '1099'], list(self.bg.candidates)[:3])
        self.assertEqual(2, site.search.call_count)
        self.assertEqual([419], site.search.call_args_list[0].kwargs['categories'])
        self.assertEqual('SEEDERS', site.search.call_args_list[0].kwargs['sort'])
        self.assertEqual(100, site.search.call_args_list[0].kwargs['page_size'])
        first = self.bg.store.get('1100')
        first['phase'] = 'finished'
        self.bg.store.put(first)
        restarted = Background(self.home, self.settings, self.db)
        restarted.scan(site, config, GIB)
        self.assertEqual('queued', restarted.store.get('100')['phase'])
        self.assertEqual('finished', restarted.store.get('1100')['phase'])
        self.assertEqual(2, site.search.call_count)
        self.assertEqual({'1100', '100'}, restarted.store.round_used(['1100', '100', '9999']))

    def test_completed_retained_content_moves_without_stop_or_delete(self):
        row = self.launch()
        self.bg.store.save({'retain_path': str(self.home / 'retained')})
        self.bg.manual('retain', {'id': '123', 'enabled': True})
        row.update(amount_left=0, progress=1, state='uploading')
        start = len(self.qb.calls)
        self.bg.notify({row['hash']})
        self.bg.finish_notified()
        task = self.bg.store.get('123')
        self.assertEqual('moving', task['phase'])
        self.assertEqual(['move'], [item[0] for item in self.qb.calls[start:]])
        moved = Path(task['move_target'])
        moved.mkdir(parents=True)
        (moved / 'file').touch()
        row.update(save_path=str(moved), content_path=str(moved / 'file'))
        self.bg.tick()
        self.assertEqual('retained', self.bg.store.get('123')['phase'])
        self.assertTrue((moved / 'file').is_file())
        self.assertFalse(any(action == 'delete' for action, *_ in self.qb.calls))

    def test_foreground_conflict_releases_only_owned_task(self):
        row = self.launch()
        untouched = 'a' * 40
        self.qb.live[untouched] = {'hash': untouched, 'state': 'uploading'}
        self.db.observe({'id': '123', 'name': 'foreground', 'size': 1000}, 'pending', '')
        self.bg.tick()
        self.bg.tick()
        self.assertEqual('conflict', self.bg.store.get('123')['phase'])
        self.assertIn(untouched, self.qb.live)
        self.assertNotIn(row['hash'], self.qb.live)

    def test_slow_seed_is_recorded_separately(self):
        row = self.launch()
        self.bg.store.save({'slow_minutes': 1})
        task = self.bg.store.get('123')
        task.update(slow_elapsed=58, slow_sample=time.time() - 5)
        self.bg.store.put(task)
        self.bg.tick()
        self.bg.tick()
        self.assertEqual('slow', self.bg.store.get('123')['phase'])
        self.assertNotIn(row['hash'], self.qb.live)

    def test_expansion_waits_and_adds_at_most_one(self):
        self.launch()
        future = self.bg.next_expand
        self.bg.tick()
        self.assertEqual(1, self.bg.monitor['concurrency'])
        with patch('ptfree.background.time.time', return_value=future + 1):
            self.bg.tick()
            self.assertEqual(2, self.bg.monitor['concurrency'])
            self.assertEqual(1, self.bg.monitor['slots'])

    def test_freeseed_reclaims_enough_oldest_bytes_and_preserves_unowned(self):
        qb = self.qb
        root = self.home / 'freeseed'
        config = {'managed_gib': 10, 'managed_full': 'oldest'}
        url = self.bg.store.config()['qb_url']
        for i, size in enumerate((GIB, GIB, 5*GIB), 1):
            key = str(i) * 40
            token = f'mtfree-fs-test-{i}'
            path = str(root / token)
            item = {'id': str(i), 'run_tag': token, 'path': path, 'size': size, 'hashes': [key],
                    'url': url, 'issued': time.time()-10+i, 'added_on': i}
            put(self.db, item)
            qb.live[key] = {'hash': key, 'tags': FREESEED_TAG+','+token, 'save_path': path, 'download_path': path,
                            'added_on': i, 'state': 'uploading', 'progress': 1}
        qb.live['f'*40] = {'hash': 'f'*40, 'state': 'uploading'}
        with patch('ptfree.freeseed_storage.client', return_value=(qb, root, url)), \
             patch('ptfree.freeseed_storage.inspect_torrent', return_value=(inspect_torrent(TORRENT), 6*GIB)):
            result = deliver(self.db, self.settings, config, '7', TORRENT)
        self.assertIn('已加入', result)
        self.assertEqual(['1'*40, '2'*40, '3'*40], [key for action, key, params in qb.calls if action == 'delete'])
        self.assertIn('f'*40, qb.live)

    def test_completion_registration_does_not_replace_other_program(self):
        qb = Mock()
        qb.preferences = {'autorun_program': '/srv/existing-hook', 'autorun_enabled': True}
        self.assertIn('其他完成程序', configure(qb, 'http://127.0.0.1:8080', self.home, True, ''))
        qb.set_preferences.assert_not_called()
        qb.preferences = {}
        self.assertIn('已注册', configure(qb, 'http://127.0.0.1:8080', self.home, True, ''))
        self.assertIn('%I', qb.set_preferences.call_args.kwargs['autorun_program'])

    def test_disconnect_does_not_mark_tasks_removed(self):
        self.launch()
        with patch.object(self.qb, 'torrent_info', side_effect=AppError('失联')):
            with self.assertRaises(AppError):
                self.bg.tick()
        task = self.bg.store.get('123')
        self.assertEqual('active', task['phase'])
        self.assertTrue(task['loop'])
        self.bg.tick()
        self.assertEqual('active', self.bg.store.get('123')['phase'])

    def test_capacity_check_and_duplicate_never_modify_preexisting_seed(self):
        self.ready(size=2 * GIB)
        self.bg.store.save({'capacity_gib': 1})
        self.bg.tick()
        self.assertFalse(self.qb.calls)
        task = self.bg.store.get('123')
        hash_value = task['hashes'][0]
        self.bg.store.save({'capacity_gib': 10})
        self.qb.live[hash_value] = {'hash': hash_value, 'tags': TAG, 'state': 'uploading'}
        self.bg.tick()
        self.assertFalse(self.qb.calls)
        self.assertFalse(self.bg.store.get('123')['loop'])

    def test_manual_delete_refuses_to_touch_changed_path(self):
        row = self.launch()
        row['save_path'] = str(self.home / 'other')
        self.bg.manual('delete', {'id': '123'})
        self.bg.tick()
        self.assertFalse(any(action == 'delete' for action, *_ in self.qb.calls))
        self.assertTrue(self.bg.store.get('123')['cleanup_hold'])
        self.bg.manual('remove', {'id': '123'})
        self.bg.tick()
        self.assertEqual(('delete', {'deleteFiles': 'false'}), (self.qb.calls[-1][0], self.qb.calls[-1][2]))

    def test_ram_auto_chooses_and_remembers_path_without_user_input(self):
        selected = self.home / 'autoram'
        self.bg.store.save({'storage': 'ram', 'ram_path': '', 'ram_create': True})
        with patch('ptfree.background.automatic_ram_path', return_value=selected) as choose, \
             patch('ptfree.background_storage.ram_available',
                   side_effect=[AppError('未挂载'), (64 * GIB, False, 'ram-medium')]), \
             patch('ptfree.background_storage.create_ram', side_effect=lambda path, *_: path.mkdir()):
            result = self.bg.ram_setup()
        choose.assert_called_once()
        self.assertEqual(result['storage_path'], str(selected))
        self.assertEqual(self.bg.store.config()['ram_path'], '')
        self.assertEqual(self.db.get('background_ram_auto_path'), str(selected))
        self.assertEqual((selected / '.mtfree-volume').read_text(), self.bg.store.marker)
        with patch('ptfree.background.automatic_ram_path', return_value=selected) as choose, \
             patch('ptfree.background_storage.ram_available', return_value=(64 * GIB, False, 'ram-medium')):
            self.bg.tick()
        self.assertEqual(choose.call_args.args[0], str(selected))
        self.assertEqual(self.bg.monitor['storage_path'], str(selected))
        self.assertTrue(self.bg.monitor['storage_ok'])

    def test_disable_automatic_discovery_keeps_manual_preparation(self):
        self.settings.save({'api_key': 'test-key'}, merge=True)
        self.bg.store.save({'automatic': False, 'normal': False, 'adult': False})
        self.bg.monitor.update(qb_online=True, storage_ok=True, slots=1, limit_mbps=1, room=GIB)
        self.bg.manual('add', {'id': '123', 'loop': False})
        with patch.object(self.bg, 'prepare') as prepare, patch.object(self.bg, 'scan') as scan:
            self.bg.site_tick()
        self.assertEqual(prepare.call_args.args[2]['id'], '123')
        scan.assert_not_called()

    def test_windows_ram_auto_skips_an_occupied_drive_without_switching_active_tasks(self):
        mask = 1 << (ord('R') - ord('A'))
        with patch('ptfree.background_storage.os', SimpleNamespace(name='nt')), \
             patch('ptfree.background_storage.Path', PureWindowsPath), \
             patch('ptfree.background_storage.drive_mask', return_value=mask), \
             patch('ptfree.background_storage.owned_ram', return_value=False):
            self.assertEqual(automatic_ram_path(r'R:\MTFree', 'owner'), PureWindowsPath(r'S:\MTFree'))
            self.assertEqual(automatic_ram_path(r'R:\MTFree', 'owner', pinned=True), PureWindowsPath(r'R:\MTFree'))

    def test_immediate_qb_check_refreshes_even_in_same_sampling_minute(self):
        self.bg.store.save({'enabled': False})
        self.bg.tick()
        with patch.object(self.qb, 'connect', wraps=self.qb.connect) as connection:
            self.bg.request_poll(force_qb=True)
            self.bg.tick()
        connection.assert_called_once()
        self.assertFalse(self.bg.force_qb_check.is_set())
        self.assertIn('last_checked', self.bg.monitor)

    def test_ram_mount_failure_and_replacement_prevent_fallback(self):
        self.bg.store.save({'storage': 'ram', 'ram_path': str(self.home / 'data'), 'ram_create': False})
        with patch('ptfree.background_storage.ram_available', return_value=(64 * GIB, False, 'one')):
            row = self.launch()
        with patch('ptfree.background_storage.ram_available', side_effect=AppError('未挂载')):
            self.bg.tick()
        self.assertEqual('stoppedDL', row['state'])
        with patch('ptfree.background_storage.ram_available', return_value=(64 * GIB, False, 'two')):
            self.bg.tick()
        self.assertEqual('stoppedDL', row['state'])
        self.assertIn('上一轮数据不可用', self.bg.store.get('123')['note'])

    def test_background_yields_when_foreground_job_has_site_priority(self):
        self.db.queue_job('scan')
        with self.assertRaises(AppError) as raised:
            self.bg.site_store.reserve_request('detail')
        self.assertEqual('priority', raised.exception.source)
        self.assertEqual(0, self.db.get('blocked_until', 0))

    def test_background_yields_when_foreground_uses_line_headroom(self):
        row = self.launch()
        foreground = 'f' * 40
        probe = {'id': '98', 'name': 'foreground', 'size': 1}
        self.db.observe(probe, 'done', '')
        self.db.set_state('98', 'done', '', hashes=foreground)
        self.qb.live[foreground] = {'hash': foreground, 'dlspeed': 30_000_000, 'state': 'downloading'}
        with patch.object(self.bg, 'router_sample', return_value=((645.0, 50), 2, '')):
            self.bg.tick()
        self.assertEqual('stoppedDL', row['state'])
        self.assertIn('让道', self.bg.monitor['reason'])

    def test_peak_uses_covered_contiguous_intervals_and_idle_time(self):
        now = 1795226400
        start = now // 60 * 60 - 300
        for i in range(60):
            stamp = start + 5 * i
            self.bg.store.add_interval(stamp, stamp + 5, wan=(625_000_000, 12_500_000),
                                       job=(1181250000, 2) if i < 30 else (0, 0))
        history = self.bg.store.history('all', now=now)
        self.assertAlmostEqual(1000, history['peak_mbps'])
        self.assertAlmostEqual(945, history['job_average_mbps'])
        self.assertAlmostEqual(94.5, history['index'])
        self.assertEqual(1.0, history['job_coverage'])
        with self.db.connect() as conn:
            conn.execute('DELETE FROM background_minutes WHERE minute=?', (start + 120,))
        self.assertIsNone(self.bg.store.history('all', now=now)['peak_mbps'])

    def test_qb_test_uses_draft_without_saving_or_leaking_saved_credentials(self):
        self.bg.store.save({'qb_password': 'test-saved-pass'})
        with patch('ptfree.background.QB') as client:
            client.return_value.connect.return_value = 'v5.2.0'
            self.bg.qb_test({'qb_url': 'http://127.0.0.1:8091', 'qb_user': 'admin', 'qb_password': ''})
            client.assert_called_once_with('http://127.0.0.1:8091', 'admin', '', '')
            client.reset_mock()
            self.bg.qb_test({'qb_password': ''})
            client.assert_called_once_with('http://127.0.0.1:8180', '', 'test-saved-pass', '')
        self.assertEqual('http://127.0.0.1:8180', self.bg.store.config()['qb_url'])

    def test_qb_api_key_is_encrypted_and_used_by_background(self):
        key = 'qbt_' + 'K' * 28
        self.bg.store.save({'qb_auth': 'api_key', 'qb_api_key': key})
        stored = self.db.get('background_config')
        self.assertNotEqual(key, stored['qb_api_key'])
        public = self.bg.status()['config']
        self.assertNotIn('qb_api_key', public)
        self.assertTrue(public['qb_api_key_set'])
        with patch('ptfree.background.QB') as client:
            self.bg.get_qb(self.bg.store.config())
            client.assert_called_once_with(public['qb_url'], '', '', key)


    def test_secrets_are_encrypted_and_web_view_is_masked(self):
        self.bg.store.save({'qb_password': 'password-qb-123', 'ros_password': 'password-ros-123'})
        self.assertNotIn(b'password-qb-123', self.db.path.read_bytes())
        self.assertNotIn('password-qb-123', json.dumps(self.bg.status()))
        self.assertTrue(self.bg.status()['config']['qb_password_set'])


class UploadPriorityTests(unittest.TestCase):
    def setUp(self):
        self.control = UploadPriority(650)

    def window(self, start, up, down, *, other=0, wan_up=64, high_latency=False, ceiling=650, line_down=900):
        samples = (up,) * 7 if isinstance(up, (int, float)) else up
        for offset, upload in zip(range(0, 91, 15), samples, strict=True):
            rate = self.control.observe(start + offset, ceiling, line_down, upload, down, other,
                                        wan_up, 70, high_latency)
        return rate

    def test_real_upload_drop_triggers_reduction_then_recovers_in_steps(self):
        self.assertEqual(350, self.window(0, 50, 40))
        self.assertEqual(45, self.control.target)
        self.assertEqual(400, self.window(105, (50, 50, 50, 50, 1, 1, 1), 340))
        self.assertEqual(270, self.window(210, 15, 360, wan_up=12))
        self.assertTrue(self.control.hold_new)
        self.assertEqual(270, self.window(315, 46, 255))
        self.assertEqual('protect', self.control.state)
        self.assertEqual(320, self.window(420, 47, 255))
        self.assertFalse(self.control.hold_new)

    def test_no_upload_recovery_restores_cap_and_relearns_demand(self):
        self.window(0, 60, 40)
        self.assertEqual(247.5, self.window(105, 11, 330))
        self.assertEqual(350, self.window(210, 10, 230))
        self.assertEqual(9, self.control.target)
        self.assertEqual(400, self.window(315, 12, 340))
        self.assertFalse(self.control.hold_new)

    def test_missing_upload_sample_and_extra_fast_polls_do_not_adjust_limit(self):
        self.window(0, 40, 40)
        self.assertEqual(350, self.window(105, (None,) * 7, 340))
        self.assertTrue(self.control.hold_new)
        for now in range(196, 211):
            rate = self.control.observe(now, 650, 900, 2, 340, 0, None, 70, False)
            self.assertEqual(350, rate)
        self.assertEqual(36, self.control.target)

    def test_router_downlink_usage_adjusts_budget_even_when_upload_is_stable(self):
        self.window(0, 55, 40)
        self.assertEqual(400, self.window(105, 55, 330, other=200))
        self.assertEqual(200, self.window(210, 55, 390, other=700))
        self.assertEqual(10, self.window(315, 55, 190, other=900))
        self.assertEqual(60, self.window(420, 55, 10, other=0, wan_up=None))

    def test_saturated_uplink_and_high_latency_make_confirmed_drop_probe_stronger(self):
        self.window(0, 55, 40)
        self.assertAlmostEqual(231, self.window(105, 35, 330, wan_up=69, high_latency=True))

    def test_adjustments_wait_for_full_window_and_settling_time(self):
        self.assertEqual(350, self.window(0, 55, 40))
        for now in range(105, 195, 15):
            self.assertEqual(350, self.control.observe(now, 650, 900, 12, 320, 0, 68, 70, False))
            self.assertFalse(self.control.hold_new)
        self.assertEqual(240, self.control.observe(195, 650, 900, 12, 320, 0, 68, 70, False))

        self.assertEqual(240, self.control.observe(205, 650, 900, 11, 224, 0, 68, 70, False))
        for now in range(210, 300, 15):
            self.assertEqual(240, self.control.observe(now, 650, 900, 11, 224, 0, 68, 70, False))
            self.assertTrue(self.control.hold_new)
        self.assertEqual(350, self.control.observe(300, 650, 900, 11, 224, 0, 68, 70, False))
        self.assertFalse(self.control.hold_new)

    def test_low_upload_demand_uses_download_budget_without_slow_ramp(self):
        self.assertEqual(650, self.window(0, 0.4, 45))
        self.assertEqual(200, self.window(105, 0.7, 550, other=700))
        self.assertEqual(650, self.window(210, 0.7, 180))
        self.assertFalse(self.control.hold_new)

    def test_upload_drop_does_not_throttle_a_download_that_was_already_slow(self):
        self.assertEqual(350, self.window(0, 50, 40))
        self.assertEqual(400, self.window(105, 8, 32, wan_up=15))
        self.assertIsNone(self.control.trial)
        self.assertFalse(self.control.hold_new)


class QBYieldTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        self.settings = Settings(home)
        self.db = Store(home)
        self.db.init()
        self.bg = Background(home, self.settings, self.db)
        self.url = 'http://127.0.0.1:8091'
        self.bg.store.save({'qb_url': self.url, 'qb_yield_enabled': True, 'latency_peer': 'test.example'})
        self.rtt = 12
        self.guard = QBYield(self.bg.store, lambda _: self.rtt)
        self.client = Mock()
        self.prefs = {'dl_limit': 0, 'up_limit': 4096, 'alt_dl_limit': 50000, 'alt_up_limit': 3072}
        self.client.get_preferences.side_effect = lambda: self.prefs.copy()
        self.client.set_preferences.side_effect = lambda **values: self.prefs.update(values)
        self.client.transfer_info.return_value = {'dl_info_speed': 30_000 * 1024, 'up_info_speed': 2048 * 1024}
        self.client.request.return_value = b'0'
        mock_client = patch.object(self.guard, 'qb', return_value=self.client)
        mock_client.start()
        self.addCleanup(mock_client.stop)
        for _ in range(6):
            self.guard.tick()

    def congest(self):
        self.rtt = 90
        self.guard.tick()
        self.guard.tick()

    def test_global_caps_act_on_download_and_upload_without_background_tasks(self):
        self.assertFalse(self.bg.store.config()['enabled'])
        self.assertFalse(self.client.set_preferences.called)
        self.congest()
        self.assertEqual((23400, 1597), (self.prefs['dl_limit'], self.prefs['up_limit']))
        self.assertTrue(self.db.get(LEASE))
        self.rtt = 12
        self.guard.tick()
        self.assertEqual(23400, self.prefs['dl_limit'])
        for _ in range(8):
            self.guard.tick()
        self.assertEqual((0, 4096), (self.prefs['dl_limit'], self.prefs['up_limit']))
        self.assertIsNone(self.db.get(LEASE))

    def test_disable_restores_only_values_still_owned(self):
        self.congest()
        self.prefs['dl_limit'] = 22500
        self.bg.store.save({'qb_yield_enabled': False})
        self.guard.tick()
        self.assertEqual((22500, 4096), (self.prefs['dl_limit'], self.prefs['up_limit']))
        self.assertIsNone(self.db.get(LEASE))

    def test_upload_priority_restores_and_suspends_global_qb_caps(self):
        self.congest()
        self.bg.store.save({'enabled': True, 'save_path': '/test/downloads', 'bandwidth_mode': 'upload_priority'})
        self.guard.tick()
        self.assertEqual((0, 4096), (self.prefs['dl_limit'], self.prefs['up_limit']))
        self.assertIsNone(self.db.get(LEASE))
        self.assertIn('上传优先', self.guard.state['message'])
        self.client.set_preferences.reset_mock()
        for _ in range(9):
            self.guard.tick()
        self.client.set_preferences.assert_not_called()

    def test_uncertain_update_is_recovered_after_restart_even_in_other_speed_mode(self):
        self.client.request.return_value = b'1'

        def interrupted(**values):
            self.prefs.update(values)
            raise AppError('连接中断', 'qb')

        self.client.set_preferences.side_effect = interrupted
        with self.assertRaisesRegex(AppError, '连接中断'):
            self.congest()
        self.assertIn('pending', self.db.get(LEASE)['entries']['alt_dl_limit'])
        self.client.request.return_value = b'0'
        self.client.set_preferences.side_effect = lambda **values: self.prefs.update(values)
        new_guard = QBYield(self.bg.store, lambda _: 12)
        with patch.object(new_guard, 'qb', return_value=self.client):
            new_guard.tick()
        self.assertEqual((0, 4096, 50000, 3072), tuple(self.prefs.values()))
        self.assertIsNone(self.db.get(LEASE))

    def test_timed_out_probes_release_caps(self):
        self.congest()
        self.rtt = None
        for _ in range(4):
            self.guard.tick()
        self.assertEqual((0, 4096), (self.prefs['dl_limit'], self.prefs['up_limit']))
        self.assertIsNone(self.db.get(LEASE))

    def test_legacy_router_write_is_rejected_before_opening_connection(self):
        with patch('ptfree.background.RouterOS') as router:
            with self.assertRaisesRegex(AppError, '已停用'):
                self.bg.router_action('apply', {})
        router.assert_not_called()


class QBProtocolTests(unittest.TestCase):
    def test_bearer_api_connect_and_command_without_cookie_login(self):
        key = 'qbt_' + 'A' * 28
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_api(self):
                seen.append((self.path, self.headers.get('Authorization'), self.headers.get('Cookie')))
                expected = {'/api/v2/app/version': b'v5.2.0', '/api/v2/app/preferences': b'{}',
                            '/api/v2/torrents/stop': b''}
                valid = self.headers.get('Authorization') == 'Bearer ' + key and self.path in expected
                self.send_response(200 if valid else 401)
                self.end_headers()
                if valid:
                    self.wfile.write(expected[self.path])

            do_GET = handle_api
            do_POST = handle_api

        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            address = f'http://127.0.0.1:{server.server_port}'
            try:
                client = QB(address, '', '', key)
                self.assertEqual('v5.2.0', client.connect())
                client.action('stop', 'a' * 40)
                with self.assertRaisesRegex(AppError, '拒绝 API 密钥'):
                    QB(address, '', '', 'qbt_' + 'B' * 28).connect()
            finally:
                server.shutdown()
                thread.join(3)
        self.assertEqual(['/api/v2/app/version', '/api/v2/app/preferences', '/api/v2/torrents/stop',
                          '/api/v2/app/version'], [path for path, *_ in seen])
        self.assertTrue(all(cookie is None for _, _, cookie in seen))

    def test_http_login_full_sync_add_and_precise_delete(self):
        received = {}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def send_data(self, status, data, cookie=None):
                self.send_response(status)
                if cookie:
                    self.send_header('Set-Cookie', cookie)
                self.end_headers()
                self.wfile.write(data)
            def do_GET(self):
                if self.headers.get('Cookie') != 'SID=test-id':
                    return self.send_data(403, b'')
                path = self.path.split('?', 1)[0].rsplit('/', 1)[-1]
                payloads = {'version': b'v5.1.4', 'preferences': b'{}',
                            'maindata': b'{"full_update":true,"server_state":{"free_space_on_disk":100}}'}
                self.send_data(200, payloads[path])
            def do_POST(self):
                path = self.path.rsplit('/', 1)[-1]
                data = self.rfile.read(int(self.headers['Content-Length']))
                if path == 'login':
                    received['login'] = parse_qs(data.decode())
                    return self.send_data(200, b'Ok.', 'SID=test-id; HttpOnly')
                if path == 'add':
                    part = BytesParser(policy=policy.default).parsebytes(
                        b'MIME-Version: 1.0\r\nContent-Type: ' + self.headers['Content-Type'].encode() + b'\r\n\r\n' + data)
                    received['add'] = {p.get_param('name', header='content-disposition'): p.get_payload(decode=True)
                                       for p in part.iter_parts()}
                else:
                    received[path] = parse_qs(data.decode())
                self.send_data(200, b'Ok.')
        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = QB(f'http://127.0.0.1:{server.server_port}', 'qb', 'pass')
                self.assertEqual('v5.1.4', client.connect())
                self.assertEqual({}, client.snapshot()[0])
                client.add(TORRENT, '/mnt/ram/one', None, 1000)
                client.delete_tags({'mtfree-tag'})
                client.action('delete', 'a' * 40, deleteFiles='false')
                client.set_preferences(dl_limit=16000, up_limit=512)
            finally:
                server.shutdown()
                thread.join(3)
        self.assertEqual({'username': ['qb'], 'password': ['pass']}, received['login'])
        self.assertEqual(TORRENT, received['add']['torrents'])
        self.assertEqual(b'true', received['add']['stopped'])
        self.assertEqual(TAG.encode(), received['add']['tags'])
        self.assertEqual({'tags': ['mtfree-tag']}, received['deleteTags'])
        self.assertEqual(b'/mnt/ram/one', received['add']['downloadPath'])
        self.assertEqual({'hashes': ['a' * 40], 'deleteFiles': ['false']}, received['delete'])
        self.assertEqual({'dl_limit': 16000, 'up_limit': 512}, json.loads(received['setPreferences']['json'][0]))


class RulesTests(unittest.TestCase):
    def test_legacy_rollback_only_removes_its_queues_and_marks(self):
        router = Mock()
        state = {'/queue/tree': [
                    {'.id': '*root', 'name': 'mtf-test-down', 'parent': 'bridge', 'comment': 'mtf-test:down'},
                    {'.id': '*leaf', 'name': 'mtf-test-down-small', 'parent': 'mtf-test-down', 'comment': 'mtf-test:down:small'},
                    {'.id': '*old', 'name': 'custom', 'parent': 'other'},
                 ],
                 '/ip/firewall/mangle': [{'.id': '*v4', 'comment': 'mtf-test:down'}],
                 '/ipv6/firewall/mangle': [{'.id': '*v6', 'comment': 'mtf-test:down'}]}
        removed = []

        def call(command, **params):
            section, action = command.rsplit('/', 1)
            if action == 'print':
                return list(state[section])
            if action == 'remove':
                removed.append(params['.id'])
                state[section][:] = [row for row in state[section] if row['.id'] != params['.id']]

        router.call.side_effect = call
        remove_rules(router, 'mtf-test', True)
        self.assertEqual(['*leaf', '*root', '*v4', '*v6'], removed)
        self.assertEqual('*old', state['/queue/tree'][0]['.id'])
        self.assertEqual(1, len(state['/queue/tree']))

    def test_legacy_rollback_leaves_foreign_children_alone(self):
        router = Mock()
        router.call.return_value = [
            {'.id': '*root', 'name': 'owned', 'comment': 'mtf-test:down'},
            {'.id': '*custom', 'name': 'custom', 'parent': 'owned'},
        ]
        with self.assertRaisesRegex(AppError, '手动添加的子队列'):
            remove_rules(router, 'mtf-test', False)
        self.assertEqual(1, router.call.call_count)

    def test_timed_free_routing_and_path_checks_both_directories(self):
        now = int(time.time())
        item = {'free': True, 'visible': True, 'banned': False, 'hr': False,
                'size': 100 * GIB, 'free_end': now+21600, 'created': now-172800}
        self.assertEqual(('freeseed', now+21600), classify(item, DEFAULTS, now))
        event = dict(DEFAULTS, site_free_start=dt.datetime.fromtimestamp(now-20, dt.timezone.utc).isoformat(), site_free_end=dt.datetime.fromtimestamp(now+21600, dt.timezone.utc).isoformat())
        self.assertEqual('', eligible(item, event, now))
        self.assertEqual(('background', now+21600), classify({**item, 'free_end': None}, event, now))
        self.assertEqual(('freeseed', now+21600), classify({**item, 'created': now-3600}, event, now))
        self.assertFalse(owned_path('/one', {'save_path': '/one', 'download_path': '/ssd'}, True))
        self.assertFalse(owned_path('/one', {'save_path': '/one', 'content_path': '/one/../ssd'}, True))
