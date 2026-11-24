# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import http.client
import json
import os
import sys
from pathlib import Path

from .common import AppError, launch_args
from .qb import hash_keys, local_endpoint


def command(home):
    args = (launch_args('--data', home, 'qb-complete', '%I', '%J') if getattr(sys, 'frozen', False)
            else [sys.executable, str(Path(__file__).with_name('qb_notify_entry.py').resolve()), str(home), '%I', '%J'])
    if os.name == 'nt' and Path(args[0]).name.lower() == 'python.exe':
        quiet = Path(args[0]).with_name('pythonw.exe')
        if quiet.is_file():
            args[0] = str(quiet)
    # qB splits a QProcess command line directly. Only the final two arguments are substitutions.
    if any(any(char in arg for char in ('"', '%', '\n', '\r')) for arg in args[:-2]):
        raise AppError('PTFree 安装路径含 qB 通知命令不支持的字符，使用定时检查', 'config')
    return ' '.join(f'"{part}"' for part in args)


def configure(qb, url, home, enable, previous):
    if not local_endpoint(url):
        return '远程 qB 使用定时检查'
    prefs = qb.preferences
    current = prefs.get('autorun_program') or ''
    desired = command(home) if enable else ''
    if current and current not in (previous, desired):
        return 'qB 已配置其他完成程序，使用定时检查'
    if enable:
        if current != desired or not prefs.get('autorun_enabled'):
            qb.set_preferences(autorun_enabled=True, autorun_program=desired)
        return '已注册 qB 完成通知'
    if current and current == previous:
        qb.set_preferences(autorun_enabled=False, autorun_program='')
    return '定时检查已开启'


def notify(home, values):
    hashes = sorted(hash_keys(set(values)))
    if not hashes:
        return 0
    try:
        cfg = json.loads((home / 'qb-notify.json').read_text(encoding='utf-8'))
        conn = http.client.HTTPConnection(cfg['host'], cfg['port'], timeout=2)
        try:
            conn.request('POST', '/api/internal/qb-completed', json.dumps({'hashes': hashes}),
                         {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + cfg['token']})
            return 0 if conn.getresponse().status == 202 else 1
        finally:
            conn.close()
    except (ValueError, KeyError, OSError, http.client.HTTPException):
        return 1
