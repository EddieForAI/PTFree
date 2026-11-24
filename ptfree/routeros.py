# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import hmac
import socket
import ssl
import struct

from .common import AppError


def length_bytes(length):
    if length < 0x80:
        return bytes((length,))
    if length < 0x4000:
        return struct.pack('!H', length | 0x8000)
    if length < 0x200000:
        return (length | 0xC00000).to_bytes(3, 'big')
    if length < 0x10000000:
        return struct.pack('!I', length | 0xE0000000)
    raise ValueError('RouterOS word too large')


class RouterOS:
    def __init__(self, config):
        self.config = config
        self.socket = None
        self.stream = None

    def __enter__(self):
        c = self.config
        try:
            raw = socket.create_connection((c['ros_host'], c['ros_port']), timeout=3)
            self.socket = raw
            context = ssl.create_default_context()
            if c['ros_fingerprint']:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            self.socket = context.wrap_socket(raw, server_hostname=c['ros_host'])
            actual = hashlib.sha256(self.socket.getpeercert(binary_form=True)).hexdigest()
            if c['ros_fingerprint'] and not hmac.compare_digest(actual, c['ros_fingerprint']):
                raise AppError('RouterOS TLS 证书指纹不匹配', 'router')
            self.stream = self.socket.makefile('rb')
            self.call('/login', name=c['ros_user'], password=c['ros_password'])
            return self
        except (OSError, AppError) as error:
            self.__exit__()
            if isinstance(error, AppError):
                raise
            raise AppError('RouterOS API-SSL 连接失败，请检查服务、证书和防火墙', 'router') from None

    def __exit__(self, *args):
        if self.stream:
            self.stream.close()
        if self.socket:
            self.socket.close()

    def read(self, size):
        result = self.stream.read(size)
        if len(result) != size:
            raise AppError('RouterOS API 连接提前关闭', 'router')
        return result

    def sentence(self):
        words = []
        for _ in range(256):
            first = self.read(1)[0]
            extra = next((n for n, high in enumerate((0x80, 0xC0, 0xE0, 0xF0, 0xF1)) if first < high), None)
            if extra is None:
                break
            mask = (0x7f, 0x3f, 0x1f, 0x0f, 0)[extra]
            n = int.from_bytes(bytes((first & mask,)) + self.read(extra), 'big')
            if not n:
                return words
            if n > 65536:
                break
            words.append(self.read(n).decode('utf-8', errors='replace'))
        raise AppError('RouterOS API 响应格式异常', 'router')

    def call(self, command, query=(), **fields):
        words = [command, *(f'={key}={value}' for key, value in fields.items()), *query]
        encoded = [word.encode() for word in words]
        try:
            self.socket.sendall(b''.join(length_bytes(len(word)) + word for word in encoded) + b'\x00')
            rows = []
            trapped = False
            for _ in range(4096):
                words = self.sentence()
                if not words:
                    continue
                row = dict(word[1:].split('=', 1) for word in words[1:] if word.startswith('=') and '=' in word[1:])
                if words[0] == '!trap':
                    trapped = True
                elif words[0] == '!re':
                    rows.append(row)
                elif words[0] == '!done':
                    if trapped or command == '/login' and 'ret' in row:
                        raise AppError('RouterOS 拒绝操作，请核对版本、命令参数和账号权限', 'router')
                    return rows
                elif words[0] == '!fatal':
                    break
        except (OSError, ValueError):
            pass
        raise AppError('RouterOS 未正常响应，操作结果待核对', 'router')

    def counters(self, interface):
        result = self.call('/interface/print', (f'?name={interface}',),
                           **{'stats': '', '.proplist': '.id,name,rx-byte,tx-byte,running'})
        try:
            row, = result
            if row.get('running') == 'false':
                raise ValueError()
            rx, tx = int(row['rx-byte']), int(row['tx-byte'])
            if min(rx, tx) < 0:
                raise ValueError()
            return row['.id'], rx, tx
        except (KeyError, ValueError):
            raise AppError('PPPoE 接口未运行或没有可用流量计数器', 'router') from None


MANGLE = ('/ip/firewall/mangle', '/ipv6/firewall/mangle')
QUEUE = '/queue/tree'


def owned_rows(router, path, marker):
    return [row for row in router.call(path + '/print', **{'.proplist': '.id,comment,name,parent'})
            if row.get('comment', '').startswith(marker + ':')]


def remove_rules(router, marker, ipv6):
    all_queues = router.call(QUEUE + '/print', **{'.proplist': '.id,comment,name,parent'})
    queues = [row for row in all_queues if row.get('comment', '').startswith(marker + ':')]
    names = {row.get('name') for row in queues}
    if any(row.get('parent') in names for row in all_queues if row not in queues):
        raise AppError('旧版整形队列下有手动添加的子队列，请先在 RouterOS 调整其父队列', 'router')

    queues.sort(key=lambda row: row.get('parent') not in names)
    for row in queues:
        router.call(QUEUE + '/remove', **{'.id': row['.id']})
    for path in MANGLE[:2 if ipv6 else 1]:
        for row in owned_rows(router, path, marker):
            router.call(path + '/remove', **{'.id': row['.id']})
