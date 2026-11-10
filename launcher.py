"""Foreground owner of the instance lock, child processes, and crash recovery."""
import argparse
import ctypes
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

from core import atomic_json

ROOT = Path(__file__).resolve().parent
RUN = Path('/run/nura-tests')
LOCK_ENV = 'NURA_TEST_LOCK_FD'
PARENT_ENV = 'NURA_TEST_PARENT_PID'
# Recovery journals written by app.py, actions.py and external_display.py.
JOURNALS = ('console.json', 'display-restore.json', 'restore.json', 'mixer-restore.json',
            'bluetooth-restore.json', 'nfc-restore.json', 'camera-ois-restore.json')


class AlreadyRunning(RuntimeError):
    pass


class Instance:
    def __init__(self, runtime=RUN):
        self.runtime = Path(runtime)
        self.fd = None

    def __enter__(self):
        self.runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fd = os.open(self.runtime / 'instance.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise AlreadyRunning('nura-tests is already running; Close it or use nura-tests --stop.') from None
        return self

    def __exit__(self, *_):
        # Do not unlink or explicitly unlock: surviving children share this lock.
        os.close(self.fd)


def prctl(option, value):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(option, ctypes.c_ulong(value), 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def managed_options():
    """Pass the lock to hardware workers and commands until they have exited."""
    value = os.environ.get(LOCK_ENV)
    if value is None:
        return {}
    fd = int(value)
    os.fstat(fd)
    return {'pass_fds': (fd,)}


def arm_parent_death():
    """Called after exec, before touching hardware; also closes the fork/exec race."""
    if LOCK_ENV not in os.environ:
        return
    managed_options()
    parent = int(os.environ[PARENT_ENV])
    prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG; retained across ordinary exec.
    if os.getppid() != parent:
        raise InterruptedError('The launcher exited before startup completed')


def process_start(pid):
    try:
        # comm may contain spaces and parentheses; fields after its closing ')' start at 3.
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (OSError, IndexError):
        return None


def children():
    return [int(pid) for pid in Path(f'/proc/self/task/{os.getpid()}/children').read_text().split()]


def stop_children(grace=3):
    """The subreaper adopts orphaned workers, including ones in other sessions."""
    deadline = time.monotonic() + grace
    killed = False
    sent = set()
    while True:
        while True:
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break
        pending = children()
        if not pending:
            return
        if time.monotonic() >= deadline:
            if killed:
                raise RuntimeError('Some test processes did not exit; their instance lock remains held')
            killed = True
            deadline = time.monotonic() + grace
            sent.clear()
        sig = signal.SIGKILL if killed else signal.SIGTERM
        for pid in pending:
            if pid in sent:
                continue
            try:
                # A direct, unreaped child cannot have its PID reused here.
                if os.getpgid(pid) == pid:
                    os.killpg(pid, sig)
                else:
                    os.kill(pid, sig)
            except ProcessLookupError:
                pass
            sent.add(pid)
        time.sleep(.03)


def request_stop(runtime=RUN, timeout=40):
    runtime = Path(runtime)
    try:
        with Instance(runtime):
            print('nura-tests is not running.')
            return 0
    except AlreadyRunning:
        pass
    try:
        owner = json.loads((runtime / 'instance.json').read_text())
        delivered = False
        for key in ('launcher', 'app'):
            identity = owner.get(key, {})
            pid = identity.get('pid')
            if pid is None:
                continue
            try:
                # Pin the process before checking its start time, avoiding PID reuse.
                fd = os.pidfd_open(pid)
                try:
                    if identity.get('start') is not None and process_start(pid) == identity['start']:
                        signal.pidfd_send_signal(fd, signal.SIGTERM)
                        delivered = True
                        break
                finally:
                    os.close(fd)
            except ProcessLookupError:
                continue
        if not delivered:
            raise RuntimeError('The launcher has exited; a surviving child still holds the lock')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with Instance(runtime):
                    print('nura-tests stopped.')
                    return 0
            except AlreadyRunning:
                time.sleep(.05)
        raise RuntimeError('nura-tests is still cleaning up; the instance lock remains held')
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print(f'Could not stop nura-tests: {error}', file=sys.stderr)
        return 1


def discard_journals(runtime=RUN):
    """Give up on a restoration that cannot succeed, so the next launch can start."""
    runtime = Path(runtime)
    try:
        with Instance(runtime):
            pending = [runtime / name for name in JOURNALS if (runtime / name).exists()]
            if not pending:
                print('No unfinished restoration to discard.')
                return 0
            print('Discarding unfinished restoration. These saved settings were NOT restored:')
            for path in pending:
                print(f'--- {path.name}\n{path.read_text().rstrip()}')
                path.unlink()
            return 0
    except (AlreadyRunning, OSError) as error:
        print(f'Could not discard the recovery journal: {error}', file=sys.stderr)
        return 1


def supervise(command, recovery, runtime=RUN, grace=8):
    """Keep the invoking shell occupied through UI exit, child reaping and recovery."""
    runtime = Path(runtime)
    parent = os.getppid()
    with Instance(runtime) as instance:
        prctl(36, 1)  # PR_SET_CHILD_SUBREAPER
        owner = {'launcher': {'pid': os.getpid(), 'start': process_start(os.getpid())}}
        atomic_json(runtime / 'instance.json', owner)
        env = {**os.environ, LOCK_ENV: str(instance.fd), PARENT_ENV: str(os.getpid()),
               'PYTHONUNBUFFERED': '1'}
        options = dict(env=env, pass_fds=(instance.fd,), start_new_session=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        interrupted = 0
        handlers = {}
        output_available = True
        status = 1
        proc = None

        def interrupt(sig, _):
            nonlocal interrupted
            if not interrupted:
                interrupted = sig

        with (runtime / 'launcher.log').open('ab', buffering=0) as log:
            def emit(data):
                nonlocal output_available
                log.write(data)
                if output_available:
                    try:
                        sys.stdout.buffer.write(data)
                        sys.stdout.buffer.flush()
                    except (BrokenPipeError, OSError):
                        output_available = False

            def run_recovery():
                p = subprocess.Popen(recovery, **options)
                try:
                    data = p.communicate(timeout=30)[0]
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    data = p.communicate()[0]
                    emit(b'Recovery timed out; journals retained for the next launch.\n')
                emit(data)
                stop_children(grace=min(grace,3))
                return p.returncode

            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                handlers[sig] = signal.signal(sig, interrupt)
            # Also covers non-interactive SSH, where no controlling tty sends HUP.
            prctl(1, signal.SIGHUP)
            if os.getppid() != parent:
                interrupted = signal.SIGHUP
            emit(f'\nStarting nura-tests (PID {os.getpid()})\n'.encode())
            try:
                # Recover an earlier interrupted invocation before opening the devices.
                if run_recovery() != 0:
                    raise RuntimeError('Recovery failed; see /run/nura-tests/launcher.log. If it cannot '
                                       'succeed, nura-tests --discard-journal abandons it.')
                if command and not interrupted:
                    proc = subprocess.Popen(command, **options)
                    owner['app'] = {'pid': proc.pid, 'start': process_start(proc.pid)}
                    atomic_json(runtime / 'instance.json', owner)
                    stopping = None
                    while proc.poll() is None:
                        if interrupted and stopping is None:
                            proc.terminate()
                            stopping = time.monotonic()
                        if stopping is not None and time.monotonic() - stopping > grace:
                            proc.kill()
                        if select.select([proc.stdout], [], [], .05)[0]:
                            data = os.read(proc.stdout.fileno(), 65536)
                            if data:
                                emit(data)
                    status = proc.wait()
                    # Stop/reap descendants before recovery; they may still hold DRM or audio.
                    stop_children(grace=min(grace,3))
                    emit(proc.stdout.read())
                    proc.stdout.close()
                else:
                    status = 0
            except Exception as error:
                emit(f'nura-tests: {error}\n'.encode())
                status = 1
            finally:
                try:
                    if proc and proc.poll() is None:
                        proc.kill()
                        proc.wait()
                    stop_children(grace=min(grace,3))
                    if run_recovery() != 0:
                        status = 1
                except Exception as error:
                    emit(f'Cleanup failed: {error}\n'.encode())
                    status = 1
                if proc and proc.stdout:
                    proc.stdout.close()
                (runtime / 'instance.json').unlink(missing_ok=True)
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
        return 128 + interrupted if interrupted else 128 - status if status < 0 else status


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run Nura hardware checks in the foreground.')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--stop', action='store_true', help='stop the running instance and wait for cleanup')
    mode.add_argument('--restore', action='store_true', help='recover interrupted changes without opening the UI')
    mode.add_argument('--discard-journal', action='store_true',
                      help='abandon a restoration that keeps failing, printing what was not restored')
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.exit(1, 'Run nura-tests as root.\n')
    if args.stop:
        return request_stop()
    if args.discard_journal:
        return discard_journals()
    try:
        command = [sys.executable, '-u', str(ROOT / 'app.py'), '--managed']
        return supervise(None if args.restore else command, command + ['--restore'])
    except (OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
