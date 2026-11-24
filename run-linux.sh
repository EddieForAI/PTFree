#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
data_dir="${MTFREE_DATA:-${XDG_DATA_HOME:-$HOME/.local/share}/mtfree}"
bind_address="${MTFREE_HOST:-127.0.0.1}"
service_port="${MTFREE_PORT:-8787}"
if [[ -x bin/ptfree && ! -x .venv/bin/python ]]; then
  exec bin/ptfree --data "$data_dir" --host "$bind_address" --port "$service_port" run
fi
exec .venv/bin/python -m ptfree --data "$data_dir" --host "$bind_address" --port "$service_port" run
