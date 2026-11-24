# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

FROM python:3.12-slim-bookworm

ARG PIP_INDEX_URL=https://pypi.org/simple
RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --index-url "$PIP_INDEX_URL" --only-binary=:all: -r requirements.txt

COPY LICENSE NOTICE THIRD-PARTY-NOTICES.md ./
COPY ptfree ./ptfree
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MTFREE_DATA=/data \
    MTFREE_WATCH_DIR=/watch \
    MTFREE_WEB_PORT=8787 \
    TZ=Asia/Shanghai
EXPOSE 8787
USER 1026:100
CMD ["python", "-m", "ptfree", "--host", "0.0.0.0", "run"]
