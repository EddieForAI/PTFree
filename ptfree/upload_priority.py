# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

"""Control PCDN download speed using observed upload, in Mbps."""
from collections import deque
from statistics import median


SAMPLE_SECONDS = 10
WINDOW_SECONDS = 90
SETTLE_SECONDS = 15
MIN_UPLOAD = 2


class UploadPriority:
    def __init__(self, ceiling):
        self.floor = min(10, ceiling)
        self.rate = min(50, ceiling)
        self.baseline = None
        self.target = None
        self.state = 'learning'
        self.reason = '建立上传基线'
        self.trial = None
        self.hold_new = False
        self.samples = deque(maxlen=WINDOW_SECONDS // SAMPLE_SECONDS + 1)
        self.window_start = None
        self.last_sample = None
        self.settle_until = 0
        self.retry_at = 0

    def change_rate(self, rate, now, state, reason):
        if abs(self.rate - rate) >= .001:
            self.rate = rate
            self.settle_until = now + SETTLE_SECONDS
            self.samples.clear()
            self.window_start = None
        self.state, self.reason = state, reason

    def observe(self, now, ceiling, line_down, qb_up, job_down, other_down, wan_up, line_up, high_latency):
        if qb_up is None:
            self.samples.clear()
            self.window_start = None
            self.state, self.reason = 'missing', '等待 qB 上传测速，保持下载上限'
            self.hold_new = True
            return min(self.rate, ceiling)
        if self.state == 'missing':
            self.state = 'probe' if self.trial else 'learning' if self.baseline is None else 'steady'
            self.reason = 'qB 测速已恢复，等待新样本'
            self.hold_new = bool(self.trial)
        if now < self.settle_until or (self.last_sample is not None and now - self.last_sample < SAMPLE_SECONDS):
            return min(self.rate, ceiling)
        if self.last_sample is not None and now - self.last_sample > 60:
            self.samples.clear()
            self.window_start = None
        self.last_sample = now
        if self.window_start is None:
            self.window_start = now
        self.samples.append((qb_up, job_down, other_down, wan_up is not None and wan_up >= line_up * .92))
        if len(self.samples) < 3 or now - self.window_start < WINDOW_SECONDS:
            return min(self.rate, ceiling)

        upload = median(sample[0] for sample in self.samples)
        download = median(sample[1] for sample in self.samples)
        other = median(sample[2] for sample in self.samples)
        near_full = sum(sample[3] for sample in self.samples) * 2 > len(self.samples)
        self.samples.clear()
        self.window_start = None
        budget = min(ceiling, max(self.floor, line_down - other))

        if self.baseline is None:
            self.baseline = upload
            if upload >= MIN_UPLOAD:
                self.change_rate(min(350, budget), now, 'steady', '已建立上传基线，开始调节下载')
            else:
                self.change_rate(budget, now, 'steady', '上传需求较低，按可用下行分配')
        elif self.trial:
            prior_rate, prior_up, prior_down = self.trial
            self.trial = None
            recovered = upload >= prior_up + max(.5, prior_up * .12)
            if download <= prior_down * .9 and recovered:
                self.baseline = max(self.baseline, upload)
                self.change_rate(min(self.rate, budget), now, 'protect', '降下载后上传回升，保持当前下载上限')
            else:
                self.baseline = upload
                self.retry_at = now + 120
                self.change_rate(min(prior_rate, budget), now, 'steady', '降低下载未带来可验证的上传回升，恢复试探前上限')
        elif (now >= self.retry_at and self.baseline >= MIN_UPLOAD and upload < self.baseline * .9
              and download > self.floor * 1.15 and self.rate > self.floor
              and (download >= min(100, ceiling * .25) or (near_full and high_latency))):
            ratio = .70 if near_full and high_latency else .75
            reduced = max(self.floor, min(self.rate * ratio, download * ratio, budget))
            self.trial = self.rate, upload, download
            self.change_rate(reduced, now, 'probe', 'qB 上传持续低于目标，降低下载并观察回升')
        elif budget < self.rate:
            self.change_rate(budget, now, 'steady', '其他流量占用下行，收紧托管下载额度')
        elif self.baseline < MIN_UPLOAD and upload < MIN_UPLOAD:
            self.baseline = upload
            self.change_rate(budget, now, 'steady', '上传需求较低，按可用下行分配')
        else:
            if download <= self.floor * 1.15 and upload < self.baseline * .9:
                self.baseline = upload
            else:
                self.baseline = max(self.baseline, upload)
            self.change_rate(min(budget, self.rate + 50), now, 'steady',
                             '上传稳定，逐步放开下载' if self.rate < budget else '维持当前托管下载额度')
        self.target = self.baseline * .9 if self.baseline >= MIN_UPLOAD else None
        self.hold_new = self.state in ('probe', 'protect')
        return self.rate
