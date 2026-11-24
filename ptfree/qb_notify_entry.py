# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

"""Absolute-path entry point for qBittorrent's completion hook."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ptfree.completion import notify

if __name__ == '__main__':
    raise SystemExit(notify(Path(sys.argv[1]), sys.argv[2:4]))
