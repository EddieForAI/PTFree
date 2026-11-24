#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
data_dir="${MTFREE_DATA:-${XDG_DATA_HOME:-$HOME/.local/share}/mtfree}"
if [[ -x bin/ptfree ]]; then
  if bin/ptfree --data "$data_dir" init; then
    chmod +x run-linux.sh install-service-linux.sh
    echo "Installed standalone Linux x86_64 program. Data: $data_dir"
    echo 'Start with: ./run-linux.sh'
    exit 0
  fi
  echo 'Standalone executable cannot run on this system. Trying the source installation.'
fi
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
if ! python3 -m venv .venv; then
  echo 'On Ubuntu/Debian run: sudo apt update && sudo apt install -y python3-venv'
  exit 1
fi
if ! compgen -G 'wheels/*.whl' > /dev/null || ! .venv/bin/python -m pip install --no-index --find-links wheels -r requirements.txt; then
  .venv/bin/python -m pip install --index-url https://pypi.mirrors.ustc.edu.cn/simple --timeout 30 --retries 2 -r requirements.txt
fi
.venv/bin/python -m ptfree --data "$data_dir" init
chmod +x run-linux.sh install-service-linux.sh
echo "Installed. Data: $data_dir"
echo 'Read the initial password .txt file in the data directory.'
echo 'Start with: ./run-linux.sh'
