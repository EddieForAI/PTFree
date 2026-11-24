/* SPDX-FileCopyrightText: 2026 EddieForAI
 * SPDX-License-Identifier: Apache-2.0 */

'use strict';
(() => {
  const graph = $('#bg-chart');
  const period = $('#bg-period');
  const plot = [
    ['wan_rx', 'PPPoE 下行', '#76e3ba'], ['wan_tx', 'PPPoE 上行', '#96baff'],
    ['job_rx', 'Freeseed 下载', '#ffd27a'], ['job_tx', 'Freeseed 上传', '#d9a4ff'],
  ];
  const rate = value => value == null ? '—' : Number(value.toFixed(1)).toLocaleString('zh-CN') + ' Mbps';
  const ratio = value => value == null ? '—' : (value * 100).toFixed(1) + '%';
  const chartTime = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false});
  const labelTime = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false});
  const timeLabel = time => labelTime.format(new Date(time * 1000));
  let history = null;
  let cursor = null;
  let historyOffset = 0;
  let historyRequest = 0;
  let lastHistoryLoad = 0;
  let loading = false;
  let optionsLoaded = false;

  async function refreshStatistics() {
    if (loading || !csrf || $('#shell').hidden || currentView !== 'dashboard') return;
    loading = true;
    try {
      const status = await api('/api/monitor');
      $('#summary-qb').textContent = status.error || status.qb_note || (status.checked_at ? '已连接' : '等待采样');
      $('#summary-router').textContent = status.error || status.router_note || (status.checked_at ? '已连接' : '等待采样');
      $('#bg-now').textContent = rate(status.wan_rx) + ' / ' + rate(status.wan_tx);
      $('#bg-latency').textContent = status.error || status.router_note ||
        (status.latency == null ? '暂无延迟样本' : '延迟 ' + status.latency.toFixed(1) + ' ms');
      $('#bg-overview-storage').textContent = [
        status.volume_total != null && `种子目录卷已用 ${formatBytes(status.volume_used)} / ${formatBytes(status.volume_total)}`,
        status.volume_free != null && `种子目录卷空闲 ${formatBytes(status.volume_free)}`,
        status.qb_default_free != null && `qB 默认下载卷空闲 ${formatBytes(status.qb_default_free)}`,
        status.capacity != null && `Freeseed 配额已用 ${formatBytes(status.reserved)} / ${formatBytes(status.capacity)}`,
        status.qb_note,
      ].filter(Boolean).join(' · ') || '等待存储采样';
      if (Date.now() - lastHistoryLoad > 12000) await loadHistory();
    } catch (error) {
      $('#bg-overview-storage').textContent = error.message;
    } finally {
      loading = false;
    }
  }

  async function loadHistory() {
    if (!csrf) return;
    const request = ++historyRequest;
    lastHistoryLoad = Date.now();
    const response = await api(`/api/monitor/stats?period=${period.value}&offset=${historyOffset}`);
    if (request !== historyRequest) return;
    history = response;
    cursor = null;
    $('#bg-chart-detail').hidden = true;
    $('#bg-peak').textContent = history.peak_mbps == null ? '数据不足' : rate(history.peak_mbps);
    const cutoff = history.job_accounting_since;
    const olderStats = cutoff && history.start < cutoff
      ? ` · ${timeLabel(cutoff)} 前的任务流量可能漏计` : '';
    $('#bg-window').textContent = `${timeLabel(history.start)} — ${timeLabel(history.end)} · 北京时间 · 实测为已观测峰值${olderStats}`;
    $('#bg-total-wan').textContent = history.wan_coverage ? `${formatBytes(history.wan_rx)} / ${formatBytes(history.wan_tx)}` : '—';
    $('#bg-total-jobs').textContent = history.job_coverage ? `${formatBytes(history.job_rx)} / ${formatBytes(history.job_tx)}` : '—';
    $('#bg-coverage').textContent = `PPPoE ${ratio(history.wan_coverage)} · Freeseed ${ratio(history.job_coverage)}`;
    $('#bg-older').disabled = historyOffset === 30 || period.value === 'all';
    $('#bg-newer').disabled = historyOffset === 0;
    const totals = $('#bg-download-totals');
    totals.replaceChildren();
    for (const [key, label] of [['day', '最近24小时'], ['week', '最近7天'], ['month', '最近30天'], ['year', '最近365天'], ['all', '累计']]) {
      const block = el('div');
      const total = history.download_totals?.[key];
      block.append(el('dt', label), el('dd', total == null ? '—' : formatBytes(total)));
      totals.append(block);
    }
    const events = $('#bg-events');
    events.replaceChildren();
    for (const item of history.events) {
      const line = el('div', undefined, 'event');
      line.append(el('time', timeLabel(item.ts)), el('span', '采样', 'info'), el('span', item.message));
      events.append(line);
    }
    if (!history.events.length) events.append(el('p', '此时段没有采样事件。', 'empty'));
    draw();
  }

  function draw() {
    if (!history || !graph.clientWidth) return;
    const width = graph.clientWidth;
    const height = graph.clientHeight;
    const pixelRatio = Math.min(2, window.devicePixelRatio || 1);
    graph.width = width * pixelRatio;
    graph.height = height * pixelRatio;
    const ctx = graph.getContext('2d');
    ctx.scale(pixelRatio, pixelRatio);
    const inset = {left: 52, top: 16, bottom: 29, right: 16};
    const top = inset.top;
    const bottom = height - inset.bottom;
    const span = Math.max(1, history.end - history.start);
    const peak = Math.max(10, ...history.points.flatMap(point => plot.map(([key]) => point[key] || 0)));
    const max = Math.ceil(peak / (peak > 100 ? 100 : 10)) * (peak > 100 ? 100 : 10);
    const x = time => inset.left + (time - history.start) / span * (width - inset.left - inset.right);
    const y = v => bottom - v / max * (bottom - top);
    ctx.font = '11px system-ui';
    ctx.strokeStyle = '#213347';
    ctx.fillStyle = '#99aec6';
    for (let i = 0; i <= 4; i++) {
      const value = i * max / 4;
      const row = y(value);
      ctx.beginPath(); ctx.moveTo(inset.left, row); ctx.lineTo(width - inset.right, row); ctx.stroke();
      ctx.textAlign = 'right'; ctx.fillText(String(Number(value.toFixed(1))), inset.left - 8, row + 4);
      const stamp = history.start + span / 4 * i;
      ctx.textAlign = i === 0 ? 'left' : i === 4 ? 'right' : 'center';
      if (width > 420 || i % 2 === 0) ctx.fillText(chartTime.format(new Date(stamp * 1000)), x(stamp), height - 10);
    }
    ctx.lineWidth = 1.6;
    for (const [key, , color] of plot) {
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      let previous = null;
      for (const point of history.points) {
        if (point[key] == null) { previous = null; continue; }
        const a = x(point.ts);
        const b = y(point[key]);
        if (previous && point.ts - previous.ts <= history.bucket) {
          ctx.beginPath(); ctx.moveTo(x(previous.ts), y(previous[key])); ctx.lineTo(a, b); ctx.stroke();
        } else {
          ctx.beginPath(); ctx.arc(a, b, 2, 0, Math.PI * 2); ctx.fill();
        }
        previous = point;
      }
    }
    if (!history.points.length) {
      ctx.textAlign = 'center'; ctx.fillStyle = '#99aec6'; ctx.fillText('暂无流量采样', width / 2, height / 2);
    }
    if (cursor != null && history.points[cursor]) {
      const point = history.points[cursor];
      ctx.strokeStyle = '#d6e9fd90';
      ctx.beginPath(); ctx.moveTo(x(point.ts), top); ctx.lineTo(x(point.ts), bottom); ctx.stroke();
      const detail = $('#bg-chart-detail');
      detail.textContent = `${timeLabel(point.ts)} · ${plot.map(([key, label]) => `${label} ${rate(point[key])}`).join(' · ')}${point.latency == null ? '' : ` · 延迟 ${point.latency.toFixed(1)} ms`}`;
      detail.hidden = false;
    }
  }

  new ResizeObserver(draw).observe(graph);
  graph.onpointermove = event => {
    if (!history || !history.points.length) return;
    const fraction = (event.offsetX - 52) / Math.max(1, graph.clientWidth - 68);
    const stamp = history.start + fraction * (history.end - history.start);
    let closest = 0;
    history.points.forEach((p, i) => { if (Math.abs(p.ts - stamp) < Math.abs(history.points[closest].ts - stamp)) closest = i; });
    cursor = closest;
    draw();
  };
  graph.onpointerleave = () => { cursor = null; $('#bg-chart-detail').hidden = true; draw(); };
  graph.onkeydown = event => {
    if (!history?.points.length || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    cursor = Math.max(0, Math.min(history.points.length - 1, (cursor ?? -1) + (event.key === 'ArrowRight' ? 1 : -1)));
    draw();
  };
  function changePeriod(offset) {
    historyOffset = offset;
    loadHistory().catch(error => toast(error.message));
  }
  period.onchange = () => changePeriod(0);
  $('#bg-older').onclick = () => changePeriod(historyOffset + 1);
  $('#bg-newer').onclick = () => changePeriod(Math.max(0, historyOffset - 1));

  const forms = [$('#global-event-form'), $('#qb-options-form'), $('#router-options-form')];
  const qbForm = forms[1];
  function updateQBFields() {
    const useKey = qbForm.elements.qb_auth.value === 'api_key';
    for (const [name, visible] of [['qb_user', !useKey], ['qb_password', !useKey], ['qb_api_key', useKey]]) {
      const field = qbForm.elements.namedItem(name);
      field.closest('label').hidden = !visible;
      field.disabled = !visible;
    }
  }
  qbForm.elements.qb_auth.addEventListener('change', updateQBFields);

  function fillOptions(config, selected = forms) {
    for (const form of selected) {
      for (const field of form.elements) {
        if (!field.name) continue;
        field.value = field.type === 'password' ? '' : config[field.name];
        if (field.type === 'password') field.placeholder = config[field.name + '_set'] ? '已保存，留空保留' : '未设置';
      }
    }
    updateQBFields();
  }

  async function loadOptions() {
    if (!optionsLoaded && csrf) {
      fillOptions(await api('/api/options'));
      optionsLoaded = true;
    }
  }

  function formData(form) {
    return Object.fromEntries([...form.elements].filter(field => field.name && !field.disabled).map(
      field => [field.name, field.type === 'number' ? Number(field.value) : field.value]));
  }

  for (const form of forms) {
    form.onsubmit = async event => {
      event.preventDefault();
      try {
        const result = await api('/api/options', formData(form));
        fillOptions(result.config, [form]);
        toast(result.message);
      } catch (error) {
        toast(error.message);
      }
    };
    const test = form.querySelector('[data-test]');
    if (test) test.onclick = async () => {
      if (!form.reportValidity()) return;
      test.disabled = true;
      try {
        const result = await api('/api/options/' + test.dataset.test + '/test', formData(form));
        toast([result.message, result.version].filter(Boolean).join(' · '));
      } catch (error) {
        toast(error.message);
      } finally {
        test.disabled = false;
      }
    };
  }

  document.addEventListener('mtfree:view', event => {
    if (['global-config', 'site-config'].includes(event.detail)) loadOptions().catch(error => toast(error.message));
    if (event.detail === 'dashboard') refreshStatistics();
  });
  setInterval(() => {
    if (!csrf) optionsLoaded = false;
    if (['global-config', 'site-config'].includes(currentView) && !$('#shell').hidden && !optionsLoaded)
      loadOptions().catch(error => toast(error.message));
    refreshStatistics();
  }, 3000);
})();
