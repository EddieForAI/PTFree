# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

"""Reduce qB's global rate while latency remains above its usual range."""
from __future__ import annotations

import threading
from collections import deque
from statistics import median

from .common import AppError, safe_error
from .qb import QB, connection_args

LEASE = 'qb_yield_limits'
INTERVAL = 3
LIMIT_FIELDS = ('dl_limit', 'up_limit', 'alt_dl_limit', 'alt_up_limit')
FLOORS = (1024, 128)  # KiB/s; zero would remove a limit in qB.
IDLE = (64, 16)


def same_limit(left, right):
    return max(0, left) == max(0, right)


class QBYield:
    def __init__(self, store, probe):
        self.store = store
        self.db = store.db
        self.probe = probe
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='mtfree-qb-yield', daemon=True)
        self.state = {'message': '未开启'}
        self.client = None
        self.client_args = None
        self.samples = deque(maxlen=40)
        self.peer = None
        self.high = self.clear = self.idle = self.missed = 0
        self.held = set()
        self.recovery_pending = True

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=8)

    def qb(self, config):
        args = connection_args(config)
        if args != self.client_args or self.client is None:
            self.client = QB(*args)
            self.client_args = args
        return self.client

    def remember(self, url, entries):
        self.db.put(LEASE, {'url': url, 'entries': entries} if entries else None)

    def read_owned(self, url, qb, entries):
        prefs = qb.get_preferences()
        limits = {}
        for field in LIMIT_FIELDS:
            value = prefs.get(field)
            if type(value) is not int or value < -1:
                raise AppError('qB 未返回有效的全局限速，暂停自动让路', 'qb')
            limits[field] = value
        changed = False
        for field, entry in list(entries.items()):
            actual, pending = limits[field], entry.get('pending')
            if pending is not None and same_limit(actual, pending):
                entry['last'] = actual
            elif not same_limit(actual, entry['last']):
                self.held.add(field)
                del entries[field]
                changed = True
                continue
            if pending is not None:
                entry.pop('pending')
                changed = True
            if same_limit(actual, entry['original']):
                del entries[field]
                changed = True
        if changed:
            self.remember(url, entries)
        return limits

    def write(self, url, qb, entries, changes):
        if not changes:
            return
        for field, cap in changes.items():
            entries[field]['pending'] = cap
        self.remember(url, entries)
        qb.set_preferences(**changes)
        for field, cap in changes.items():
            entry = entries[field]
            if same_limit(cap, entry['original']):
                del entries[field]
            else:
                entry['last'] = cap
                entry.pop('pending', None)
        self.remember(url, entries)

    def restore(self, url, qb, entries):
        if not entries:
            return
        self.read_owned(url, qb, entries)
        self.write(url, qb, entries, {field: entry['original'] for field, entry in entries.items()})

    def reset_samples(self, peer):
        self.peer = peer
        self.samples.clear()
        self.high = self.clear = self.idle = self.missed = 0
        self.held.clear()

    def sample(self, config, qb, entries):
        url = config['qb_url']
        peer = (url, config['latency_peer'], config['latency_extra_ms'])
        if peer != self.peer:
            self.restore(url, qb, entries)
            self.reset_samples(peer)
        transfer = qb.transfer_info()
        try:
            speeds = [max(0, int(transfer[field])) // 1024 for field in ('dl_info_speed', 'up_info_speed')]
        except (KeyError, ValueError, TypeError):
            raise AppError('qB 未返回有效的全局流量，暂停自动让路', 'qb') from None
        hungry = any(speed >= boundary for speed, boundary in zip(speeds, IDLE))
        self.idle = 0 if hungry else self.idle + 1
        rtt = self.probe(config['latency_peer'])
        self.state = {'message': '观察 qB 与外网延迟', 'rtt': rtt,
                      'down_mbps': speeds[0] * 8192 / 1e6, 'up_mbps': speeds[1] * 8192 / 1e6}
        if self.idle >= 3:
            self.restore(url, qb, entries)
            self.held.clear()
        if rtt is None:
            self.high = self.clear = 0
            self.missed += 1
            if self.missed >= 4:
                self.restore(url, qb, entries)
            self.state['message'] = '外网探测暂未收到回复'
            return
        self.missed = 0
        if len(self.samples) < 6:
            self.samples.append(rtt)
            self.state['message'] = '正在建立延迟基线，qB 空闲时更容易校准'
            return
        baseline = median(sorted(self.samples)[:max(3, len(self.samples) // 3)])
        self.state['baseline'] = baseline
        extra = rtt - baseline
        threshold = config['latency_extra_ms']
        if not hungry or extra <= threshold / 2:
            self.samples.append(rtt)
        self.high = self.high + 1 if extra >= threshold and hungry else 0
        self.clear = self.clear + 1 if extra <= threshold / 2 else 0
        if self.clear >= 3:
            self.held.clear()
        if not entries and self.high < 2:
            return
        mode = qb.request('transfer/speedLimitsMode').strip()
        if mode not in (b'0', b'1'):
            raise AppError('无法读取 qB 限速模式，暂停自动让路', 'qb')
        active = LIMIT_FIELDS[2:] if mode == b'1' else LIMIT_FIELDS[:2]
        limits = self.read_owned(url, qb, entries)
        changes = {field: entry['original'] for field, entry in entries.items() if field not in active}
        for field, speed, floor in zip(active, speeds, FLOORS):
            entry = entries.get(field)
            if self.high >= 2 and speed > floor and field not in self.held:
                ceiling = limits[field] if limits[field] > 0 else speed
                cap = max(floor, int(min(speed, ceiling) * .78))
                if ceiling - cap >= max(1, floor // 16):
                    entries.setdefault(field, {'original': limits[field], 'last': limits[field], 'peak': speed})
                    changes[field] = cap
            elif self.clear >= 3 and entry:
                next_cap = max(entry['last'] + floor // 4, int(entry['last'] * 1.15))
                target = min(entry['original'], entry['peak']) if entry['original'] > 0 else entry['peak']
                changes[field] = entry['original'] if next_cap >= target else next_cap
        self.write(url, qb, entries, changes)
        if entries:
            self.state['message'] = '延迟恢复，逐步还原 qB 限速' if self.clear >= 3 else 'qB 整体让路中'
            self.state.update({direction + '_limit_mbps': entries[field]['last'] * 8192 / 1e6
                               for direction, field in zip(('down', 'up'), active) if field in entries})

    def tick(self, *, stopping=False):
        config = self.store.config()
        lease = self.db.get(LEASE)
        suspended = config['qb_yield_enabled'] and config['enabled'] and config['bandwidth_mode'] == 'upload_priority'
        enabled = config['qb_yield_enabled'] and not (stopping or suspended)
        idle_message = '流量助手上传优先运行中，qB 整体让路暂不生效' if suspended else '未开启'
        if not enabled and not lease:
            self.state = {'message': idle_message}
            if self.peer is not None:
                self.reset_samples(None)
            return
        url = config['qb_url']
        if lease and lease['url'] != url:
            raise AppError('请恢复原 qB 连接地址，以还原之前的限速', 'qb')
        qb = self.qb(config)
        entries = lease['entries'] if lease else {}
        if not enabled or self.recovery_pending:
            self.restore(url, qb, entries)
            self.recovery_pending = False
        if enabled and not self.stop.is_set():
            self.sample(config, qb, entries)
        else:
            self.state = {'message': (idle_message + '，原有限速已还原') if suspended and lease
                          else '已还原 qB 原有限速' if lease else idle_message}
            if self.peer is not None:
                self.reset_samples(None)

    def run(self):
        delay = INTERVAL
        while not self.stop.is_set():
            self.wake.clear()
            with self.lock:
                try:
                    self.tick()
                    delay = INTERVAL if self.peer is not None else 30
                except (AppError, OSError) as error:
                    self.state = {'message': safe_error(error), 'pending': bool(self.db.get(LEASE))}
                    self.client = None
                    delay = min(30, delay + INTERVAL)
            self.wake.wait(delay)
        with self.lock:
            try:
                self.tick(stopping=True)
            except (AppError, OSError) as error:
                self.store.log('qB 限速待下次启动还原：' + safe_error(error))
