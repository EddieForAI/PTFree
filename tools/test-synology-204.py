# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0
# Run from synology/: PYTHONPATH=. python ../tools/test-synology-204.py
import tempfile, time
from pathlib import Path
from ptfree.store import Store
from ptfree.statistics_store import StatisticsStore
from ptfree.traffic import init_file_receipts, record_file_receipt, collect_foreground, delta
from ptfree.freeseed_storage import put
with tempfile.TemporaryDirectory() as d:
 db=Store(Path(d));db.init(); stats=StatisticsStore(db); init_file_receipts(db)
 now=1795536300; url='http://127.0.0.1:8180'; h='a'*40; j='b'*40; db.put('background_config',{'qb_url':url}); record_file_receipt(db,'1',[h],now-301); put(db,{'id':'2','url':url,'hashes':[j],'added_on':now-300,'issued':now-301,'run_tag':'test'})
 live={k:{'hash':k,'added_on':now-300,'downloaded':100,'uploaded':200,'state':'uploading'} for k in (h,j)}
 assert len(collect_foreground(db,url,live))==2
 stats.add_interval(now-15,now,job=(30,60));
 for period, days in [('day',1),('week',7),('month',30),('year',365)]:
  result=stats.history(period,now=now); assert result['end']-result['start']==days*86400; assert result['job_tx']==60 and result['download_totals'][period]==30
  prior=stats.history(period,offset=1,now=now); assert prior['end']==result['start'] and prior['job_tx']==0
 k='c'*40
 db.observe(dict(id='3',name='legacy seed',size=100),'done','')
 db.set_state('3','done','种子文件已保存到目标目录',completed=now-300)
 live[k]=dict(hash=k,name='legacy seed',total_size=100,added_on=now-299,downloaded=0,uploaded=500)
 assert (k, now-299) in collect_foreground(db,url,live)
 stats.add_interval(now,now+125,job=(1,113),wan=(5,7))
 result=stats.history('day',now=now+125)
 assert (result['job_rx'],result['job_tx'],result['wan_tx'])==(31,173,0)
 assert result['job_accounting_since']==now-15
 print('Synology smoke: file+API ownership, 4 rolling windows, previous pages, totals passed')
