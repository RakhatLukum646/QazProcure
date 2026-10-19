"""Container startup: read mounted secrets after image build and exec one service."""
import os
from pathlib import Path
import sys
from urllib.parse import quote


def secret(name):
    path = Path('/run/secrets') / name
    value = path.read_text(encoding='utf-8').strip()
    if not value:
        raise SystemExit(f'Secret {name} is empty')
    return value


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ('api', 'worker'):
        raise SystemExit('Expected api or worker')
    env = os.environ.copy()
    password = quote(secret('postgres_password'), safe='')
    env['DATABASE_URL'] = f'postgresql+psycopg://qazprocure:{password}@db:5432/qazprocure'
    if sys.argv[1] == 'worker':
        env['OWS_TOKEN'] = secret('ows_token')
        command = [sys.executable, '-m', 'app.cli', 'worker']
    else:
        command = [sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8765']
    os.execvpe(command[0], command, env)


if __name__ == '__main__':
    main()
