# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from ptfree.settings import Settings
from ptfree.store import Store


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0)); return s.getsockname()[1]


def wait_until(fn,timeout=12):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            result=fn()
            if result:return result
        except (OSError,ValueError,KeyError): pass
        time.sleep(.1)
    raise AssertionError('timed out waiting for condition')


class WebIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(); cls.home=Path(cls.tmp.name); cls.port=free_port()
        cls.settings=Settings(cls.home); cls.settings.set_password('integration-password-123')
        cls.settings.save({'watch_dir':str(cls.home/'seed-dir'),'min_disk_gib':0},merge=True)
        cls.proc=subprocess.Popen([sys.executable,'-m','ptfree','--data',str(cls.home),'--port',str(cls.port),'serve'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        wait_until(lambda:cls.request('GET','/healthz')[0]==200)
        status,data,headers=cls.request('POST','/api/login',{'password':'integration-password-123'})
        assert status==200
        cls.cookie=headers['set-cookie'].split(';')[0]; cls.csrf=data['csrf']
    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate(); cls.proc.wait(timeout=8); cls.tmp.cleanup()
    @classmethod
    def request(cls,method,path,body=None,headers=None,authenticated=False):
        h={'Content-Type':'application/json'}
        if authenticated:h.update(Cookie=cls.cookie,**{'X-CSRF-Token':cls.csrf})
        h.update(headers or {})
        c=http.client.HTTPConnection('127.0.0.1',cls.port,timeout=10)
        c.request(method,path,json.dumps(body).encode() if body is not None else None,h)
        r=c.getresponse(); raw=r.read(); status=r.status; rh=dict((k.lower(),v) for k,v in r.getheaders()); c.close()
        if 'application/json' in rh.get('content-type',''): raw=json.loads(raw)
        return status,raw,rh
    def test_private_api_requires_login(self): self.assertEqual(self.request('GET','/api/status')[0],401)
    def test_admin_session_can_read_masked_config(self):
        status,data,_=self.request('GET','/api/status',authenticated=True)
        self.assertEqual(status,200); self.assertNotIn('api_key',data['config']); self.assertIn('csrf',data)
    def test_csrf_and_origin_rejected(self):
        self.assertEqual(self.request('POST','/api/control',{'enabled':False},{'Cookie':self.cookie})[0],403)
        self.assertEqual(self.request('POST','/api/control',{'enabled':False},{'Origin':'https://evil.invalid'},True)[0],403)
    def test_dns_rebinding_host_rejected(self):
        self.assertEqual(self.request('GET','/healthz',headers={'Host':'evil.invalid'})[0],403)
    def test_atomic_config_validation(self):
        before=self.settings.get()['interval']
        result=self.request('POST','/api/config',{'interval':1},authenticated=True)
        self.assertEqual(result[0],400); self.assertEqual(self.settings.get()['interval'],before)
    def test_directory_job_runs_outside_web_process(self):
        status,data,_=self.request('POST','/api/actions',{'kind':'test-dir'},authenticated=True)
        self.assertEqual(status,202)
        def done():
            _,job,_=self.request('GET','/api/jobs/'+data['id'],authenticated=True)
            return job if job['state']not in ('queued','running') else None
        job=wait_until(done)
        self.assertEqual(job['state'],'done'); self.assertIn('free_gib',job['result'])
    def test_metrics_require_auth(self):
        self.assertEqual(self.request('GET','/metrics')[0],401)
        status,data,_=self.request('GET','/metrics',authenticated=True)
        self.assertEqual(status,200); self.assertIn(b'mtfree_up 1',data)
    def test_metrics_bearer_is_readonly(self):
        token=(self.home/'metrics.token').read_text()
        self.assertEqual(self.request('GET','/metrics',headers={'Authorization':'Bearer '+token})[0],200)
        self.assertEqual(self.request('GET','/api/status',headers={'Authorization':'Bearer '+token})[0],401)
    def test_static_assets_and_security_headers(self):
        status,data,headers=self.request('GET','/')
        self.assertEqual(status,200); self.assertIn('default-src',headers['content-security-policy']); self.assertIn(b'PTFree',data); self.assertIn(b'/ptfree.png',data)
        self.assertEqual(self.request('GET','/../../settings.json')[0],401)

    def test_license_and_attribution_match_distributed_text(self):
        root = Path(__file__).resolve().parents[1]
        _, page, _ = self.request('GET', '/')
        self.assertIn(b'2026 EddieForAI', page)
        for route, filename in (('/license', 'LICENSE'), ('/notice', 'NOTICE')):
            with self.subTest(route=route):
                self.assertIn(f'href="{route}"'.encode(), page)
                status, body, headers = self.request('GET', route)
                self.assertEqual(status, 200)
                self.assertEqual(headers['content-type'], 'text/plain; charset=utf-8')
                self.assertEqual(body, (root / filename).read_bytes())


@unittest.skipIf(os.name=='nt','POSIX process-group recovery exercised on Linux')
class SupervisorIntegrationTests(unittest.TestCase):
    def test_web_shutdown_requires_login_and_csrf_and_stops_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            Settings(home).set_password('integration-password-123')
            port = free_port()
            proc = subprocess.Popen(
                [sys.executable, '-m', 'ptfree', '--data', tmp, '--port', str(port), 'run'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            )

            def request(path, body=None, headers=None):
                conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                try:
                    conn.request('POST' if body is not None else 'GET', path,
                                 json.dumps(body) if body is not None else None,
                                 {'Content-Type': 'application/json', **(headers or {})})
                    response = conn.getresponse()
                    return response.status, json.loads(response.read()), dict(response.getheaders())
                finally:
                    conn.close()

            try:
                wait_until(lambda: request('/healthz')[0] == 200)
                self.assertEqual(request('/api/shutdown', {})[0], 401)
                status, data, headers = request('/api/login', {'password': 'integration-password-123'})
                self.assertEqual(status, 200)
                cookie = headers['Set-Cookie'].split(';')[0]
                self.assertEqual(request('/api/shutdown', {}, {'Cookie': cookie})[0], 403)
                self.assertEqual(request('/api/shutdown', {}, {'Cookie': cookie, 'X-CSRF-Token': data['csrf']})[0], 200)
                self.assertEqual(proc.wait(timeout=8), 0)
                self.assertEqual(Store(home).get('supervisor_restarts', 0), 0)
                with self.assertRaises(OSError):
                    request('/healthz')
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=8)

    def test_crashed_web_process_is_restarted(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp); port=free_port()
            proc=subprocess.Popen([sys.executable,'-m','ptfree','--data',tmp,'--port',str(port),'run'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            try:
                heartbeat=home/'heartbeat.json'
                def pid(): return json.loads(heartbeat.read_text())['pid'] if heartbeat.exists() else None
                original=wait_until(pid)
                os.kill(original,signal.SIGKILL)
                replacement=wait_until(lambda:(p if (p:=pid())!=original else None),timeout=15)
                self.assertNotEqual(original,replacement)
                self.assertGreaterEqual(Store(home).get('supervisor_restarts'),1)
            finally:
                proc.terminate(); proc.wait(timeout=10)

    def test_job_wall_clock_deadline_kills_stuck_worker(self):
        from ptfree.server import Coordinator
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp); settings=Settings(home);db=Store(home);db.init()
            coordinator=Coordinator(home,settings,db)
            jid=db.queue_job('test-dir')
            with db.connect() as d:d.execute("UPDATE jobs SET state='running',started=? WHERE id=?",(time.time()-60,jid))
            coordinator.process=subprocess.Popen([sys.executable,'-c','import time; time.sleep(600)'])
            child=coordinator.process;coordinator.jid=jid;coordinator.started=time.time()-60;coordinator.budget=1
            coordinator.enabled_before=False;coordinator.dry_before=True
            try:
                coordinator.tick();self.assertIsNotNone(child.poll());self.assertEqual(db.job(jid)['state'],'error')
                self.assertIn('超过时间上限',db.job(jid)['result']['message'])
            finally:coordinator.close()


if __name__=='__main__':unittest.main()
