# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import time

from .common import AppError, atomic_write, safe_error
from .background_selection import site_window
from .background_store import BackgroundStore
from .freeseed_storage import client as managed_client, deliver, yield_duplicate
from .qb import hash_keys
from .files import recover_torrent, save_torrent, test_directory
from .network import MTeam
from .policy import matches, normalize
from .settings import Settings
from .store import Store
from .torrent import inspect_torrent


class Worker:
    def __init__(self,home,site_factory=MTeam):
        self.home=home; self.settings=Settings(home); self.c=self.settings.get()
        self.db=Store(home,local_limits=self.c['local_request_limits'])
        self.site=site_factory(self.c,self.db)
        config = BackgroundStore(self.db, self.settings).config()
        self.site_event_end = site_window(config, time.time())
        self.failure_key = self.db.get('site_probe')
        self.failure_count = 2 if self.failure_key else 0

    def clear_site_failures(self, *stages):
        if self.failure_key and self.failure_key[0] in stages:
            self.failure_key = None
            self.failure_count = 0
            if self.db.get('site_probe'):
                self.db.put('site_probe',None)

    def record_site_failure(self, error):
        key = [error.stage,error.status,error.code,str(error)]
        if self.failure_key and key != self.failure_key:
            self.clear_site_failures(self.failure_key[0])
        self.failure_key = key
        self.failure_count += 1
        if self.failure_count >= 3:
            self.db.put('site_probe',key)
            until = self.db.cooldown(60,'repeated_error',error.stage)
            self.db.event(f'连续接口错误，60 秒后试一条。{error}','warning')
            return until
        return 0

    def query_site_history(self, torrent_ids):
        try:
            return self.site.download_history(torrent_ids)
        except AppError as error:
            raise AppError(f'站点下载历史查询失败，本轮暂缓下载。{error}',
                           error.kind, error.retry_after, stage=error.stage,
                           status=error.status, code=error.code, source=error.source) from error

    def match_offer(self, item):
        end = self.site_event_end
        now = time.time()
        if end and now < end:
            intrinsic = item.get('intrinsic_end')
            if not item.get('personal_end') and (not intrinsic or abs(intrinsic - end) <= 120):
                if not item.get('created') or now - item['created'] >= 86400:
                    return False, '全站免费期间，非单种促销的老种由后台筛选'
                item['promotion_end'] = None
                if not item['free']:
                    item.update(free=True, free_end=end)
        return matches(item, self.c, now)

    def process(self,row,site_reason,listed=None):
        tid=row['id']; attempts=row['attempts']+1
        try:
            if row['attempts']>=self.c['max_attempts']:
                self.db.set_state(tid,'exhausted','已达到最大尝试次数；可在历史中手动重试')
                return 'failed'
            # Count an attempt before blocking work, so forced termination does not
            # reset the retry budget on the next process.
            self.db.set_state(tid,row['state'],'正在处理',attempts=attempts)
            cache=self.home/'cache'/(tid+'.torrent')
            if row['state']=='publishing':
                recover_torrent(self.db,row)
                cache.unlink(missing_ok=True)
                return 'done'
            if site_reason and not cache.exists():
                self.db.set_state(tid, 'site_existing', site_reason, attempts=row['attempts'])
                cache.unlink(missing_ok=True)
                return 'site_existing'
            self.db.pace_torrent()
            t=normalize(self.site.detail(tid))
            self.clear_site_failures('detail')
            # A detail response can confirm FREE while omitting the campaign fields.
            if t['free'] and t['free_end'] is None and t['promotion_end'] is None and listed and listed['promotion_end']:
                t['promotion_end'] = listed['promotion_end']
                t['free_end'] = listed['free_end']
            ok,reason=self.match_offer(t)
            if not ok:
                self.db.set_state(tid,'skipped',reason)
                cache.unlink(missing_ok=True)
                return 'skipped'
            if not self.c['managed_gib']:
                test_directory(self.c)
            if cache.exists():
                data=cache.read_bytes()
            else:
                data=self.site.download(tid)
                self.clear_site_failures('token','download')
                inspect_torrent(data)
                atomic_write(cache,data)
            torrent_hashes = inspect_torrent(data)
            # Free eligibility is checked once again after network/file operations.
            ok,reason=self.match_offer(t)
            if not ok:
                self.db.set_state(tid,'skipped',reason); cache.unlink(missing_ok=True)
                return 'skipped'
            if self.c['managed_gib']:
                reason = deliver(self.db, self.settings, self.c, tid, data)
                self.db.set_state(tid, row['state'], reason, hashes=','.join(torrent_hashes))
            else:
                yield_duplicate(self.db, hash_keys(torrent_hashes))
                reason = save_torrent(self.c, self.db, row, data)
            self.db.set_state(tid,'done',reason,completed=time.time(),attempts=attempts,temp='')
            cache.unlink(missing_ok=True)
            self.db.event('种子 '+tid+'：'+reason)
            return 'done'
        except Exception as e:
            kind=getattr(e,'kind','temporary')
            if kind == 'capacity':
                self.db.set_state(tid, 'pending', safe_error(e), attempts=row['attempts'], retry_at=time.time()+30)
                return 'deferred'
            if kind == 'size':
                self.db.set_state(tid, 'skipped', safe_error(e), attempts=row['attempts'])
                return 'skipped'
            if kind=='auth':
                self.db.put('auth_error',True)
            if kind=='rate':
                until = self.db.defer_rate(e)
                state = 'waiting_quota' if e.source == 'quota' else row['state']
                self.db.set_state(tid,state,safe_error(e),attempts=row['attempts'],retry_at=until)
                self.db.event('种子 '+tid+'：'+safe_error(e),'warning')
                return 'deferred'
            delay=max(getattr(e,'retry_after',0),min(3600,30*2**min(attempts,7)))
            paused_until = self.record_site_failure(e) if isinstance(e,AppError) and e.source == 'site' else 0
            # Preserve the staged file so an interrupted write can be recovered.
            with self.db.connect() as d:
                current=d.execute('SELECT state FROM torrents WHERE id=?',(tid,)).fetchone()[0]
            state='exhausted' if attempts>=self.c['max_attempts'] else (current if current=='publishing' else 'failed')
            self.db.set_state(tid,state,safe_error(e),attempts=attempts,retry_at=paused_until or time.time()+delay)
            self.db.event('种子 '+tid+'：'+safe_error(e),'error')
            return 'failed'

    def scan(self,preview=False):
        if not (preview or self.c['dry_run']) and not (self.c['watch_dir'].strip() or self.c['managed_gib']):
            raise AppError('请先设置种子保存目录', 'config')
        raw=self.site.search()
        self.db.put('last_search_ok',time.time())
        if not preview and not self.c['dry_run']:
            fingerprint = hashlib.sha256(self.c['api_key'].encode()).hexdigest()
            self.db.sync_history_account(fingerprint)
        preview_rows=[]
        for item in raw:
            try:
                t=normalize(item); ok,reason=matches(t,self.c)
            except AppError:
                self.db.event('列表中有异常种子元数据，本条已跳过','warning'); continue
            preview_rows.append({**t,'matched':ok,'reason':reason})
            if not preview:
                self.db.observe(t,'pending' if ok else 'skipped',reason)
        if preview or self.c['dry_run']:
            existing = self.query_site_history([item['id'] for item in preview_rows if item['matched']])
            for item in preview_rows:
                if item['matched'] and item['id'] in existing:
                    item.update(matched=False, reason=existing[item['id']])
            return {'message':'预览完成，未下载种子文件','items':preview_rows,
                    'matched':sum(x['matched'] for x in preview_rows),'site_existing':len(existing),'preview':True}
        with self.db.connect() as d:
            rows=[dict(r) for r in d.execute("""SELECT * FROM torrents WHERE
               state IN ('pending','failed','publishing','waiting_quota') AND retry_at<=?
               ORDER BY CASE state WHEN 'publishing' THEN 0 WHEN 'waiting_quota' THEN 1
                                   WHEN 'failed' THEN 2 ELSE 3 END,
               first_seen DESC""",(time.time(),))]
        listed_by_id = {item['id']: item for item in preview_rows}
        results = []
        for start in range(0,len(rows),100):
            group = rows[start:start+100]
            existing = self.query_site_history([row['id'] for row in group if row['state'] != 'publishing'])
            for row in group:
                results.append(self.process(row, existing.get(row['id']), listed_by_id.get(row['id'])))
                if self.db.get('auth_error',False) or self.db.get('blocked_until',0) > time.time():
                    break
            else:
                continue
            break
        self.db.put('last_scan_ok',time.time())
        paused_until = self.db.get('blocked_until',0)
        if paused_until <= time.time():
            paused_until = 0
        return {'message':'队列暂缓' if paused_until else '扫描完成',
                'paused_until':paused_until,
                'found':len(raw),'matched':sum(x['matched'] for x in preview_rows),
                'processed':len(results),'done':results.count('done'),'failed':results.count('failed'),
                'site_existing':results.count('site_existing')}

    def run(self,kind):
        if kind=='test-site':
            result=self.site.profile(); self.db.put('auth_error',False); return result
        if kind=='test-dir':
            if self.c['managed_gib']:
                managed_client(self.db, self.settings, self.c)
                return {'message': 'qB 连接及 Freeseed 托管配置校验成功'}
            return test_directory(self.c)
        if kind in ('scan','preview'):
            if not self.c['freeseed_mteam']:
                raise AppError('Freeseed 未选择 M-Team', 'config')
            return self.scan(preview=kind=='preview')
        raise AppError('不支持的操作','config')


def run_job(home,jid):
    db=Store(home)
    try:
        job=db.job(jid)
        if not job:
            return 2
        result=Worker(home).run(job['kind'])
        db.finish_job(jid,result)
        db.put('last_job_error','')
        return 0
    except Exception as e:
        message=safe_error(e); kind=getattr(e,'kind','temporary')
        until = db.defer_rate(e) if kind=='rate' else 0
        db.finish_job(jid,{'message':message,'kind':kind,'retry_after':getattr(e,'retry_after',0),
                           'paused_until':until},'error')
        db.put('last_job_error','' if kind=='rate' else message)
        db.event(message,'warning' if kind=='rate' else 'error')
        if kind=='auth':
            db.put('auth_error',True)
        return 1
