"""GNSS sentence handling, the receiver worker and the card's states; no phone needed."""
import json
import os
import tempfile
import threading
import time
import tty
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import ImageFont
import actions
import app
from gnss_live import Receiver, capture
from gnss_view import headline, GREEN, GOLD, RED


def nmea(body):
    value = 0
    for char in body:
        value ^= ord(char)
    return f'${body}*{value:02X}'


FIX = [nmea(s) for s in (
    'GPGSV,2,1,06,02,67,210,44,05,45,120,41,07,12,300,22,12,80,010,47',
    'GPGSV,2,2,06,19,09,250,,24,55,280,43',
    'GLGSV,1,1,03,65,50,100,39,66,70,200,42,72,10,050,21',
    'GPGGA,140322.00,5231.2005,N,01324.2972,E,1,08,0.9,34.2,M,39.5,M,,',
    'GPRMC,140322.00,A,5231.2005,N,01324.2972,E,0.1,0.0,111126,,,A',
    'GNGSA,A,3,02,05,12,24,,,,,,,,,1.6,0.9,1.3',
    'GNGSA,A,3,65,66,,,,,,,,,,,1.6,0.9,1.3')]
NO_FIX = [nmea(s) for s in (
    'GPGSV,1,1,03,02,67,210,24,05,45,120,,12,80,010,27',
    'GPGGA,140402.00,,,,,0,,,,,,,,',
    'GPRMC,140402.00,V,,,,,,,111126,,,N',
    'GNGSA,A,1,,,,,,,,,,,,,,,')]


def receiver(lines, now=100, started=90):
    result = Receiver(started)
    for line in lines:
        result.feed(line, now)
    return result


class ReceiverTests(unittest.TestCase):
    def test_fix_reports_position_time_and_the_satellites_used(self):
        state = receiver(FIX).snapshot(100.5)
        self.assertEqual((state['status'], state['fix_type']), ('fix', 3))
        self.assertAlmostEqual(state['latitude'], 52.520008, places=6)
        self.assertAlmostEqual(state['longitude'], 13.404953, places=6)
        self.assertEqual((state['altitude_m'], state['hdop']), (34.2, .9))
        self.assertEqual(state['utc'], '2026-11-11 14:03:22')
        self.assertEqual((state['used'], state['in_view']), (6, 9))
        self.assertEqual(state['seconds_to_fix'], 10)
        # Used satellites first, strongest first; GLONASS 65.. are slots R01..
        self.assertEqual([s['label'] for s in state['satellites']],
                         ['G12', 'G02', 'G24', 'R02', 'G05', 'R01', 'G07', 'R08', 'G19'])
        self.assertEqual([s['used'] for s in state['satellites']], [True] * 6 + [False] * 3)
        self.assertIsNone(state['satellites'][-1]['snr'])
        json.dumps(state, allow_nan=False)

    def test_southern_and_western_hemispheres_are_negative(self):
        state = receiver([nmea('GPGGA,010203,3351.6500,S,07040.1000,W,1,05,1.2,520.0,M,,M,,')]).snapshot(100)
        self.assertAlmostEqual(state['latitude'], -33.860833, places=6)
        self.assertAlmostEqual(state['longitude'], -70.668333, places=6)
        self.assertEqual(state['utc'], '01:02:03')
        self.assertEqual(state['used'], 5)      # no GSA: the count the fix itself reports

    def test_lost_fix_keeps_the_last_position_with_its_age(self):
        r = receiver(FIX, now=100)
        for line in NO_FIX:
            r.feed(line, 140)
        state = r.snapshot(140)
        self.assertEqual((state['status'], state['fix_type'], state['used']), ('searching', None, 0))
        self.assertAlmostEqual(state['latitude'], 52.520008, places=6)
        self.assertEqual(state['position_age'], 40)
        self.assertEqual([s['label'] for s in state['satellites']], ['G12', 'G02', 'G05'])
        self.assertFalse(any(s['used'] for s in state['satellites']))

    def test_never_fixed_has_no_position(self):
        state = receiver(NO_FIX).snapshot(100)
        self.assertEqual(state['status'], 'searching')
        self.assertIsNone(state['latitude']); self.assertIsNone(state['position_age'])
        self.assertIsNone(state['seconds_to_fix'])

    def test_silent_receiver_is_not_reported_as_fixed(self):
        r = receiver(FIX, now=100)
        self.assertEqual(r.snapshot(102)['status'], 'fix')
        state = r.snapshot(130)
        self.assertEqual((state['status'], state['silent_seconds']), ('silent', 30))
        self.assertEqual((state['satellites'], state['used']), ([], 0))
        fresh = Receiver(0)
        self.assertEqual(fresh.snapshot(2)['status'], 'waiting')
        self.assertEqual(fresh.snapshot(9)['status'], 'silent')

    def test_damaged_sentences_are_counted_and_ignored(self):
        r = receiver(NO_FIX)
        good = FIX[3]
        for bad in (good[:-2] + '00', good[:-3], good.replace('5231', '4231'), 'noise',
                    nmea('GPGGA,140322.00,52x1.2,N,01324.2972,E,1,08,0.9,34.2,M,39.5,M,,'),
                    nmea('GPGSV,1,1,01,xx,67,210,44'), nmea('GPGGA,1')):
            r.feed(bad, 100)
        state = r.snapshot(100)
        self.assertEqual(state['status'], 'searching'); self.assertIsNone(state['latitude'])
        self.assertEqual(state['rejected'], 6)       # 'noise' is not a sentence at all

    def test_numbered_systems_and_repeated_signals(self):
        r = receiver([
            nmea('GPGSV,1,1,02,05,45,120,30,24,55,280,25,1'),     # L1
            nmea('GPGSV,1,1,02,05,45,120,41,24,55,280,,8'),       # L5: G05 stronger, G24 absent
            nmea('GAGSV,1,1,01,05,30,090,38,7'),
            nmea('GPGGA,140322.00,5231.2005,N,01324.2972,E,1,02,0.9,34.2,M,39.5,M,,'),
            nmea('GNGSA,A,3,24,,,,,,,,,,,,1.6,0.9,1.3,1'),
            nmea('GNGSA,A,3,05,,,,,,,,,,,,1.6,0.9,1.3,3')])       # Galileo 05, not GPS 05
        rows = {s['label']: s for s in r.snapshot(100)['satellites']}
        self.assertEqual((rows['G05']['snr'], rows['G24']['snr']), (41, 25))
        self.assertEqual({k: v['used'] for k, v in rows.items()}, {'E05': True, 'G24': True, 'G05': False})


    def test_satellites_used_without_a_signal_report_are_still_listed(self):
        # As seen on the phone: BeiDou in the fix (system 4), but no BeiDou GSV.
        r = receiver([
            nmea('GPGSV,1,1,02,26,64,322,31,27,17,247,18,1'),
            nmea('GPGGA,183101.00,5231.2005,N,01324.2972,E,1,01,1.0,98.7,M,39.5,M,,'),
            nmea('GPGSA,A,2,26,,,,,,,,,,,,1.3,1.0,0.8,1'),
            nmea('GNGSA,A,2,26,,,,,,,,,,,,1.3,1.0,0.8,1'),
            nmea('GNGSA,A,2,,,,,,,,,,,,,1.3,1.0,0.8,3'),
            nmea('GNGSA,A,2,24,35,,,,,,,,,,,1.3,1.0,0.8,4')])
        state = r.snapshot(100)
        self.assertEqual((state['fix_type'], state['used'], state['in_view']), (2, 3, 4))
        self.assertEqual([(s['label'], s['used'], s['snr']) for s in state['satellites']],
                         [('G26', True, 31), ('C24', True, None), ('C35', True, None), ('G27', False, 18)])
        self.assertEqual(sum(s['used'] for s in state['satellites']), state['used'])


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name)
        self.master, self.slave = os.openpty()
        self.addCleanup(os.close, self.master); self.addCleanup(os.close, self.slave)
        tty.setraw(self.slave)
        self.device = os.ttyname(self.slave)

    def state(self):
        try:
            return json.loads((self.runtime / 'scan.json').read_text())
        except (OSError, ValueError):
            return {}

    def test_follows_the_stream_until_stopped_and_keeps_the_raw_sentences(self):
        def send():
            deadline = time.monotonic() + 5
            while self.state().get('status') != 'fix' and time.monotonic() < deadline:
                os.write(self.master, ''.join(line + '\r\n' for line in FIX).encode())
                time.sleep(.05)
            written.append((self.runtime / 'nmea.log').stat().st_size)
            (self.runtime / 'stop').touch()
        written = []
        sender = threading.Thread(target=send); sender.start()
        result = capture(self.runtime, self.runtime / 'nmea.log', self.device)
        sender.join()
        self.assertEqual(result['status'], 'fix'); self.assertTrue(result['fix_seen'])
        self.assertGreater(written[0], 0)      # on disk while running, not only at exit
        self.assertEqual(self.state()['status'], 'fix')
        self.assertIn(FIX[3], (self.runtime / 'nmea.log').read_text())

    def test_sentences_queued_before_the_card_opened_are_discarded(self):
        # With no reader the service's pty fills up with an old fix.
        os.set_blocking(self.master, False)
        try:
            for _ in range(500):
                os.write(self.master, ''.join(line + '\r\n' for line in FIX).encode())
        except BlockingIOError:
            pass
        def send():
            deadline = time.monotonic() + 5
            while not self.state() and time.monotonic() < deadline:
                time.sleep(.01)
            os.write(self.master, ''.join(line + '\r\n' for line in NO_FIX).encode())
            while not self.state().get('sentences') and time.monotonic() < deadline:
                time.sleep(.01)
            (self.runtime / 'stop').touch()
        sender = threading.Thread(target=send); sender.start()
        result = capture(self.runtime, self.runtime / 'nmea.log', self.device)
        sender.join()
        self.assertEqual((result['status'], result['fix_seen'], result['sentences'], result['rejected']),
                         ('searching', False, len(NO_FIX), 0))

    def test_a_receiver_that_never_spoke_is_an_error(self):
        threading.Timer(.3, (self.runtime / 'stop').touch).start()
        with self.assertRaisesRegex(RuntimeError, 'No NMEA received'):
            capture(self.runtime, self.runtime / 'nmea.log', self.device)
        self.assertEqual(self.state()['status'], 'waiting')

    def test_missing_device_names_the_service(self):
        with self.assertRaisesRegex(RuntimeError, 'sdx55-gnss.service'):
            capture(self.runtime, self.runtime / 'nmea.log', str(self.runtime / 'absent'))

    def test_stopped_service_is_reported_without_opening_its_stale_link(self):
        with patch('actions.command', return_value='inactive\n'), patch('gnss_live.capture') as follow:
            with self.assertRaisesRegex(RuntimeError, 'not running'):
                actions.perform('gnss')
            follow.assert_not_called()


class CardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); (root / 'runtime').mkdir()
        for target, value in [('app.RUN', root / 'runtime'), ('actions.RUN', root / 'runtime')]:
            p = patch(target, value); p.start(); self.addCleanup(p.stop)
        self.ui = app.App(root / 'state', artifact_root=root / 'mqa', capture_logs=False)
        self.ui.renderer.font = lambda size, bold=False: ImageFont.load_default(size=size)

    def test_banner_tells_the_states_apart(self):
        fix = receiver(FIX).snapshot(100)
        self.assertEqual(headline(fix, True), ('3D FIX', GREEN, '6 satellites used · HDOP 0.9'))
        self.assertEqual(headline(receiver(NO_FIX).snapshot(100), True),
                         ('NO FIX', GOLD, 'Searching 0:10 · 3 in view · best 27 dB-Hz'))
        lost = receiver(FIX, now=100)
        for line in NO_FIX:
            lost.feed(line, 175)
        self.assertEqual(headline(lost.snapshot(175), True)[2], 'Lost 1:15 ago · 3 in view · best 27 dB-Hz')
        self.assertEqual(headline(receiver(FIX).snapshot(165), True), ('NO DATA', RED, 'Receiver silent for 1:05'))
        self.assertEqual(headline({}, True)[0], 'STARTING')
        self.assertEqual(headline(fix, False, 'failed')[:2], ('RECEIVER ERROR', RED))

    def test_shortcut_opens_the_card_and_starts_the_receiver_worker(self):
        with patch('app.subprocess.Popen') as spawn:
            self.ui.act('jump_gnss', force=True)
            self.assertEqual(spawn.call_args.args[0][2], 'gnss')
        self.assertEqual(app.STEPS[self.ui.session.data['index']]['id'], 'gnss')
        enabled = {b[0]: b[4] for b in self.ui.renderer.buttons(self.ui)}
        self.assertTrue(enabled['pass']); self.assertNotIn('run', enabled)
        self.ui.worker = None

    def test_full_sky_fits_between_the_title_and_the_controls(self):
        self.ui.mode = 'test'
        self.ui.session.data['index'] = next(i for i, s in enumerate(app.STEPS) if s['action'] == 'gnss')
        crowded = FIX + [nmea(f'GAGSV,1,1,04,' + ','.join(f'{n:02d},{n},{n * 9},{n + 10}' for n in range(start, start + 4)))
                         for start in range(1, 37, 4)]
        states = [{}, receiver(NO_FIX).snapshot(100), receiver(FIX).snapshot(100), receiver(crowded).snapshot(100)]
        self.assertEqual(states[-1]['in_view'], 45)
        frames = []
        for state in states:
            self.ui.scan_state = state
            image = self.ui.renderer.render(self.ui); frames.append(image)
            self.assertEqual(len(image.crop((0, 0, 480, app.SAFE_TOP)).getcolors()), 1)
            self.assertEqual(len(image.crop((0, 812, 480, 880)).getcolors()), 1)
        self.assertEqual(len({frame.getpixel((22, 195)) for frame in frames[:3]}), 3)   # banner colour


if __name__ == '__main__':
    unittest.main()
