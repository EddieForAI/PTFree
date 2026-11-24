# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from ptfree.background import Background
from ptfree.settings import Settings
from ptfree.store import Store
from ptfree.traffic import collect_foreground, record_file_receipt, init_file_receipts, delta
from ptfree.freeseed_storage import put
from ptfree.sites import public_sites, record_health
from ptfree.qb import QB

H = 'a'*40
J = 'b'*40
K = 'c'*40
URL = 'http://127.0.0.1:8180'
NOW = dt.datetime(2026,11,25,0,5,tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp()

def torrent(h=H, added=NOW-300, downloaded=10, uploaded=100, **kw):
    return dict(hash=h,added_on=int(added),downloaded=downloaded,uploaded=uploaded,state='uploading',progress=1,**kw)

class Release204(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.home=Path(tmp.name); self.db=Store(self.home); self.db.init()
        self.settings=Settings(self.home)

    def background(self):
        bg=Background(self.home,self.settings,self.db)
        bg.store.save({'qb_url':URL})
        return bg

    def test_completed_file_seed_keeps_uploading_and_manual_excluded(self):
        bg=self.background()
        record_file_receipt(self.db,'1',[H],NOW-310)
        record_file_receipt(self.db,'2',[J],NOW-100)
        live={H:torrent(),J:torrent(J),K:torrent(K)}
        owned=collect_foreground(self.db,URL,live)
        self.assertEqual(set(owned),{(H,int(NOW-300))})
        with patch('ptfree.background.time.time',return_value=NOW):
            bg.measure_jobs([],{},owned)
        live[H]['uploaded']+=2**30
        live[J]['uploaded']+=2**30
        live[K]['uploaded']+=2**30
        with patch('ptfree.background.time.time',return_value=NOW+15):
            bg.measure_jobs([],{},collect_foreground(self.db,URL,live))
        history=bg.store.history('day',now=NOW+15)
        self.assertEqual(history['job_tx'],2**30)
        self.assertEqual(history['job_rx'],0)
        self.assertAlmostEqual(history['job_coverage'],15/86400)
        # Same hash, later manually added instance must not inherit ownership.
        self.assertEqual(collect_foreground(self.db,URL,{H:torrent(added=NOW+60)}),{})
        self.assertEqual(collect_foreground(self.db,'http://other:8180',live),{})

    def test_api_seed_and_duplicate_cross_module_count_once(self):
        bg=self.background()
        put(self.db,dict(id='1',url=URL,hashes=[H],issued=NOW-310,added_on=int(NOW-300),run_tag='test-run'))
        row=torrent(); owned=collect_foreground(self.db,URL,{H:row})
        self.assertTrue(owned)
        with patch('ptfree.background.time.time',return_value=NOW):
            bg.measure_jobs([{'run_tag':'bg'}],{'bg':row},owned,owned)
        row['uploaded']+=50000; row['downloaded']+=1000
        with patch('ptfree.background.time.time',return_value=NOW+15):
            bg.measure_jobs([{'run_tag':'bg'}],{'bg':row},owned,owned)
        h=bg.store.history('day',now=NOW+15)
        self.assertEqual((h['job_rx'],h['job_tx']),(1000,50000))

    def test_old_file_migration_survives_restart(self):
        self.db.put('background_config',{'qb_url':URL})
        self.db.observe(dict(id='1',name='old',size=1),'done','')
        self.db.set_state('1','done','种子文件已保存到目标目录',hashes=H,target='old.torrent',completed=NOW-295)
        init_file_receipts(self.db)
        self.assertTrue(collect_foreground(self.db,URL,{H:torrent()}))
        init_file_receipts(Store(self.home))
        self.assertTrue(collect_foreground(Store(self.home),URL,{H:torrent()}))
        self.assertFalse(collect_foreground(self.db,URL,{H:torrent(added=NOW-600)}))

    def test_all_rolling_windows_and_previous_pages(self):
        bg=self.background()
        bg.store.add_interval(NOW-600,NOW-585,job=(300,900),wan=(400,1000))
        for period, days in [('day',1),('week',7),('month',30),('year',365)]:
            h=bg.store.history(period,now=NOW)
            self.assertEqual((h['start'],h['end']),(NOW-days*86400,NOW))
            self.assertEqual((h['job_rx'],h['job_tx']),(300,900))
            prev=bg.store.history(period,offset=1,now=NOW)
            self.assertEqual((prev['start'],prev['end']),(NOW-2*days*86400,h['start']))
            self.assertEqual(prev['job_tx'],0)
            self.assertEqual(h['download_totals'][period],300)

    def test_partial_boundary_no_double_count(self):
        bg=self.background()
        now=NOW+30; edge=now-86400
        bg.store.add_interval(edge-30,edge,job=(30,60))
        bg.store.add_interval(edge,edge+30,job=(30,60))
        for offset in (0,1):
            h=bg.store.history('day',offset=offset,now=now)
            self.assertEqual(h['job_tx'],60)
            self.assertEqual(h['job_rx'],30)

    def test_reset_counter_and_new_job(self):
        key=(H,int(NOW))
        self.assertEqual(delta(key,(10,500),{key:(20,400)},NOW-15),(0,100))
        self.assertEqual(delta(key,(10,500),{},NOW-15),(10,500))
        self.assertEqual(delta(key,(10,500),{},NOW+15),(0,0))

    def test_qb_incremental_snapshot_keeps_completed_seed(self):
        qb=QB(URL,'','')
        qb.json=Mock(side_effect=[{'rid':1,'full_update':True,'torrents':{H:torrent()},'server_state':{}},
            {'rid':2,'torrents':{H:{'uploaded':999}},'server_state':{}},
            {'rid':3,'torrents_removed':[H]}])
        first,_=qb.traffic_snapshot(); self.assertEqual(first[H]['uploaded'],100)
        second,_=qb.traffic_snapshot(); self.assertEqual(second[H]['uploaded'],999); self.assertEqual(second[H]['state'],'uploading')
        self.assertEqual(qb.traffic_snapshot()[0],{})

    def test_registry_status_per_adapter_and_no_fake_sites(self):
        config={'site_added':True,'api_key_set':True,'freeseed_mteam':True}
        record_health(self.db,'mteam','ok')
        sites=public_sites(config,{'mteam_enabled':False},self.db)
        self.assertEqual(len(sites),1)
        self.assertEqual((sites[0]['id'],sites[0]['health'],sites[0]['freeseed_selected'],sites[0]['traffic_selected']),('mteam','ok',True,False))
        record_health(self.db,'mteam','auth')
        self.assertEqual(public_sites(config,{},self.db)[0]['health'],'auth')
        config['api_key_set']=False
        self.assertEqual(public_sites(config,{},self.db)[0]['health'],'unconfigured')

if __name__=='__main__': unittest.main()
