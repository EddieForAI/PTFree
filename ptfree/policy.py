# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import datetime as dt
import time

from .common import AppError

TZ=dt.timezone(dt.timedelta(hours=8))


def timestamp(value):
    if value in (None,'',0):
        return None
    if isinstance(value,(int,float)):
        return value/1000 if value>1e12 else value
    try:
        date=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return date.replace(tzinfo=TZ).timestamp() if date.tzinfo is None else date.timestamp()
    except (ValueError,OverflowError):
        return None


def hr_flag(raw, status):
    for obj in (raw,status):
        for key in ('hitAndRun','hitAndRunEnabled','hr','isHr','isHR'):
            if key in obj and obj[key] is not None:
                v=obj[key]
                if v in (True,1,'1','true','TRUE','YES'):
                    return True
    return False


def normalize(raw):
    try:
        tid=str(raw['id'])
        if not tid.isdigit() or len(tid)>30:
            raise ValueError()
        status=raw.get('status') or {}
        if not isinstance(status,dict):
            raise ValueError()
        size=int(raw['size'])
        if size<0:
            raise ValueError()
        discount=str(status.get('discount','')).upper()
        end=timestamp(status.get('discountEndTime'))
        invalid_end=bool(status.get('discountEndTime')) and end is None
        free=discount in ('FREE','_2X_FREE','2X_FREE') and not invalid_end
        intrinsic_free, intrinsic_end = free, end if free else None
        promotion_end=end if free else None
        personal_end = None
        mall=status.get('mallSingleFree') or {}
        if isinstance(mall,dict) and mall.get('status')=='ONGOING':
            mall_end=timestamp(mall.get('endDate'))
            mall_start=timestamp(mall.get('startDate'))
            now=time.time()
            if mall_end and mall_end>now and (mall_start is None or mall_start<=now):
                if not free or (end is not None and mall_end>end):
                    end=mall_end
                personal_end=mall_end
                promotion_end=max(promotion_end or 0,mall_end)
                free=True
        return {'id':tid,'name':str(raw.get('name') or raw.get('originFileName') or tid)[:500],
                'size':size,'seeders':int(status.get('seeders') or 0),
                'created':timestamp(raw.get('createdDate')),
                'free':free,'free_end':end,'promotion_end':promotion_end,'hr':hr_flag(raw,status),
                'intrinsic_free':intrinsic_free,'intrinsic_end':intrinsic_end,'personal_end':personal_end,
                'visible':status.get('visible',True) is not False,
                'banned':bool(status.get('banned',False))}
    except (KeyError,TypeError,ValueError,OverflowError):
        raise AppError('种子元数据缺失或格式异常，已跳过','protocol') from None


def matches(t,c,n=None):
    n=time.time() if n is None else n
    if not t['visible'] or t['banned']:
        return False,'不可见或已封禁'
    if c['free_only'] and not t['free']:
        return False,'非确认免费种子'
    if c['free_only'] and t['free_end'] is not None and (
        t['free_end'] <= n or t['free_end']-n < c['min_free_minutes']*60
    ):
        return False,'免费剩余时间不足或已过期'
    gib=t['size']/1024**3
    if gib<c['min_gib'] or (c['max_gib'] and gib>c['max_gib']):
        return False,'体积不符合条件'
    if t['seeders']<c['min_seeders'] or (c['max_seeders'] and t['seeders']>c['max_seeders']):
        return False,'做种人数不符合条件'
    age_exceeded = c['max_age_hours'] and (
        t['created'] is None or n-t['created']>c['max_age_hours']*3600 or t['created']>n+300
    )
    if age_exceeded and not (t['free'] and (t.get('promotion_end') or 0)>n):
        return False,'发布时间缺失或超出范围'
    if t['hr'] is True:
        return False,'带有 HR 标记'
    name=t['name'].casefold()
    include=[x.strip().casefold() for x in c['include'].splitlines() if x.strip()]
    exclude=[x.strip().casefold() for x in c['exclude'].splitlines() if x.strip()]
    if include and not any(x in name for x in include):
        return False,'未命中白名单'
    if any(x in name for x in exclude):
        return False,'命中黑名单'
    return True,'符合条件（限时免费）' if age_exceeded else '符合条件'
