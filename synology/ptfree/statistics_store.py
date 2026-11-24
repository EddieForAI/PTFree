# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import datetime as dt
import math
import time
from collections import deque

from .policy import TZ
from .traffic import interval_rows


class StatisticsStore:
    def __init__(self, db):
        self.db = db
        with db.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS freeseed_traffic_days (day INTEGER PRIMARY KEY, bytes INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS freeseed_traffic_events (ts REAL PRIMARY KEY, message TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS freeseed_traffic_minutes (
                  minute INTEGER PRIMARY KEY,
                  wan_down INTEGER NOT NULL, wan_up INTEGER NOT NULL,
                  job_down INTEGER NOT NULL, job_up INTEGER NOT NULL,
                  wan_s REAL NOT NULL, job_s REAL NOT NULL, latency REAL NOT NULL, latency_n INTEGER NOT NULL
                );
            """)

    def event(self, message):
        with self.db.connect() as conn:
            conn.execute('INSERT INTO freeseed_traffic_events VALUES (?,?)', (time.time(), message))

    def add_interval(self, start, end, *, wan=None, job=None, latency=None):
        with self.db.connect() as conn:
            for minute, data in interval_rows(start, end, wan=wan, job=job, latency=latency):
                conn.execute('''INSERT INTO freeseed_traffic_minutes VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(minute) DO UPDATE SET wan_down=wan_down+excluded.wan_down, wan_up=wan_up+excluded.wan_up,
                    job_down=job_down+excluded.job_down, job_up=job_up+excluded.job_up,
                    wan_s=wan_s+excluded.wan_s, job_s=job_s+excluded.job_s,
                    latency=latency+excluded.latency, latency_n=latency_n+excluded.latency_n''', (minute, *data))
                if job is not None:
                    conn.execute('''INSERT INTO freeseed_traffic_days VALUES (?,?)
                        ON CONFLICT(day) DO UPDATE SET bytes=bytes+excluded.bytes''', ((minute+28800)//86400, data[2]))
            if job is not None and end > start:
                conn.execute("INSERT OR IGNORE INTO kv VALUES ('freeseed_job_accounting_since', ?)", (str(start),))

    def download_totals(self, now):
        # Use the same elapsed windows as the graph; the all-time total is
        # durable even if minute-level history is ever compacted in the future.
        spans = (86400, 7 * 86400, 30 * 86400, 365 * 86400)
        boundaries = tuple(int(now - span) for span in spans)
        with self.db.connect() as conn:
            totals = [0] * 4
            seen = [False] * 4
            for row in conn.execute('SELECT minute,job_down,job_s FROM freeseed_traffic_minutes WHERE minute>=? AND minute<?',
                                    (boundaries[-1] // 60 * 60, now)):
                minute, count, covered = row
                if not covered and not count:
                    continue
                for i, boundary in enumerate(boundaries):
                    if minute + 60 <= boundary:
                        continue
                    fraction = min(1, max(0, min(now, minute + 60) - max(boundary, minute)) /
                                   max(1e-9, min(now, minute + 60) - minute))
                    totals[i] += round(count * fraction)
                    seen[i] = True
            all_time = conn.execute('SELECT SUM(bytes) FROM freeseed_traffic_days').fetchone()[0]
        return dict(zip(('day', 'week', 'month', 'year', 'all'),
                        [v if found else None for v, found in zip(totals, seen)] + [all_time]))

    def history(self, period, offset=0, now=None):
        now = time.time() if now is None else now
        durations = {'day': (86400, 300), 'week': (7 * 86400, 1800),
                     'month': (30 * 86400, 7200), 'year': (365 * 86400, 86400)}
        if period in durations:
            duration, bucket = durations[period]
            end = now - offset * duration
            start = end - duration
        else:
            with self.db.connect() as conn:
                first = conn.execute('SELECT MIN(minute) FROM freeseed_traffic_minutes').fetchone()[0]
            start, end = (first if first is not None else now), now
            bucket = max(86400, math.ceil((end - start) / (86400 * 600)) * 86400)
        start, end = float(start), float(end)
        total = [0] * 8
        windows = deque(maxlen=5)
        buckets = {}
        peak, peak_windows = 0, 0
        with self.db.connect() as conn:
            for row in conn.execute('SELECT * FROM freeseed_traffic_minutes WHERE minute>=? AND minute<? ORDER BY minute', (start // 60 * 60, end)):
                minute, *values = row
                overlap_start, overlap_end = max(start, minute), min(end, minute + 60)
                if overlap_end <= overlap_start:
                    continue
                # Stored data is aggregated by minute. Estimate only the fractional
                # boundary minute; the current, incomplete minute is already partial.
                recorded_end = min(now, minute + 60)
                fraction = min(1, (overlap_end - overlap_start) / max(1e-9, recorded_end - minute))
                if fraction < 1:
                    values = [round(v * fraction) if i < 4 else v * fraction for i, v in enumerate(values)]
                windows.append((minute, values[0], values[4]))
                if (len(windows) == 5 and windows[-1][0] - windows[0][0] == 240
                        and all(item[2] >= 59.5 for item in windows)):
                    peak_windows += 1
                    peak = max(peak, sum(item[1] for item in windows) * 8 / sum(item[2] for item in windows) / 1e6)
                index = start + (max(start, minute) - start) // bucket * bucket
                group = buckets.setdefault(index, [0] * 8)
                for i, value in enumerate(values):
                    total[i] += value
                    group[i] += value
        span = max(1, end - start)
        wan_down, wan_up, job_down, job_up, wan_s, job_s, *_ = total
        job_mbps = job_down * 8 / span / 1e6 if job_s else None
        def mbps(count, seconds):
            return round(count * 8 / seconds / 1e6, 3) if seconds else None
        points = [{'ts': i, 'wan_rx': mbps(values[0], values[4]), 'wan_tx': mbps(values[1], values[4]),
                   'job_rx': mbps(values[2], values[5]), 'job_tx': mbps(values[3], values[5]),
                   'latency': values[6] / values[7] if values[7] else None}
                  for i, values in buckets.items()]
        with self.db.connect() as conn:
            events = [dict(row) for row in conn.execute("""SELECT ts,message FROM freeseed_traffic_events WHERE ts>=? AND ts<? ORDER BY ts DESC LIMIT 200""", (start, end))]
        return {'start': start, 'end': end, 'bucket': bucket, 'peak_mbps': peak or None, 'peak_windows': peak_windows,
                'index': job_mbps / peak * 100 if peak and job_mbps is not None else None,
                'wan_rx': wan_down, 'wan_tx': wan_up, 'job_rx': job_down, 'job_tx': job_up,
                'wan_coverage': min(1, wan_s / span), 'job_coverage': min(1, job_s / span),
                'job_average_mbps': job_mbps, 'points': points, 'events': events,
                'download_totals': self.download_totals(now),
                'job_accounting_since': self.db.get('freeseed_job_accounting_since')}
