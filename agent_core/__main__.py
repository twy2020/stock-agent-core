import argparse
import atexit
import os
from pathlib import Path
from waitress import serve
from .app import create_app
from .config import load_config


def lock_process(path):
    """Prevent two worker processes recovering/executing the same SQLite database."""
    lock=open(str(path)+'.lock','a+b');lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        lock.close();raise SystemExit('Another Core process already owns this database.') from None
    return lock


def main():
    parser=argparse.ArgumentParser(description='Independent Agent Core (one process per SQLite database)')
    parser.add_argument('--config',default=os.getenv('AGENT_CORE_CONFIG','config.json'))
    parser.add_argument('--host',default=None);parser.add_argument('--port',type=int,default=None)
    args=parser.parse_args();config=load_config(args.config)
    Path(config['database']).parent.mkdir(parents=True,exist_ok=True)
    process_lock=lock_process(config['database']);atexit.register(process_lock.close)
    app=create_app(config,config_path=args.config);atexit.register(app.extensions['core_worker'].close)
    serve(app,host=args.host or config.get('host','127.0.0.1'),port=args.port or config.get('port',18200),threads=16,
          max_request_body_size=48*1024*1024,channel_timeout=360)


if __name__=='__main__':main()
