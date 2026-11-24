# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import datetime
import email.utils
import http.client
import json
import re
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urljoin, urlsplit

from .common import AppError, REQUEST_LABELS


def site_host(host):
    return bool(host) and any(host == x or host.endswith('.'+x) for x in ('m-team.cc','m-team.io'))


def resolve_site_download_url(value, base_url, stage='种子下载链接'):
    if not isinstance(value, str) or not value.strip():
        raise AppError(f'{stage}为空或格式无效', 'protocol')
    value = value.strip()
    if '\\' in value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise AppError(f'{stage}含有非法字符', 'protocol')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme or value.startswith('//')) and not parsed.netloc:
            raise ValueError('missing host')
        target = urlsplit(urljoin(base_url, value))
        port = target.port
    except ValueError:
        raise AppError(f'{stage}格式或端口无效', 'protocol') from None

    if target.scheme not in ('http', 'https') or not target.hostname or port == 0:
        raise AppError(f'{stage}不是有效的 HTTP 或 HTTPS 地址', 'protocol')
    if target.username is not None or target.password is not None:
        raise AppError(f'{stage}包含用户名或密码，已拦截', 'protocol')
    return target._replace(fragment='').geturl()


def connect_address(address, timeout, source_address=None, network='system', **kwargs):
    host, port = address
    family = socket.AF_INET if network == 'ipv4_only' else socket.AF_UNSPEC
    addresses = socket.getaddrinfo(host,port,family,socket.SOCK_STREAM)
    if network == 'ipv4_prefer':
        addresses.sort(key=lambda a: a[0] != socket.AF_INET)
    deadline = time.monotonic()+timeout
    last = None
    for af, socktype, proto, _, sa in addresses:
        remaining=deadline-time.monotonic()
        if remaining <= 0:
            break
        sock=socket.socket(af,socktype,proto)
        try:
            # A broken IPv4 address must leave time for other addresses / IPv6.
            sock.settimeout(min(remaining,5))
            if source_address:
                sock.bind(source_address)
            sock.connect(sa)
            sock.settimeout(max(1,deadline-time.monotonic()))
            return sock
        except OSError as e:
            last=e; sock.close()
    raise last or TimeoutError('connection deadline')


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes


class Transport:
    def __init__(self, config):
        self.config=config

    def request(self, method, url, body=None, headers=None, redirects=0, site=False):
        p=urlsplit(url)
        if p.scheme not in ('http','https') or not p.hostname or p.username is not None or p.password is not None:
            raise AppError('服务器返回了不支持的地址', 'protocol')
        if site and (p.scheme!='https' or not site_host(p.hostname) or p.port not in (None,443)):
            raise AppError('认证 API 地址不属于 M-Team HTTPS 域名，已停止请求', 'protocol')
        timeout=self.config['timeout']
        conn=(http.client.HTTPSConnection(p.hostname,p.port,timeout=timeout,context=ssl.create_default_context())
              if p.scheme=='https' else http.client.HTTPConnection(p.hostname,p.port,timeout=timeout))
        conn._create_connection=lambda address, timeout, source_address=None, **kw: connect_address(address,timeout,source_address,self.config['network'])
        hdr={'User-Agent':'MTFree/1.0 (+local unattended downloader)','Accept-Encoding':'identity', 'Connection':'close'}
        hdr.update(headers or {})
        try:
            conn.request(method,(p.path or '/')+('?' + p.query if p.query else ''),body=body,headers=hdr)
            res=conn.getresponse()
            rh={k.lower():v for k,v in res.getheaders()}
            if res.status in (301,302,303,307,308):
                if redirects <= 0 or method != 'GET' or not rh.get('location'):
                    raise AppError('接口发生重定向，请检查 API 域名或网络', 'protocol')
                new_url = resolve_site_download_url(rh['location'], url, '种子下载跳转')
                # Keep credentials on the original request.
                conn.close()
                return self.request('GET',new_url,redirects=redirects-1,site=site)
            size=int(rh.get('content-length','0'))
            if size > 16*1024*1024:
                raise AppError('响应文件超过 16 MiB 限制','protocol')
            content=bytearray()
            deadline=time.monotonic()+timeout
            while True:
                if time.monotonic()>deadline:
                    raise AppError('响应读取超时','temporary')
                chunk=res.read1(65536)
                if not chunk:
                    break
                content.extend(chunk)
                if len(content)>16*1024*1024:
                    raise AppError('响应文件超过 16 MiB 限制','protocol')
            return Response(res.status,rh,bytes(content))
        except AppError:
            raise
        except (OSError,http.client.HTTPException,ValueError):
            raise AppError('网络请求失败或超时，请检查 DNS、IPv4/IPv6 与 TLS 连接','temporary') from None
        finally:
            conn.close()


def retry_after(value):
    try:
        if isinstance(value,str) and re.fullmatch(r'[0-9]{1,9}',value.strip()):
            return max(1,int(value))
        return max(1,email.utils.parsedate_to_datetime(value).timestamp()-time.time())
    except (TypeError,ValueError,AttributeError,OverflowError,OSError):
        return 900


def request_error(stage, message, kind='protocol', *, status=None, code=None, wait=0, source=None):
    diagnostic = [REQUEST_LABELS[stage]]
    if status is not None:
        diagnostic.append(f'HTTP {status}')
    if code:
        diagnostic.append(f'业务码 {code}')
    return AppError(f'{message}（{" · ".join(diagnostic)}）', kind, wait,
                    stage=stage, status=status, code=code, source=source)


class MTeam:
    def __init__(self, config, store, transport=None):
        self.c=config; self.db=store; self.http=transport or Transport(config)

    def send(self, stage, *args, **kwargs):
        try:
            return self.http.request(*args, **kwargs)
        except AppError as error:
            raise request_error(stage, str(error), error.kind) from None

    def rate_error(self, stage, response, code=None):
        source = 'http_429' if response.status == 429 else 'site_rate'
        wait = retry_after(response.headers.get('retry-after'))
        self.db.cooldown(wait, source, stage)
        return request_error(stage, 'M-Team 接口限流', 'rate',
                             status=response.status, code=code, wait=wait, source=source)

    def api(self, endpoint, payload, kind, form=False):
        if not self.c['api_key']:
            raise AppError('尚未填写 M-Team API Access Token','auth')
        if kind!='profile' and self.db.get('auth_error',False):
            raise AppError('认证异常，等待更新令牌或认证测试通过','auth')
        self.db.reserve_request(kind)
        if self.db.get('blocked_until',0)>time.time():
            raise AppError('站点请求已进入冷却期','rate',self.db.get('blocked_until')-time.time(),source='active')
        body=urlencode(payload).encode() if form else json.dumps(payload).encode()
        r=self.send(kind,'POST',self.c['api_base']+'/'+endpoint,body,
                            {'x-api-key':self.c['api_key'],
                             'Content-Type':'application/x-www-form-urlencoded' if form else 'application/json'},site=True)
        if r.status in (401,403):
            raise request_error(kind, 'M-Team 拒绝认证，请检查 API 令牌及访问权限', 'auth', status=r.status)
        if r.status==429:
            raise self.rate_error(kind,r)
        if r.status!=200:
            raise request_error(kind, 'M-Team 请求失败', 'temporary', status=r.status, source='site')
        try:
            data=json.loads(r.body)
            if not isinstance(data,dict):
                raise ValueError()
        except (ValueError,UnicodeError):
            raise request_error(kind, 'M-Team 返回了非 JSON 内容', status=r.status, source='site') from None
        code=str(data.get('code','')).upper()
        if code not in ('0','SUCCESS'):
            if code in ('401','403','UNAUTHORIZED','FORBIDDEN','INVALID_API_KEY','TOKEN_EXPIRED'):
                raise request_error(kind, 'API 令牌无效、失效或权限不足', 'auth', status=r.status, code=code)
            if code in ('429','TOO_MANY_REQUESTS','RATE_LIMIT'):
                raise self.rate_error(kind,r,code)
            # Log only bounded numeric or symbolic business codes, never the raw message.
            safe_code = re.fullmatch(r'(?:-?[0-9]{1,10}|[A-Z]{1,16}(?:_[A-Z0-9]{1,16}){1,3}|FAIL|FAILED|ERROR)',code)
            logged_code = code if safe_code and self.c['api_key'].upper() not in code else None
            raise request_error(kind, 'M-Team 返回业务错误', status=r.status,
                                code=logged_code, source='site')
        return data.get('data')

    def profile(self):
        p=self.api('member/profile',{},'profile')
        if not isinstance(p,dict):
            raise AppError('用户信息响应格式已变化','protocol')
        return {'message':'M-Team 认证成功','username':str(p.get('username','已认证'))[:100]}

    def search(self, mode=None, page=1, sort='CREATED_DATE', *, discount=None, categories=(), page_size=None):
        payload = {'mode': mode or self.c['mode'], 'categories': list(categories), 'visible': 1,
                   'pageNumber': page, 'pageSize': page_size or self.c['page_size'],
                   'sortField': sort, 'sortDirection': 'DESC'}
        if discount:
            payload['discount'] = discount
        p = self.api('torrent/search', payload, 'search')
        rows=p.get('data') if isinstance(p,dict) else None
        if not isinstance(rows,list) or any(not isinstance(x,dict) for x in rows):
            raise AppError('种子列表结构已变化，已停止本轮下载','protocol')
        return rows

    def detail(self, tid):
        p=self.api('torrent/detail',{'id':tid},'detail',form=True)
        if not isinstance(p,dict) or str(p.get('id'))!=str(tid):
            raise request_error('detail', '种子详情结构不正确', status=200, source='site')
        return p

    def download_history(self, torrent_ids):
        tids = list(dict.fromkeys(torrent_ids))
        if not tids:
            return {}
        data = self.api('tracker/queryHistory', {'tids': tids}, 'history')
        if not isinstance(data, dict):
            raise AppError('站点下载历史响应格式不正确', 'protocol')
        history = data.get('historyMap')
        peers = data.get('peerMap')
        if not isinstance(history, dict) or not isinstance(peers, dict):
            raise AppError('站点下载历史缺少历史或活跃记录表', 'protocol')

        existing = {}
        for tid in tids:
            for records in (history, peers):
                if tid in records and not isinstance(records[tid], dict):
                    raise AppError('站点返回的种子历史记录无效', 'protocol')
            if tid in peers:
                existing[tid] = (
                    '站点显示正在做种' if str(peers[tid].get('left')) == '0'
                    else '站点已有活跃下载或做种任务'
                )
            elif tid in history:
                completed = str(history[tid].get('timesCompleted', '0'))
                existing[tid] = (
                    '站点显示曾经完成下载' if completed.isdigit() and completed.strip('0')
                    else '站点已有下载历史'
                )
        return existing

    def download(self,tid):
        endpoint = 'torrent/genDlToken'
        link = self.api(endpoint, {'id': tid}, 'token', form=True)
        try:
            url = resolve_site_download_url(link, f"{self.c['api_base']}/{endpoint}")
        except AppError as error:
            raise request_error('token', str(error), status=200, source='site') from None
        self.db.reserve_request('download')
        r=self.send('download','GET',url,redirects=3)
        if r.status==429:
            raise self.rate_error('download',r)
        if r.status!=200:
            raise request_error('download', '种子文件下载失败', 'temporary', status=r.status, source='site')
        return r.body
