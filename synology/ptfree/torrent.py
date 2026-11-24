# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib

from .common import AppError


def inspect_torrent(data, include_size=False):
    # Hash the exact original info slice, never a re-encoded dictionary.
    pos=0; nodes=0; info_slice=None
    def read(depth=0):
        nonlocal pos,nodes,info_slice
        nodes+=1
        if depth>64 or nodes>300000 or pos>=len(data):
            raise ValueError()
        c=data[pos:pos+1]
        if c==b'i':
            end=data.index(b'e',pos); v=int(data[pos+1:end]); pos=end+1; return v
        if c in (b'd',b'l'):
            pos+=1; obj={} if c==b'd' else []
            while data[pos:pos+1]!=b'e':
                if c==b'd':
                    k=read(depth+1)
                    if not isinstance(k,bytes) or k in obj:
                        raise ValueError()
                    start=pos; v=read(depth+1); obj[k]=v
                    if depth==0 and k==b'info':
                        info_slice=data[start:pos]
                else:
                    obj.append(read(depth+1))
            pos+=1; return obj
        colon=data.index(b':',pos)
        size=int(data[pos:colon])
        if size<0 or colon+1+size>len(data):
            raise ValueError()
        pos=colon+1; v=data[pos:pos+size]; pos+=size; return v
    try:
        if not data or len(data)>16*1024*1024:
            raise ValueError()
        root=read()
        info=root[b'info']
        if pos!=len(data) or not isinstance(info,dict) or not info.get(b'name') or not info_slice:
            raise ValueError()
        if not isinstance(info.get(b'piece length'),int) or info[b'piece length']<=0:
            raise ValueError()
        hashes=[]
        if b'pieces' in info:
            if not isinstance(info[b'pieces'],bytes) or len(info[b'pieces'])%20:
                raise ValueError()
            if b'length' not in info and b'files' not in info:
                raise ValueError()
            hashes.append(hashlib.sha1(info_slice).hexdigest())
        if info.get(b'meta version')==2 and b'file tree' in info:
            hashes.append(hashlib.sha256(info_slice).hexdigest())
        if not hashes:
            raise ValueError()
        if not include_size:
            return hashes
        def files_v2(tree):
            for key, child in tree.items():
                if key == b'':
                    yield child[b'length']
                else:
                    yield from files_v2(child)
        if b'length' in info:
            lengths = [info[b'length']]
        elif b'files' in info:
            lengths = [file[b'length'] for file in info[b'files']]
        else:
            lengths = list(files_v2(info[b'file tree']))
        if not lengths or any(not isinstance(n,int) or n<0 for n in lengths):
            raise ValueError()
        return hashes, sum(lengths)
    except (AttributeError,KeyError,ValueError,TypeError,IndexError,RecursionError,OverflowError):
        raise AppError('下载内容不是有效的 BitTorrent 种子文件（可能返回登录页或错误页）','protocol') from None
