# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .common import AppError, safe_error
from .freeseed_options import ROUTER_FIELDS
from .freeseed_storage import attributed, managed_tasks
from .qb import QB, connection_args, hash_keys, hashes
from .routeros import RouterOS
from .statistics_store import StatisticsStore
from .traffic import init_file_receipts, collect_foreground, delta, counters as traffic_counters
from .storage_paths import GIB, qpath


class Monitor:
    """Read counters without changing qB tasks, speeds, or RouterOS rules."""

    def __init__(self, settings, options, db):
        self.settings = settings
        self.options = options
        self.db = db
        self.history = StatisticsStore(db)
        init_file_receipts(db)
        self.status = {}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='freeseed-monitor', daemon=True)
        self.wan = self.jobs = self.qb = self.qb_key = self.router_key = None
        self.messages = {}

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5)

    def record_status(self, source, message):
        if self.messages.get(source) != message:
            self.history.event(f'{source} · {message}')
            self.messages[source] = message

    def run(self):
        while not self.stop.is_set():
            start = time.monotonic()
            try:
                self.sample()
            except Exception as error:
                self.wan = None
                self.status = {'error': safe_error(error)}
            self.stop.wait(max(0.5, 15 - (time.monotonic() - start)))

    def sample(self):
        config = self.options.config()
        freeseed = self.settings.get()
        status = {'router_note': '未配置 RouterOS', 'qb_note': '未配置 qB 采样', 'checked_at': time.time()}
        if freeseed['watch_dir'] and not freeseed['managed_gib']:
            path = Path(freeseed['watch_dir']).expanduser()
            try:
                usage = shutil.disk_usage(path)
                status.update(volume_free=usage.free, volume_used=usage.used, volume_total=usage.total)
            except OSError:
                pass
        self.sample_router(config, status)
        self.sample_qb(config, freeseed, status)
        self.status = status

    def sample_router(self, config, status):
        key = tuple(config[name] for name in sorted(ROUTER_FIELDS))
        if key != self.router_key:
            self.router_key = key
            self.wan = None
        if not config['ros_host']:
            return
        try:
            with RouterOS(config) as router:
                interface, rx, tx = router.counters(config['ros_wan'])
        except (AppError, OSError) as error:
            self.wan = None
            status['router_note'] = safe_error(error)
            self.record_status('RouterOS', status['router_note'])
            return
        stamp = time.time()
        previous = self.wan
        self.wan = stamp, interface, rx, tx
        status['router_note'] = '正在建立 PPPoE 采样基线'
        self.record_status('RouterOS', '已读取 PPPoE 计数')
        if previous and interface == previous[1] and rx >= previous[2] and tx >= previous[3]:
            seconds = stamp - previous[0]
            if 0 < seconds <= 45:
                counts = rx - previous[2], tx - previous[3]
                status['latency'] = probe_latency(config['latency_peer'])
                self.history.add_interval(previous[0], stamp, wan=counts, latency=status['latency'])
                status['wan_rx'], status['wan_tx'] = (count * 8 / seconds / 1e6 for count in counts)
                status['router_note'] = ''

    def sample_qb(self, config, freeseed, status):
        key = connection_args(config)
        if key != self.qb_key:
            self.qb_key = key
            self.jobs = self.qb = None
        if not config['qb_url']:
            return
        try:
            if self.qb is None:
                candidate = QB(*key)
                candidate.connect()
                self.qb = candidate
            live, state = self.qb.snapshot()
        except (AppError, OSError) as error:
            self.qb = None
            status['qb_note'] = safe_error(error)
            self.record_status('qB', status['qb_note'])
            return
        status['qb_note'] = ''
        self.record_status('qB', '已读取任务计数')
        free = state.get('free_space_on_disk')
        if type(free) is int and free >= 0:
            status['qb_default_free'] = free
        counters = {identity: value for identity, row in collect_foreground(self.db, config['qb_url'], live).items()
                    if (value := traffic_counters(row)) is not None}
        if freeseed['managed_gib']:
            root = qpath(freeseed['managed_path'])
            reserved = 0
            rows_by_key = {key: row for row in live.values() for key in hash_keys(hashes(row))}
            for record in managed_tasks(self.db, config['qb_url'], root):
                keys = hash_keys(record['hashes'])
                nearby = {rows_by_key[key]['hash']: rows_by_key[key] for key in keys & rows_by_key.keys()}
                row = attributed(record, nearby, self.qb)
                if row:
                    reserved += record['size']
                    collect_counters(row, counters)
                elif nearby or time.time() - record['issued'] < 30:
                    reserved += record['size']
            status.update(reserved=reserved, capacity=freeseed['managed_gib'] * GIB)
        stamp = time.time()
        previous = self.jobs
        if previous:
            counts = [0, 0]
            for identity, values in counters.items():
                for index, amount in enumerate(delta(identity, values, previous[1], previous[0])):
                    counts[index] += amount
            self.history.add_interval(previous[0], stamp, job=counts)
        self.jobs = stamp, counters


def collect_counters(row, counters):
    values = row.get('downloaded'), row.get('uploaded')
    if all(type(value) is int and value >= 0 for value in values):
        counters[row['hash'], row.get('added_on')] = values


def probe_latency(host):
    if not host:
        return None
    try:
        response = subprocess.run(['ping', '-n', '-c', '1', '-W', '1', host], check=True,
                                  capture_output=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    latency = re.search(rb'time[=<]([0-9.]+)\s*ms', response.stdout)
    return float(latency[1]) if latency else None
