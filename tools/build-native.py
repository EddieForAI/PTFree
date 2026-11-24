# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

"""Build the standalone launcher on its target OS using a prepared environment."""
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
subprocess.run(
    [sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm', str(root / 'tools' / 'ptfree.spec')],
    cwd=root,
    check=True,
)
print('Executable created in dist/.')
