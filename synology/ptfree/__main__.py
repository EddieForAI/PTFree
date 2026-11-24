# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import argparse
import getpass
import os
import signal
import subprocess
import sys
import time

from . import __version__
from .common import AppError, SHUTDOWN_EXIT_CODE, WINDOWS_NO_CONSOLE, data_home, safe_error, launch_args
from .settings import Settings
from .store import Store


def lock(home):
    handle=open(home/'instance.lock','a+b')
    if os.name=='nt':
        import msvcrt
        handle.seek(0); handle.write(b'0'); handle.flush(); handle.seek(0)
        msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
    else:
        import fcntl
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    return handle


def supervise(home,host,port):
    from .server import terminate
    instance=lock(home)
    settings=Settings(home); db=Store(home); db.init()
    password_file=home/'初始管理密码.txt'
    print(f'PTFree {__version__} | http://{host}:{port}',flush=True)
    print('Data: '+str(home),flush=True)
    if password_file.exists():
        print('初始管理密码保存在：'+str(password_file),flush=True)
    stop=False
    def stopping(*args):
        nonlocal stop
        stop=True
    signal.signal(signal.SIGTERM,stopping); signal.signal(signal.SIGINT,stopping)
    child=None; crashes=0; winjob=None
    try:
        while not stop:
            heartbeat=home/'heartbeat.json'; heartbeat.unlink(missing_ok=True)
            if os.name == 'nt':
                flags = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | WINDOWS_NO_CONSOLE}
            else:
                flags = {'start_new_session': True}
            child=subprocess.Popen(launch_args('--data',home,'--host',host,'--port',port,'serve'),**flags)
            if os.name=='nt':
                from .winjob import WindowsJob
                winjob=WindowsJob(child)
            started=time.time(); lastcheck=started
            while child.poll() is None and not stop:
                time.sleep(0.5)
                if time.time()-lastcheck<3:
                    continue
                lastcheck=time.time()
                last=heartbeat.stat().st_mtime if heartbeat.exists() else started
                if time.time()-last>45:
                    db.event('网页服务心跳中断，看门狗正在重启服务','error')
                    terminate(child,tree=True); break
            requested_shutdown = child.poll() == SHUTDOWN_EXIT_CODE
            terminate(child,tree=True)
            child = None
            if winjob:
                winjob.close(); winjob=None
            if stop or requested_shutdown:
                break
            db.put('supervisor_restarts',db.get('supervisor_restarts',0)+1)
            crashes=crashes+1 if time.time()-started<60 else 1
            db.event('服务进程退出，正在自动恢复','warning')
            wait=min(60,2**min(crashes,6))
            until=time.monotonic()+wait
            while not stop and time.monotonic()<until:
                time.sleep(0.5)
    finally:
        terminate(child,tree=True)
        if winjob:
            winjob.close()
        instance.close()


def main():
    parser=argparse.ArgumentParser(description='PTFree · PT小管家')
    parser.add_argument('--data'); parser.add_argument('--host',default='127.0.0.1'); parser.add_argument('--port',type=int,default=os.environ.get('MTFREE_WEB_PORT', '8787'))
    parser.add_argument('command',nargs='?',default='run',choices=['run','serve','worker','init','reset-password'])
    parser.add_argument('job',nargs='?')
    args=parser.parse_args(); home=data_home(args.data)
    if args.command=='worker':
        from .worker import run_job
        return run_job(home,args.job)
    if args.command=='serve':
        from .server import serve
        return serve(home,args.host,args.port)
    if args.command=='reset-password':
        settings=Settings(home)
        a=getpass.getpass('New admin password (12+ chars): ')
        b=getpass.getpass('Repeat password: ')
        if a!=b:
            raise AppError('两次输入的密码不同','config')
        settings.set_password(a); print('管理密码已重置'); return 0
    if args.command=='init':
        Settings(home); Store(home).init(); print('初始化完成：'+str(home)); return 0
    supervise(home,args.host,args.port)
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (AppError,OSError) as exc:
        print(safe_error(exc),file=sys.stderr)
        raise SystemExit(1)
