"""Local test artifacts: /tmp/mqa/YYYY-MM-DD/NNN/."""
from datetime import datetime
from pathlib import Path
import shutil, subprocess
from core import now

ROOT = Path('/tmp/mqa')


def ensure_run(data, root=ROOT):
    root = Path(root)
    previous = data.get('output_directory')
    if previous and Path(previous).is_dir():
        return Path(previous)
    day = root / datetime.now().astimezone().strftime('%Y-%m-%d')
    day.mkdir(parents=True, exist_ok=True)
    number = max((int(p.name) for p in day.iterdir() if p.is_dir() and p.name.isdigit()), default=0) + 1
    while True:
        directory = day / f'{number:03d}'
        try:
            directory.mkdir()
            break
        except FileExistsError:
            number += 1
    (directory / 'photos').mkdir()
    (directory / 'logs').mkdir()
    data['output_directory'] = str(directory)
    if previous:
        data['previous_output_directory'] = previous
    return directory


def collect_logs(directory, initial=False):
    directory = Path(directory)
    commands = {
        'dmesg-start.log' if initial else 'dmesg.log': ['dmesg'],
        'logs/services.log': ['journalctl', '-b', '-u', 'iio-sensor-proxy.service',
                              '-u', 'sdx55-gnss.service', '--no-pager', '-o', 'short-monotonic'],
    }
    result = {'captured': now(), 'files': {}}
    launcher_log=Path('/run/nura-tests/launcher.log')
    if launcher_log.exists():
        try:shutil.copyfile(launcher_log,directory/'logs/app.log')
        except OSError as error:result['files']['logs/app.log']={'error':str(error)}
        else:result['files']['logs/app.log']={'copied':True}
    for name, command in commands.items():
        target = directory / name
        try:
            with target.open('w') as log:
                completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=6)
            result['files'][name] = {'returncode': completed.returncode}
        except (OSError, subprocess.TimeoutExpired) as error:
            result['files'][name] = {'error': str(error)}
    return result
