"""Userland crash supervisor. No privileged service installation or trading actions."""
import argparse
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main():
    parser=argparse.ArgumentParser(description='Keep a BASIS collector running; stop with SIGTERM or Ctrl-C')
    parser.add_argument('--db',default='data/basis.sqlite3')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--config')
    parser.add_argument('--no-collect',action='store_true')
    args=parser.parse_args()
    Path(args.db).parent.mkdir(parents=True,exist_ok=True)
    lock=open(args.db+'.supervisor.lock','a')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('A supervisor already owns this tape')
    child=None
    stopping=False
    def report(message):
        print(datetime.now(timezone.utc).isoformat(timespec='seconds'),'BASIS supervisor',message,flush=True)
    def stop(signum,frame):
        nonlocal stopping
        stopping=True
        if child and child.poll() is None:
            child.terminate()
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    command=[sys.executable,'-m','basislab','--db',args.db,'serve','--port',str(args.port)]
    if args.config: command+=['--config',args.config]
    if args.no_collect: command+=['--no-collect']
    backoff=1
    try:
        while not stopping:
            started=time.monotonic()
            child=subprocess.Popen(command,env=dict(os.environ,OPENBLAS_NUM_THREADS='1'))
            report(f'child={child.pid} started')
            while child.poll() is None and not stopping:
                time.sleep(.5)
            if stopping:
                try: child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    report('shutdown deadline exceeded; terminating owned child')
                    child.kill();child.wait()
                break
            report(f'child={child.pid} exited code={child.returncode}; restart in {backoff}s')
            until=time.monotonic()+backoff
            while not stopping and time.monotonic()<until:
                time.sleep(min(.5,max(0,until-time.monotonic())))
            backoff=1 if time.monotonic()-started>300 else min(60,backoff*2)
    finally:
        report('stopped')
        lock.close()


if __name__=='__main__':
    main()
