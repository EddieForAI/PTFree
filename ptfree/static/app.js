/* SPDX-FileCopyrightText: 2026 EddieForAI
 * SPDX-License-Identifier: Apache-2.0 */

'use strict';
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
let csrf='',statusData=null,configLoaded=false,page=1,historyTotal=0,currentView='dashboard',polling=false,shutdownRequested=false;
const states={seen:'已发现',pending:'等待处理',waiting_quota:'等待配额',skipped:'已筛除',site_existing:'站点已有记录',failed:'等待重试',exhausted:'重试耗尽',done:'已完成',publishing:'待恢复保存'};
const date=t=>t?new Date(t*1000).toLocaleString('zh-CN',{hour12:false}):'尚无记录';
function formatBytes(bytes) {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return '—';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  if (unit && unit < units.length - 1 && Number(value.toFixed(2)) >= 1024) {
    value /= 1024;
    unit++;
  }
  return value.toFixed(unit === 0 ? 0 : 2) + ' ' + units[unit];
}
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function toast(text){$('#toast').textContent=text;$('#toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('#toast').hidden=true,6000);}
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-CSRF-Token':csrf}:{},body:body?JSON.stringify(body):undefined});const data=await r.json();if(r.status===401&&path!='/api/login'){showLogin();throw Error('会话已过期，请重新登录');}if(!r.ok)throw Error(data.message||'操作失败');return data;}
function showLogin(){$('#shell').hidden=true;$('#login').hidden=false;csrf='';configLoaded=false;siteAdding=false;landingPending=true;}
function showView(view) {
  const titles = {
    dashboard: '总运行概览',
    overview: 'Freeseed抢上传 · 运行概览',
    settings: '服务配置',
    history: '下载历史',
    logs: '运行日志',
    'site-config': 'PT配置',
    'global-config': '全局配置',
    help: '使用帮助',
    'bg-overview': '流量助手 · 运行概览',
    'bg-tasks': '种子管理',
    'bg-schedule': '调度与容量',
  };
  currentView = view;
  $$('.view').forEach(panel => panel.hidden = panel.id !== view);
  $$('.nav').forEach(button => {
    const active = button.dataset.view === view && (!button.dataset.siteId || button.dataset.siteId === activeSiteId);
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  const notice = $('#notice');
  notice.hidden = view !== 'overview' || !notice.textContent;
  $('#page-title').textContent = titles[view];
  if (view === 'history') loadHistory().catch(error => toast(error.message));
  document.dispatchEvent(new CustomEvent('mtfree:view', {detail: view}));
  window.scrollTo({top: 0});
}
$$('[data-view]').forEach(b=>b.onclick=()=>showView(b.dataset.view));$('.jump-logs').onclick=()=>showView('logs');
const freeseedForm = $('#config-form');
const siteForm = $('#global-site-form');
const modulesForm = $('#modules-form');
const sitePicker = $('#site-picker');
let siteAdding = false;
let landingPending = true;
let activeSiteId = '';
let catalogSignature = '';

function configuredSites(config) {
  return (statusData?.sites || []).filter(site => Boolean(config[site.configured_field]));
}
function siteIcon(site, large = false) {
  const icon = el('span', site.name.slice(0, 1), 'site-registry-icon' + (large ? ' site-icon-24' : ''));
  icon.setAttribute('aria-hidden', 'true');
  const image = el('img');
  image.alt = '';
  image.referrerPolicy = 'no-referrer';
  image.src = site.favicon;
  image.onerror = () => image.remove();
  icon.append(image);
  return icon;
}
function renderSiteCatalog(config) {
  const sites = statusData?.sites || [];
  const added = configuredSites(config);
  const signature = JSON.stringify(sites.map(site => [site.id, site.name, site.favicon, site.freeseed_field,
    site.traffic_field, Boolean(config[site.configured_field])]));
  if (signature !== catalogSignature) {
    catalogSignature = signature;
    const nav = $('#site-nav-list'), picker = $('#site-picker-list');
    nav.replaceChildren();
    picker.replaceChildren();
    if (!added.length) {
      const button = el('button', '站点管理', 'nav');
      button.type = 'button'; button.dataset.view = 'site-config';
      button.onclick = () => { activeSiteId = ''; showView('site-config'); updateNavigation(statusData.config); };
      nav.append(button);
    }
    for (const site of sites) {
      const isAdded = Boolean(config[site.configured_field]);
      const choice = el('button', undefined, 'site-choice');
      choice.type = 'button'; choice.disabled = isAdded;
      choice.append(siteIcon(site, true), document.createTextNode(site.name));
      if (isAdded) choice.append(el('small', '已添加'));
      choice.onclick = () => {
        sitePicker.close(); activeSiteId = site.id; siteAdding = true;
        const form = document.querySelector(`form[data-site-panel="${site.id}"]`);
        if (form === siteForm) fillConfig(statusData.config, [siteForm]);
        showView('site-config'); updateNavigation(statusData.config);
        form?.querySelector('input:not([type=checkbox])')?.focus();
      };
      picker.append(choice);
      if (!isAdded) continue;
      const button = el('button', undefined, 'nav');
      button.type = 'button'; button.dataset.view = 'site-config'; button.dataset.siteId = site.id;
      button.append(siteIcon(site), el('span', site.name));
      button.onclick = () => { activeSiteId = site.id; siteAdding = false; showView('site-config'); updateNavigation(statusData.config); };
      nav.append(button);
    }
    // Rebuild only when the catalog changes, keeping unsaved checkbox edits
    // during normal status polling. Field names preserve the existing API.
    for (const [selector, field, selected] of [['#freeseed-site-options', 'freeseed_field', 'freeseed_selected'],
      ['#traffic-site-options', 'traffic_field', 'traffic_selected']]) {
      const target = $(selector);
      const previous = Object.fromEntries([...target.querySelectorAll('input')].map(input => [input.name, input.checked]));
      target.replaceChildren();
      for (const site of added) {
        if (!site[field]) continue;
        const label = el('label', undefined, 'bg-check'), input = el('input');
        input.type = 'checkbox'; input.name = site[field];
        input.checked = Object.hasOwn(previous, input.name) ? previous[input.name] :
          Boolean(Object.hasOwn(config, input.name) ? config[input.name] : site[selected]);
        label.append(input, siteIcon(site), document.createTextNode(site.name)); target.append(label);
      }
    }
  }
  if (!siteAdding && !added.some(site => site.id === activeSiteId)) activeSiteId = added[0]?.id || '';
  for (const button of $$('#site-nav-list .nav')) {
    const active = currentView === 'site-config' && (!button.dataset.siteId || button.dataset.siteId === activeSiteId);
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  }
  for (const empty of $$('[data-site-empty]')) empty.hidden = Boolean(empty.previousElementSibling.children.length);
  $('#site-empty').hidden = Boolean(added.length || siteAdding);
  for (const panel of $$('[data-site-panel]')) {
    panel.hidden = panel.dataset.sitePanel !== activeSiteId ||
      (!added.some(site => site.id === activeSiteId) && !(siteAdding && panel === siteForm));
  }
}
function renderSiteHealth(status) {
  const target = $('#site-health-list'), sites = configuredSites(status.config);
  target.replaceChildren();
  if (!sites.length) {
    const row = el('div', undefined, 'check-row');
    row.append(el('b', 'PT 站点 API'), el('span', '未配置')); target.append(row);
  }
  const labels = {ok:'最近请求成功', auth:'认证异常', rate:'请求暂缓', error:'最近请求失败',
    untested:'令牌已保存 · 尚未验证', unconfigured:'未配置'};
  for (const site of sites) {
    const row = el('div', undefined, 'check-row');
    const label = el('b', site.name + ' API');
    const value = el('span', labels[site.health] || '尚未检查');
    if (site.health_ts) value.title = '最近检查：' + date(site.health_ts);
    row.append(label, value); target.append(row);
  }
}

function updateNavigation(config) {
  const sectionFlags = {
    '#freeseed-nav': config.module_freeseed,
    '#traffic-nav': config.module_traffic,
    '#site-nav': config.module_freeseed || config.module_traffic,
  };
  for (const [selector, visible] of Object.entries(sectionFlags)) {
    const group = $(selector);
    if (group) group.hidden = !visible;
  }
  renderSiteCatalog(config);
  const mteam = configuredSites(config).some(site => site.id === 'mteam');
  $('#site-test').hidden = !mteam;
  $('#site-cancel').hidden = !siteAdding;
  siteForm.elements.api_key.required = !mteam;
  const currentNav = $(`.nav[data-view="${currentView}"]`);
  if (currentNav?.closest('[hidden]')) showView('dashboard');
}

$('#site-add').onclick = () => sitePicker.showModal();
$('#site-nav-add').onclick = () => {
  showView('site-config');
  sitePicker.showModal();
};
$('#site-picker-close').onclick = () => sitePicker.close();
$('#site-cancel').onclick = () => {
  siteAdding = false;
  fillConfig(statusData.config, [siteForm]);
  updateNavigation(statusData.config);
  $('#site-add').focus();
};

modulesForm.onsubmit = async event => {
  event.preventDefault();
  const data = {};
  for (const field of modulesForm.querySelectorAll('input')) data[field.name] = field.checked;
  const submit = modulesForm.querySelector('button');
  submit.disabled = true;
  try {
    const result = await api('/api/modules', data);
    statusData.config = result.config;
    updateNavigation(result.config);
    toast(result.message);
    await refresh();
  } catch (error) {
    toast(error.message);
  } finally {
    submit.disabled = false;
  }
};
function addUnlimitedToggle(field) {
  const input = field.querySelector('input');
  const title = field.querySelector('label');
  const head = el('div', undefined, 'limit-heading');
  const toggle = el('input');
  toggle.type = 'checkbox';
  toggle.dataset.unlimitedFor = input.name;
  toggle.setAttribute('aria-controls', input.id);
  toggle.setAttribute('aria-label', title.textContent + '不限');
  const label = el('label', undefined, 'bg-check');
  label.append(toggle, document.createTextNode('不限'));
  field.prepend(head);
  head.append(title, label);

  const validate = () => {
    input.setCustomValidity(input.valueAsNumber === 0 ? '请输入大于 0 的数值，或勾选「不限」。' : '');
  };
  input.addEventListener('input', validate);
  toggle.addEventListener('change', () => {
    if (toggle.checked) toggle.dataset.previousValue = input.value;
    input.value = toggle.checked ? '' : (toggle.dataset.previousValue || '');
    input.disabled = toggle.checked;
    validate();
  });
}

function fillUnlimitedFields(form, config) {
  for (const toggle of form.querySelectorAll('[data-unlimited-for]')) {
    const input = form.elements.namedItem(toggle.dataset.unlimitedFor);
    toggle.checked = config[input.name] === 0;
    input.disabled = toggle.checked;
    if (toggle.checked) input.value = '';
    delete toggle.dataset.previousValue;
    input.setCustomValidity('');
  }
}

function writeUnlimitedFields(form, data) {
  for (const toggle of form.querySelectorAll('[data-unlimited-for]:checked:not(:disabled)')) {
    data[toggle.dataset.unlimitedFor] = 0;
  }
}

freeseedForm.querySelectorAll('.limit-field').forEach(addUnlimitedToggle);
$('#help-topics').addEventListener('change', event => {
  for (const panel of $$('#help [data-help-topic]')) {
    panel.hidden = panel.dataset.helpTopic !== event.target.value;
  }
});

function fillConfig(config, forms = [freeseedForm, siteForm, modulesForm]) {
  for (const form of forms) {
    for (const field of form.elements) {
      if (!field.name) continue;
      if (field.name === 'api_key') {
        field.value = '';
        field.placeholder = config.api_key_set ? '已保存，留空保留原值' : '尚未填写';
      } else if (field.type === 'checkbox') field.checked = config[field.name];
      else field.value = config[field.name];
    }
    fillUnlimitedFields(form, config);
  }
  configLoaded = true;
}
function renderEvents(target,rows){target.replaceChildren();if(!rows.length){target.append(el('p','还没有运行事件。','empty'));return;}for(const row of rows){const div=el('div',undefined,'event');div.append(el('time',date(row.ts)),el('span',{error:'错误',warning:'提醒',info:'正常'}[row.level]||row.level,row.level),el('span',row.message));target.append(div);}}
function updateNotice(s) {
  let message = s.last_job_error;
  const seconds = Math.ceil(s.blocked_until - Date.now()/1000);
  if (seconds > 0) {
    const {source,stage} = s.cooldown_reason || {};
    const label = {quota:'本地请求配额',http_429:'站点返回 HTTP 429',site_rate:'站点接口限流',repeated_error:'连续接口错误'}[source] || '请求冷却';
    const step = {search:'列表查询',detail:'查详情',history:'查站点历史',token:'取下载令牌',download:'下载种子文件',profile:'认证测试'}[stage];
    const parts = [Math.floor(seconds/3600),Math.floor(seconds/60)%60,seconds%60];
    const remaining = parts.map((value,index)=>String(value).padStart(2,'0')+[' 小时',' 分',' 秒'][index]).join(' ');
    const resume = date(Math.max(s.blocked_until,s.next_scan||0));
    const status = s.auth_error ? '需先恢复 API 认证。' : !s.config.enabled ? '服务已暂停。' :
      (source==='repeated_error' ? '预计 '+resume+' 先试一条。' : '预计 '+resume+' 恢复。');
    message = label+(step?' · '+step:'')+'。冷却剩余 '+remaining+'。'+status;
  } else if (s.auth_error) {
    message = 'M-Team 认证异常，自动请求已暂停。请在配置中更新令牌，或点击测试认证。';
  } else if (s.config.dry_run) {
    message = '当前为仅预览模式。确认筛选结果后，取消服务配置中的「仅预览」才能真实下载。';
  }
  $('#notice').hidden = currentView !== 'overview' || !message;
  $('#notice').textContent = message || '';
}
function update(s){statusData=s;csrf=s.csrf;updateNavigation(s.config);$('#version').textContent='v'+s.version;const c=s.config,n=Date.now()/1000,blocked=s.blocked_until>n;let state=!c.freeseed_mteam?'未选择站点':!c.enabled?'已暂停':s.auth_error?'认证异常':blocked?'请求暂缓':c.dry_run?'预览运行中':'运行中';$('#service-state').textContent=state;$('#service-detail').textContent=s.auth_error?'到 PT配置更新令牌或测试认证后恢复':blocked?'冷却至 '+date(s.blocked_until):c.dry_run?'仅预览筛选结果':c.network==='ipv4_prefer'?'IPv4 优先 · 失败自动恢复':c.network==='ipv4_only'?'仅 IPv4 · 失败自动恢复':'系统网络顺序';$('#live').textContent='● 已连接';$('#last-check').textContent=s.last_search_ok?new Date(s.last_search_ok*1000).toLocaleTimeString('zh-CN',{hour12:false}):'尚未检查';$('#next-check').textContent=c.enabled?'下次计划 '+date(Math.max(s.next_scan||n,s.blocked_until||0)):'启动后自动检查';const counts=s.stats.counts;$('#pending-count').textContent=((counts.pending||0)+(counts.waiting_quota||0)+(counts.publishing||0))+' / '+((counts.failed||0)+(counts.exhausted||0));$('#restart-count').textContent='看门狗重启 '+s.supervisor_restarts+' 次';renderSiteHealth(s);$('#summary-freeseed').textContent=c.module_freeseed?state:'未启用';$('#summary-freeseed-done').textContent=(counts.done||0).toLocaleString('zh-CN');$('#summary-freeseed-detail').textContent=`等待 ${((counts.pending||0)+(counts.waiting_quota||0)+(counts.publishing||0))} · 失败 ${((counts.failed||0)+(counts.exhausted||0))}`;$('#dir-state').textContent=c.watch_dir||'尚未设置';$('#running').textContent=s.running_job?'正在执行：'+({'scan':'扫描与保存','preview':'只读预览','test-site':'测试认证','test-dir':'测试目录'}[s.running_job.kind]||s.running_job.kind)+' · '+date(s.running_job.started):'当前没有正在执行的操作。';$('#start').disabled=c.enabled||!c.freeseed_mteam||!c.api_key_set;$('#pause').disabled=!c.enabled;for(const button of $$('[data-action=preview], [data-action=scan]'))button.disabled=!c.freeseed_mteam||!c.api_key_set;updateNotice(s);const fsEvents=s.stats.events.filter(row=>!/^(?:PCDN反制|流量助手) · /.test(row.message));renderEvents($('#recent-events'),fsEvents.slice(0,5));renderEvents($('#all-events'),fsEvents);$('#budgets').replaceChildren();for(const [key,label,cap]of [['search','列表查询',900],['detail','详情查询',null],['history','站点历史查询',900],['token','下载令牌',800],['download','种子文件下载',800]]){const max=c.local_request_limits?cap:null;let row=el('div',undefined,'budget-row'),v=s.stats.requests_24h[key]||0;row.append(el('span',label),el('span',v+(max?' / '+max:' 次')));$('#budgets').append(row);if(max){const bar=el('div',undefined,'budget-bar'),fill=el('div',undefined,'budget-fill');fill.style.width=Math.min(100,v/max*100)+'%';bar.append(fill);$('#budgets').append(bar);}}if(!configLoaded)fillConfig(c);}
async function refresh(){if(polling||shutdownRequested)return;polling=true;try{const s=await api('/api/status');if(shutdownRequested)return;$('#login').hidden=true;$('#shell').hidden=false;update(s);if(landingPending){landingPending=false;showView('dashboard');}}catch(e){if(csrf&&!shutdownRequested){$('#live').textContent='● 连接中断';toast(e.message);}}finally{polling=false;}}
$('#login-form').onsubmit=async e=>{e.preventDefault();try{const d=await api('/api/login',{password:$('#password').value});csrf=d.csrf;$('#password').value='';$('#login-error').textContent='';configLoaded=false;await refresh();}catch(err){$('#login-error').textContent=err.message;}};
$('#stop-service').onclick = async () => {
  if (!window.confirm('停止整个 PTFree 服务？网页会断开。重新启动需使用启动脚本、计划任务或 systemd。')) return;
  const button = $('#stop-service');
  button.disabled = true;
  shutdownRequested = true;
  try {
    const result = await api('/api/shutdown', {});
    $('#live').textContent = '● 正在停止';
    $('#notice').hidden = false;
    $('#notice').textContent = result.message;
    $('#refresh').disabled = true;
  } catch (error) {
    shutdownRequested = false;
    button.disabled = false;
    toast(error.message);
  }
};
$('#refresh').onclick=()=>{refresh();document.dispatchEvent(new CustomEvent('mtfree:view',{detail:currentView}));};$('#logout').onclick=async()=>{await api('/api/logout',{});showLogin();};
for (const form of [freeseedForm, siteForm]) {
  form.onsubmit = async event => {
    event.preventDefault();
    const data = {};
    for (const field of form.elements) {
      if (!field.name || field.disabled) continue;
      data[field.name] = field.type === 'checkbox' ? field.checked : field.type === 'number' ? Number(field.value) : field.value;
    }
    writeUnlimitedFields(form, data);
    try {
      const result = await api(form === siteForm ? '/api/site' : '/api/config', data);
      if (statusData) statusData.config = result.config;
      if (form === siteForm) siteAdding = false;
      fillConfig(result.config, [form]);
      updateNavigation(result.config);
      toast(result.message);
      await refresh();
    } catch (error) {
      toast(error.message);
    }
  };
}
$('#reload-config').onclick=()=>{if(statusData)fillConfig(statusData.config,[freeseedForm]);toast('已载入已保存的配置');};
for(const [id,enabled]of [['start',true],['pause',false]])$('#'+id).onclick=async()=>{try{toast((await api('/api/control',{enabled})).message);await refresh();}catch(e){toast(e.message);}};
$('#change-password').onsubmit=async e=>{e.preventDefault();try{const form=e.target;const result=await api('/api/password',{old:form.elements.old.value,new:form.elements.new.value});form.reset();showLogin();toast(result.message);}catch(err){toast(err.message);}};
function showResult(result){const content=$('#result-content');content.replaceChildren();content.append(el('p',result.message||'操作完成','result-summary'));for(const [key,label]of [['username','用户'],['version','版本'],['free_gib','可用空间'],['found','发现'],['matched','符合条件'],['processed','处理'],['site_existing','站点已有记录，已跳过'],['failed','失败']]){if(result[key]!==undefined)content.append(el('p',label+'：'+(key==='free_gib'?formatBytes(result[key]*1024**3):result[key])));}if(result.items){const wrap=el('div',undefined,'table-wrap'),table=el('table'),head=el('tr');for(const h of ['种子','体积','做种','HR','筛选结果'])head.append(el('th',h));table.append(head);for(const t of result.items){const tr=el('tr');tr.append(el('td',t.name,'preview-name'),el('td',formatBytes(t.size)),el('td',String(t.seeders)),el('td',t.hr===null?'未知':t.hr?'有':'无'),el('td',t.reason,t.matched?'info':'muted'));table.append(tr);}wrap.append(table);content.append(wrap);}if(!$('#result-dialog').open)$('#result-dialog').showModal();}
$('#close-dialog').onclick=()=>$('#result-dialog').close();
async function action(button,kind){button.disabled=true;try{const {id}=await api('/api/actions',{kind});toast('操作已排队，完成后显示结果。');let job;do{await new Promise(r=>setTimeout(r,1200));job=await api('/api/jobs/'+id);await refresh();}while(['queued','running'].includes(job.state));showResult(job.result);if(currentView==='history')await loadHistory();}catch(e){toast(e.message);}finally{button.disabled=false;}}
$$('[data-action]').forEach(b=>b.onclick=()=>action(b,b.dataset.action));
function historyTime(t) {
  if (t.state !== 'done') return t.first_seen > 0 ? '首次发现 '+date(t.first_seen) : '发现时间未记录';
  if (!(t.completed > 0)) return '保存时间未记录';
  const label = {
    '目标目录已存在相同种子文件': '确认已存在',
    '已恢复种子文件保存记录': '记录恢复',
  }[t.reason] || '种子保存';
  return label+' '+date(t.completed);
}
async function loadHistory(){const h=await api('/api/history?page='+page+'&state='+encodeURIComponent($('#history-state').value));historyTotal=h.total;const body=$('#history-body');body.replaceChildren();for(const t of h.items){const row=el('tr'),name=el('td');name.append(el('div',t.name),el('small','ID '+t.id+' · '+historyTime(t)));row.append(name,el('td',formatBytes(t.size)),el('td',states[t.state]||t.state),el('td',String(t.attempts)),el('td',t.reason));const op=el('td');if(['failed','exhausted','publishing'].includes(t.state)){const b=el('button','重试');b.onclick=async()=>{try{toast((await api('/api/retry',{id:t.id})).message);await loadHistory();}catch(e){toast(e.message);}};op.append(b);}row.append(op);body.append(row);}if(!h.items.length){const td=el('td','暂无记录','empty');td.colSpan=6;body.append(td);}$('#page-info').textContent='第 '+page+' 页 · 共 '+h.total+' 条';$('#prev-page').disabled=page<=1;$('#next-page').disabled=page*50>=h.total;}
$('#history-state').onchange=()=>{page=1;loadHistory().catch(e=>toast(e.message));};$('#prev-page').onclick=()=>{if(page>1){page--;loadHistory().catch(e=>toast(e.message));}};$('#next-page').onclick=()=>{if(page*50<historyTotal){page++;loadHistory().catch(e=>toast(e.message));}};
refresh();setInterval(()=>{if(!$('#shell').hidden)refresh();},5000);
setInterval(()=>{if(statusData&&!shutdownRequested&&!$('#shell').hidden)updateNotice(statusData);},1000);
