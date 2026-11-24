# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path, PurePosixPath, PureWindowsPath
import re

from .qb import local_endpoint

GIB = 1024**3


def qpath(value):
    return PureWindowsPath(value) if re.match(r'^[A-Za-z]:|^\\\\', value) else PurePosixPath(value)


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
