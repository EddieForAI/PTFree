# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import re

from .common import AppError
from .policy import timestamp
from .qb import QB, QB_FIELDS, connection_args, validate_connection
from .routeros import RouterOS

ROUTER_FIELDS = {'ros_host', 'ros_port', 'ros_user', 'ros_password', 'ros_fingerprint', 'ros_wan'}
EVENT_FIELDS = {'site_free_start', 'site_free_end'}
SECRETS = {'qb_password', 'qb_api_key', 'ros_password'}
DEFAULTS = {
    'site_free_start': '', 'site_free_end': '',
    'qb_url': '', 'qb_auth': 'password', 'qb_user': '', 'qb_password': '', 'qb_api_key': '',
    'ros_host': '', 'ros_port': 8729, 'ros_user': '', 'ros_password': '',
    'ros_fingerprint': '', 'ros_wan': 'pppoe-CT', 'latency_peer': '',
}


class FreeseedOptions:
    def __init__(self, db, settings):
        self.db = db
        self.cipher = settings.cipher

    def decode(self, saved, public=False):
        value = {key: saved.get(key, default) for key, default in DEFAULTS.items()}
        for key in SECRETS:
            if public:
                value[key + '_set'] = bool(value.pop(key))
            elif value[key]:
                value[key] = self.cipher.decrypt(value[key].encode()).decode()
        return value

    def config(self, public=False):
        return self.decode(self.db.get('background_config', {}), public)

    @staticmethod
    def merge(saved, draft):
        if set(draft) - DEFAULTS.keys():
            raise AppError('配置含未知字段', 'config')
        if any(type(value) is not type(DEFAULTS[key]) or
               isinstance(value, str) and (len(value) > 2048 or any(ord(ch) < 32 for ch in value))
               for key, value in draft.items()):
            raise AppError('配置字段格式不正确', 'config')
        value = {**saved, **draft}
        value['qb_url'] = value['qb_url'].rstrip('/')
        for keys, same in (({'qb_password', 'qb_api_key'}, value['qb_url'] == saved['qb_url']),
                           ({'ros_password'}, (value['ros_host'], value['ros_port']) ==
                            (saved['ros_host'], saved['ros_port']))):
            for key in keys:
                value[key] = draft.get(key) or (saved[key] if same else '')
        if draft.keys() & QB_FIELDS:
            validate_connection(value)
        if draft.keys() & EVENT_FIELDS:
            start, end = (timestamp(value[key]) for key in ('site_free_start', 'site_free_end'))
            if any(value[key] for key in EVENT_FIELDS) and not (start and end and start < end):
                raise AppError('请填写有效的全站免费开始和结束时间（北京时间）', 'config')
        if draft.keys() & (ROUTER_FIELDS | {'latency_peer'}):
            for key in ('ros_host', 'latency_peer'):
                host = value[key]
                if host and (host.startswith('-') or not re.fullmatch(r'[A-Za-z0-9_.:-]+', host)):
                    raise AppError('请填写有效的主机名或 IP 地址', 'config')
            if not 1 <= value['ros_port'] <= 65535:
                raise AppError('RouterOS 端口超出范围', 'config')
            digest = value['ros_fingerprint'].replace(':', '').lower()
            if digest and not re.fullmatch(r'[0-9a-f]{64}', digest):
                raise AppError('证书指纹应为 SHA-256 的 64 位十六进制值', 'config')
            value['ros_fingerprint'] = digest
        return value

    def save(self, draft):
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT value FROM kv WHERE key='background_config'").fetchone()
            saved = json.loads(row[0]) if row else {}
            value = self.merge(self.decode(saved), draft)
            for key in SECRETS:
                if value[key]:
                    value[key] = self.cipher.encrypt(value[key].encode()).decode()
            conn.execute("INSERT INTO kv VALUES('background_config',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (json.dumps({**saved, **value}),))
        return self.decode(value, public=True)

    def qb_test(self, draft):
        if set(draft) - QB_FIELDS:
            raise AppError('qB 测试参数无效', 'config')
        config = self.merge(self.config(), draft)
        if not config['qb_url']:
            raise AppError('请填写 qB Web API 地址', 'config')
        version = QB(*connection_args(config)).connect()
        return {'message': 'qB 连接成功，请保存连接设置。', 'version': version}

    def router_test(self, draft):
        if set(draft) - (ROUTER_FIELDS | {'latency_peer'}):
            raise AppError('RouterOS 测试参数无效', 'config')
        config = self.merge(self.config(), draft)
        if not config['ros_host'] or not config['ros_wan']:
            raise AppError('请填写 RouterOS 地址和 PPPoE 接口名', 'config')
        with RouterOS(config) as router:
            router.counters(config['ros_wan'])
        return {'message': '已读取 PPPoE 流量计数，请保存连接设置。'}
