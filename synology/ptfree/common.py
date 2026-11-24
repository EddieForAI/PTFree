# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import secrets
import subprocess
import tempfile
import time
import sys
from pathlib import Path


WINDOWS_NO_CONSOLE = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
SHUTDOWN_EXIT_CODE = 42


REQUEST_LABELS = {
    'search': '列表查询', 'detail': '查详情', 'history': '查站点历史',
    'token': '取下载令牌', 'download': '下载种子文件', 'profile': '认证测试',
}


class AppError(Exception):
    def __init__(self, message, kind='temporary', retry_after=0, *,
                 stage=None, status=None, code=None, source=None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after
        self.stage = stage
        self.status = status
        self.code = code
        self.source = source


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        if os.name != 'nt':
            os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_json(path, value):
    atomic_write(Path(path), json.dumps(value, ensure_ascii=False, indent=2).encode())


def data_home(value=None):
    path = Path(value or os.environ.get('MTFREE_DATA', Path.home() / '.mtfree')).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        os.chmod(path, 0o700)
    return path


def safe_error(exc):
    # Never log arbitrary network exception messages, response bodies or URLs.
    return str(exc) if isinstance(exc, AppError) else f'操作失败（{type(exc).__name__}）；请检查网络、路径和权限'


def new_id():
    return secrets.token_hex(12)


def now():
    return time.time()


def launch_args(*args):
    return [sys.executable, *([] if getattr(sys,'frozen',False) else ['-m','ptfree']), *map(str,args)]
