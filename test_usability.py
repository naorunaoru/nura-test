"""Lifecycle, radio output and touch-evidence regression tests; no phone needed."""
import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image, ImageFont
import actions
import app
from radio_scan import BluetoothDevices, wifi_rows
from touch_trace import TouchTrace


class UsabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);runtime=self.root/'runtime';runtime.mkdir()
        for target,value in [('app.RUN',runtime),('actions.RUN',runtime)]:
            p=patch(target,value);p.start();self.addCleanup(p.stop)
        p=patch('actions.snapshot',return_value={});p.start();self.addCleanup(p.stop)
        self.ui=app.App(self.root/'state',artifact_root=self.root/'mqa',capture_logs=False)
        self.addCleanup(self.stop_worker)

    def stop_worker(self):
        if self.ui.worker:
            self.ui.cancel()
            try:self.ui.worker.wait(timeout=3)
            except Exception:os.killpg(self.ui.worker.pid,signal.SIGKILL);self.ui.worker.wait()
            self.ui.worker=None
        if self.ui.touch_trace:self.ui.touch_trace.close()

    def wait_transition(self):
        deadline=time.monotonic()+3
        while self.ui.pending_action is not None and time.monotonic()<deadline:
            self.ui.poll_worker();time.sleep(.01)
        self.assertIsNone(self.ui.pending_action)

    def select(self,action):
        self.ui.session.data['index']=next(i for i,s in enumerate(app.STEPS) if s['action']==action)

    def test_live_verdict_stops_and_saves_without_a_timer(self):
        self.ui.act('start',force=True)
        self.assertIsNotNone(self.ui.worker)
        enabled={b[0]:b[4] for b in self.ui.renderer.buttons(self.ui)}
        self.assertTrue(all(enabled[k] for k in ('pass','fail','skip','back','pause')))
        self.assertNotIn('run',enabled)
        original=self.ui.worker
        self.ui.act('pass',force=True);self.wait_transition()
        answer=self.ui.session.data['answers']['display']
        self.assertTrue(answer['evidence']['run']['ok'])
        self.assertLess(answer['evidence']['run']['seconds'],2)
        self.assertEqual(original.poll(),0)
        self.assertEqual(app.STEPS[self.ui.session.data['index']]['action'],'touch')
        self.assertIsNotNone(self.ui.worker)
        self.ui.act('pause',force=True);self.wait_transition()
        self.assertEqual(self.ui.mode,'welcome');self.assertIsNone(self.ui.worker)

    def test_sensor_starts_on_entry_and_manual_action_waits(self):
        self.select('proximity')
        with patch('app.subprocess.Popen') as spawn:
            self.ui.enter_test();self.assertEqual(spawn.call_args.args[0][2],'proximity')
            self.ui.worker=None
        self.select('torch0')
        with patch('app.subprocess.Popen') as spawn:
            self.ui.enter_test();spawn.assert_not_called()
        enabled={b[0]:b[4] for b in self.ui.renderer.buttons(self.ui)}
        self.assertFalse(enabled['pass']);self.assertTrue(enabled['run'])

    def test_camera_shortcut_and_stop_remain_available_during_control_change(self):
        with patch('app.subprocess.Popen') as spawn:
            self.ui.act('jump_camera',force=True)
            self.assertEqual(spawn.call_args.args[0][2],'camera_main')
        self.ui.camera_state={'phase':'preview','caps':{'model':'imx689'},'frames':8,'ack':0}
        self.ui.act('cam_ois',force=True)
        cmd=json.loads((app.RUN/'camera-command.json').read_text())
        self.assertEqual((cmd['op'],cmd['ois']),('preview',True))
        enabled={b[0]:b[4] for b in self.ui.renderer.buttons(self.ui)}
        self.assertTrue(enabled['cam_stop']);self.assertFalse(enabled['cam_ois'])
        self.ui.act('cam_stop',force=True)
        self.assertTrue((app.RUN/'camera-abort').exists())
        self.assertEqual(json.loads((app.RUN/'camera-command.json').read_text())['op'],'stop')
        self.ui.camera_state.update(phase='idle',error='Exact size rejected')
        enabled={b[0]:b[4] for b in self.ui.renderer.buttons(self.ui)}
        self.assertFalse(enabled['pass']);self.assertTrue(enabled['fail'] and enabled['skip'])
        self.ui.worker=None

    def test_back_stops_old_worker_before_starting_previous_check(self):
        self.select('touch');self.ui.enter_test();previous=self.ui.worker
        self.ui.act('back',force=True);self.wait_transition()
        self.assertIsNotNone(previous.poll())
        self.assertEqual(app.STEPS[self.ui.session.data['index']]['id'],'display')
        self.assertIsNotNone(self.ui.worker)

    def test_touch_trace_retains_all_points_and_contact_identity(self):
        trace=TouchTrace(self.ui.output_directory,'test',(100,1100),(200,2200))
        for i in range(650):
            trace.record(i%2,dict(raw_x=100+i,raw_y=2200,tracking_id=42+i%2),
                         'up' if i==649 else 'down' if i<2 else 'move',dict(mode='test',step='touch'),i/120)
        trace.dropped(dict(mode='test',step='touch'));trace.close()
        rows=[json.loads(line) for line in trace.path.read_text().splitlines()]
        self.assertEqual(len(rows),652);self.assertEqual(rows[0]['screen'],[1440,3168])
        self.assertEqual(rows[1]['normalized'],[0,1]);self.assertEqual(rows[1]['screen'],[0,3167])
        self.assertEqual(rows[-2]['seq'],650);self.assertEqual(rows[-2]['tracking_id'],43)
        self.assertEqual(rows[-2]['phase'],'up');self.assertEqual(rows[-1]['type'],'dropped')

    def test_touch_swipe_over_verdict_does_not_activate_it(self):
        self.ui.mode='test';self.select('touch');self.ui.touch_axes=((0,479),(0,1055))
        self.ui.touch_coverage=Image.new('RGBA',(480,1056))
        point=dict(raw_x=30,raw_y=1000,active=True,begin=None,tracking_id=10,new=True,changed=True)
        with patch.object(self.ui,'act') as act:
            self.ui.touch_frame({0:point},1)
            point.update(raw_x=100,changed=True);self.ui.touch_frame({0:point},2)
            point.update(raw_x=30,active=False,release=True,changed=True);self.ui.touch_frame({0:point},3)
            act.assert_not_called()
        self.assertNotEqual(self.ui.touch_coverage.getpixel((100,1000))[3],0)
        self.assertEqual(self.ui.touch_coverage.getpixel((65,1000))[3],0) # never bridge unobserved gaps

    def test_trace_interval_saved_with_verdict(self):
        self.ui.touch_axes=((0,479),(0,1055));self.ui.reset_touch_trace()
        self.select('touch');self.ui.enter_test()
        point=dict(raw_x=240,raw_y=500,active=True,begin=None,tracking_id=10,new=True,changed=True)
        self.ui.touch_frame({0:point},1)
        self.ui.act('fail',force=True);self.wait_transition()
        evidence=self.ui.session.data['answers']['touch']['evidence']
        self.assertEqual(evidence['touch_trace']['after_seq'],0)
        self.assertEqual(evidence['touch_trace']['through_seq'],1)
        self.assertTrue((self.ui.output_directory/evidence['touch_coverage']).is_file())

    def test_passive_checks_have_no_worker_deadline(self):
        self.select('light');self.ui.started=time.monotonic()-10000
        with patch('app.subprocess.Popen') as spawn:
            worker=spawn.return_value;worker.poll.return_value=None;self.ui.worker=worker
            with patch.object(self.ui,'cancel') as cancel:self.ui.poll_worker();cancel.assert_not_called()
            self.ui.worker=None

    def test_cleanup_failure_keeps_current_check_open(self):
        from types import SimpleNamespace
        self.select('proximity');self.ui.mode='test'
        self.ui.worker=SimpleNamespace(poll=lambda:0,returncode=0)
        self.ui.pending_action='pass'
        (app.RUN/'result.json').write_text(json.dumps({'ok':True}))
        with patch('actions.recover',side_effect=RuntimeError('restore failed')):
            self.ui.poll_worker()
        self.assertEqual(self.ui.mode,'test')
        self.assertNotIn('proximity',self.ui.session.data['answers'])
        self.assertTrue(self.ui.result['cleanup_failed'])
        self.assertIsNone(self.ui.pending_action)

    def tap(self,x,y,event_time):
        point=dict(raw_x=x,raw_y=y,active=True,begin=None,tracking_id=10,new=True,changed=True)
        self.ui.touch_frame({0:point},event_time)
        point.update(active=False,release=True,changed=True);self.ui.touch_frame({0:point},event_time+.05)

    def test_tap_made_before_the_page_was_drawn_does_not_act_on_it(self):
        # Taps queue up while a verdict is being saved; they belong to the old page.
        self.ui.mode='test';self.select('light');self.ui.touch_axes=((0,479),(0,1055))
        self.ui.mark_presented(now=100)
        with patch.object(self.ui,'act') as act:
            self.tap(240,1000,99.5);act.assert_not_called()
            self.tap(240,1000,100.5);act.assert_called_once_with('fail')

    def test_tap_is_ignored_until_the_new_page_is_drawn(self):
        self.ui.mode='test';self.select('light');self.ui.touch_axes=((0,479),(0,1055))
        self.ui.mark_presented(now=100)
        self.ui.session.data['index']+=1
        with patch.object(self.ui,'act') as act:
            self.tap(240,1000,100.5);act.assert_not_called()
            self.ui.mark_presented(now=101)
            self.tap(240,1000,101.5);act.assert_called_once_with('fail')

    def test_taps_still_work_after_the_clock_is_set_back(self):
        self.ui.mode='test';self.select('light');self.ui.touch_axes=((0,479),(0,1055))
        self.ui.mark_presented(now=time.time()+3600)
        with patch.object(self.ui,'act') as act:
            self.tap(240,1000,time.time());act.assert_called_once_with('fail')

    def test_pass_is_refused_after_a_live_check_reported_an_error(self):
        self.ui.mode='test';self.select('wifi')
        self.ui.result={'ok':False,'error':'Wi-Fi is off. Enable it, then Retry.'}
        enabled={b[0]:b[4] for b in self.ui.renderer.buttons(self.ui)}
        self.assertFalse(enabled['pass']);self.assertTrue(enabled['fail']);self.assertTrue(enabled['skip'])
        self.ui.act('pass',force=True)
        self.assertNotIn('wifi',self.ui.session.data['answers'])

    def test_camera_inset_and_long_labels_fit(self):
        # Portable test font, no device font installation required.
        self.ui.renderer.font=lambda size,bold=False:ImageFont.load_default(size=size)
        for mode in ('welcome','test','summary'):
            self.ui.mode=mode
            image=self.ui.renderer.render(self.ui)
            self.assertEqual(image.crop((0,0,480,app.SAFE_TOP)).getbbox(),(0,0,480,app.SAFE_TOP))
            self.assertEqual(len(image.crop((0,0,480,app.SAFE_TOP)).getcolors()),1)


class RadioTests(unittest.TestCase):
    def test_bluetooth_filters_controller_noise_cache_and_merges_updates(self):
        devices=BluetoothDevices()
        devices.feed('[NEW] Device 01:02:03:04:05:06 Cached headphones')
        devices.feed('Discovery started')
        devices.feed('[CHG] Controller AA:BB:CC:DD:EE:FF Discovering: yes')
        devices.feed('[NEW] Device 11:22:33:44:55:66 Keyboard')
        devices.feed('\x1b[0;93m[CHG]\x1b[0m Device 11:22:33:44:55:66 RSSI: 0xffffffc3 (-61)')
        devices.feed('[CHG] Device 11:22:33:44:55:66 Name: Desk keyboard')
        devices.feed('[CHG] Device 11:22:33:44:55:66 ManufacturerData.Value:')
        devices.feed('  00 30 20 90')
        rows=devices.rows();self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['name'],'Desk keyboard');self.assertEqual(rows[0]['rssi_dbm'],-61)
        devices.feed('[CHG] Device 01:02:03:04:05:06 RSSI: -40')
        self.assertEqual(len(devices.rows()),2)
        self.assertEqual(devices.rows()[0]['name'],'Cached headphones')
        with self.assertRaises(RuntimeError):devices.feed('Failed to start discovery: org.bluez.Error.NotReady')

    def test_wifi_escaped_names_hidden_ssids_and_distinct_access_points(self):
        output=r'AA\:BB\:CC\:DD\:EE\:01:lab\:west\\AP:36:81:WPA2:*'+'\n'+r'AA\:BB\:CC\:DD\:EE\:02::1:30::'+'\n'+r'AA\:BB\:CC\:DD\:EE\:03:lab\:west\\AP:6:50:WPA2:'
        rows=wifi_rows(output)
        self.assertEqual(len(rows),3);self.assertEqual(rows[0]['name'],'lab:west\\AP')
        self.assertEqual(rows[0]['address'],'AA:BB:CC:DD:EE:01');self.assertTrue(rows[0]['connected'])
        self.assertEqual(rows[-1]['name'],'');self.assertEqual(rows[-1]['security'],'Open')

    def test_wifi_does_not_enable_disabled_radio(self):
        with patch('actions.command',return_value='disabled\n') as command,patch('actions.scan_update'):
            with self.assertRaisesRegex(RuntimeError,'Wi-Fi is off'):actions.wifi_scan()
            self.assertEqual(command.call_count,1)

    def test_stream_preserves_split_unicode_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as d,patch.object(actions,'RUN',Path(d)):
            lines=[]
            def receive(line):
                lines.append(line);(Path(d)/'stop').touch()
            code="import os,time;os.write(1,b'caf\\xc3');time.sleep(.04);os.write(1,b'\\xa9\\n');time.sleep(3)"
            actions.stream([sys.executable,'-c',code],on_line=receive)
            self.assertEqual(lines,['café'])

    def test_bluetooth_power_restored_after_discovery_error(self):
        with tempfile.TemporaryDirectory() as d,patch.object(actions,'RUN',Path(d)):
            calls=[]
            def command(args,**kwargs):
                calls.append(args)
                return 'Controller AA:BB:CC:DD:EE:FF\nPowered: no' if args[-1]=='show' else ''
            with patch('actions.command',side_effect=command),patch('actions.stream',side_effect=RuntimeError('discovery failed')):
                with self.assertRaisesRegex(RuntimeError,'discovery failed'):actions.bluetooth_scan()
            self.assertIn(['bluetoothctl','power','on'],calls)
            self.assertEqual(calls[-1],['bluetoothctl','power','off'])
            self.assertFalse((Path(d)/'bluetooth-restore.json').exists())

    def test_recovery_replays_nfc_state_after_worker_dies(self):
        with tempfile.TemporaryDirectory() as d,patch.object(actions,'RUN',Path(d)):
            journal=Path(d)/'nfc-restore.json'
            journal.write_text(json.dumps({'powered':False,'polling':False}))
            with patch('actions.command',return_value='b true') as command:
                actions.recover()
            calls=[c.args[0] for c in command.call_args_list]
            self.assertTrue(any(args[-1]=='StopPollLoop' for args in calls))
            self.assertEqual(calls[-1][-3:],['Powered','b','false'])
            self.assertFalse(journal.exists())

    def test_scan_command_cancels_without_waiting_for_timeout(self):
        with tempfile.TemporaryDirectory() as d,patch.object(actions,'RUN',Path(d)):
            (Path(d)/'stop').touch();start=time.monotonic()
            self.assertEqual(actions.command([sys.executable,'-c','import time;time.sleep(20)'],cancellable=True),'')
            self.assertLess(time.monotonic()-start,2)


if __name__=='__main__':unittest.main()
