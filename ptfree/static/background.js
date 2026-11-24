/* SPDX-FileCopyrightText: 2026 EddieForAI
 * SPDX-License-Identifier: Apache-2.0 */

'use strict';

(() => {
  const apiRoot = '/api/background';
  const sections = {
    'bg-selection-fields': [
      ['min_seeders', '最少做种人数', 'number'],
      ['min_size_gib', '种子大小下限（GiB）', 'number'], ['max_size_gib', '种子大小上限（GiB）', 'limit'],
      ['scan_minutes', '候选补页间隔（分钟）', 'number', '当前批次耗尽后才查下一批。'],
      ['recheck_minutes', '免费复核间隔（分钟）', 'number', '开始前单独核对，以后按此间隔复核运行中的种子。'],
    ],
    'mteam-categories-field': [
      ['categories', '综合区分类 ID', 'text', '留空查全部，电影填 419。多个用逗号分隔。'],
    ],
    'bg-event-fields': [
      ['site_free_start', '开始（北京时间）', 'datetime-local'],
      ['site_free_end', '结束（北京时间）', 'datetime-local'],
    ],
    'bg-ssd-fields': [
      ['ssd_path', '固态盘下载目录', 'text', 'qB 主机上的专用目录。远程 qB 使用默认下载目录下的直属子目录。'],
      ['capacity_gib', '流量助手存储配额（GiB）', 'number', '按所选种子的完整大小预留。'],
      ['min_free_gib', '留给其他程序的盘空间（GiB）', 'number'],
    ],
    'bg-ram-path-fields': [
      ['ram_path', '已有内存盘的下载目录', 'text'],
    ],
    'bg-ram-size-fields': [
      ['ram_capacity_gib', '内存盘大小（GiB）', 'number'], ['ram_reserve_gib', '为系统保留内存（GiB）', 'number'],
    ],
    'bg-storage-fields': [
      ['retain_path', '保留并转移到', 'text', '填写 qB 主机上的目标目录。选中保留的内容完成后由 qB 转移。'],
    ],
    'bg-concurrency-fields': [
      ['max_active', '最大同时下载数', 'number', '从 1 个任务起步，约每 30 秒判断是否增加一个。'],
    ],
    'bg-slow-fields': [
      ['slow_minutes', '判定时长（分钟）', 'number'], ['slow_mbps', '速率阈值（Mbps）', 'number'],
    ],
    'bg-bandwidth-fields': [
      ['line_mbps', '参考下行（Mbps）', 'number'], ['line_up_mbps', '参考上行（Mbps）', 'number'],
      ['reserve_mbps', '家庭预留下行（Mbps）', 'number'],
      ['max_mbps', '托管下载总上限（Mbps）', 'limit'],
    ],
    'bg-shared-bandwidth-fields': [
      ['auto_line', '从 PPPoE 历史估算线路速率', 'checkbox', '使用近 7 天记录的连续 5 分钟峰值。样本不足或峰值低于对应参考值的 65% 时，使用参考值。'],
      ['reserve_up_mbps', '家庭预留上行（Mbps）', 'number'],
      ['max_up_mbps', '托管上传总上限（Mbps）', 'limit'],
    ],
    'bg-latency-probe-fields': [
      ['latency_peer', '外网 Ping 地址', 'text'], ['latency_extra_ms', '延迟升高阈值（ms）', 'number'],
    ],
    'bg-latency-fields': [
      ['latency_interval', '托管任务采样间隔（秒）', 'number'],
      ['latency_action', '持续升高时', {reserve: '额外预留带宽', pause: '暂停后台下载'}],
      ['latency_reserve_mbps', '额外预留下行（Mbps）', 'number'],
      ['latency_reserve_up_mbps', '额外预留上行（Mbps）', 'number'],
    ],
    'bg-qb-fields': [
      ['qb_url', 'Web API 地址', 'url'],
      ['qb_auth', '认证方式', {password: '用户名和密码', api_key: 'API 密钥（qB 5.2+）'}],
      ['qb_user', '用户名', 'text'], ['qb_password', '密码', 'password'],
      ['qb_api_key', 'API 密钥', 'password'],
    ],
    'bg-qb-check-fields': [
      ['qb_hook', '检查方式', {'false': '定时检查', 'true': '完成通知 + 定时检查'}],
      ['qb_poll_seconds', '检查间隔（15–35 秒）', 'number'],
    ],
    'bg-router-fields': [
      ['ros_host', 'RouterOS IP 或主机名', 'text'], ['ros_port', 'API-SSL 端口', 'number'],
      ['ros_user', '用户名', 'text'], ['ros_password', '密码', 'password'], ['ros_fingerprint', 'TLS 证书 SHA-256 指纹', 'text'],
      ['ros_wan', 'PPPoE 接口名', 'text'],
    ],
  };
  for (const [sectionId, rows] of Object.entries(sections)) {
    for (const [name, label, kind, hint] of rows) {
      const field = el(kind === 'limit' ? 'div' : 'label');
      const input = el(typeof kind === 'object' ? 'select' : 'input');
      input.name = name;
      if (name === 'qb_url') input.placeholder = 'http://127.0.0.1:8091';
      if (typeof kind === 'object') {
        for (const [key, text] of Object.entries(kind)) {
          const option = el('option', text);
          option.value = key;
          input.append(option);
        }
      } else {
        input.type = kind === 'limit' ? 'number' : kind;
        if (kind === 'password') input.autocomplete = 'new-password';
        if (name === 'qb_poll_seconds') {
          input.min = '15';
          input.max = '35';
        }
        if (kind === 'number' || kind === 'limit') {
          input.required = true;
          input.step = 'any';
        }
      }
      if (kind === 'limit') {
        field.className = 'limit-field';
        input.id = 'setting-' + name;
        input.min = name === 'max_size_gib' ? '0' : '1';
        if (name !== 'max_size_gib') input.step = '1';
        const title = el('label', label);
        title.htmlFor = input.id;
        field.append(title, input);
        addUnlimitedToggle(field);
      } else if (kind === 'checkbox') {
        field.className = 'bg-check' + (hint ? ' bg-wide-check' : '');
        field.append(input, document.createTextNode(label));
      } else field.append(document.createTextNode(label), input);
      if (hint) field.append(el('small', hint));
      if (['ssd_path', 'ram_path', 'retain_path', 'qb_url', 'qb_auth', 'qb_api_key', 'ros_fingerprint', 'auto_line'].includes(name))
        field.classList.add('bg-field-wide');
      document.getElementById(sectionId).append(field);
    }
  }

  let configLoaded = false;
  let fetching = false;
  let current = null;
  let history = null;
  let historyOffset = 0;
  let historyRequest = 0;
  let lastHistoryLoad = 0;
  let lastTrafficLoad = 0;
  let cursor = null;
  const graph = $('#bg-chart');
  const plot = [
    ['wan_rx', 'PPPoE 下行', '#73e2b1'], ['wan_tx', 'PPPoE 上行', '#8db1ff'],
    ['job_rx', 'PTFree 下载', '#f7ca76'], ['job_tx', 'PTFree 上传', '#d59df8'],
  ];
  const period = $('#bg-period');
  const rate = value => value == null ? '—' : Number(value.toFixed(1)).toLocaleString('zh-CN') + ' Mbps';
  const ratio = (value, scale = 100) => value == null ? '—' : (value * scale).toFixed(1) + '%';
  const chartTime = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false});
  const labelTime = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false});
  const checkTime = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
  const phases = {queued: '排队', ready: '等待提交', adding: '正在加入 qB', active: '托管中', deleting: '清理中', finished: '已完成', removed: '已移除', skipped: '已跳过', expired: '免费结束', slow: '疑似慢种', conflict: '已让给 Freeseed', moving: '正在转移', retained: '已保留'};
  const activePhases = new Set(['queued', 'ready', 'adding', 'active', 'deleting', 'moving']);
  const timeLabel = time => labelTime.format(new Date(time * 1000));

  const schedule = $('#bg-schedule-form');
  const autoRam = () => schedule.querySelector('[name="ram_create"][value="true"]').checked;
  let replaceRamPath = false;

  function updateFields() {
    const ram = schedule.querySelector('[name="storage"][value="ram"]').checked;
    const automatic = schedule.elements.automatic.checked;
    const computed = autoRam();
    for (const [selector, visible] of [['#bg-ssd-fields', !ram], ['#bg-ram-fields', ram],
      ['#bg-ram-path-fields', ram && !computed], ['#bg-ram-auto', ram && computed]]) {
      const group = $(selector);
      group.hidden = !visible;
      for (const input of group.querySelectorAll('input, button')) input.disabled = !visible;
    }
    for (const name of ['min_seeders', 'scan_minutes']) schedule.elements[name].disabled = !automatic;
    const uploadPriority = schedule.elements.bandwidth_mode.value === 'upload_priority';
    for (const selector of ['#bg-shared-bandwidth', '#bg-shared-latency']) {
      $(selector).hidden = uploadPriority;
      $(selector).disabled = uploadPriority;
    }
    $('#bg-shared-hint').hidden = uploadPriority;
    $('#bg-upload-priority-hint').hidden = !uploadPriority;
  }
  schedule.addEventListener('change', event => {
    if (event.target.name === 'ram_create') replaceRamPath = autoRam();
    if (['storage', 'ram_create', 'automatic', 'bandwidth_mode'].includes(event.target.name)) updateFields();
  });

  const qbFields = $('#bg-qb-fields');
  const qbAuth = qbFields.querySelector('[name="qb_auth"]');

  function updateQBFields() {
    const keyMode = qbAuth.value === 'api_key';
    for (const [name, visible] of [['qb_user', !keyMode], ['qb_password', !keyMode], ['qb_api_key', keyMode]]) {
      const field = qbFields.querySelector(`[name="${name}"]`);
      field.disabled = !visible;
      field.closest('label').hidden = !visible;
    }
  }
  qbAuth.addEventListener('change', updateQBFields);

  const sourcesForm = $('#mteam-traffic-form');
  const eventForm = $('#global-event-form');
  const siteForms = [sourcesForm, eventForm];
  const configForms = [schedule, $('#bg-connections-form'), ...siteForms];
  const updateSourceFields = () => {
    sourcesForm.elements.categories.disabled = !sourcesForm.elements.normal.checked;
  };
  sourcesForm.elements.normal.addEventListener('change', updateSourceFields);
  function fillConfig(config, forms = configForms) {
    for (const form of forms) {
      for (const field of form.elements) {
        if (!field.name) continue;
        if (field.type === 'radio') field.checked = String(config[field.name]) === field.value;
        else if (field.type === 'password') {
          field.value = '';
          field.placeholder = config[field.name + '_set'] ? '已保存，留空保留' : '未设置';
        } else if (field.type === 'checkbox') field.checked = config[field.name];
        else field.value = config[field.name];
      }
      fillUnlimitedFields(form, config);
    }
    if (forms.includes(schedule)) {
      updateFields();
      replaceRamPath = false;
    }
    if (forms.includes($('#bg-connections-form'))) updateQBFields();
    if (forms.includes(sourcesForm)) updateSourceFields();
    configLoaded = true;
  }

  function emptyRow(container, columns, message) {
    const row = el('tr');
    const cell = el('td', message, 'empty');
    cell.colSpan = columns;
    row.append(cell);
    container.append(row);
  }

  function confirm(title, body, act) {
    $('#bg-confirm-title').textContent = title;
    $('#bg-confirm-content').replaceChildren(body);
    const button = $('#bg-confirm-yes');
    button.hidden = !act;
    button.onclick = async () => {
      button.disabled = true;
      try {
        await act();
        $('#bg-confirm').close();
      } catch (error) {
        toast(error.message);
      } finally {
        button.disabled = false;
      }
    };
    $('#bg-confirm').showModal();
  }

  async function taskAction(action, id, data = {}) {
    const result = await api(apiRoot + '/task/' + action, {id, ...data});
    toast(result.message);
    await refresh();
  }

  function button(text, action) {
    const result = el('button', text);
    result.type = 'button';
    result.onclick = async () => {
      result.disabled = true;
      try { await action(); } catch (error) { toast(error.message); }
      finally { result.disabled = false; }
    };
    return result;
  }

  function renderTasks() {
    if (!current) return;
    const target = $('#bg-task-rows');
    const candidates = $('#bg-candidate-rows');
    const rows = current.tasks.filter(task => $('#bg-show-history').checked || activePhases.has(task.phase));
    target.replaceChildren();
    for (const task of rows) {
      const name = el('td', task.name);
      name.append(el('small', '#' + task.id + (task.loop ? ' · 循环' : '') + (task.cycles ? ` · 完成 ${task.cycles} 轮` : '')));
      const size = el('td', task.size ? formatBytes(task.size) : '—');
      if (task.progress != null) size.append(el('small', ratio(task.progress)));
      const speed = el('td', `${rate(task.dlspeed != null ? task.dlspeed * 8 / 1e6 : null)} / ${rate(task.upspeed != null ? task.upspeed * 8 / 1e6 : null)}`);
      const status = el('td', current.monitor.qb_online === false && ['adding', 'active', 'deleting'].includes(task.phase)
        ? 'qB 离线，状态未知' : phases[task.phase]);
      status.append(el('small', task.note));
      const ops = el('td');
      const actions = el('div', undefined, 'bg-inline-actions');
      if (activePhases.has(task.phase) && !['deleting', 'moving'].includes(task.phase)) {
        actions.append(button(task.hold_user ? '恢复' : '暂停', () => taskAction(task.hold_user ? 'resume' : 'pause', task.id)),
          button(task.loop ? '取消循环' : '设为循环', () => taskAction('loop', task.id, {enabled: !task.loop})),
          button('移除', () => taskAction('remove', task.id)));
        if (task.phase === 'active') {
          const retain = el('input');
          retain.type = 'checkbox';
          retain.checked = Boolean(task.retain);
          retain.onchange = async () => {
            try { await taskAction('retain', task.id, {enabled: retain.checked}); }
            catch (error) { retain.checked = !retain.checked; toast(error.message); }
          };
          const label = el('label', undefined, 'bg-check bg-retain');
          label.append(retain, document.createTextNode('保留并转移'));
          actions.append(label, button('连文件删除', () => confirm('删除任务和文件',
            el('p', `确定删除「${task.name}」的任务与下载内容？`), () => taskAction('delete', task.id))));
        }
      } else if (['skipped', 'removed', 'finished', 'slow', 'expired'].includes(task.phase)) {
        actions.append(button('重新排队', () => taskAction('add', task.id)));
      }
      ops.append(actions);
      const tr = el('tr');
      tr.append(name, size, speed, status, ops);
      target.append(tr);
    }
    if (!rows.length) emptyRow(target, 5, '没有托管任务');
    candidates.replaceChildren();
    for (const item of current.candidates) {
      const tr = el('tr');
      const name = el('td', item.name);
      name.append(el('small', `#${item.id} · ${item.scope === 'adult' ? '成人区' : '综合区'}`));
      tr.append(name, el('td', formatBytes(item.size)), el('td', String(item.seeders)),
        el('td', item.free_end ? timeLabel(item.free_end) : '未标到期时间'), el('td', item.reason || '符合条件'));
      candidates.append(tr);
    }
    if (!current.candidates.length) emptyRow(candidates, 5, '尚无扫描结果');
  }

  function renderStatus(status) {
    current = status;
    const monitor = status.monitor;
    const siteReady = Boolean(configuredSites(statusData?.config || {}).some(site => status.config[site.traffic_field]));
    $('#bg-run').disabled = status.config.enabled || !siteReady;
    $('#bg-add button').disabled = !siteReady;
    $('#bg-stop').disabled = !status.config.enabled;
    $('#bg-overview-status').textContent = monitor.reason || '等待同步';
    const priority = monitor.qb_online ? monitor.upload_priority : null;
    const budget = priority ? `托管下载上限 ${rate(monitor.limit_mbps)} · 流量助手上传不限`
      : monitor.limit_mbps != null && `后台额度 ${rate(monitor.limit_mbps)} 下行 / ${rate(monitor.upload_limit_mbps)} 上行`;
    const queue = monitor.qb_online && monitor.qb_download_runnable != null
      ? `托管已放行 ${monitor.qb_download_runnable} · qB 排队 ${monitor.qb_download_queued}` : '';
    $('#bg-router-status').textContent = [budget, queue, monitor.router_note].filter(Boolean).join(' · ');
    $('#bg-upload-status').hidden = !priority;
    if (priority) {
      $('#bg-upload-status').textContent = [
        'qB 总上传 ' + rate(priority.qb_upload_mbps),
        '流量助手上传 ' + rate(priority.hosted_upload_mbps),
        '保护目标 ' + (priority.target_mbps == null ? '待建立' : rate(priority.target_mbps)),
      ].join(' · ');
    }
    $('#summary-qb').textContent = status.config.qb_url ? (monitor.qb_online ? '已连接' : '未连接') : '未配置';
    $('#summary-router').textContent = monitor.router_note || (status.config.ros_host ? '已连接' : '未配置');
    $('#summary-traffic').textContent = !statusData.config.module_traffic ? '未启用'
      : status.config.enabled ? monitor.reason || '运行中' : '已暂停';
    $('#traffic-active').textContent = status.tasks.filter(task => task.phase === 'active').length + ' 项托管中';
    $('#traffic-queued').textContent = status.tasks.filter(task => ['queued', 'ready', 'adding'].includes(task.phase)).length + ' 项等待加入';
    $('#traffic-capacity').textContent = monitor.capacity == null ? '—' : `${formatBytes(monitor.reserved)} / ${formatBytes(monitor.capacity)}`;
    $('#traffic-storage-note').textContent = monitor.storage_ok ? '存储就绪' : monitor.storage_note || '等待存储检查';
    $('#bg-now').textContent = `${rate(monitor.wan_rx)} / ${rate(monitor.wan_tx)}`;
    $('#bg-latency').textContent = monitor.latency != null ? `探测延迟 ${monitor.latency.toFixed(1)} ms` : '暂无延迟样本';
    $('#bg-site-status').textContent = status.site_status || '等待站点请求';
    const quota = monitor.storage_ok
      ? `流量助手存储配额已用 ${formatBytes(monitor.reserved)} / ${formatBytes(monitor.capacity)}`
      : monitor.storage_note || monitor.reason || '等待存储校验';
    const volume = [
      monitor.volume_total != null && `卷已用 ${formatBytes(monitor.volume_used)} / ${formatBytes(monitor.volume_total)}`,
      monitor.volume_free != null && `卷空闲 ${formatBytes(monitor.volume_free)}`,
    ];
    $('#bg-storage-status').textContent = quota;
    $('#bg-overview-storage').textContent = [...volume, quota].filter(Boolean).join(' · ');
    const guard = status.qb_yield || {};
    $('#bg-qb-yield-status').textContent = [
      guard.message,
      guard.rtt != null && `当前延迟 ${guard.rtt.toFixed(1)} ms`,
      guard.baseline != null && `基线 ${guard.baseline.toFixed(1)} ms`,
      guard.down_mbps != null && `qB 实时总下 / 上行 ${rate(guard.down_mbps)} / ${rate(guard.up_mbps)}`,
      guard.down_limit_mbps != null && `qB 下行上限 ${rate(guard.down_limit_mbps)}`,
      guard.up_limit_mbps != null && `qB 上行上限 ${rate(guard.up_limit_mbps)}`,
      guard.pending && 'qB 原限速待还原',
    ].filter(Boolean).join(' · ');
    $('#bg-legacy-qos').hidden = !status.qos_applied;
    const stamp = monitor.last_checked ? checkTime.format(new Date(monitor.last_checked * 1000)) : '';
    $('#bg-hook-status').textContent = !status.config.qb_url ? '请先保存 qB 连接设置'
      : !monitor.qb_online ? monitor.reason || 'qB 未连接'
      : [status.hook_status, stamp && `最近检查 ${stamp}`].filter(Boolean).join(' · ');
    if (status.config.storage === 'ram') {
      $('#bg-ram-path-status').textContent = monitor.storage_ok && monitor.storage_path
        ? `已就绪 · ${monitor.storage_path}`
        : monitor.storage_note || '启动时自动分配目录';
    } else {
      $('#bg-ram-path-status').textContent = '启动时自动分配目录';
    }
    if (!configLoaded) fillConfig(status.config);
    if (currentView === 'bg-tasks') renderTasks();
  }

  const needsBackground = view => view.startsWith('bg-') || ['dashboard', 'site-config', 'global-config'].includes(view);

  async function refresh() {
    if (fetching || !csrf || !needsBackground(currentView)) return;
    fetching = true;
    try {
      renderStatus(await api(apiRoot));
      if (currentView === 'dashboard' && (history == null || (historyOffset === 0 && Date.now() - lastHistoryLoad > 12000))) {
        await loadHistory();
      }
      if (currentView === 'bg-overview' && Date.now() - lastTrafficLoad > 12000) await loadTrafficHistory();
    } catch (error) {
      $('#bg-overview-status').textContent = error.message;
    } finally {
      fetching = false;
    }
  }

  async function loadHistory() {
    const request = ++historyRequest;
    lastHistoryLoad = Date.now();
    const response = await api(`${apiRoot}/stats?period=${period.value}&offset=${historyOffset}`);
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
    $('#bg-coverage').textContent = `PPPoE ${ratio(history.wan_coverage)} · PTFree ${ratio(history.job_coverage)}`;
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
    draw();
  }

  async function loadTrafficHistory() {
    lastTrafficLoad = Date.now();
    const today = await api(`${apiRoot}/stats?period=day`);
    $('#bg-index').textContent = ratio(today.index, 1);
    $('#bg-average').textContent = today.job_average_mbps == null ? '等待下载采样' : `最近24小时 PTFree 平均下载 ${rate(today.job_average_mbps)}`;
    const events = $('#bg-events');
    events.replaceChildren();
    for (const item of today.events) {
      const line = el('div', undefined, 'event');
      line.append(el('time', timeLabel(item.ts)), el('span', '调度', 'info'), el('span', item.message.replace(/^(?:PCDN反制|流量助手) · 调度：/, '')));
      events.append(line);
    }
    if (!today.events.length) events.append(el('p', '最近24小时没有调度记录。', 'empty'));
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
  $('#bg-show-history').onchange = renderTasks;
  $('#bg-close').onclick = () => $('#bg-confirm').close();

  async function save(data, form) {
    const target = siteForms.includes(form) ? '/api/site/traffic' : apiRoot + '/config';
    const result = await api(target, data);
    if (form) fillConfig(result.config, [form]);
    toast(result.message);
    await refresh();
  }
  for (const form of configForms) {
    form.onsubmit = async event => {
      event.preventDefault();
      const data = {};
      for (const field of form.elements) {
        if (!field.name || field.matches(':disabled') || field.type === 'radio' && !field.checked) continue;
        data[field.name] = field.type === 'checkbox' ? field.checked : field.type === 'number' ? Number(field.value) : field.name === 'qb_password' ? field.value : field.value.trim();
      }
      writeUnlimitedFields(form, data);
      if ('ram_create' in data) data.ram_create = data.ram_create === 'true';
      if ('qb_hook' in data) data.qb_hook = data.qb_hook === 'true';
      if (data.storage === 'ram' && data.ram_create && replaceRamPath) data.ram_path = '';
      try { await save(data, form); } catch (error) { toast(error.message); }
    };
  }
  $('#bg-run').onclick = () => save({enabled: true}).catch(error => toast(error.message));
  $('#bg-stop').onclick = () => save({enabled: false}).catch(error => toast(error.message));
  $('#bg-add').onsubmit = async event => {
    event.preventDefault();
    try {
      const form = event.target;
      await taskAction('add', form.elements.id.value.trim(), {loop: form.elements.loop.checked});
      form.reset();
    } catch (error) { toast(error.message); }
  };

  $('#bg-ram-setup').onclick = async event => {
    const button = event.currentTarget;
    if (![schedule.elements.ram_capacity_gib, schedule.elements.ram_reserve_gib]
      .every(field => field.reportValidity())) return;
    const status = $('#bg-ram-path-status');
    button.disabled = true;
    try {
      const data = {
        storage: 'ram', ram_create: true,
        ram_capacity_gib: Number(schedule.elements.ram_capacity_gib.value),
        ram_reserve_gib: Number(schedule.elements.ram_reserve_gib.value),
      };
      if (replaceRamPath) data.ram_path = '';
      await api(apiRoot + '/config', data);
      replaceRamPath = false;
      const result = await api(apiRoot + '/ram/setup', {});
      status.textContent = `${result.message} · ${result.storage_path} · 可用 ${result.free_gib} GiB`;
      await refresh();
    } catch (error) {
      status.textContent = error.message;
    } finally {
      updateFields();
    }
  };

  async function connectionAction(action, output, data = {}) {
    output.hidden = false;
    output.textContent = '等待响应…';
    try {
      const response = await api(apiRoot + '/' + action, data);
      output.textContent = `${response.message}${response.version ? ' · ' + response.version : ''}${response.free_gib == null ? '' : ' · 可用 ' + response.free_gib + ' GiB'}`;
      return response;
    } catch (error) {
      output.textContent = error.message;
      if (action.endsWith('/rollback')) throw error;
      return null;
    }
  }
  const qosResult = $('#bg-qos-result');
  $('#bg-qb-test').onclick = async event => {
    const button = event.currentTarget;
    const data = {};
    for (const field of qbFields.querySelectorAll('input, select')) {
      if (!field.disabled) data[field.name] = field.name === 'qb_password' ? field.value : field.value.trim();
    }
    button.disabled = true;
    try {
      await connectionAction('qb/test', $('#bg-qb-result'), data);
    } finally {
      button.disabled = false;
    }
  };
  $('#bg-qb-check').onclick = async event => {
    const button = event.currentTarget;
    button.disabled = true;
    const result = await connectionAction('qb/check', $('#bg-qb-check-result'));
    if (result) setTimeout(() => { button.disabled = false; }, 2000);
    else button.disabled = false;
  };
  $('#bg-ros-test').onclick = () => connectionAction('router/test', $('#bg-router-result'));
  $('#bg-ros-rollback').onclick = () => confirm('撤销旧版 RouterOS 整形',
    el('p', '将移除这个 PTFree 实例创建的旧队列和包标记。'), async () => {
      await connectionAction('router/rollback', qosResult);
      toast(qosResult.textContent);
      await refresh();
    });
  document.addEventListener('mtfree:view', event => {
    if (!needsBackground(event.detail)) return;
    if (event.detail === 'dashboard') {
      lastHistoryLoad = 0;
      draw();
    }
    if (event.detail === 'bg-overview') lastTrafficLoad = 0;
    refresh();
  });
  setInterval(() => { if (!$('#shell').hidden) refresh(); }, 5000);
})();
