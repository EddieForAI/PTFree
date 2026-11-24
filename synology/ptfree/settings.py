# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import os
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

from .common import AppError, atomic_json, atomic_write

DEFAULT = {
    'module_freeseed': False, 'site_added': False,
    'freeseed_mteam': True,
    'enabled': False, 'dry_run': True, 'api_base': 'https://api.m-team.cc/api',
    'mode': 'adult', 'interval': 180, 'jitter': 20, 'page_size': 100,
    'network': 'ipv4_prefer', 'timeout': 20, 'watch_dir': os.environ.get('MTFREE_WATCH_DIR', ''),
    'max_attempts': 5, 'local_request_limits': False,
    'min_gib': 0, 'max_gib': 100, 'min_seeders': 0, 'max_seeders': 0,
    'max_age_hours': 24, 'min_free_minutes': 30, 'include': '', 'exclude': '',
    'free_only': True, 'min_disk_gib': 10,
    'managed_gib': 0, 'managed_full': 'wait', 'managed_path': '',
    'api_key': '',
}
MTEAM_FIELDS = {'api_base', 'api_key', 'network', 'timeout', 'mode', 'page_size', 'local_request_limits'}
SECRETS = {'api_key'}
LEGACY_FIELDS = {
    'module_traffic',
    'batch_size', 'hr_policy',
    'delivery', 'concurrency', 'qb_url', 'qb_username', 'qb_password',
    'qb_savepath', 'qb_category', 'qb_tags', 'qb_paused', 'qb_download_kib',
    'qb_upload_kib', 'qb_max_active',
}
LOCK = threading.RLock()


def validate(value):
    c = copy.deepcopy(DEFAULT)
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise AppError('配置包含未知字段', 'config')
    c.update(value)
    for k, default in DEFAULT.items():
        if isinstance(default, bool):
            if not isinstance(c[k], bool):
                raise AppError(f'{k} 必须为布尔值', 'config')
        elif isinstance(default, (int, float)):
            if isinstance(c[k], bool) or not isinstance(c[k], (int, float)) or not math.isfinite(c[k]):
                raise AppError(f'{k} 必须为有限数字', 'config')
        elif not isinstance(c[k], str) or len(c[k]) > 8192 or any(x in c[k] for x in ('\x00', '\r')):
            raise AppError(f'{k} 格式不正确', 'config')
    ranges = {'interval': (120,86400), 'jitter': (0,600), 'page_size': (1,100),
              'timeout': (5,60),
              'max_attempts': (1,20), 'min_gib': (0,100000), 'max_gib': (0,100000),
              'min_seeders': (0,10000000), 'max_seeders': (0,10000000),
              'max_age_hours': (0,87600), 'min_free_minutes': (0,100000),
              'min_disk_gib': (0,100000), 'managed_gib': (0,100000)}
    floats = {'min_gib', 'max_gib', 'max_age_hours', 'min_free_minutes', 'min_disk_gib'}
    for k, (low, high) in ranges.items():
        if not low <= c[k] <= high or (k not in floats and int(c[k]) != c[k]):
            raise AppError(f'{k} 范围应为 {low}～{high}', 'config')
        if k not in floats:
            c[k] = int(c[k])
    if c['max_gib'] and c['max_gib'] < c['min_gib']:
        raise AppError('最大体积应大于或等于最小体积', 'config')
    for k, allowed in {'network': ['ipv4_prefer','ipv4_only','system'],
                       'mode': ['adult','normal','movie','music','tvshow','all'],
                       'managed_full': ['wait','oldest']}.items():
        if c[k] not in allowed:
            raise AppError(f'{k} 选项不正确', 'config')
    p = urlsplit(c['api_base'])
    if p.scheme != 'https' or p.hostname not in ('api.m-team.cc','api.m-team.io') or p.port not in (None,443) or p.username or p.password or p.query or p.fragment or p.path.rstrip('/') != '/api':
        raise AppError('API 地址仅支持 https://api.m-team.cc/api 或 https://api.m-team.io/api', 'config')
    c['api_base'] = c['api_base'].rstrip('/')
    if c['managed_gib']:
        from .storage_paths import qpath
        path = qpath(c['managed_path'])
        if not path.is_absolute() or not path.name or '..' in path.parts:
            raise AppError('启用 Freeseed 容量管理时，请填写 qB 上的专用绝对路径', 'config')
    if c['enabled'] and not c['freeseed_mteam']:
        raise AppError('请先在 Freeseed 服务配置中勾选 M-Team', 'config')
    if c['enabled'] and (not c['api_key'].strip() or not (c['managed_gib'] or c['watch_dir'].strip())):
        raise AppError('启动前请填写 API 令牌和种子目录', 'config')
    if '\n' in c['api_key']:
        raise AppError('API 令牌不允许换行', 'config')
    return c


class Settings:
    def __init__(self, home: Path):
        self.home = home
        self.path = home / 'settings.json'
        self.keypath = home / 'secret.key'
        if not self.keypath.exists():
            if self.path.exists():
                raise AppError('secret.key 丢失，请从备份恢复，禁止覆盖已有密钥', 'config')
            atomic_write(self.keypath, Fernet.generate_key())
        self.cipher = Fernet(self.keypath.read_bytes())
        if not self.path.exists():
            self.save(DEFAULT)
        self.authpath = home / 'auth.json'
        if not self.authpath.exists():
            self.set_password(secrets.token_urlsafe(18), initial=True)
        if not (home / 'metrics.token').exists():
            atomic_write(home / 'metrics.token',secrets.token_urlsafe(32).encode())

    def get(self):
        with LOCK:
            raw = json.loads(self.path.read_text('utf-8'))
            if not isinstance(raw, dict):
                raise AppError('配置文件应为 JSON 对象', 'config')
            migrate = bool(LEGACY_FIELDS.intersection(raw))
            if migrate:
                raw = {key: value for key, value in raw.items() if key not in LEGACY_FIELDS}
                if not raw.get('managed_gib') and isinstance(raw.get('watch_dir', ''), str) and not raw.get('watch_dir', '').strip():
                    raw['enabled'] = False
            if 'module_freeseed' not in raw:
                raw.update({'module_freeseed': True, 'site_added': bool(raw.get('api_key')),})
                migrate = True
            config = dict(raw)
            for k in SECRETS:
                config[k] = self.cipher.decrypt(raw[k].encode()).decode() if raw.get(k) else ''
            config = validate(config)
            if migrate:
                atomic_json(self.path, raw)
            return config

    def public(self):
        c = self.get()
        for k in SECRETS:
            c[k + '_set'] = bool(c.pop(k))
        return c

    def save(self, value, merge=False):
        with LOCK:
            if merge:
                base = self.get()
                for k in SECRETS:
                    if value.get(k) == '':
                        value = {a:b for a,b in value.items() if a != k}
                base.update(value)
            else:
                base = value
            c = validate(base)
            raw = dict(c)
            for k in SECRETS:
                raw[k] = self.cipher.encrypt(raw[k].encode()).decode() if raw[k] else ''
            atomic_json(self.path, raw)
            return c

    def set_password(self, password, initial=False):
        if len(password) < 12 or len(password) > 200:
            raise AppError('管理密码长度应为 12～200 字符', 'config')
        salt = secrets.token_hex(16)
        auth = {'salt': salt, 'hash': hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384,r=8,p=1).hex(), 'session_key': secrets.token_hex(32)}
        atomic_json(self.home / 'auth.json', auth)
        if initial:
            atomic_write(self.home / '初始管理密码.txt', password.encode())
        elif (self.home / '初始管理密码.txt').exists():
            (self.home / '初始管理密码.txt').unlink()

    def check_password(self, password):
        auth = json.loads(self.authpath.read_text())
        value = hashlib.scrypt(password.encode(),salt=auth['salt'].encode(),n=16384,r=8,p=1).hex()
        return hmac.compare_digest(value, auth['hash'])

    def session_key(self):
        return bytes.fromhex(json.loads(self.authpath.read_text())['session_key'])
