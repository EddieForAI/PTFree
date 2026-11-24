# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import secrets
import ssl
from http.cookies import SimpleCookie
from urllib.parse import urlencode, urlsplit

from .common import AppError

MAX_REPLY = 32 * 1024 * 1024


def endpoint(url):
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or port == 0 or parsed.netloc.endswith(':')
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(ord(char) <= 32 or char == '\\' for char in url)):
            raise ValueError()
        return parsed
    except (ValueError, TypeError):
        raise AppError('qB 地址必须是 WebUI 的 HTTP 或 HTTPS 地址', 'config') from None


QB_FIELDS = {'qb_url', 'qb_auth', 'qb_user', 'qb_password', 'qb_api_key'}


def validate_connection(config):
    for name in QB_FIELDS:
        value = config[name]
        if not isinstance(value, str) or len(value) > 2048 or any(ord(char) < 32 for char in value):
            raise AppError('qB 连接参数格式不正确', 'config')
    if config['qb_auth'] not in ('password', 'api_key'):
        raise AppError('qB 认证方式无效', 'config')
    if config['qb_url']:
        endpoint(config['qb_url'])
        if config['qb_auth'] == 'api_key' and not re.fullmatch(r'qbt_[A-Za-z0-9]{28}', config['qb_api_key']):
            raise AppError('请粘贴 qB WebUI 中完整的 API 密钥（以 qbt_ 开头）', 'config')


def connection_args(config):
    if config['qb_auth'] == 'api_key':
        return config['qb_url'], '', '', config['qb_api_key']
    return config['qb_url'], config['qb_user'], config['qb_password'], ''


def local_endpoint(url):
    host = endpoint(url).hostname
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def tags(row):
    return {item.strip() for item in str(row.get('tags', '')).split(',')}


def hashes(row):
    return {str(row.get(key) or '').lower() for key in ('hash', 'infohash_v1', 'infohash_v2')} - {'', '0' * 40, '0' * 64}


def hash_keys(values):
    return {value[:40] for value in values if re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value)}


class QB:
    def __init__(self, url, user, password, api_key=""):
        self.url = endpoint(url)
        self.origin = f'{self.url.scheme}://{self.url.netloc}'
        self.user = user
        self.password = password
        self.api_key = api_key
        self.cookie = ''
        self.version = None
        self.preferences = None

    def _call(self, method, api, body=None, content_type=None):
        connection = http.client.HTTPSConnection if self.url.scheme == 'https' else http.client.HTTPConnection
        conn = connection(self.url.hostname, self.url.port, timeout=5)
        headers = {'Origin': self.origin, 'Referer': self.origin + self.url.path.rstrip('/') + '/'}
        if self.api_key:
            headers['Authorization'] = 'Bearer ' + self.api_key
        elif self.cookie:
            headers['Cookie'] = self.cookie
        if content_type:
            headers['Content-Type'] = content_type
        try:
            conn.request(method, self.url.path.rstrip('/') + '/api/v2/' + api, body, headers)
            response = conn.getresponse()
            data = response.read(MAX_REPLY + 1)
            if len(data) > MAX_REPLY:
                raise AppError('qB 响应超过大小限制', 'protocol')
            return response.status, data, response.getheader('Set-Cookie')
        except ssl.SSLCertVerificationError:
            raise AppError('qB HTTPS 证书验证失败，请核对证书信任和地址', 'qb') from None
        except ssl.SSLError:
            raise AppError('qB HTTPS 握手失败，请核对 qB WebUI 是否启用 HTTPS。未启用时请选择 http://', 'qb') from None
        except (OSError, http.client.HTTPException):
            raise AppError('无法连接 qB WebUI，请检查 qB 是否运行，以及协议、监听地址和端口', 'qb') from None
        finally:
            conn.close()

    def login(self):
        status, data, cookie = self._call('POST', 'auth/login', urlencode({
            'username': self.user, 'password': self.password,
        }).encode(), 'application/x-www-form-urlencoded')
        if status != 200 or data.strip() != b'Ok.':
            raise AppError('qB WebUI 登录失败，请检查用户名、密码和 WebUI 访问限制', 'qb')
        value = SimpleCookie(cookie or '')
        self.cookie = 'SID=' + value['SID'].value if 'SID' in value else ''

    def request(self, path, form=None, multipart=None):
        method = 'GET' if form is None and multipart is None else 'POST'
        body = urlencode(form).encode() if form is not None else None
        ctype = 'application/x-www-form-urlencoded' if form is not None else None
        if multipart:
            body, ctype = multipart
        status, data, _ = self._call(method, path, body, ctype)
        if status in (401, 403) and self.api_key:
            raise AppError('qB 拒绝 API 密钥，请核对密钥和 qB 版本（需要 5.2 或以上）', 'qb')
        if status in (401, 403):
            self.login()
            status, data, _ = self._call(method, path, body, ctype)
        if status != 200:
            raise AppError(f'qB 接口拒绝操作（HTTP {status}）', 'qb')
        return data

    def json(self, path, expected):
        try:
            value = json.loads(self.request(path))
            if not isinstance(value, expected):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise AppError('qB WebUI 返回了无法识别的数据', 'protocol') from None

    def connect(self):
        if self.version is None:
            version = self.request('app/version').decode('ascii', errors='replace').strip()
            match = re.match(r'^v?(\d+)\.(\d+)', version)
            if not match or tuple(map(int, match.groups())) < (4, 3):
                raise AppError('Freeseed 需要 qBittorrent 4.3 或以上版本', 'config')
            detected = tuple(map(int, match.groups()))
            if self.api_key and detected < (5, 2):
                raise AppError('qB API 密钥需要 5.2 或以上版本，请改用用户名和密码', 'config')
            self.version = detected
            self.version_text = version[:60]
        self.get_preferences()
        return self.version_text

    def get_preferences(self):
        self.preferences = self.json('app/preferences', dict)
        return self.preferences

    def snapshot(self):
        result = self.json('sync/maindata?rid=0', dict)
        torrents = result.get('torrents', {})
        server = result.get('server_state')
        if result.get('full_update') is not True or not isinstance(server, dict) or not isinstance(torrents, dict):
            raise AppError('qB 没有返回完整列表，暂缓同步', 'protocol')
        if any(not isinstance(value, dict) or not hash_keys([key]) for key, value in torrents.items()):
            raise AppError('qB 种子列表格式异常，暂缓同步', 'protocol')
        return {key: {**value, 'hash': key} for key, value in torrents.items()}, server

    def torrent_info(self, keys):
        if not keys:
            return {}
        values = self.json('torrents/info?' + urlencode({'hashes': '|'.join(sorted(keys))}), list)
        if any(not isinstance(row, dict) or not hash_keys([str(row.get('hash', ''))]) for row in values):
            raise AppError('qB 种子列表格式异常，暂缓同步', 'protocol')
        return {row['hash']: row for row in values}

    def add(self, data, path, run_tag, *, tag):
        fields = {
            'savepath': path, 'autoTMM': 'false', 'tags': tag + ',' + run_tag if run_tag else tag,
            'contentLayout': 'Subfolder',
        }
        fields['stopped' if self.version >= (5, 0) else 'paused'] = 'true'
        if self.version >= (5, 1):
            fields.update(useDownloadPath='true', downloadPath=path)
        boundary = 'MTFree-' + secrets.token_hex(16)
        chunks = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
            for key, value in fields.items()
        ]
        chunks.extend((
            f'--{boundary}\r\nContent-Disposition: form-data; name="torrents"; filename="mtfree.torrent"\r\n'
            'Content-Type: application/x-bittorrent\r\n\r\n'.encode(), data, f'\r\n--{boundary}--\r\n'.encode(),
        ))
        reply = self.request('torrents/add', multipart=(b''.join(chunks), f'multipart/form-data; boundary={boundary}'))
        if reply.strip() != b'Ok.':
            raise AppError('qB 未接受种子文件', 'qb')

    def action(self, action, torrent_hash, **fields):
        if not hash_keys([torrent_hash]):
            raise AppError('qB 任务哈希无效', 'protocol')
        verbs = {'start': 'start' if self.version >= (5, 0) else 'resume', 'delete': 'delete'}
        self.request('torrents/' + verbs[action], {'hashes': torrent_hash, **fields})
