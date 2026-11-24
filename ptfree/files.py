# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from .common import AppError, atomic_write, new_id
from .torrent import inspect_torrent
from .traffic import record_file_receipt


def test_directory(c):
    if not c['watch_dir'].strip():
        raise AppError('请先设置种子目录','config')
    path=Path(c['watch_dir']).expanduser()
    path.mkdir(parents=True,exist_ok=True)
    probe=path/('.mtfree-probe-'+new_id())
    try:
        atomic_write(probe,b'MTFree write test')
        if probe.read_bytes()!=b'MTFree write test':
            raise AppError('目录读写校验失败','config')
    finally:
        probe.unlink(missing_ok=True)
    free=shutil.disk_usage(path).free
    if free<c['min_disk_gib']*1024**3:
        raise AppError('种子目录所在磁盘剩余空间不足','capacity')
    return {'message':'目录可读写','free_gib':round(free/1024**3,2)}


def save_torrent(c,store,row,data):
    path=Path(c['watch_dir']).expanduser()
    path.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(path).free<c['min_disk_gib']*1024**3+len(data):
        raise AppError('种子目录所在磁盘剩余空间不足','capacity')
    target=path/('MT-'+row['id']+'.torrent')
    temp=path/('.MT-'+row['id']+'.part')
    if target.exists():
        if target.read_bytes()!=data:
            raise AppError('目标种子文件名冲突，未覆盖','config')
        values = inspect_torrent(data)
        record_file_receipt(store, row['id'], values)
        store.set_state(row['id'], row['state'], '目标目录已存在相同种子文件',
                        hashes=','.join(values))
        return '目标目录已存在相同种子文件'
    # Record the staged file before making the final .torrent name visible.
    atomic_write(temp,data)
    values = inspect_torrent(data)
    issued = time.time()
    store.set_state(row['id'],'publishing','正在保存种子文件',target=str(target),temp=str(temp),
                    hashes=','.join(values))
    os.replace(temp,target)
    record_file_receipt(store, row['id'], values, issued)
    return '种子文件已保存到目标目录'


def recover_torrent(store,row):
    temp=Path(row['temp']); target=Path(row['target'])
    if not row['temp'] or not row['target']:
        raise AppError('保存记录不完整，请人工检查','config')
    if temp.exists():
        os.replace(temp,target)
        reason='已恢复种子文件保存'
    else:
        # The file may already have been moved by a folder monitor after the rename.
        reason='已恢复种子文件保存记录'
    record_file_receipt(store, row['id'], row['hashes'].split(','), row['updated'])
    store.set_state(row['id'],'done',reason,completed=time.time(),temp='')
