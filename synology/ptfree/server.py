# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import random
import secrets
import signal
import subprocess
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .freeseed_options import FreeseedOptions
from .monitor import Monitor
from .common import AppError, SHUTDOWN_EXIT_CODE, WINDOWS_NO_CONSOLE, atomic_json, safe_error, launch_args
from .settings import MTEAM_FIELDS, Settings
from .store import Store


def terminate(process,tree=False):
    if process is None or (process.poll() is not None and not tree):
        return
    if os.name=='nt' and tree:
        subprocess.run(
            ['taskkill', '/PID', str(process.pid), '/T', '/F'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=WINDOWS_NO_CONSOLE,
        )
    elif tree:
        try:
            os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:
            return
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if tree and os.name!='nt':
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
        else:
            process.kill()
        process.wait(timeout=5)


class Coordinator:
    def __init__(self,home,settings,db):
        self.home=home; self.settings=settings; self.db=db
        self.process=None; self.jid=None; self.started=0; self.budget=0
        self.next_auto=time.time()+5; self.failures=0; self.stopping=False
        with db.connect() as d:
            rows=d.execute("SELECT id FROM jobs WHERE state='running'").fetchall()
            for row in rows:
                d.execute("UPDATE jobs SET state='error',finished=?,result=? WHERE id=?",(time.time(),json.dumps({'message':'服务重启，操作中断；下载状态将在下轮核对'}),row[0]))
        self.enabled_before=None

    def tick(self):
        c=self.settings.get(); n=time.time()
        if self.process:
            running_job=self.db.job(self.jid)
            # Pause and dry-run changes also stop an active download worker.
            if self.enabled_before and not c['enabled'] and running_job['kind']=='scan':
                terminate(self.process)
                self.db.finish_job(self.jid,{'message':'服务已暂停，当前扫描已终止'},'cancelled')
            elif running_job['kind']=='scan' and c['dry_run'] and not self.dry_before:
                terminate(self.process)
                self.db.finish_job(self.jid,{'message':'已切换预览模式，当前真实扫描已终止'},'cancelled')
            elif n-self.started>self.budget:
                terminate(self.process)
                self.db.finish_job(self.jid,{'message':'操作超过时间上限，已终止卡住的进程；稍后自动恢复'},'error')
                self.db.event('操作进程超时，已强制终止并保留重试记录','error')
            if self.process.poll() is not None:
                job=self.db.job(self.jid)
                if job['state']=='running':
                    self.db.finish_job(self.jid,{'message':'操作进程意外退出，稍后将重试'},'error')
                    job=self.db.job(self.jid)
                if job['kind']=='scan':
                    paused_until = job['result'].get('paused_until',0)
                    if paused_until:
                        self.failures = 0
                        self.next_auto = max(n,paused_until)
                    else:
                        self.failures=self.failures+1 if job['state']=='error' else 0
                        delay=max(c['interval'],min(3600,30*2**min(self.failures,7)))
                        self.next_auto=n+delay+random.uniform(0,c['jitter'])
                self.process=None; self.jid=None
        if self.enabled_before is False and c['enabled']:
            self.next_auto=min(self.next_auto,n+1)
        self.enabled_before=c['enabled']; self.dry_before=c['dry_run']
        if not self.process:
            with self.db.connect() as d:
                row=d.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if row is None and c['enabled'] and n>=self.next_auto and n>=self.db.get('blocked_until',0) and not self.db.get('auth_error',False):
                self.db.queue_job('scan')
                with self.db.connect() as d:
                    row=d.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if row:
                self.jid=row['id']; self.started=n
                # Wall-clock bounds include DNS, slow response bodies and SMB filesystem hangs.
                self.budget=60 if row['kind'].startswith('test-') else 900
                with self.db.connect() as d:
                    d.execute("UPDATE jobs SET state='running',started=? WHERE id=?",(n,self.jid))
                self.process = subprocess.Popen(
                    launch_args('--data', self.home, 'worker', self.jid),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=WINDOWS_NO_CONSOLE,
                )
            with self.db.connect() as d:
                d.execute("DELETE FROM jobs WHERE state NOT IN ('running','queued') AND id NOT IN (SELECT id FROM jobs ORDER BY created DESC LIMIT 200)")
        self.db.put('next_scan',self.next_auto)

    def close(self):
        terminate(self.process)


class WebServer(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True


def serve(home,host,port):
    settings=Settings(home); db=Store(home); db.init()
    if not settings.get()['local_request_limits']:
        db.release_local_wait()
    coordinator=Coordinator(home,settings,db)
    options=FreeseedOptions(db,settings)
    monitor=Monitor(settings,options,db)
    static=Path(__file__).parent/'static'
    def save_freeseed_settings(value):
        config = settings.save(value, merge=True)
        if not config['local_request_limits'] and db.release_local_wait():
            coordinator.next_auto = min(coordinator.next_auto, time.time())
        if value.get('api_key'):
            db.put('auth_error', False)

    module_lock = threading.RLock()
    stop=threading.Event()
    shutdown_requested = threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig,lambda *args:stop.set())

    class Handler(BaseHTTPRequestHandler):
        server_version='PTFree'
        def log_message(self,*args):
            pass

        def setup(self):
            super().setup(); self.connection.settimeout(10)

        def send(self,status,value,content_type='application/json; charset=utf-8',extra=None):
            body=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode()
            self.send_response(status)
            for k,v in {'Content-Type':content_type,'Content-Length':str(len(body)),
                        'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
                        'X-Frame-Options':'DENY','Referrer-Policy':'no-referrer',
                        'Content-Security-Policy':"default-src 'self'; img-src 'self' https://static.m-team.cc/favicon.ico; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",**(extra or {})}.items():
                self.send_header(k,v)
            self.end_headers(); self.wfile.write(body)

        def origin_ok(self):
            host_header=self.headers.get('Host','')
            try:
                p=urlsplit('http://'+host_header)
                if p.hostname!='localhost':
                    ipaddress.ip_address(p.hostname)
                if p.port not in (None,port):
                    return False
            except (ValueError,TypeError):
                return False
            origin=self.headers.get('Origin')
            return not origin or origin=='http://'+host_header

        def session(self):
            try:
                cookie=SimpleCookie(); cookie.load(self.headers.get('Cookie',''))
                token=cookie['mtfree_session'].value
                payload,sig=token.rsplit('.',1)
                expected=hmac.new(settings.session_key(),payload.encode(),hashlib.sha256).hexdigest()
                if not hmac.compare_digest(sig,expected) or not 0<=time.time()-float(payload.split(':')[0])<=43200:
                    return None
                return token
            except (ValueError,KeyError,TypeError):
                return None

        def csrf(self,session):
            return hmac.new(settings.session_key(),('csrf:'+session).encode(),hashlib.sha256).hexdigest()

        def json_body(self):
            if not self.headers.get('Content-Type','').startswith('application/json'):
                raise AppError('请求应为 JSON 格式','config')
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=65536:
                raise AppError('请求内容过大或为空','config')
            value=json.loads(self.rfile.read(size))
            if not isinstance(value,dict):
                raise AppError('请求应为 JSON 对象','config')
            return value

        def do_GET(self):
            try:
                if not self.origin_ok():
                    return self.send(403,{'message':'请求来源不正确，请使用 IP 地址访问'})
                p=urlsplit(self.path)
                if p.path=='/healthz':
                    return self.send(200,{'alive':True})
                assets={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/statistics.js':('statistics.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
                assets.update({
                    '/ptfree.png': ('ptfree.png', 'image/png'),
                    '/license': ('legal/LICENSE', 'text/plain; charset=utf-8'),
                    '/notice': ('legal/NOTICE', 'text/plain; charset=utf-8'),
                })
                if p.path in assets:
                    name,mime=assets[p.path]; return self.send(200,(static/name).read_bytes(),mime)
                session=self.session()
                metric_auth=p.path=='/metrics' and hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+(home/'metrics.token').read_text())
                if not session and not metric_auth:
                    return self.send(401,{'message':'请先登录'})
                if p.path=='/api/status':
                    c=settings.public()
                    return self.send(200,{'version':__version__,'csrf':self.csrf(session),'config':c,
                        'stats':db.stats(),'next_scan':db.get('next_scan'),
                        'last_search_ok':db.get('last_search_ok'),'last_scan_ok':db.get('last_scan_ok'),
                        'last_job_error':db.get('last_job_error',''),'auth_error':db.get('auth_error',False),
                        'blocked_until':db.get('blocked_until',0),'cooldown_reason':db.get('cooldown_reason'),
                        'running_job':db.job(coordinator.jid) if coordinator.jid else None,
                        'supervisor_restarts':db.get('supervisor_restarts',0)})
                if p.path == '/api/options':
                    return self.send(200, options.config(public=True))
                if p.path == '/api/monitor':
                    return self.send(200, monitor.status)
                if p.path == '/api/monitor/stats':
                    query = parse_qs(p.query)
                    period = query.get('period', ['day'])[0]
                    offset = int(query.get('offset', ['0'])[0])
                    if period not in ('day', 'week', 'month', 'year', 'all') or not 0 <= offset <= 30:
                        raise AppError('统计时间范围无效', 'config')
                    return self.send(200, monitor.history.history(period, offset))
                if p.path=='/api/history':
                    q=parse_qs(p.query); page=max(1,min(100000,int(q.get('page',['1'])[0])))
                    return self.send(200,db.history(page,q.get('state',[''])[0]))
                if p.path.startswith('/api/jobs/'):
                    job=db.job(p.path.rsplit('/',1)[1])
                    return self.send(200 if job else 404,job or {'message':'操作不存在'})
                if p.path=='/metrics':
                    stats=db.stats(); lines=['# TYPE mtfree_up gauge','mtfree_up 1',f'mtfree_enabled {int(settings.get()["enabled"])}',
                         f'mtfree_last_search_success_timestamp {db.get("last_search_ok",0)}',
                         f'mtfree_supervisor_restarts_total {db.get("supervisor_restarts",0)}',
                         f'mtfree_auth_error {int(db.get("auth_error",False))}']
                    lines.extend('mtfree_torrents{state="'+k+'"} '+str(v) for k,v in stats['counts'].items())
                    return self.send(200,('\n'.join(lines)+'\n').encode(),'text/plain; version=0.0.4')
                self.send(404,{'message':'页面不存在'})
            except (BrokenPipeError,ConnectionResetError):
                pass
            except Exception as e:
                self.send(400,{'message':safe_error(e)})

        def do_POST(self):
            try:
                if not self.origin_ok():
                    return self.send(403,{'message':'请求来源不正确'})
                path=urlsplit(self.path).path
                value=self.json_body()
                if path=='/api/login':
                    # Persisted global throttle also limits password-hash CPU usage.
                    with db.connect() as d:
                        d.execute('BEGIN IMMEDIATE')
                        r=d.execute("SELECT value FROM kv WHERE key='login_times'").fetchone()
                        times=[t for t in (json.loads(r[0]) if r else []) if time.time()-t<60]
                        if len(times)>=5:
                            return self.send(429,{'message':'登录尝试过多，请等待一分钟'})
                        times.append(time.time())
                        d.execute("INSERT INTO kv VALUES('login_times',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(times),))
                    password=value.get('password','')
                    if not isinstance(password,str) or len(password)>200 or not settings.check_password(password):
                        return self.send(401,{'message':'管理密码不正确'})
                    payload=str(int(time.time()))+':'+secrets.token_hex(16)
                    token=payload+'.'+hmac.new(settings.session_key(),payload.encode(),hashlib.sha256).hexdigest()
                    return self.send(200,{'message':'登录成功','csrf':self.csrf(token)},extra={'Set-Cookie':f'mtfree_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200'})
                session=self.session()
                if not session:
                    return self.send(401,{'message':'请先登录'})
                if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),self.csrf(session)):
                    return self.send(403,{'message':'会话校验失败，请刷新后重新操作'})
                if stop.is_set():
                    return self.send(503, {'message': '服务正在停止'})
                if path == '/api/shutdown':
                    shutdown_requested.set()
                    try:
                        self.send(200, {'message': '服务正在退出。重新启动 PTFree 后刷新页面。'})
                    finally:
                        stop.set()
                    return
                if path=='/api/logout':
                    return self.send(200,{'message':'已退出'},extra={'Set-Cookie':'mtfree_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
                if path == '/api/options':
                    return self.send(200, {'message': '配置已保存', 'config': options.save(value)})
                if path == '/api/options/qb/test':
                    return self.send(200, options.qb_test(value))
                if path == '/api/options/router/test':
                    return self.send(200, options.router_test(value))
                if path == '/api/modules':
                    allowed = {'module_freeseed'}
                    if not value or set(value) - allowed or any(type(flag) is not bool for flag in value.values()):
                        raise AppError('模块配置不正确', 'config')
                    with module_lock:
                        if value.get('module_freeseed') is False:
                            settings.save({**value, 'enabled': False}, merge=True)
                            with db.connect() as d:
                                d.execute("UPDATE jobs SET state='cancelled',finished=?,result=? WHERE state='queued' AND kind IN ('scan','preview')",
                                          (time.time(), json.dumps({'message': 'Freeseed 模块已停用'})))
                        else:
                            settings.save(value, merge=True)
                    return self.send(200, {'message': '模块设置已保存', 'config': settings.public()})
                if path == '/api/site':
                    if set(value) - MTEAM_FIELDS or not value:
                        raise AppError('站点配置不正确', 'config')
                    with module_lock:
                        config = settings.get()
                        if not config['site_added'] and not (isinstance(value.get('api_key'), str) and value['api_key'].strip()):
                            raise AppError('添加 M-Team 时请填写 API Access Token', 'config')
                        if coordinator.process:
                            raise AppError('请等待 Freeseed 当前操作结束，再保存站点配置', 'busy')
                        save_freeseed_settings({**value, 'site_added': True})
                    return self.send(200, {'message': 'M-Team 设置已保存', 'config': settings.public()})
                if path=='/api/config':
                    if {'module_freeseed', 'module_traffic', 'site_added', 'enabled'} & value.keys():
                        raise AppError('请使用相应的模块、站点或运行控制入口', 'config')
                    with module_lock:
                        if coordinator.process:
                            raise AppError('请等待当前操作结束，或先点击暂停服务，再保存配置', 'busy')
                        if value.get('freeseed_mteam') is False:
                            value = {**value, 'enabled': False}
                        save_freeseed_settings(value)
                    db.event('配置已保存')
                    return self.send(200,{'message':'配置已保存','config':settings.public()})
                if path=='/api/control':
                    if set(value)!={'enabled'} or not isinstance(value['enabled'],bool):
                        raise AppError('控制参数不正确','config')
                    with module_lock:
                        if value['enabled'] and not settings.get()['module_freeseed']:
                            raise AppError('请先在全局配置启用 Freeseed抢上传', 'config')
                        settings.save({'enabled': value['enabled']}, merge=True)
                    if not value['enabled']:
                        with db.connect() as d:
                            d.execute("UPDATE jobs SET state='cancelled',finished=?,result=? WHERE state='queued' AND kind='scan'",(time.time(),json.dumps({'message':'已暂停'})))
                    return self.send(200,{'message':'已启动' if value['enabled'] else '已暂停'})
                if path=='/api/password':
                    if not settings.check_password(str(value.get('old',''))[:200]):
                        raise AppError('当前密码不正确','auth')
                    settings.set_password(str(value.get('new','')))
                    return self.send(200,{'message':'管理密码已修改，请重新登录'},extra={'Set-Cookie':'mtfree_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict'})
                if path=='/api/retry':
                    if coordinator.process:
                        raise AppError('请等待当前操作结束后重试','busy')
                    tid=str(value.get('id',''))
                    with db.connect() as d:
                        d.execute("UPDATE torrents SET state=CASE WHEN temp<>'' THEN 'publishing' ELSE 'pending' END,attempts=0,retry_at=0 WHERE id=? AND state IN ('failed','exhausted','publishing')",(tid,))
                    return self.send(200,{'message':'已加入下次扫描的重试队列'})
                if path=='/api/actions':
                    kind=value.get('kind')
                    if kind not in ('preview','scan','test-site','test-dir'):
                        raise AppError('操作类型不正确','config')
                    config = settings.get()
                    if kind in ('scan', 'preview'):
                        if not config['module_freeseed']:
                            raise AppError('请先在全局配置启用 Freeseed抢上传', 'config')
                        if not config['freeseed_mteam']:
                            raise AppError('请先在 Freeseed 服务配置中勾选 M-Team', 'config')
                    if kind=='scan' and not config['enabled']:
                        raise AppError('请先启动服务；需要只读检查时使用预览','config')
                    if kind in ('scan','preview','test-site') and db.get('blocked_until',0)>time.time():
                        raise AppError('站点冷却期内不能手动请求，请等待冷却结束','rate')
                    return self.send(202,{'id':db.queue_job(kind),'message':'操作已排队'})
                self.send(404,{'message':'接口不存在'})
            except (BrokenPipeError,ConnectionResetError):
                pass
            except Exception as e:
                self.send(400,{'message':safe_error(e)})

    httpd=WebServer((host,port),Handler); httpd.timeout=0.5
    db.event('网页服务已启动')
    monitor.start()
    last=0
    try:
        while not stop.is_set():
            httpd.handle_request()
            if stop.is_set():
                break
            coordinator.tick()
            if time.monotonic()-last>2:
                atomic_json(home/'heartbeat.json',{'time':time.time(),'pid':os.getpid()}); last=time.monotonic()
    finally:
        monitor.close()
        try:
            coordinator.close()
        finally:
            httpd.server_close()
    if shutdown_requested.is_set():
        if coordinator.jid:
            job = db.job(coordinator.jid)
            if job and job['state'] == 'running':
                db.finish_job(coordinator.jid, {'message': '服务已停止，操作中断'}, 'cancelled')
        db.event('管理员从网页停止了服务')
        return SHUTDOWN_EXIT_CODE
    return 0
