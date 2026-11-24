# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
import sys

root = Path(SPECPATH).parent
analysis = Analysis(
    [str(root / 'tools' / 'native-entry.py')],
    pathex=[str(root)],
    datas=[(str(root / 'ptfree' / 'static'), 'ptfree/static')],
)
if sys.platform.startswith('linux'):
    analysis.exclude_system_libraries()

archive = PYZ(analysis.pure)
exe = EXE(archive, analysis.scripts, analysis.binaries, analysis.datas,
          name='ptfree', console=True, upx=False)
