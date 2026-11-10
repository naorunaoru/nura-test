"""Real process lifecycle tests use synthetic children, never phone hardware."""
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import launcher

FIXTURE = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
import launcher
role, mode, directory = sys.argv[1:]
root = Path(directory)
if role == 'caller':
    subprocess.Popen([sys.executable, __file__, 'guard', 'wait', directory])
    while not (root/'ready').exists(): time.sleep(.01)
    sys.exit(0)
if role == 'guard':
    command = [sys.executable, '-u', __file__, 'ui', mode, directory]
    recovery = [sys.executable, '-u', __file__, 'recover', mode, directory]
    sys.exit(launcher.supervise(command, recovery, root, grace=.6))
if role == 'recover':
    events = root/'recovery'
    count = len(events.read_text().splitlines()) if events.exists() else 0
    with events.open('a') as out: out.write('recovered\n')
    if mode == 'failed-recovery': sys.exit(5)
    if mode == 'slow-recovery' and count == 1:
        (root/'recovering').touch()
        while not (root/'release').exists(): time.sleep(.01)
    sys.exit(0)
launcher.arm_parent_death()
(root/(role+'.pid')).write_text(str(os.getpid()))
if role == 'grandchild':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True: time.sleep(.01)
if role == 'worker':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    subprocess.Popen([sys.executable, __file__, 'grandchild', mode, directory],
        start_new_session=True, env={**os.environ,launcher.PARENT_ENV:str(os.getpid())},
        **launcher.managed_options())
    while True: time.sleep(.01)
if mode == 'crash':
    subprocess.Popen([sys.executable, __file__, 'worker', mode, directory],
        start_new_session=True, env={**os.environ,launcher.PARENT_ENV:str(os.getpid())},
        **launcher.managed_options())
    while not (root/'grandchild.pid').exists(): time.sleep(.01)
    print('fixture traceback', flush=True)
    os.kill(os.getpid(), signal.SIGKILL)
if mode == 'hung':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (root/'ready').touch()
    while True: time.sleep(.01)
if mode in ('wait','parent-death'):
    stopping = False
    def stop(*_):
        global stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    (root/'ready').touch()
    while not stopping: time.sleep(.01)
    (root/'stopping').touch()
    time.sleep(.5 if mode == 'parent-death' else .15)
    (root/'stopped').touch()
    sys.exit(0)
print('fixture output', flush=True)
sys.exit(7 if mode == 'exit-code' else 0)
'''


class LockTests(unittest.TestCase):
    def test_lock_survives_owner_close_until_inheriting_child_exits(self):
        with tempfile.TemporaryDirectory() as d:
            with launcher.Instance(d) as lock:
                child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                         pass_fds=(lock.fd,))
            try:
                with self.assertRaises(launcher.AlreadyRunning):
                    with launcher.Instance(d): pass
            finally:
                child.terminate();child.wait()
            with launcher.Instance(d): pass
            self.assertTrue((Path(d)/'instance.lock').exists())

    def test_unused_console_is_selected_from_kernel(self):
        import app
        def ioctl(fd, request, value):
            self.assertEqual(request,0x5600)
            value[:]=struct.pack('i',3)
        with patch('app.os.open',return_value=99),patch('app.os.close'),patch('app.fcntl.ioctl',side_effect=ioctl):
            self.assertEqual(app.unused_vt(),3)

    def test_console_and_actions_restore_even_if_external_recovery_fails(self):
        import app
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'console.json').write_text(json.dumps({'vt':3,'previous_vt':2}))
            with patch.object(app,'RUN',root),patch('external_display.recover',side_effect=RuntimeError('display failed')),\
                 patch('actions.recover') as recover,patch('app.os.open',return_value=99) as opened,\
                 patch('app.os.close'),patch('app.fcntl.ioctl'),patch('app.Path',side_effect=lambda p:root/str(p).lstrip('/')):
                self.assertFalse(app.restore_console())
                recover.assert_called_once()
                self.assertEqual(opened.call_args.args[0],'/dev/tty3')
            self.assertFalse((root/'console.json').exists())


    def test_discard_journal_removes_pending_restores_and_prints_them(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'bluetooth-restore.json').write_text(json.dumps({'powered':False}))
            (root/'mixer-restore.json').write_text(json.dumps({'VA_DEC0 Volume':'84'}))
            (root/'launcher.log').write_text('kept')
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(launcher.discard_journals(root),0)
            self.assertFalse((root/'bluetooth-restore.json').exists())
            self.assertFalse((root/'mixer-restore.json').exists())
            self.assertEqual((root/'launcher.log').read_text(),'kept')
            # The saved values are the only record of what was not restored.
            self.assertIn('VA_DEC0 Volume',output.getvalue());self.assertIn('84',output.getvalue())

    def test_discard_journal_is_refused_while_an_instance_holds_the_lock(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'restore.json').write_text('{}')
            with launcher.Instance(root),contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(launcher.discard_journals(root),1)
            self.assertTrue((root/'restore.json').exists())


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux subreaper and pidfd integration')
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.script=self.root/'fixture.py';self.script.write_text(FIXTURE)
        self.env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parent),'PYTHONDONTWRITEBYTECODE':'1'}
        self.processes=[];self.addCleanup(self.cleanup)

    def cleanup(self):
        for proc in self.processes:
            if proc.poll() is None:
                proc.terminate()
                try:proc.wait(timeout=4)
                except subprocess.TimeoutExpired:proc.kill();proc.wait()
            if proc.stdout:proc.stdout.close()
        for name in ('ui','worker','grandchild'):
            path=self.root/(name+'.pid')
            if path.exists():
                try:
                    pid=int(path.read_text())
                    if str(self.script).encode() in Path(f'/proc/{pid}/cmdline').read_bytes():os.kill(pid,signal.SIGKILL)
                except (FileNotFoundError,ProcessLookupError):pass

    def start(self,mode):
        proc=subprocess.Popen([sys.executable,str(self.script),'guard',mode,str(self.root)],
                              env=self.env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        self.processes.append(proc);return proc

    def wait_file(self,name):
        deadline=time.monotonic()+5
        while not (self.root/name).exists() and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue((self.root/name).exists(),name)

    def test_exit_status_and_output_are_preserved(self):
        proc=self.start('exit-code');output=proc.communicate(timeout=6)[0]
        self.assertEqual(proc.returncode,7)
        self.assertIn(b'fixture output',output)
        self.assertIn('fixture output',(self.root/'launcher.log').read_text())
        self.assertEqual((self.root/'recovery').read_text().splitlines(),['recovered']*2)
        with launcher.Instance(self.root):pass

    def test_second_launch_does_not_run_recovery_or_touch_hardware(self):
        with launcher.Instance(self.root):
            proc=self.start('normal');output=proc.communicate(timeout=5)[0]
        self.assertNotEqual(proc.returncode,0)
        self.assertIn(b'already running',output)
        self.assertFalse((self.root/'recovery').exists())
        self.assertFalse((self.root/'ui.pid').exists())

    def test_signals_wait_for_ui_cleanup(self):
        for sig in (signal.SIGINT,signal.SIGHUP,signal.SIGTERM):
            with self.subTest(signal=sig):
                for name in ('ready','stopping','stopped'): (self.root/name).unlink(missing_ok=True)
                proc=self.start('wait');self.wait_file('ready');proc.send_signal(sig)
                proc.communicate(timeout=5)
                self.assertEqual(proc.returncode,128+sig)
                self.assertTrue((self.root/'stopped').exists())

    def test_crash_reaps_workers_and_grandchildren_in_other_sessions(self):
        proc=self.start('crash');output=proc.communicate(timeout=7)[0]
        self.assertEqual(proc.returncode,137,output)
        for name in ('worker','grandchild'):
            self.assertIsNone(launcher.process_start(int((self.root/(name+'.pid')).read_text())))
        self.assertEqual(len((self.root/'recovery').read_text().splitlines()),2)
        self.assertIn('fixture traceback',(self.root/'launcher.log').read_text())

    def test_hung_app_is_killed_before_recovery(self):
        proc=self.start('hung');self.wait_file('ready');proc.send_signal(signal.SIGINT)
        proc.communicate(timeout=5)
        self.assertEqual(proc.returncode,130)
        self.assertIsNone(launcher.process_start(int((self.root/'ui.pid').read_text())))
        self.assertEqual(len((self.root/'recovery').read_text().splitlines()),2)

    def test_stop_command_waits_for_lock_release(self):
        proc=self.start('wait');self.wait_file('ready')
        self.assertEqual(launcher.request_stop(self.root,timeout=5),0)
        proc.communicate(timeout=5)
        self.assertTrue((self.root/'stopped').exists())
        with launcher.Instance(self.root):pass

    def test_stale_pid_identity_does_not_signal_another_process(self):
        proc=self.start('wait');self.wait_file('ready')
        owner=json.loads((self.root/'instance.json').read_text())
        for identity in owner.values():identity['start']='not-this-process'
        (self.root/'instance.json').write_text(json.dumps(owner))
        self.assertEqual(launcher.request_stop(self.root,timeout=.1),1)
        self.assertIsNone(proc.poll())
        self.assertFalse((self.root/'stopping').exists())

    def test_lock_is_held_through_recovery(self):
        proc=self.start('slow-recovery');self.wait_file('recovering')
        with self.assertRaises(launcher.AlreadyRunning):
            with launcher.Instance(self.root):pass
        (self.root/'release').touch();proc.communicate(timeout=5)
        self.assertEqual(proc.returncode,0)
        with launcher.Instance(self.root):pass

    def test_launcher_kill_stops_app_without_releasing_lock_early(self):
        proc=self.start('parent-death');self.wait_file('ready');proc.kill()
        self.wait_file('stopping')
        with self.assertRaises(launcher.AlreadyRunning):
            with launcher.Instance(self.root):pass
        self.wait_file('stopped');proc.communicate(timeout=5)
        deadline=time.monotonic()+3
        while True:
            try:
                with launcher.Instance(self.root):break
            except launcher.AlreadyRunning:
                if time.monotonic()>deadline:raise
                time.sleep(.01)

    def test_failed_preflight_recovery_does_not_start_app(self):
        proc=self.start('failed-recovery');proc.communicate(timeout=5)
        self.assertEqual(proc.returncode,1)
        self.assertFalse((self.root/'ui.pid').exists())

    def test_caller_exit_stops_foreground_app_without_a_terminal(self):
        proc=subprocess.Popen([sys.executable,str(self.script),'caller','wait',str(self.root)],
                              env=self.env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        self.processes.append(proc)
        proc.communicate(timeout=6)
        self.assertEqual(proc.returncode,0)
        self.assertTrue((self.root/'stopped').exists())
        with launcher.Instance(self.root):pass


if __name__ == '__main__':unittest.main()
