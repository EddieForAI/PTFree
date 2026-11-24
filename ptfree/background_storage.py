# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path, PureWindowsPath

from .background_store import GIB, qpath
from .common import AppError, WINDOWS_NO_CONSOLE, atomic_write
from .qb import local_endpoint


def windows_memory():
    import ctypes
    from ctypes import wintypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('load', wintypes.DWORD),
                    *((key, ctypes.c_ulonglong) for key in ('total', 'available', 'page', 'page_avail',
                                                            'virtual', 'virtual_avail', 'ext'))]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise AppError('无法读取 Windows 可用内存', 'storage')
    return status.available


def linux_memory():
    match = re.search(r'^MemAvailable:\s+(\d+)', Path('/proc/meminfo').read_text(), re.MULTILINE)
    if not match:
        raise AppError('无法读取 Linux 可用内存', 'storage')
    return int(match[1]) * 1024


def imdisk_command(*args):
    exe = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'imdisk.exe'
    if not exe.is_file():
        raise AppError('请在 Windows 安装 ImDisk 和 AWEAlloc 物理内存驱动', 'storage')
    return subprocess.run([str(exe), *args], capture_output=True, timeout=30, creationflags=WINDOWS_NO_CONSOLE)


def drive_mask():
    import ctypes

    mask = ctypes.WinDLL('kernel32', use_last_error=True).GetLogicalDrives()
    if not mask:
        raise AppError('无法读取 Windows 盘符', 'storage')
    return mask


def letter_in_use(letter, mask):
    return bool(mask & (1 << (ord(letter.upper()) - ord('A'))))


def owned_ram(path, owner):
    try:
        return (path / '.mtfree-volume').read_bytes() == owner.encode('ascii')
    except OSError:
        return False


def automatic_ram_path(previous, owner, *, pinned=False):
    if os.name != 'nt':
        return Path('/mnt') / ('mtfree-ram-' + owner)
    mask = drive_mask()
    if previous:
        path = Path(previous)
        drive = path.drive
        if re.fullmatch(r'[D-Zd-z]:', drive):
            if pinned or not letter_in_use(drive[0], mask) or owned_ram(path, owner):
                return path
    for letter in 'RSTUVWXYZQPONMLKJIHGFED':
        if not letter_in_use(letter, mask):
            return Path(letter + r':\MTFree')
    raise AppError('找不到可供内存盘使用的空闲盘符', 'storage')


def create_ram(path, size, reserve):
    if os.name == 'nt':
        drive = path.anchor[:2]
        if not re.fullmatch(r'[D-Zd-z]:', drive) or letter_in_use(drive[0], drive_mask()):
            raise AppError('内存盘自动创建需要未占用的盘符，如 R:\\MTFree', 'storage')
        if windows_memory() < size + reserve:
            raise AppError('可用内存不足以创建内存盘并满足系统预留', 'storage')
        result = imdisk_command('-a', '-s', str(size), '-o', 'awe', '-m', drive, '-p', '/fs:ntfs /q /y')
        if result.returncode:
            raise AppError('创建内存盘失败，请检查 AWEAlloc 驱动及 PTFree 是否以管理员权限运行', 'storage')
        path.mkdir(parents=True, exist_ok=True)
        return
    if os.geteuid() != 0:
        raise AppError('自动创建 tmpfs 需要挂载权限。请在有挂载权限的环境启动或预先挂载', 'storage')
    if linux_memory() < size + reserve:
        raise AppError('可用内存不足以创建内存盘并满足系统预留', 'storage')
    if path.resolve() != path or path.is_mount():
        raise AppError('内存盘自动创建要求空的专用目录，且无符号链接', 'storage')
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise AppError('内存盘自动创建要求空的专用目录', 'storage')
    owner = path.stat()
    flags = f'size={size},nosuid,nodev,noexec,uid={owner.st_uid},gid={owner.st_gid},mode=0770'
    if len(Path('/proc/swaps').read_text().splitlines()) > 1:
        flags += ',noswap'
    proc = subprocess.run(['mount', '-t', 'tmpfs', '-o', flags, 'mtfree', '--', str(path)],
                          capture_output=True, timeout=10)
    if proc.returncode:
        raise AppError('自动挂载 tmpfs 失败，请检查权限及内核 noswap 支持', 'storage')


def verify_windows_ram(kernel, drive):
    import ctypes
    from ctypes import wintypes

    class DiskGeometry(ctypes.Structure):
        _fields_ = [('size', ctypes.c_longlong), ('media_type', wintypes.DWORD),
                    ('tracks', wintypes.DWORD), ('sectors', wintypes.DWORD), ('sector_bytes', wintypes.DWORD)]

    class CreateData(ctypes.Structure):
        _fields_ = [('device_number', wintypes.DWORD), ('geometry', DiskGeometry),
                    ('image_offset', ctypes.c_longlong), ('flags', wintypes.DWORD),
                    ('letter', wintypes.WCHAR), ('filename_length', wintypes.WORD),
                    ('filename', wintypes.WCHAR * 1024)]

    result = ctypes.create_unicode_buffer(4096)
    if not kernel.QueryDosDeviceW(ctypes.c_wchar_p(drive), result, len(result)):
        raise AppError('内存盘盘符尚未挂载', 'storage')
    match = re.fullmatch(r'\\Device\\ImDisk(\d+)', result.value, re.IGNORECASE)
    if not match:
        raise AppError('当前盘符未确认使用 ImDisk 物理内存盘', 'storage')
    path = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'imdisk.cpl'
    try:
        query = ctypes.WinDLL(str(path), use_last_error=True).ImDiskQueryDevice
    except (OSError, AttributeError):
        raise AppError('无法读取 ImDisk 驱动类型，请检查安装', 'storage') from None
    query.argtypes = [wintypes.DWORD, ctypes.POINTER(CreateData), wintypes.DWORD]
    query.restype = wintypes.BOOL
    data = CreateData()
    # From the ImDisk public header: FILE device + physical-memory AWEAlloc backend.
    if not query(int(match[1]), ctypes.byref(data), ctypes.sizeof(data)) or data.flags & 0xff00 != 0x1100:
        raise AppError('当前盘符未确认使用 AWEAlloc 物理内存', 'storage')


def ram_available(path):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        verify_windows_ram(kernel, path.anchor[:2])
        serial = wintypes.DWORD()
        if not kernel.GetVolumeInformationW(ctypes.c_wchar_p(path.anchor), None, 0, ctypes.byref(serial),
                                             None, None, None, 0):
            raise AppError('无法核验 RAM 盘的卷标识', 'storage')
        return windows_memory(), True, f'{path.anchor}{serial.value}'
    resolved = path.resolve(strict=True)
    mount = None
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left, right = line.split(' - ', 1)
        fields = left.split()
        point = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4]))
        if resolved.is_relative_to(point) and (mount is None or len(point.parts) >= len(mount[0].parts)):
            mount = point, right.split(), fields[0]
    if mount is None or mount[1][0] != 'tmpfs':
        raise AppError('下载目录当前不在 Linux tmpfs 上', 'storage')
    no_swap = 'noswap' in mount[1][2].split(',') or len(Path('/proc/swaps').read_text().splitlines()) <= 1
    if not no_swap:
        raise AppError('当前 tmpfs 可写入交换空间，请用 noswap 挂载，或关闭系统交换空间', 'storage')
    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    return linux_memory(), False, boot_id + ':' + mount[2]


def storage_usage(config, server):
    if not config['save_path'] or not local_endpoint(config['qb_url']):
        return {'volume_free': server.get('free_space_on_disk')}
    try:
        usage = shutil.disk_usage(Path(config['save_path']))
    except OSError:
        return {}
    return {'volume_used': usage.used, 'volume_total': usage.total, 'volume_free': usage.free}


def capacity(config, qb, server, *, create=False):
    path = qpath(config['save_path'])
    if not path.is_absolute():
        raise AppError('请先配置 qB 下载目录', 'storage')
    ram = config['storage'] == 'ram'
    medium = None
    if qb.preferences.get('temp_path_enabled') and qb.version < (5, 1):
        raise AppError('当前 qB 启用了全局未完成目录。请关闭该选项，或升级到支持单种下载路径的 qB 版本', 'storage')
    if local_endpoint(config['qb_url']):
        native_win = os.name == 'nt'
        if native_win != isinstance(path, PureWindowsPath):
            raise AppError('下载目录格式与 PTFree 所在平台不一致', 'storage')
        local = Path(path)
        if ram:
            owner = config.get('ram_owner')
            try:
                available, allocated, medium = ram_available(local)
                if owner and not owned_ram(local, owner):
                    raise AppError('此位置已有其他卷或挂载，无法作为本实例的自动内存盘', 'storage')
            except (AppError, OSError):
                if not (create and config.get('ram_create', True)):
                    raise
                create_ram(local, int(config['ram_capacity_gib'] * GIB), int(config['ram_reserve_gib'] * GIB))
                available, allocated, medium = ram_available(local)
                if owner:
                    atomic_write(local / '.mtfree-volume', owner.encode('ascii'))
        if not local.is_dir():
            if ram and os.name == 'nt':
                local.mkdir(parents=True, exist_ok=True)
            else:
                raise AppError('下载目录未挂载或不存在，请先在 qB 主机上创建', 'storage')
        free = shutil.disk_usage(local).free
        if ram:
            headroom = available - int(config['ram_reserve_gib'] * GIB)
            if headroom < 0:
                raise AppError('系统剩余内存低于预留值，后台下载暂停', 'storage')
            if not allocated:
                free = min(free, headroom)
    else:
        if ram:
            raise AppError('内存模式需要 PTFree 与 qB 同机，且 WebUI 使用回环地址，以核验 RAM 盘', 'storage')
        default = qpath(str(qb.preferences.get('save_path') or ''))
        if (not default.is_absolute() or type(path) is not type(default) or path == default
                or path.parent != default):
            raise AppError('远程 qB 磁盘模式请使用默认下载目录下的专用直属子目录', 'storage')
        free = server.get('free_space_on_disk')
        if not isinstance(free, int) or free < 0:
            raise AppError('远程 qB 未提供默认盘可用容量', 'storage')
    disk_limit = int(config['capacity_gib'] * GIB)
    if ram:
        return int(config['ram_capacity_gib'] * GIB), free, medium
    return disk_limit, max(0, free - int(config['min_free_gib'] * GIB)), medium


def owned_path(root, row, require_download_path=False):
    expected = qpath(root)
    save = row.get('save_path')
    if not save or qpath(save) != expected or row.get('auto_tmm') is True:
        return False
    actual = row.get('download_path')
    if require_download_path and not actual:
        return False
    if actual and qpath(actual) != expected:
        return False
    content = row.get('content_path')
    if not content:
        return True
    path = qpath(content)
    return type(path) is type(expected) and '..' not in path.parts and path.is_relative_to(expected)


def cleanup_empty_root(root, url):
    if not local_endpoint(url):
        return
    try:
        Path(root).rmdir()
    except (ValueError, OSError):
        pass
