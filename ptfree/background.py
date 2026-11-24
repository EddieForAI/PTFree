# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from statistics import median

from .background_storage import automatic_ram_path, capacity, cleanup_empty_root, owned_path, storage_usage
from .background_store import ACTIVE, GIB, BackgroundStore, qpath
from .background_selection import PAGE_SIZE, classify, search_page, site_window, size_reason
from .common import AppError, WINDOWS_NO_CONSOLE, atomic_write, new_id, safe_error
from .completion import command as completion_command, configure as configure_completion
from .network import MTeam
from .policy import matches, normalize
from .qb import QB, QB_FIELDS, TAG, connection_args, hash_keys, hashes, local_endpoint, stopped, tags, validate_connection
from .qb_yield import QBYield
from .routeros import RouterOS, remove_rules
from .store import Store
from .torrent import inspect_torrent
from .traffic import collect_foreground, counters as traffic_counters, identity as traffic_identity, delta as traffic_delta
from .upload_priority import UploadPriority

POLL = 15
IDLE_POLL = 60
EXPAND_SECONDS = 30
MUTABLE = ('queued', 'ready', *ACTIVE)
LEGACY_TAG = re.compile(r'mtfree-[0-9a-f]{24}')


def ident(tid):
    tid = str(tid)
    if not re.fullmatch('[0-9]{1,30}', tid):
        raise AppError('请输入有效的 M-Team 种子 ID', 'config')
    return tid


def next_cycle(task, message):
    return {'id': task['id'], 'name': task['name'], 'size': task['size'],
            'phase': 'queued' if task['loop'] else 'finished', 'loop': task['loop'],
            'cycles': task.get('cycles', 0) + 1, 'manual': task.get('manual', False),
            'note': message, 'retry': time.time() + 30, 'revision': new_id()}


def eligible(item, config, now):
    if classify(item, config, now)[0] != 'background':
        return '未确认属于后台可下载的免费种子'
    return size_reason(item['size'], config)


def check_at(now, end, config):
    interval = config['recheck_minutes'] * 60
    return min(now + interval, end) if end else now + interval


def is_complete(row, size):
    return int(row.get('amount_left', size)) == 0 and float(row.get('progress', 0)) >= 1


def torrent_row(task, lookup):
    return lookup.get(task.get('run_tag'))


def lookup_owned(tasks, live):
    by_key = {key: row for row in live.values() for key in hash_keys(hashes(row))}
    matched = {}
    for task in tasks:
        run_tag = task.get('run_tag')
        if not run_tag:
            continue
        for key in hash_keys(task['hashes']):
            row = by_key.get(key)
            if not row:
                continue
            row_tags = tags(row)
            if TAG not in row_tags:
                continue
            if task.get('qb_hash') and task['qb_hash'] != row['hash']:
                continue
            if task.get('added_on') and task['added_on'] != row.get('added_on'):
                continue
            if task.get('shared_qb_tag'):
                confirmed = task.get('qb_hash') and task.get('added_on')
                if not confirmed and not (task['phase'] == 'adding' and row.get('added_on')
                                          and owned_path(task['path'], row)):
                    continue
            elif run_tag not in row_tags:
                continue
            matched[run_tag] = row
            break
    return matched


def download_limits(tasks, lookup, total, reserve_slot, now):
    """Split the download cap across runnable tasks and a slot about to be submitted."""
    runnable = set()
    waiting = set()
    for task in tasks:
        row = torrent_row(task, lookup)
        if (not row or task['phase'] not in ('adding', 'active') or is_complete(row, task['size'])
                or task.get('pending_delete') or (task.get('hold_user') and stopped(row))
                or task.get('free_hold') or (task.get('free_end') or float('inf')) <= now):
            continue
        if row.get('state') == 'queuedDL':
            waiting.add(task['run_tag'])
        elif not stopped(row) or task['phase'] == 'adding' or task.get('held_by_app') or task.get('resume_user'):
            runnable.add(task['run_tag'])

    # qB can promote a waiting task before the next poll. Give it a small positive
    # cap until then; zero would remove the cap.
    participants = len(runnable) + (reserve_slot and not waiting)
    if not total:
        return runnable, waiting, 0, 0
    waiting_limit = min(1024, max(1, total // max(1, len(waiting) + participants)))
    running_limit = max(1, (total - waiting_limit * len(waiting)) // max(1, participants))
    return runnable, waiting, running_limit, waiting_limit


def ping(host):
    if not host:
        return None
    args = ['-n', '1', '-w', '1200'] if os.name == 'nt' else ['-n', '-c', '1', '-W', '1']
    try:
        result = subprocess.run(['ping', *args, host], capture_output=True, timeout=2,
                                creationflags=WINDOWS_NO_CONSOLE)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(rb'(?:time|\xe6\x97\xb6\xe9\x97\xb4|\xca\xb1\xbc\xe4)\s*([=<])\s*(\d+(?:[.,]\d+)?)\s*ms',
                      result.stdout, re.IGNORECASE)
    if not match or result.returncode:
        return None
    return float(match[2].replace(b',', b'.'))


class Background:
    def __init__(self, home, settings, db):
        self.home, self.settings = home, settings
        self.store = BackgroundStore(db, settings)
        self.site_store = Store(home, background=True)
        self.lock = threading.RLock()
        self.router_lock = threading.Lock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.monitor_wake = threading.Event()
        self.poll_requested = threading.Event()
        self.force_qb_check = threading.Event()
        self.notify_lock = threading.Lock()
        self.notifications = set()
        self.notify_supported = False
        self.hook_status = ''
        self.next_expand = 0
        self.concurrency = 1
        self.line = [0, 0]
        self.next_line = 0
        self.next_ping = 0
        self.last_latency = None
        self.congestion = 0
        self.ping_failures = 0
        self.storage_state = {}
        self.next_storage = 0
        self.next_ram_attempt = 0
        self.qb = None
        self.qb_key = None
        self.tag_cleanup_at = 0
        self.tag_cleanup_error = ''
        self.qb_yield = QBYield(self.store, ping)
        self.attempt_scan = 0
        page = db.get('background_candidate_page') or {}
        self.candidates = {item['id']: item for item in page.get('items', [])}
        self.prev_jobs = None
        self.prev_wan = None
        self.prev_config = None
        self.delay_samples = []
        self.allowed_rate = None
        self.allowed_upload = None
        self.last_policy = None
        self.upload_controller = None
        self.upload_controller_key = None
        self.monitor = {'qb_online': False, 'storage_ok': False, 'reason': '尚未配置后台下载'}
        self.site_status = ''
        self.threads = [threading.Thread(target=self.run_monitor, name='mtfree-monitor', daemon=True),
                        threading.Thread(target=self.run_site, name='mtfree-site', daemon=True)]

    def start(self):
        self.qb_yield.start()
        for thread in self.threads:
            thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        self.monitor_wake.set()
        self.qb_yield.close()
        for thread in self.threads:
            thread.join(timeout=2)

    def status(self):
        tasks = self.store.tasks()
        hidden = {'run_tag', 'revision', 'hashes', 'added_on', 'medium', 'shared_qb_tag'}
        return {'config': self.store.config(public=True), 'monitor': self.monitor, 'site_status': self.site_status,
                'candidates': list(self.candidates.values())[:200],
                'tasks': [{k: v for k, v in task.items() if k not in hidden} for task in tasks],
                'hook_status': self.hook_status, 'qb_yield': self.qb_yield.state.copy(),
                'qos_applied': bool(self.store.db.get('background_qos_applied'))}

    def change_config(self, patch):
        with self.router_lock, self.lock, self.qb_yield.lock:
            before = self.store.config(public=True)
            if patch.get('mteam_enabled') is False:
                patch = {**patch, 'enabled': False}
            saved = self.store.save(patch)
            self.qb_yield.wake.set()
            selection_fields = ('mteam_enabled', 'normal', 'adult', 'categories', 'storage', 'site_free_start', 'site_free_end',
                                'min_seeders', 'min_size_gib', 'max_size_gib', 'capacity_gib', 'ram_capacity_gib')
            if any(before[key] != saved[key] for key in selection_fields):
                self.store.reset_round()
                self.attempt_scan = 0
                self.candidates = {}
            self.wake.set()
            self.next_ram_attempt = 0
            self.request_poll()
            self.store.log('配置已保存')
            return saved

    def manual(self, command, value):
        tid = ident(value.get('id', ''))
        with self.lock:
            task = self.store.get(tid)
            if command == 'add':
                if not self.store.config()['mteam_enabled']:
                    raise AppError('请先在调度与容量中勾选 M-Team', 'config')
                if not isinstance(value.get('loop', False), bool):
                    raise AppError('循环选项格式不正确', 'config')
                if task and task['phase'] in MUTABLE:
                    raise AppError('此种子已在托管队列中，请在列表里操作', 'busy')
                task = {'id': tid, 'name': task['name'] if task else '#' + tid,
                        'size': task['size'] if task else 0, 'phase': 'queued', 'manual': True,
                        'loop': value.get('loop', False), 'cycles': task.get('cycles', 0) if task else 0,
                        'retry': 0, 'note': '等待后台调度', 'revision': new_id()}
            elif not task:
                raise AppError('没有找到该任务', 'config')
            elif command == 'loop':
                if task['phase'] not in MUTABLE or not isinstance(value.get('enabled'), bool):
                    raise AppError('此任务目前无法设置循环', 'config')
                task['loop'] = value['enabled']
            elif command == 'retain':
                if task['phase'] not in ('queued', 'ready', 'adding', 'active') or not isinstance(value.get('enabled'), bool):
                    raise AppError('当前无法设置保留', 'config')
                if value['enabled'] and not self.store.config()['retain_path']:
                    raise AppError('请先设置完成后的转移目录', 'config')
                task['retain'] = value['enabled']
            elif command in ('pause', 'resume'):
                if task['phase'] not in ('adding', 'active', 'queued', 'ready'):
                    raise AppError('此任务目前无法暂停或恢复', 'config')
                task['hold_user'] = command == 'pause'
                if command == 'resume':
                    task.pop('retry', None)
                    if task['phase'] == 'active':
                        task['resume_user'] = True
            elif command in ('remove', 'delete'):
                if task['phase'] == 'moving':
                    raise AppError('qB 正在移动内容，请等待移动完成', 'busy')
                if task['phase'] in ACTIVE:
                    task.update(loop=False, pending_delete=command, note='等待 qB 确认移除')
                elif task['phase'] in ('ready', 'queued'):
                    self.unstage(task['id'])
                    task.update(phase='removed', loop=False, note='已移除队列任务')
                else:
                    raise AppError('此任务已经结束', 'config')
            else:
                raise AppError('未知任务操作', 'config')
            task['revision'] = new_id()
            self.store.put(task)
            self.wake.set()
            self.request_poll()
        return {'message': '已更新任务'}

    def router_action(self, command, value):
        if command not in ('test', 'rollback'):
            raise AppError('旧版 RouterOS 整形已停用，现有规则可撤销', 'config')
        with self.router_lock:
            c = self.store.config()
            if not c['ros_host']:
                raise AppError('请先保存 RouterOS 连接配置', 'config')
            with RouterOS(c) as router:
                if command == 'test':
                    key, rx, tx = router.counters(c['ros_wan'])
                    return {'message': 'RouterOS 连接成功，PPPoE 流量计数可读', 'rx': rx, 'tx': tx}
                remove_rules(router, self.store.marker, c['qos_ipv6'])
                self.store.db.put('background_qos_applied', False)
                self.store.log('已撤销旧版 RouterOS 整形规则')
                return {'message': '本实例创建的旧版整形规则已移除'}

    def get_qb(self, config):
        key = connection_args(config)
        if self.qb_key != key or self.qb is None:
            if self.qb_key != key:
                self.prev_jobs = None
            self.qb = QB(*key)
            self.qb.connect()
            self.qb_key = key
            self.next_storage = 0
            self.storage_state = {}
            self.hook_key = None
            self.tag_cleanup_at = 0
            self.tag_cleanup_error = ''
        return self.qb

    def clean_legacy_tags(self, qb, url, tasks):
        if time.monotonic() < self.tag_cleanup_at:
            return
        self.tag_cleanup_at = time.monotonic() + 60
        try:
            labels = {label for label in qb.list_tags() if LEGACY_TAG.fullmatch(label)}
            if not labels:
                self.tag_cleanup_at = float('inf')
                return
            live, _ = qb.snapshot()
            assigned = {label: set() for label in labels}
            for row in live.values():
                for label in tags(row) & labels:
                    assigned[label].add(row['hash'])

            active = {task['run_tag'] for task in tasks if task.get('run_tag')}
            records = [task for task in (*tasks, *self.store.tasks('retained'))
                       if task.get('qb_url') == url and task.get('run_tag') in labels]
            keys = {key for task in records for key in hash_keys(task['hashes'])}
            owned = lookup_owned(records, qb.torrent_info(keys))
            removable = {label for label, rows in assigned.items() if not rows and label not in active}
            for task in records:
                label = task['run_tag']
                row = owned.get(label)
                if not row or not row.get('added_on') or assigned[label] != {row['hash']}:
                    continue
                locations = [qpath(task['path'])]
                if task.get('move_target'):
                    locations.append(qpath(task['move_target']))
                if qpath(row.get('save_path') or '') not in locations:
                    continue
                # Commit the matching information before changing the qB label.
                if not task.get('shared_qb_tag'):
                    task.update(shared_qb_tag=True, qb_hash=row['hash'], added_on=row['added_on'])
                    self.store.put(task)
                removable.add(label)
            if removable:
                names = sorted(removable)
                for offset in range(0, len(names), 100):
                    qb.delete_tags(names[offset:offset + 100])
                self.store.log(f'已清理 {len(names)} 个旧 qB 任务标签')
            self.tag_cleanup_error = ''
            if active.isdisjoint(labels - removable):
                self.tag_cleanup_at = float('inf')
        except AppError as error:
            message = safe_error(error)
            if message != self.tag_cleanup_error:
                self.store.log('暂缓清理旧 qB 任务标签：' + message, 'warning')
                self.tag_cleanup_error = message

    def storage_config(self, config, *, provision=False, pinned=False):
        if config['storage'] != 'ram' or not config['ram_create'] or config['save_path']:
            return config
        config = config.copy()
        if not local_endpoint(config['qb_url']):
            raise AppError('内存盘要求 PTFree 和 qB 同机，qB 地址须为回环地址', 'storage')
        key = 'background_ram_auto_path'
        chosen = self.store.db.get(key, '')
        if provision:
            actual = str(automatic_ram_path(chosen, self.store.marker, pinned=pinned))
            if actual != chosen:
                self.store.db.put(key, actual)
            chosen = actual
        config.update(save_path=chosen, ram_owner=self.store.marker)
        if chosen and config['retain_path']:
            source, target = map(qpath, (chosen, config['retain_path']))
            if source.is_relative_to(target) or target.is_relative_to(source):
                raise AppError('保留目录需与自动内存盘目录分开', 'storage')
        return config

    def ram_setup(self):
        with self.lock:
            config = self.store.config()
            if config['storage'] != 'ram' or not config['qb_url']:
                raise AppError('请先保存内存盘模式和 qB 连接设置', 'config')
            qb = self.get_qb(config)
            config = self.storage_config(config, provision=True, pinned=bool(self.store.tasks(*ACTIVE)))
            _, free, _ = capacity(config, qb, {}, create=True)
            self.request_poll()
            return {'message': '内存盘已就绪', 'storage_path': config['save_path'], 'free_gib': round(free / GIB, 2)}

    def qb_test(self, draft):
        if set(draft) - QB_FIELDS:
            raise AppError('qB 测试参数无效', 'config')
        saved = self.store.config()
        config = {**saved, **draft}
        url = config['qb_url']
        if not url:
            raise AppError('请填写 qB Web API 地址，例如 http://127.0.0.1:8091', 'config')
        same_endpoint = isinstance(url, str) and url.rstrip('/') == saved['qb_url'].rstrip('/')
        for name in ('qb_password', 'qb_api_key'):
            if name in draft and draft[name] == '' and same_endpoint:
                config[name] = saved[name]
            elif not same_endpoint and name not in draft:
                config[name] = ''
        validate_connection(config)
        version = QB(*connection_args(config)).connect()
        return {'message': 'qB 连接成功。保存连接设置后供后台使用。', 'version': version}

    def cache(self, tid):
        return self.home / 'background-cache' / (ident(tid) + '.torrent')

    def unstage(self, tid):
        self.cache(tid).unlink(missing_ok=True)

    def request_poll(self, *, force_qb=False):
        if force_qb:
            self.force_qb_check.set()
        self.poll_requested.set()
        self.monitor_wake.set()

    def notify(self, values):
        with self.notify_lock:
            if len(self.notifications) < 256:
                self.notifications.update(values)
        self.monitor_wake.set()

    def foreground(self, tasks):
        by_id = self.store.foreground_ids(task['id'] for task in tasks)
        by_hash = self.store.foreground_hashes({key for task in tasks for key in hash_keys(task['hashes'])})
        return by_id | {task['id'] for task in tasks if hash_keys(task['hashes']) & by_hash}

    def finish_notified(self):
        with self.notify_lock:
            values = self.notifications
            self.notifications = set()
        if not values:
            return False
        with self.lock:
            c = self.store.config()
            tasks = [task for task in self.store.tasks(*ACTIVE) if hash_keys(task.get('hashes', ())) & values]
            if not c['qb_url'] or not tasks:
                return True
            qb = self.get_qb(c)
            keys = set().union(*(hash_keys(task['hashes']) for task in tasks))
            live = qb.torrent_info(keys)
            lookup = lookup_owned(tasks, live)
            foreground = self.foreground(tasks)
            for task in tasks:
                row = torrent_row(task, lookup)
                if row and is_complete(row, task['size']):
                    current = traffic_counters(row)
                    identity = traffic_identity(row)
                    if self.prev_jobs and current is not None and identity is not None:
                        sampled_at, samples = self.prev_jobs
                        stamp = time.time()
                        self.store.add_interval(sampled_at, stamp,
                                                job=traffic_delta(identity, current, samples, sampled_at), extra=True)
                        samples[identity] = current
                    upload = None if c['bandwidth_mode'] == 'upload_priority' else 0
                    self.reconcile(qb, c, task, lookup, keys, 0, upload, self.monitor.get('storage_ok', False),
                                   self.monitor.get('medium'), foreground)
        return True

    def run_monitor(self):
        backoff = 0
        next_poll = 0
        while not self.stop.is_set():
            self.monitor_wake.clear()
            now = time.time()
            try:
                self.finish_notified()
                if now >= next_poll or self.poll_requested.is_set() and (not backoff or self.force_qb_check.is_set()):
                    self.poll_requested.clear()
                    self.tick()
                    backoff = 0
                    config = self.store.config()
                    engaged = self.monitor.get('engaged') or config['ros_host'] or config['qb_url'] or config['enabled']
                    delay = config['qb_poll_seconds'] if engaged else IDLE_POLL
                    next_poll = time.time() + delay
                    if self.monitor.get('next_expiry'):
                        next_poll = min(next_poll, self.monitor['next_expiry'])
            except Exception as error:
                self.force_qb_check.clear()
                self.monitor = {**self.monitor, 'qb_online': False, 'storage_ok': False, 'reason': safe_error(error)}
                self.qb = None
                backoff = min(120, max(15, backoff * 2))
                next_poll = time.time() + backoff
            self.monitor_wake.wait(max(.2, next_poll - time.time()))

    def latency_sample(self, c):
        now = time.monotonic()
        if not c['latency_peer'] or now < self.next_ping:
            return self.last_latency if c['latency_peer'] else None
        value = (self.qb_yield.state.get('rtt') if c['qb_yield_enabled'] and c['bandwidth_mode'] == 'shared'
                 else ping(c['latency_peer']))
        self.last_latency = value
        interval = c['latency_interval']
        if value is None:
            self.ping_failures = min(4, self.ping_failures + 1)
            self.next_ping = now + min(3600, interval * 2**self.ping_failures)
            return None
        self.ping_failures = 0
        self.next_ping = now + interval
        normal = median(self.delay_samples) if len(self.delay_samples) >= 6 else None
        if normal is not None and value - normal > c['latency_extra_ms']:
            self.congestion = min(4, self.congestion + 1)
        else:
            self.congestion = max(0, self.congestion - 1)
            self.delay_samples = self.delay_samples[-39:] + [value]
        return value

    def router_sample(self, c):
        key = tuple(c[name] for name in ('ros_host', 'ros_port', 'ros_wan', 'latency_peer'))
        if key != self.prev_config:
            self.prev_wan = None
            self.delay_samples = []
            self.next_ping = self.congestion = self.ping_failures = 0
            self.last_latency = None
            self.prev_config = key
        latency = self.latency_sample(c)
        if not c['ros_host']:
            return None, latency, '未配置 RouterOS'
        try:
            with RouterOS(c) as router:
                interface, rx, tx = router.counters(c['ros_wan'])
        except AppError as error:
            self.prev_wan = None
            return None, latency, safe_error(error)
        stamp = time.time()
        previous = self.prev_wan
        self.prev_wan = stamp, interface, rx, tx
        if not previous or interface != previous[1] or rx < previous[2] or tx < previous[3]:
            return None, latency, '正在建立采样基线'
        elapsed = stamp - previous[0]
        if not 0 < elapsed <= 45:
            return None, latency, '重新建立采样基线'
        wan = rx - previous[2], tx - previous[3]
        self.store.add_interval(previous[0], stamp, wan=wan, latency=latency)
        return tuple(n * 8 / elapsed / 1e6 for n in wan), latency, ''

    def measure_jobs(self, tasks, lookup, foreground=None, retained=None):
        stamp = time.time()
        # Keyed by actual qB instance, not by module or scheduler round. A
        # torrent can be known to both modules but its counters are counted once.
        owned = {}
        for task in tasks:
            row = torrent_row(task, lookup)
            if row and (key := traffic_identity(row)) is not None:
                owned[key] = row
        owned.update(retained or {})
        owned.update(foreground or {})
        counters = {key: value for key, row in owned.items()
                    if (value := traffic_counters(row)) is not None}
        previous = self.prev_jobs
        if previous:
            total = [0, 0]
            for key, values in counters.items():
                for i, n in enumerate(traffic_delta(key, values, previous[1], previous[0])):
                    total[i] += n
            self.store.add_interval(previous[0], stamp, job=tuple(total))
        self.prev_jobs = stamp, counters

    def allowance(self, c, wan, latency, transfer, lookup):
        if not c['enabled']:
            return 0, 0, '后台已暂停'
        # A lightly used line is not evidence of its maximum capacity.
        line_down, line_up = [peak if c['auto_line'] and peak >= fallback * .65 else fallback
                              for peak, fallback in zip(self.line, (c['line_mbps'], c['line_up_mbps']))]
        down = line_down - c['reserve_mbps']
        up = line_up - c['reserve_up_mbps']
        if self.congestion >= 2:
            if c['latency_action'] == 'pause':
                return 0, 0, '延迟持续升高，暂停后台'
            down -= c['latency_reserve_mbps']
            up -= c['latency_reserve_up_mbps']
        if c['ros_host'] and wan is None:
            return 0, 0, '等待 RouterOS 流量数据'
        hosted_rates = [sum(int(row.get(key) or 0) for row in lookup.values()) * 8 / 1e6
                        for key in ('dlspeed', 'upspeed')]
        qb_rates = [int(transfer.get(key) or 0) * 8 / 1e6 for key in ('dl_info_speed', 'up_info_speed')]
        other = [max(0, qb_rates[i] - hosted_rates[i], (wan or (0, 0))[i] - hosted_rates[i])
                 for i in range(2)]
        if wan is not None and wan[0] >= line_down * .97 and qb_rates[0] - hosted_rates[0] > 1:
            return 0, 0, 'Freeseed 或其他 qB 下载正争抢线路，后台让道'
        if wan is None and other[0] > 1:
            return 0, 0, 'qB 有普通下载或 Freeseed 任务，后台让道'
        remaining = min(c['max_mbps'] or float('inf'), down - other[0])
        up_remaining = min(c['max_up_mbps'] or float('inf'), up - other[1])
        if up_remaining < .2 or remaining < 3:
            return 0, 0, 'Freeseed 或家庭流量争抢，后台让道'
        return remaining, up_remaining, '延迟升高，追加家庭预留' if self.congestion >= 2 else '根据可用带宽调度'

    def upload_priority_allowance(self, c, wan, latency, transfer, lookup, now):
        key = tuple(c[name] for name in ('qb_url', 'ros_host', 'ros_port', 'ros_wan', 'line_mbps',
                                        'line_up_mbps', 'reserve_mbps', 'max_mbps'))
        # Keep a small positive download allowance even if all headroom is temporarily gone.
        line_down = max(1, c['line_mbps'] - c['reserve_mbps'])
        ceiling = min(c['max_mbps'] or line_down, line_down)
        if self.upload_controller is None or key != self.upload_controller_key:
            self.upload_controller = UploadPriority(ceiling)
            self.upload_controller_key = key
        controller = self.upload_controller
        rates = {key: transfer.get(key) for key in ('dl_info_speed', 'up_info_speed')}
        rates = {key: value * 8 / 1e6 if type(value) is int and value >= 0 else None
                 for key, value in rates.items()}
        hosted_down = sum(int(row.get('dlspeed') or 0) for row in lookup.values()) * 8 / 1e6
        hosted_up = sum(int(row.get('upspeed') or 0) for row in lookup.values()) * 8 / 1e6
        other_down = max(0, max(wan[0] if wan else 0, rates['dl_info_speed'] or 0) - hosted_down)
        rate = controller.observe(now, ceiling, line_down, rates['up_info_speed'], hosted_down,
                                  other_down, wan[1] if wan else None, c['line_up_mbps'],
                                  latency is not None and self.congestion >= 2)
        details = {'state': controller.state, 'baseline_mbps': controller.baseline, 'target_mbps': controller.target,
                   'qb_upload_mbps': rates['up_info_speed'], 'qb_download_mbps': rates['dl_info_speed'],
                   'hosted_upload_mbps': hosted_up, 'hosted_download_mbps': hosted_down}
        return rate, '上传优先 · ' + controller.reason, controller.hold_new, details

    def finish_delete(self, task):
        self.unstage(task['id'])
        mode = task.get('delete_mode')
        if mode in ('auto', 'delete', 'slow', 'expired', 'conflict'):
            cleanup_empty_root(task['path'], task['qb_url'])
        if mode == 'auto':
            updated = next_cycle(task, '本轮已完成，待开始下一轮' if task['loop'] else '已完成并清理')
        else:
            notes = {'slow': '疑似慢种，已清理', 'expired': '免费已结束，已清理',
                     'conflict': '已释放给 Freeseed', 'remove': '已按手动指令移除', 'delete': '已按手动指令删除'}
            phase = mode if mode in ('slow', 'expired', 'conflict') else 'removed'
            updated = {**task, 'phase': phase, 'loop': False, 'note': notes.get(mode, '任务已移除')}
        self.store.put(updated)

    def delete_one(self, qb, task, row, mode):
        with_files = mode != 'remove'
        require_temp_path = bool(qb.preferences.get('temp_path_enabled')) and float(row.get('progress', 0)) < 1
        if with_files and not owned_path(task['path'], row, require_temp_path):
            task.update(phase='active', note='qB 路径已变化，无法安全删除文件',
                        pending_delete=None, cleanup_hold=True)
            self.store.put(task)
            return
        task.update(phase='deleting', delete_mode=mode, delete_ack=False)
        self.store.put(task)
        qb.action('delete', row['hash'], deleteFiles='true' if with_files else 'false')
        task['delete_ack'] = True
        self.store.put(task)

    @staticmethod
    def queue_action(qb, updates, action, torrent_hash, **fields):
        if updates is None:
            qb.action(action, torrent_hash, **fields)
        else:
            updates.setdefault((action, tuple(fields.items())), []).append(torrent_hash)

    def reconcile(self, qb, c, task, lookup, all_keys, rate, upload_rate, storage_ok, medium,
                  foreground=(), updates=None, starts=None, *, check_slow=True):
        row = torrent_row(task, lookup)
        now = time.time()
        if not row:
            if task['phase'] == 'deleting' and task.get('delete_ack'):
                self.finish_delete(task)
            elif task['phase'] == 'moving':
                task.update(phase='removed', loop=False, note='qB 已移除移动中的任务，请在保留目录检查文件')
                self.store.put(task)
            elif task['phase'] == 'adding' and now < task['issued'] + 15:
                return
            else:
                self.unstage(task['id'])
                note = ('qB 已存在同哈希任务，未取得归属' if hash_keys(task['hashes']) & all_keys
                        else 'qB 已移除任务或无法确认操作，循环已停止')
                task.update(phase='removed', loop=False, note=note)
                self.store.put(task)
            return
        if task['phase'] == 'moving':
            self.reconcile_move(qb, task, row)
            return
        if task.get('medium') and medium and task['medium'] != medium:
            task.update(note='RAM 盘已更换，上一轮数据不可用', phase='active', cleanup_hold=True)
            if task.get('pending_delete') == 'delete':
                task['pending_delete'] = 'remove'
            if not stopped(row) and not task.get('pending_delete'):
                qb.action('stop', row['hash'])
            if not task.get('pending_delete'):
                self.store.put(task)
                return
        if task['phase'] == 'deleting':
            if task.get('delete_mode') != 'remove' and not storage_ok:
                return
            if task.get('delete_mode'):
                self.delete_one(qb, task, row, task['delete_mode'])
            return
        if task.get('pending_delete') == 'delete' and not storage_ok:
            task.update(note='存储不可用，暂缓删除文件')
            self.store.put(task)
            return
        if task.get('pending_delete'):
            self.delete_one(qb, task, row, task['pending_delete'])
            return
        require_temp_path = bool(qb.preferences.get('temp_path_enabled')) and float(row.get('progress', 0)) < 1
        path_ok = owned_path(task['path'], row, require_temp_path)
        fields = {'qb_hash': row['hash'], 'qb_state': row.get('state'), 'added_on': row.get('added_on'),
                  'progress': row.get('progress', 0), 'dlspeed': row.get('dlspeed', 0), 'upspeed': row.get('upspeed', 0),
                  'left': max(0, int(row.get('amount_left', task['size']))), 'phase': 'active'}
        complete = is_complete(row, task['size'])
        initial = task['phase'] == 'adding'
        task.update(fields)
        if initial:
            task['held_by_app'] = True
            self.unstage(task['id'])
        elif stopped(row) and not task.get('held_by_app') and not task.get('resume_user', False):
            task['hold_user'] = True
        elif not stopped(row) and task.get('hold_user') and task.get('paused_observed'):
            task['hold_user'] = False
        if task.pop('resume_user', False):
            task['held_by_app'] = True
        check_age = c['recheck_minutes'] * 60 + 60
        expired = task.get('free_end') and task['free_end'] <= now
        stale = task.get('last_free_at') and now - task['last_free_at'] > check_age
        if not complete and (expired or stale):
            task['free_hold'] = True
        if path_ok and storage_ok and not task.get('cleanup_hold'):
            if task['id'] in foreground:
                self.delete_one(qb, task, row, 'conflict')
                return
            if complete:
                if task.get('retain'):
                    if c['retain_path']:
                        self.move(qb, c, task, row, now)
                        return
                    task['note'] = '等待配置保留目录'
                    self.store.put(task)
                    return
                self.delete_one(qb, task, row, 'auto')
                return
            if (task.get('free_revoked') or (expired and now > task['free_end'] + 120)) and not task.get('retain'):
                self.delete_one(qb, task, row, 'expired')
                return
        if not complete and c['slow_minutes'] and c['slow_mbps'] and not task.get('retain'):
            eligible_sample = (check_slow and c['enabled'] and rate * 8 / 1e6 >= c['slow_mbps'] * 2 and not stopped(row)
                               and row.get('state') in ('downloading', 'stalledDL', 'forcedDL')
                               and not task.get('free_hold') and path_ok and storage_ok)
            last_sample = task.pop('slow_sample', None)
            if eligible_sample:
                task['slow_sample'] = now
                if int(row.get('dlspeed') or 0) * 8 / 1e6 < c['slow_mbps']:
                    task['slow_elapsed'] = task.get('slow_elapsed', 0) + (min(35, now - last_sample) if last_sample else 0)
                else:
                    task['slow_elapsed'] = 0
            if eligible_sample and task.get('slow_elapsed', 0) >= c['slow_minutes'] * 60:
                self.delete_one(qb, task, row, 'slow')
                return
        upload_limit = upload_rate or 0
        bandwidth_hold = upload_rate == 0 or rate <= 0 and not complete
        cause = ('下载路径与预期不一致' if not path_ok else '存储不可用' if not storage_ok
                 else '手动暂停' if task.get('hold_user') else '等待确认免费' if task.get('free_hold') and not complete
                 else '后台模块已停' if not c['enabled']
                 else '带宽让道' if bandwidth_hold else '')
        task['note'] = cause or ('等待核验后清理' if complete else
                                '等待 qB 下载队列' if row.get('state') == 'queuedDL' else '正在下载')
        task['paused_observed'] = stopped(row)
        # Persist intent before performing a command that can survive a connection loss.
        if cause and not stopped(row):
            task['held_by_app'] = not task.get('hold_user')
            self.store.put(task)
            self.queue_action(qb, updates, 'stop', row['hash'])
        elif not cause and stopped(row) and task.get('held_by_app'):
            self.queue_action(qb, updates, 'up-limit', row['hash'], limit=upload_limit)
            if not complete:
                self.queue_action(qb, updates, 'limit', row['hash'], limit=rate)
            self.queue_action(qb, updates, 'start', row['hash'])
            if starts is None:
                task['held_by_app'] = False
            else:
                starts.append(task)
        elif not cause:
            if abs(max(0, int(row.get('up_limit') or 0)) - upload_limit) > upload_limit * .08:
                self.queue_action(qb, updates, 'up-limit', row['hash'], limit=upload_limit)
            dl_limit = int(row.get('dl_limit') or 0)
            if not complete and (dl_limit > rate or dl_limit < rate * .92):
                self.queue_action(qb, updates, 'limit', row['hash'], limit=rate)
        self.store.put(task)

    def move(self, qb, c, task, row, now):
        target = str(qpath(c['retain_path']) / task['run_tag'])
        task.update(phase='moving', move_target=target, move_requested=now, note='qB 正在转移内容')
        self.store.put(task)
        qb.action('move', row['hash'], location=target)

    def reconcile_move(self, qb, task, row):
        if row.get('state') == 'moving':
            return
        target = qpath(task['move_target'])
        content = row.get('content_path')
        arrived = (qpath(row.get('save_path') or '') == target and content
                   and qpath(content).is_relative_to(target))
        if arrived and (not local_endpoint(task['qb_url']) or os.path.exists(content)):
            cleanup_empty_root(task['path'], task['qb_url'])
            task.update(phase='retained', loop=False, note='已转移，后续由 qB 管理')
            self.store.put(task)
        elif time.time() - task['move_requested'] >= 60:
            if owned_path(task['path'], row):
                task['move_requested'] = time.time()
                self.store.put(task)
                qb.action('move', row['hash'], location=task['move_target'])
            else:
                task.update(note='等待确认转移结果，请在 qB 检查', cleanup_hold=True)
                self.store.put(task)

    def submit_ready(self, qb, c, slots, space, live, limit, upload_limit, medium):
        all_keys = {key for row in live.values() for key in hash_keys(hashes(row))}
        for task in self.store.tasks('ready'):
            if slots <= 0 or self.stop.is_set():
                break
            if task.get('hold_user') or task['size'] > space:
                continue
            if task.get('verified_until', 0) <= time.time():
                self.unstage(task['id'])
                task.update(phase='queued', note='等待重新复核免费状态')
                self.store.put(task)
                continue
            if self.foreground([task]) or hash_keys(task['hashes']) & all_keys:
                task.update(phase='removed', loop=False, note='qB 已存在同一个种子，跳过接管')
                self.store.put(task)
                self.unstage(task['id'])
                continue
            cache = self.cache(task['id'])
            if not cache.is_file():
                task.update(phase='queued', note='准备文件已失效，等待重新复核')
                self.store.put(task)
                continue
            run_tag = 'mtfree-' + new_id()
            root = str(qpath(c['save_path']) / run_tag)
            task.update(phase='adding', path=root, qb_url=c['qb_url'], run_tag=run_tag, shared_qb_tag=True,
                        issued=time.time(), qb_hash=None, added_on=None, medium=medium)
            self.store.put(task)
            qb.add(cache.read_bytes(), root, None, limit, upload_limit)
            slots -= 1
            space -= task['size']
            all_keys.update(hash_keys(task['hashes']))
        return slots

    def tick(self):
        c = self.store.config()
        wan, latency, router_note = self.router_sample(c)
        monitor = {'qb_online': False, 'storage_ok': False, 'router_note': router_note,
                   'wan_rx': wan[0] if wan else None, 'wan_tx': wan[1] if wan else None, 'latency': latency,
                   'reason': '尚未配置 qB WebUI', 'slots': 0, 'room': 0}
        if not c['qb_url']:
            self.force_qb_check.clear()
            self.monitor = monitor
            self.prev_jobs = None
            self.upload_controller = None
            return
        with self.lock:
            if c != self.store.config():
                return
            qb = self.get_qb(c)
            monitor['qb_online'] = True
            now = time.time()
            minute = int(now) // 60
            force_check = self.force_qb_check.is_set()
            if force_check or minute != getattr(self, 'qb_pref_minute', None):
                self.force_qb_check.clear()
                qb.connect()
                self.qb_pref_minute = minute
            hook_key = (c['qb_url'], c['qb_hook'], qb.preferences.get('autorun_enabled'),
                        qb.preferences.get('autorun_program'))
            if hook_key != getattr(self, 'hook_key', None):
                try:
                    enable = c['qb_hook'] and self.notify_supported
                    previous = self.store.db.get('background_hook_command', '')
                    self.hook_status = configure_completion(qb, c['qb_url'], self.home, enable, previous)
                    if self.hook_status == '已注册 qB 完成通知':
                        self.store.db.put('background_hook_command', completion_command(self.home))
                except AppError as error:
                    self.hook_status = safe_error(error) + '，使用定时检查'
                self.hook_key = (c['qb_url'], c['qb_hook'], qb.preferences.get('autorun_enabled'),
                                 qb.preferences.get('autorun_program'))
            tasks = self.store.tasks(*ACTIVE)
            self.clean_legacy_tags(qb, c['qb_url'], tasks)
            ready = self.store.tasks('ready')
            keys = {key for task in (*tasks, *ready) for key in hash_keys(task['hashes'])}
            live = qb.torrent_info(keys)
            lookup = lookup_owned(tasks, live)
            # The scheduling lookup remains limited to Traffic Assistant jobs.
            # The independent accounting lookup includes Freeseed and retained
            # torrents, even if both download modules are stopped.
            traffic_live, traffic_server = qb.traffic_snapshot()
            foreground_rows = collect_foreground(self.store.db, c['qb_url'], traffic_live)
            retained_rows = {}
            for task in self.store.tasks('retained'):
                row = traffic_live.get(task.get('qb_hash'))
                if (task.get('qb_url') == c['qb_url'] and row and task.get('added_on')
                        and row.get('added_on') == task['added_on'] and (key := traffic_identity(row))):
                    retained_rows[key] = row
            self.measure_jobs(tasks, lookup, foreground_rows, retained_rows)
            if not local_endpoint(c['qb_url']) and now >= self.next_storage:
                self.storage_state = traffic_server.copy()
                self.next_storage = now + 60
            cap, free, medium = 0, 0, None
            try:
                can_create = c['enabled'] and now >= self.next_ram_attempt
                c = self.storage_config(c, provision=can_create, pinned=bool(tasks))
                if c['storage'] == 'ram':
                    monitor['storage_path'] = c['save_path']
                cap, free, medium = capacity(c, qb, self.storage_state, create=can_create)
                monitor['storage_ok'] = True
            except (AppError, OSError, subprocess.TimeoutExpired) as error:
                if can_create:
                    self.next_ram_attempt = now + 120
                monitor['storage_note'] = safe_error(error)
            priority = c['bandwidth_mode'] == 'upload_priority'
            if not priority and c['auto_line'] and now >= self.next_line:
                self.line = self.store.line_estimate(now)
                self.next_line = now + 300
            transfer = qb.transfer_info() if c['enabled'] or tasks else {}
            held_for_upload = False
            previous_priority = self.upload_controller is not None
            if priority and c['enabled'] and monitor['storage_ok'] and free > 0:
                current, reason, held_for_upload, details = self.upload_priority_allowance(
                    c, wan, latency, transfer, lookup, time.monotonic())
                monitor['upload_priority'] = details
                upload = None
            else:
                self.upload_controller = None
                current, upload, reason = self.allowance(c, wan, latency, transfer, lookup)
            if not monitor['storage_ok']:
                current, upload, reason = 0, 0, monitor['storage_note']
            if monitor['storage_ok'] and free <= 0:
                current, reason = 0, '达到存储保留空间，暂停下载'
            if priority:
                self.allowed_rate, self.allowed_upload = current, upload
            else:
                self.allowed_rate = (current if self.allowed_rate is None or previous_priority
                                 else min(current, self.allowed_rate + max(5, (c['max_mbps'] or current) / 5)))
                self.allowed_upload = (upload if self.allowed_upload is None
                                   else min(upload, self.allowed_upload + max(1, (c['max_up_mbps'] or upload) / 5)))
            policy = ('paused' if self.allowed_rate <= 0 else
                      (reason, round(current, 1)) if 'upload_priority' in monitor else
                      'limited' if current < (c['max_mbps'] or c['line_mbps']) else 'running')
            if policy != self.last_policy:
                event = reason
                if 'upload_priority' in monitor:
                    observed_upload = details['qb_upload_mbps']
                    speed = '待采样' if observed_upload is None else f'{observed_upload:.1f} Mbps'
                    event += f' · 托管下载上限 {current:.1f} Mbps · qB 实际上传 {speed}'
                self.store.log('调度：' + event)
                self.last_policy = policy
            managed_speed = sum(int(row.get('dlspeed') or 0) for row in lookup.values()) * 8 / 1e6
            download_count = sum(task['phase'] in ('adding', 'active') for task in tasks)
            if not download_count or not c['enabled']:
                self.concurrency = 1
                self.next_expand = now + EXPAND_SECONDS
            elif now >= self.next_expand:
                self.concurrency = min(c['max_active'], max(1, download_count))
                if not held_for_upload and self.allowed_rate > 0 and managed_speed < self.allowed_rate * .85:
                    self.concurrency = min(c['max_active'], self.concurrency + 1)
                self.next_expand = now + EXPAND_SECONDS
            active = max(1, download_count)
            if ready and self.concurrency > download_count:
                active += int(bool(download_count))
            reserve_slot = (not held_for_upload and self.concurrency > download_count
                            and any(not task.get('hold_user') and task.get('verified_until', 0) > now
                                    for task in ready))
            runnable, waiting, limit, waiting_limit = download_limits(
                tasks, lookup, int(self.allowed_rate * 1e6 / 8), reserve_slot, now)
            up_limit = None if self.allowed_upload is None else int(self.allowed_upload * 1e6 / 8 / active)
            live_keys = {key for row in live.values() for key in hash_keys(hashes(row))}
            foreground = self.foreground(tasks)
            updates, starts = {}, []
            for task in tasks:
                if self.stop.is_set():
                    return
                job_limit = limit if task.get('run_tag') in runnable else waiting_limit
                self.reconcile(qb, c, task, lookup, live_keys, job_limit, up_limit,
                               monitor['storage_ok'], medium, foreground, updates, starts,
                               check_slow=not held_for_upload)
            order = ('stop', 'up-limit', 'limit', 'start')
            for (action, fields), keys in sorted(updates.items(), key=lambda record: (
                    order.index(record[0][0]), dict(record[0][1]).get('limit', 0))):
                qb.action_many(action, keys, **dict(fields))
            for task in starts:
                task['held_by_app'] = False
                self.store.put(task)
            tasks = self.store.tasks(*ACTIVE)
            reserved = sum(task['size'] for task in tasks)
            pending = sum(task.get('left', task['size']) for task in tasks)
            room = max(0, min(cap - reserved, free - pending))
            occupied = sum(task['phase'] in ('adding', 'active') for task in tasks)
            slots = max(0, min(c['max_active'], self.concurrency) - occupied)
            if held_for_upload or waiting:
                slots = 0
            if slots and c['enabled'] and monitor['storage_ok'] and limit > 0 and (up_limit is None or up_limit > 0):
                admissions = self.submit_ready(qb, c, min(slots, 1), room, live, limit, up_limit, medium)
                if admissions < min(slots, 1):
                    self.next_expand = now + EXPAND_SECONDS
                    slots = 0
                    tasks = self.store.tasks(*ACTIVE)
                    reserved = sum(task['size'] for task in tasks)
                    pending = sum(task.get('left', task['size']) for task in tasks)
                    room = max(0, min(cap - reserved, free - pending))
            deadlines = [task['free_end'] for task in tasks if task.get('free_end', 0) and task['free_end'] > now]
            details = storage_usage(c, self.storage_state)
            monitor.update(reason=reason, reserved=reserved, capacity=cap, free=free, room=room, slots=slots,
                           limit_mbps=self.allowed_rate, upload_limit_mbps=self.allowed_upload, version=qb.version_text,
                           qb_download_runnable=len(runnable), qb_download_queued=len(waiting),
                           concurrency=self.concurrency, effective_line=self.line, engaged=bool(tasks or ready),
                           next_expiry=min(deadlines, default=None), medium=medium, last_checked=now, **details)
            self.monitor = monitor

    def run_site(self):
        while not self.stop.is_set():
            try:
                self.site_tick()
            except Exception as error:
                self.site_status = safe_error(error)
                if self.stop.wait(5):
                    break
            self.wake.wait(2)
            self.wake.clear()

    def site_tick(self):
        c = self.store.config()
        if not c['mteam_enabled']:
            self.site_status = '流量助手未选择 M-Team'
            return
        if not c['enabled']:
            return
        original = self.settings.get()
        if not original['api_key']:
            self.site_status = '请在 PT配置保存 M-Team API 令牌'
            return
        site = MTeam(original, self.site_store)
        now = time.time()
        for task in self.store.tasks('active', 'adding'):
            if (task.get('left', task['size']) > 0 and not task.get('pending_delete') and not task.get('free_revoked')
                    and task.get('check_at', 0) <= now):
                self.recheck(site, c, task)
                return
        if (not self.monitor.get('qb_online') or not self.monitor.get('storage_ok') or not self.monitor.get('slots')
                or self.monitor.get('limit_mbps', 0) <= 0):
            return
        room = self.monitor.get('room', 0)
        room -= sum(task['size'] for task in self.store.tasks('ready') if not task.get('hold_user'))
        if room <= 0:
            return
        for task in self.store.tasks('queued'):
            if task.get('hold_user') or task.get('retry', 0) > now:
                continue
            if not task.get('manual') and task['size'] > room:
                continue
            self.prepare(site, c, task, room)
            return
        if c['automatic']:
            self.scan(site, c, room)

    def refer(self, item, end):
        c = self.settings.get()
        if not c['enabled'] or c['dry_run']:
            return
        identified = {**item, 'free': True, 'free_end': end, 'promotion_end': end}
        ok, reason = matches(identified, c)
        if ok:
            self.store.db.observe(identified, 'pending', '从后台选种交给 Freeseed：' + reason)

    def scan(self, site, c, room):
        now = time.time()
        batch = self.store.db.get('background_candidate_page') or {}
        event = site_window(c, now)
        if batch and batch.get('event') != event:
            self.store.reset_round()
            batch = {}
            self.attempt_scan = 0
        if batch and self.select_candidate(batch, c, room):
            return
        if now < self.attempt_scan:
            return
        discount = batch.get('discount', 'FREE') if not event else None
        if batch and batch['has_more']:
            page = batch['page'] + 1
        else:
            page = 1
            if batch:
                if discount == 'FREE':
                    discount = '_2X_FREE'
                else:
                    self.store.reset_round()
                    batch = {}
                    discount = None if event else 'FREE'
        self.attempt_scan = now + c['scan_minutes'] * 60
        candidates = {}
        has_more = False
        try:
            for mode, rows in search_page(site, c, page, discount):
                within_seeders = False
                for row in rows:
                    try:
                        item = normalize(row)
                    except AppError:
                        continue
                    if item['seeders'] >= c['min_seeders']:
                        within_seeders = True
                    item['scope'] = mode
                    lane, deadline = classify(item, c, now)
                    item['free_end'] = deadline
                    item['lane'] = lane
                    item['reason'] = ('交给 Freeseed' if lane == 'freeseed' else
                                      '当前未确认免费、已标记 HR、不可用或发布时间缺失' if lane != 'background' else
                                      size_reason(item['size'], c))
                    if not item['reason'] and item['seeders'] < c['min_seeders']:
                        item['reason'] = '做种人数低于下限'
                    existing = candidates.get(item['id'])
                    if not existing or item['seeders'] > existing['seeders']:
                        candidates[item['id']] = item
                has_more |= len(rows) == PAGE_SIZE and within_seeders
        except AppError as error:
            self.attempt_scan = now + max(15, error.retry_after or 120)
            raise
        items = sorted(candidates.values(), key=lambda item: (-item['seeders'], int(item['id'])))
        if page > 1 and [i['id'] for i in items] == [i['id'] for i in batch.get('items', ())]:
            has_more = False
        batch = {'page': page, 'event': event, 'discount': discount, 'has_more': has_more, 'items': items}
        with self.lock:
            if c != self.store.config() or self.stop.is_set():
                return
            self.store.db.put('background_candidate_page', batch)
            self.candidates = {item['id']: item for item in items}
            for item in items:
                if item['lane'] == 'freeseed':
                    self.refer(item, item['free_end'])
            if not self.select_candidate(batch, c, room):
                self.site_status = f'第 {page} 页合并 {len(items)} 个种子，暂时没有可加入的任务'

    def select_candidate(self, batch, c, room):
        items = batch['items']
        ids = [item['id'] for item in items if not item['reason']]
        used = self.store.round_used(ids)
        foreground = self.store.foreground_ids(ids)
        with self.lock:
            if c != self.store.config() or self.stop.is_set():
                return True
            available = [item for item in items if not item['reason'] and item['id'] not in used
                         and item['id'] not in foreground]
            if available and any(not task.get('hold_user') and task.get('retry', 0) <= time.time()
                                 and task['size'] <= room for task in self.store.tasks('ready', 'queued')):
                return True
            for item in available:
                if item['size'] > room:
                    continue
                task = self.store.get(item['id'])
                if task and task['phase'] in MUTABLE:
                    continue
                self.store.put({'id': item['id'], 'name': item['name'], 'size': item['size'], 'phase': 'queued',
                                'scope': item['scope'], 'loop': False, 'manual': False,
                                'note': '自动候选，等待复核', 'revision': new_id()}, round_used=True)
                self.site_status = f'第 {batch["page"]} 页合并 {len(items)} 项 · 已选 #{item["id"]}'
                return True
        # Candidates that would fit after current downloads finish stay in the current batch.
        return bool(available and room < max(item['size'] for item in available) and self.store.tasks(*ACTIVE))

    def prepare(self, site, c, task, room):
        tid = task['id']
        staged = None
        values = {}
        try:
            site.db.pace_torrent()
            detail = normalize(site.detail(tid))
            now = time.time()
            lane, deadline = classify(detail, c, now)
            if lane == 'freeseed':
                self.refer(detail, deadline)
            reason = eligible(detail, c, now)
            if not reason and self.store.foreground_ids([tid]):
                reason = 'Freeseed 已接收这个种子'
            values.update(name=detail['name'], size=detail['size'], free_end=deadline)
            if reason:
                delay = 300
                if not task.get('manual'):
                    values['phase'] = 'skipped'
            elif detail['size'] > room:
                reason, delay = '等待容量释放', 30
                if not task.get('manual'):
                    values['phase'] = 'skipped'
            else:
                data = site.download(tid)
                torrent_hashes, size = inspect_torrent(data, include_size=True)
                values['size'] = max(size, detail['size'])
                detail['size'] = values['size']
                reason = eligible(detail, c, time.time())
                if not reason and values['size'] > room:
                    reason = '种子文件实际容量超过可用空间'
                if reason:
                    delay = 300
                    if not task.get('manual'):
                        values['phase'] = 'skipped'
                else:
                    staged, delay, reason = data, 0, '已复核免费，等待提交 qB'
                    checked = time.time()
                    valid_until = min(checked + 110, deadline or checked + 110)
                    values.update(phase='ready', hashes=torrent_hashes, verified_until=valid_until, last_free_at=checked,
                                  check_at=check_at(checked, deadline, c))
        except (AppError, OSError) as error:
            reason = safe_error(error)
            delay = max(15, getattr(error, 'retry_after', 0) or 120)
        with self.lock:
            current = self.store.get(tid)
            if (not current or current['revision'] != task['revision'] or current['phase'] != 'queued'
                    or self.stop.is_set() or c != self.store.config() or self.settings.get()['api_key'] != site.c['api_key']):
                return
            if staged:
                atomic_write(self.cache(tid), staged)
            current.update(values, note=reason, retry=time.time() + delay)
            self.store.put(current)
            if staged:
                self.request_poll()
            self.site_status = '#' + tid + ' · ' + reason

    def recheck(self, site, c, task):
        now = time.time()
        try:
            detail = normalize(site.detail(task['id']))
            lane, deadline = classify(detail, c, now)
            if lane == 'freeseed':
                self.refer(detail, deadline)
            verified = lane == 'background' and not size_reason(detail['size'], c)
            updates = {'free_end': deadline if verified else task.get('free_end'), 'free_hold': not verified,
                       'free_revoked': not verified and not task.get('retain'),
                       'check_at': check_at(now, deadline if verified else None, c)}
            if verified:
                updates['last_free_at'] = now
            self.site_status = '#' + task['id'] + (' · 已复核免费' if verified else ' · 结束后台下载')
        except (AppError, OSError) as error:
            updates = {'check_at': now + max(15, getattr(error, 'retry_after', 0) or 60)}
            self.site_status = safe_error(error)
        with self.lock:
            current = self.store.get(task['id'])
            if current and current['phase'] == 'active' and current['run_tag'] == task['run_tag']:
                current.update(updates)
                self.store.put(current)
                self.request_poll()
