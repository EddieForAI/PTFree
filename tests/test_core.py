# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

import copy
import datetime as dt
import hashlib
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ptfree.common import AppError
from ptfree.files import recover_torrent
from ptfree.network import MTeam, Response, Transport, connect_address, retry_after, site_host
from ptfree.policy import matches, normalize, timestamp
from ptfree.settings import DEFAULT, Settings, validate
from ptfree.store import Store
from ptfree.torrent import inspect_torrent
from ptfree.worker import Worker


def bencode(x):
    if isinstance(x,int): return b'i'+str(x).encode()+b'e'
    if isinstance(x,bytes): return str(len(x)).encode()+b':'+x
    if isinstance(x,list): return b'l'+b''.join(bencode(a) for a in x)+b'e'
    return b'd'+b''.join(bencode(k)+bencode(v) for k,v in sorted(x.items()))+b'e'


INFO={b'name':b'fixture.txt',b'length':4,b'piece length':16384,b'pieces':b'a'*20}
TORRENT=bencode({b'announce':b'https://tracker.example.invalid/announce',b'info':INFO})


def raw(tid='123'):
    return {'id':tid,'name':'Linux fixture','size':1024**3,'createdDate':dt.datetime.now(dt.timezone.utc).isoformat(),
            'status':{'discount':'FREE','seeders':'2','visible':True,'banned':False}}


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.home=Path(self.tmp.name)
        self.settings=Settings(self.home); self.db=Store(self.home); self.db.init()
        self.c=self.settings.get(); self.c.update(api_key='fake-test-key',watch_dir=str(self.home/'seeds'),min_disk_gib=0,max_age_hours=0,dry_run=False)
        self.settings.save(self.c)
    def tearDown(self): self.tmp.cleanup()


class SettingsTests(TempCase):
    def test_secrets_are_encrypted_and_public_response_omits_them(self):
        self.settings.save(self.c)
        text=(self.home/'settings.json').read_text()
        self.assertNotIn('fake-test-key',text)
        p=self.settings.public(); self.assertNotIn('api_key',p); self.assertTrue(p['api_key_set'])
    def test_blank_secret_preserves_and_rotation_works(self):
        self.settings.save({'api_key':''},merge=True)
        self.assertEqual(self.settings.get()['api_key'],'fake-test-key')
        self.settings.save({'api_key':'replacement-test-key'},merge=True)
        self.assertEqual(self.settings.get()['api_key'],'replacement-test-key')
    def test_legacy_directory_settings_are_migrated_without_changing_token(self):
        raw_config = json.loads(self.settings.path.read_text())
        encrypted_token = raw_config['api_key']
        raw_config.update(delivery='watch', concurrency=4, qb_url='http://localhost:8080',
                          qb_password='old-credential', enabled=True)
        self.settings.path.write_text(json.dumps(raw_config))
        config = self.settings.get()
        self.assertEqual(config['watch_dir'], self.c['watch_dir'])
        self.assertEqual(config['api_key'], self.c['api_key'])
        self.assertTrue(config['enabled'])
        migrated = json.loads(self.settings.path.read_text())
        self.assertEqual(migrated['api_key'], encrypted_token)
        self.assertTrue({'delivery', 'concurrency', 'qb_url', 'qb_password'}.isdisjoint(migrated))
    def test_legacy_client_only_settings_pause_until_directory_is_set(self):
        raw_config = json.loads(self.settings.path.read_text())
        raw_config.update(delivery='qb', watch_dir='', qb_savepath='D:/media', enabled=True)
        self.settings.path.write_text(json.dumps(raw_config))
        config = self.settings.get()
        self.assertFalse(config['enabled'])
        self.assertEqual(config['watch_dir'], '')
    def test_lost_key_does_not_silently_overwrite(self):
        self.settings.keypath.unlink()
        with self.assertRaises(AppError): Settings(self.home)
    def test_password_reset_rotates_session_secret(self):
        before=self.settings.session_key(); self.settings.set_password('my-new-password-123')
        self.assertTrue(self.settings.check_password('my-new-password-123'))
        self.assertFalse(self.settings.check_password('wrong')); self.assertNotEqual(before,self.settings.session_key())
        self.assertFalse((self.home/'初始管理密码.txt').exists())
    def test_reject_invalid_config(self):
        for change in [{'interval':1},{'interval':120.5},{'timeout':float('nan')},{'dry_run':'false'},
                       {'api_base':'https://evil.invalid/api'},{'api_base':'https://api.m-team.cc/api?x=1'},
                       {'qb_url':'http://user:pass@localhost:8080'},{'min_gib':10,'max_gib':1},{'network':'bad'}]:
            with self.subTest(change=change),self.assertRaises(AppError): validate({**self.c,**change})


class PolicyTests(unittest.TestCase):
    def setUp(self): self.c=copy.deepcopy(DEFAULT); self.c['max_age_hours']=0
    def test_free_and_double_free(self):
        for value in ('FREE','_2X_FREE'):
            r=raw(); r['status']['discount']=value
            self.assertTrue(matches(normalize(r),self.c)[0])
    def test_mall_free_only_during_active_window(self):
        r=raw(); r['status']['discount']='NORMAL'; r['status']['mallSingleFree']={'status':'ONGOING','endDate':time.time()+7200}
        self.assertTrue(matches(normalize(r),self.c)[0])
        r['status']['mallSingleFree']['endDate']=time.time()-1
        self.assertFalse(matches(normalize(r),self.c)[0])
    def test_never_treat_top_position_as_free(self):
        r=raw(); r['status'].update(discount='NORMAL',toppingLevel=1)
        self.assertFalse(matches(normalize(r),self.c)[0])
    def test_expiry_buffer(self):
        r=raw(); r['status']['discountEndTime']=time.time()+600
        self.assertFalse(matches(normalize(r),self.c)[0])
    def test_malformed_expiry_is_not_confirmed_free(self):
        r=raw(); r['status']['discountEndTime']='invalid'
        self.assertFalse(matches(normalize(r),self.c)[0])
    def test_exact_size_not_floor_rounded(self):
        r=raw(); r['size']=int(100.1*1024**3)
        self.assertFalse(matches(normalize(r),self.c)[0])
    def test_only_explicit_hr_is_excluded(self):
        t = normalize(raw())
        self.assertFalse(t['hr'])
        self.assertTrue(matches(t, self.c)[0])
        t['hr'] = True
        self.assertFalse(matches(t, self.c)[0])
    def test_white_blacklist_case_insensitive(self):
        t=normalize(raw()); self.c.update(include='other\nLINUX',exclude='windows')
        self.assertTrue(matches(t,self.c)[0]); self.c['exclude']='fixture'; self.assertFalse(matches(t,self.c)[0])
    def test_missing_date_rejected_when_age_filter_enabled(self):
        r=raw(); del r['createdDate']; self.c['max_age_hours']=24
        self.assertFalse(matches(normalize(r),self.c)[0])
    def test_only_active_timed_free_bypasses_publication_age(self):
        self.c['max_age_hours']=24
        r=raw()
        r['createdDate']='2018-01-01T00:00:00Z'
        now=time.time()
        self.assertFalse(matches(normalize(r),self.c,now)[0])
        r['status']['discountEndTime']=now+7200
        self.assertEqual(matches(normalize(r),self.c,now),(True,'符合条件（限时免费）'))
        r['status']['discountEndTime']=now+600
        self.assertEqual(matches(normalize(r),self.c,now),(False,'免费剩余时间不足或已过期'))
        self.c['min_free_minutes']=0
        r['status']['discountEndTime']=now
        self.assertFalse(matches(normalize(r),self.c,now)[0])
    def test_dates_without_zone_use_beijing_time(self):
        self.assertEqual(timestamp('2026-01-01 08:00:00'),timestamp('2026-01-01T00:00:00Z'))


class TorrentTests(unittest.TestCase):
    def test_hash_exact_info_bytes(self): self.assertEqual(inspect_torrent(TORRENT),[hashlib.sha1(bencode(INFO)).hexdigest()])
    def test_reject_html_and_truncated_payloads(self):
        for data in [b'<html>Sign in</html>',TORRENT[:-1],TORRENT+b'x',b'd4:infodee']:
            with self.subTest(data=data[:20]),self.assertRaises(AppError): inspect_torrent(data)
    def test_v2_and_hybrid(self):
        pure={b'name':b'test',b'piece length':16384,b'meta version':2,b'file tree':{}}
        self.assertEqual(len(inspect_torrent(bencode({b'info':pure}))[0]),64)
        hybrid={**pure,**INFO}; self.assertEqual(len(inspect_torrent(bencode({b'info':hybrid}))),2)
    def test_duplicate_info_dictionary_rejected(self):
        payload=b'd4:info'+bencode(INFO)+b'4:info'+bencode(INFO)+b'e'
        with self.assertRaises(AppError): inspect_torrent(payload)


class ApiTests(TempCase):
    def fake(self,response):
        calls=[]
        class Http:
            def request(self,*args,**kwargs): calls.append((args,kwargs)); return response
        api=MTeam(self.c,self.db,Http()); self.db.reserve_request=lambda kind:None
        return api,calls
    def test_api_uses_token_header_and_form_download_token(self):
        api,calls=self.fake(Response(200,{},b'{"code":"0","data":"https://api.m-team.cc/x"}'))
        api.api('torrent/genDlToken',{'id':'123'},'token',form=True)
        args,kw=calls[0]; self.assertEqual(args[2],b'id=123'); self.assertEqual(args[3]['x-api-key'],'fake-test-key'); self.assertNotIn('Cookie',args[3])
    def test_401_403_stop_authentication(self):
        for status in (401,403):
            api,_=self.fake(Response(status,{},b''))
            with self.assertRaises(AppError) as e: api.profile()
            self.assertEqual(e.exception.kind,'auth')
    def test_429_persists_cooldown(self):
        api,_=self.fake(Response(429,{'retry-after':'30'},b''))
        with self.assertRaises(AppError) as error: api.profile()
        end = self.db.get('blocked_until')
        self.assertAlmostEqual(end,time.time()+30,delta=1)
        self.assertEqual(self.db.defer_rate(error.exception),end)
        self.assertEqual(self.db.get('cooldown_reason'),{'source':'http_429','stage':'profile'})
    def test_api_message_does_not_leak_server_secrets(self):
        api,_=self.fake(Response(200,{},b'{"code":"FAILED","message":"secret-test-value"}'))
        with self.assertRaises(AppError) as e: api.profile()
        diagnostic = str(e.exception)
        for text in ('认证测试', 'HTTP 200', '业务码 FAILED'):
            self.assertIn(text,diagnostic)
        self.assertNotIn('secret-test-value',diagnostic)
    def test_reject_bad_search_shape(self):
        api,_=self.fake(Response(200,{},b'{"code":"SUCCESS","data":{"data":{}}}'))
        with self.assertRaises(AppError): api.search()
    def test_history_queries_ids_in_one_request(self):
        reply = {'code': '0', 'data': {
            'historyMap': {'2': {'timesCompleted': '1'}, '3': {'timesCompleted': '0'}},
            'peerMap': {'1': {'left': '40'}, '4': {'left': '0'}},
        }}
        api, calls = self.fake(Response(200, {}, json.dumps(reply).encode()))
        history = api.download_history(['1', '2', '3', '4', '5'])
        self.assertEqual(set(history), {'1', '2', '3', '4'})
        self.assertIn('曾经完成', history['2'])
        self.assertIn('正在做种', history['4'])
        self.assertEqual(len(calls), 1)
        args, _ = calls[0]
        self.assertTrue(args[1].endswith('/tracker/queryHistory'))
        self.assertEqual(json.loads(args[2]), {'tids': ['1', '2', '3', '4', '5']})
    def test_history_missing_maps_is_not_an_empty_success(self):
        api, _ = self.fake(Response(200, {}, b'{"code":"0","data":{"historyMap":{}}}'))
        with self.assertRaises(AppError):
            api.download_history(['123'])
    def test_authenticated_api_domain_allowlist(self):
        self.assertTrue(site_host('api.m-team.cc')); self.assertFalse(site_host('m-team.cc.evil.invalid'))
        with self.assertRaises(AppError): Transport(self.c).request('GET','https://evil.invalid/p',site=True)
    def test_download_resolves_relative_and_follows_direct_cdn_urls(self):
        for link, expected in (
            ('http://cdn.example.invalid:8080/dlv2?sign=a%2Fb', 'http://cdn.example.invalid:8080/dlv2?sign=a%2Fb'),
            ('/api/rss/dlv2?sign=a%2Fb', self.c['api_base'] + '/rss/dlv2?sign=a%2Fb'),
            ('//api.m-team.io/api/rss/dlv2?sign=a%2Fb', 'https://api.m-team.io/api/rss/dlv2?sign=a%2Fb'),
        ):
            with self.subTest(link=link):
                transport = Mock()
                transport.request.side_effect = [
                    Response(200, {}, json.dumps({'code': '0', 'data': link}).encode()),
                    Response(200, {}, TORRENT),
                ]
                api = MTeam(self.c, self.db, transport)
                self.db.reserve_request = lambda kind: None
                self.assertEqual(api.download('123'), TORRENT)
                self.assertEqual(transport.request.call_args.args, ('GET', expected))
                self.assertNotIn('headers', transport.request.call_args.kwargs)
    def test_download_rejection_logs_origin_without_token(self):
        for link in (
            'file:///private-value?sign=test-secret',
            'https://cdn.example.invalid:70000/private-value?sign=test-secret',
            'https://private-value:test-secret@cdn.example.invalid/',
        ):
            with self.subTest(link=link):
                api, calls = self.fake(Response(200, {}, json.dumps({'code': '0', 'data': link}).encode()))
                with self.assertRaises(AppError) as caught:
                    api.download('123')
                message = str(caught.exception)
                self.assertNotIn('private-value', message)
                self.assertNotIn('test-secret', message)
                self.assertEqual(len(calls), 1)
    def test_download_follows_cdn_redirects_and_drops_request_credentials(self):
        first, second, third = Mock(), Mock(), Mock()
        first.getresponse.return_value.status = 302
        first.getresponse.return_value.getheaders.return_value = [
            ('Location', 'https://fr1.halomt.com/dl?sign=a%2Fb'),
        ]
        second.getresponse.return_value.status = 307
        second.getresponse.return_value.getheaders.return_value = [('Location', '//next.example.invalid:8443/dl')]
        third.getresponse.return_value.status = 200
        third.getresponse.return_value.getheaders.return_value = []
        third.getresponse.return_value.read1.side_effect = [TORRENT, b'']
        with patch('ptfree.network.http.client.HTTPSConnection', side_effect=[first, second, third]) as https:
            result = Transport(self.c).request('GET', 'https://api.m-team.cc/dl',
                                               headers={'x-api-key': 'fake-key', 'Cookie': 'test-cookie'}, redirects=2)
        self.assertEqual(result.body, TORRENT)
        self.assertEqual(https.call_args_list[1].args[0], 'fr1.halomt.com')
        self.assertEqual(https.call_args.args[:2], ('next.example.invalid', 8443))
        self.assertEqual(second.request.call_args.args, ('GET', '/dl?sign=a%2Fb'))
        for connection in (second, third):
            self.assertNotIn('x-api-key', connection.request.call_args.kwargs['headers'])
            self.assertNotIn('Cookie', connection.request.call_args.kwargs['headers'])
    def test_download_redirect_rejects_non_web_protocol_before_connection(self):
        connection = Mock()
        connection.getresponse.return_value.status = 302
        connection.getresponse.return_value.getheaders.return_value = [
            ('Location', 'file:///private-value?sign=test-secret'),
        ]
        with patch('ptfree.network.http.client.HTTPSConnection', return_value=connection) as https:
            with self.assertRaisesRegex(AppError, '种子下载跳转') as caught:
                Transport(self.c).request('GET', 'https://api.m-team.cc/dl', redirects=1)
        self.assertNotIn('test-secret', str(caught.exception))
        self.assertEqual(https.call_count, 1)
    def test_retry_after_http_date_and_seconds(self):
        self.assertEqual(retry_after('600'),600); self.assertEqual(retry_after('bad'),900)


class StoreTests(TempCase):
    def test_upgrade_requeues_old_domain_rejections(self):
        for tid, state, reason in (
            ('1', 'exhausted', '种子下载跳转不是受信任的 M-Team 域名，已拦截（https://fr1.halomt.com）'),
            ('2', 'failed', '种子下载地址不属于 M-Team HTTPS 域名，已停止请求'),
            ('3', 'failed', '其他错误'),
            ('4', 'done', '已保存'),
        ):
            self.db.observe(normalize(raw(tid)), state, reason)
            self.db.set_state(tid, state, reason, attempts=5, retry_at=123)
        self.db.init()
        self.db.init()
        with self.db.connect() as conn:
            rows = [tuple(row) for row in conn.execute('SELECT state, attempts, retry_at FROM torrents ORDER BY id')]
        self.assertEqual(rows, [('pending', 0, 0), ('pending', 0, 0), ('failed', 5, 123), ('done', 5, 123)])

    def test_torrent_start_pacing_survives_new_worker(self):
        with patch('ptfree.store.time.time', side_effect=[100, 100, 100, 100, 101]), \
                patch('ptfree.store.time.sleep') as sleep:
            self.db.pace_torrent()
            for kind in ('detail', 'token', 'download'):
                self.db.reserve_request(kind)
            Store(self.home).pace_torrent()
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 2)

    def test_legacy_client_jobs_are_cancelled_and_pending_torrents_can_be_saved(self):
        self.db.observe(normalize(raw()), 'submitting', 'old')
        self.db.set_state('123', 'submitting', 'old', attempts=2, retry_at=time.time()+300)
        job_id = self.db.queue_job('test-qb')
        self.db.init()
        record = self.db.history()['items'][0]
        self.assertEqual(record['state'], 'pending')
        self.assertEqual(record['attempts'], 2)
        self.assertEqual(self.db.job(job_id)['state'], 'cancelled')
    def test_persistent_rolling_quota_survives_restart(self):
        with self.db.connect() as d: d.executemany('INSERT INTO requests(ts,kind) VALUES(?,?)',[(time.time(),'search')]*900)
        again=Store(self.home)
        with self.assertRaises(AppError) as e: again.reserve_request('search')
        self.assertEqual(e.exception.kind,'rate')
    def test_success_is_not_reset_by_rediscovery(self):
        t=normalize(raw()); self.db.observe(t,'pending',''); self.db.set_state(t['id'],'done','ok',completed=time.time())
        self.db.observe(t,'pending',''); self.assertEqual(self.db.history()['items'][0]['state'],'done')
    def test_failed_is_not_overwritten_before_retry_due(self):
        t=normalize(raw()); self.db.observe(t,'pending',''); self.db.set_state(t['id'],'failed','network',retry_at=time.time()+3600)
        self.db.observe(t,'pending',''); self.assertEqual(self.db.history()['items'][0]['state'],'failed')

    def test_history_orders_by_saved_or_first_seen_time_after_rescan(self):
        for tid, seen_at, completed in (('1',10,20), ('2',11,22), ('3',21,None), ('4',12,0)):
            with patch('ptfree.store.time.time',return_value=seen_at):
                self.db.observe(normalize(raw(tid)),'pending','')
            if completed is not None:
                self.db.set_state(tid,'done','已保存',completed=completed)
        with patch('ptfree.store.time.time',return_value=30):
            for tid in ('1','2','3','4'):
                self.db.observe(normalize(raw(tid)),'pending','')
        items = self.db.history()['items']
        self.assertEqual([item['id'] for item in items],['2','3','1','4'])
        self.assertEqual([item['updated'] for item in items],[30]*4)
        self.assertEqual([item['id'] for item in self.db.history(state='done')['items']],['2','1','4'])


class WorkerTests(TempCase):
    def setUp(self):
        super().setUp()
        pace = patch.object(Store,'pace_torrent')
        self.pace = pace.start()
        self.addCleanup(pace.stop)

    def fake_worker(self,download_error=None,existing=None,history_error=None):
        class Site:
            downloads=0
            def __init__(self,*args): pass
            def search(self): return [raw()]
            def detail(self,tid): return raw(tid)
            def download_history(self, torrent_ids):
                if torrent_ids and history_error:
                    raise history_error
                return {tid: existing[tid] for tid in torrent_ids if existing and tid in existing}
            def download(self,tid):
                Site.downloads+=1
                if download_error: raise download_error
                return TORRENT
        return Worker(self.home,Site),Site
    def get_row(self):
        with self.db.connect() as d:return dict(d.execute("SELECT * FROM torrents WHERE id='123'").fetchone())
    def test_success_writes_torrent_and_persists_dedup(self):
        w,site=self.fake_worker(); r=w.scan(); self.assertEqual(r['done'],1)
        self.assertEqual((self.home/'seeds'/'MT-123.torrent').read_bytes(),TORRENT)
        w.scan(); self.assertEqual(site.downloads,1); self.assertEqual(self.get_row()['state'],'done')
    def test_old_mall_promotion_is_previewed_and_rechecked_before_download(self):
        self.settings.save({'max_age_hours':24},merge=True)
        worker,site=self.fake_worker()
        listed=raw()
        listed['createdDate']='2018-01-01T00:00:00Z'
        listed['status'].update(discount='NORMAL',mallSingleFree={
            'status':'ONGOING','startDate':time.time()-60,'endDate':time.time()+7200,
        })
        worker.site.search=lambda:[listed]
        detail=raw()
        detail['createdDate']=listed['createdDate']
        worker.site.detail=lambda tid:detail
        self.assertEqual(worker.scan(preview=True)['items'][0]['reason'],'符合条件（限时免费）')
        detail['status']['discount']='NORMAL'
        self.assertEqual(worker.scan()['done'],0)
        self.assertEqual(self.get_row()['reason'],'非确认免费种子')
        self.assertEqual(site.downloads,0)
        detail['status']['discount']='FREE'
        self.assertEqual(worker.scan()['done'],1)
        self.assertEqual(site.downloads,1)
    def test_scan_drains_more_than_old_batch_limit(self):
        worker, site = self.fake_worker()
        worker.site.search = lambda: [raw(str(tid)) for tid in range(1, 37)]
        self.assertEqual(worker.scan()['done'], 36)
        self.assertEqual(site.downloads, 36)

    def test_scan_preserves_remaining_queue_when_limited(self):
        worker, site = self.fake_worker(AppError('限流', 'rate', 120))
        worker.site.search = lambda: [raw(str(tid)) for tid in range(1, 8)]
        self.assertEqual(worker.scan()['processed'], 1)
        self.assertEqual(site.downloads, 1)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM torrents WHERE state='pending'").fetchone()[0], 7)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM torrents WHERE retry_at>0").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT SUM(attempts) FROM torrents").fetchone()[0], 0)

    def test_local_quota_defers_without_failure_or_extra_delay(self):
        self.settings.save({'local_request_limits':True},merge=True)
        now = time.time()
        with self.db.connect() as conn:
            conn.executemany("INSERT INTO requests(ts,kind) VALUES(?,'detail')",[(now-3580,)]*80)
        worker,_ = self.fake_worker()
        worker.site.detail = lambda tid: worker.db.reserve_request('detail')
        result = worker.scan()
        record = self.get_row()
        self.assertEqual((record['state'], record['attempts'], result['failed']),('waiting_quota',0,0))
        self.assertAlmostEqual(record['retry_at'],now+20,delta=1)
        self.assertEqual(result['paused_until'],record['retry_at'])
        self.assertEqual(self.db.get('cooldown_reason'),{'source':'quota','stage':'detail'})
        self.assertEqual(self.db.history(state='waiting_quota')['total'],1)

    def test_default_worker_processes_with_old_local_quota_full(self):
        with self.db.connect() as conn:
            conn.executemany("INSERT INTO requests(ts,kind) VALUES(?,'detail')",[(time.time(),)]*80)
        worker,_ = self.fake_worker()

        def detail(tid):
            worker.db.reserve_request('detail')
            return raw(tid)

        worker.site.detail = detail
        self.assertEqual(worker.scan()['done'],1)
        self.assertEqual(self.db.stats()['requests_24h']['detail'],81)

    def test_disabling_local_limits_releases_legacy_quota_but_keeps_site_cooldown(self):
        self.db.observe(normalize(raw()),'failed','已达到本程序的站点请求配额，等待窗口恢复')
        self.db.event('种子 123：已达到本程序的站点请求配额，等待窗口恢复','error')
        self.db.put('blocked_until',time.time()+100)
        self.db.init()
        self.assertTrue(self.db.release_local_wait())
        self.assertEqual(self.get_row()['state'],'pending')
        self.assertIsNone(self.db.get('blocked_until'))
        self.db.event('M-Team 请求限流，已进入冷却期','error')
        until = time.time()+100
        self.db.put('blocked_until',until)
        self.assertFalse(self.db.release_local_wait())
        self.assertEqual(self.db.get('blocked_until'),until)

    def test_repeated_detail_errors_pause_then_probe_and_resume(self):
        start = time.time()
        rows = [raw(str(tid)) for tid in range(1,8)]
        error = AppError('业务错误', 'protocol', stage='detail', status=200, code='FAILED', source='site')

        def fail(tid):
            raise error

        first,_ = self.fake_worker()
        first.site.search = lambda: rows
        first.site.detail = fail
        result = first.scan()
        self.assertEqual((result['processed'],result['failed']), (3,3))
        self.assertEqual(self.pace.call_count,3)
        self.assertEqual(self.db.get('cooldown_reason'),{'source':'repeated_error','stage':'detail'})
        with patch('ptfree.worker.time.time',return_value=start+61):
            probe,_ = self.fake_worker()
            probe.site.search = lambda: rows
            probe.site.detail = fail
            result = probe.scan()
            self.assertEqual(result['processed'],1)
            self.assertEqual(result['paused_until'],start+121)
        with patch('ptfree.worker.time.time',return_value=start+200):
            recovered,_ = self.fake_worker()
            recovered.site.search = lambda: rows
            result = recovered.scan()
            self.assertEqual(result['done'],7)
            self.assertIsNone(self.db.get('site_probe'))
    def test_site_history_skips_download_and_survives_rescan(self):
        worker, site = self.fake_worker(existing={'123': '站点已有下载历史'})
        self.assertEqual(worker.scan()['site_existing'], 1)
        self.assertEqual(worker.scan()['processed'], 0)
        self.assertEqual(site.downloads, 0)
        self.assertEqual(self.get_row()['state'], 'site_existing')
    def test_token_change_rechecks_prior_site_verdict(self):
        worker, _ = self.fake_worker(existing={'123': '站点已有下载历史'})
        worker.scan()
        self.settings.save({'api_key': 'different-key'}, merge=True)
        worker, site = self.fake_worker()
        self.assertEqual(worker.scan()['done'], 1)
        self.assertEqual(site.downloads, 1)
    def test_history_failure_keeps_candidate_pending_without_downloading(self):
        worker, site = self.fake_worker(history_error=AppError('接口限流', 'rate', 180))
        with self.assertRaisesRegex(AppError, '本轮暂缓') as caught:
            worker.scan()
        self.assertEqual(caught.exception.kind, 'rate')
        self.assertEqual(site.downloads, 0)
        self.assertEqual(self.get_row()['state'], 'pending')
        self.assertEqual(self.get_row()['attempts'], 0)
    def test_preview_identifies_site_history_without_delivery(self):
        worker, site = self.fake_worker(existing={'123': '站点已有下载历史'})
        result = worker.scan(preview=True)
        self.assertEqual(result['matched'], 0)
        self.assertEqual(result['site_existing'], 1)
        self.assertEqual(site.downloads, 0)
    def test_failure_is_retryable_and_not_marked_done(self):
        w,_=self.fake_worker(AppError('测试网络故障')); w.scan()
        row=self.get_row(); self.assertEqual(row['state'],'failed'); self.assertGreater(row['retry_at'],time.time())
        w,site=self.fake_worker(); self.db.set_state('123','failed','',retry_at=0); w.scan()
        self.assertEqual(self.get_row()['state'],'done')
    def test_dry_run_does_not_fetch_or_publish(self):
        self.settings.save({'dry_run':True},merge=True); w,site=self.fake_worker(); result=w.scan()
        self.assertTrue(result['preview']); self.assertEqual(site.downloads,0); self.assertFalse((self.home/'seeds').exists())
    def test_max_retry_exhaustion(self):
        self.settings.save({'max_attempts':1},merge=True); w,_=self.fake_worker(AppError('test')); w.scan()
        self.assertEqual(self.get_row()['state'],'exhausted')
    def test_interrupted_attempt_budget_does_not_reset(self):
        t=normalize(raw()); self.db.observe(t,'pending','')
        self.db.set_state('123','pending','模拟前次操作进程中断',attempts=self.c['max_attempts'])
        w,site=self.fake_worker();w.scan()
        self.assertEqual(site.downloads,0);self.assertEqual(self.get_row()['state'],'exhausted')
    def test_recover_publish_before_rename(self):
        t=normalize(raw()); self.db.observe(t,'pending',''); target=self.home/'seeds'/'MT-123.torrent'; target.parent.mkdir()
        temp=target.parent/'.MT-123.part'; temp.write_bytes(TORRENT)
        self.db.set_state('123','publishing','',target=str(target),temp=str(temp))
        recover_torrent(self.db,self.get_row()); self.assertEqual(target.read_bytes(),TORRENT); self.assertEqual(self.get_row()['state'],'done')
    def test_recover_consumed_watch_file_does_not_publish_again(self):
        t=normalize(raw()); self.db.observe(t,'pending',''); target=self.home/'seeds'/'MT-123.torrent'
        self.db.set_state('123','publishing','',target=str(target),temp=str(target.parent/'.MT-123.part'))
        recover_torrent(self.db,self.get_row()); self.assertFalse(target.exists()); self.assertEqual(self.get_row()['state'],'done')


class NetworkOrderTests(unittest.TestCase):
    def test_ipv4_preferred_then_ipv6_fallback(self):
        attempted=[]
        class Sock:
            def __init__(self,af,*a): self.af=af
            def settimeout(self,*a):pass
            def connect(self,sa):
                attempted.append(self.af)
                if self.af==socket.AF_INET: raise OSError('v4 broken')
            def close(self):pass
        addresses=[(socket.AF_INET6,socket.SOCK_STREAM,6,'',('::1',80,0,0)),(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',80))]
        with patch('socket.getaddrinfo',return_value=addresses),patch('socket.socket',Sock):
            connect_address(('test',80),20,network='ipv4_prefer')
        self.assertEqual(attempted,[socket.AF_INET,socket.AF_INET6])
    def test_ipv4_only_resolution_family(self):
        with patch('socket.getaddrinfo',return_value=[]) as resolve:
            with self.assertRaises(TimeoutError): connect_address(('test',80),5,network='ipv4_only')
            self.assertEqual(resolve.call_args.args[2],socket.AF_INET)


if __name__=='__main__': unittest.main()
