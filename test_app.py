import json, math, struct, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from core import Session
from reports import ensure_run, collect_logs
import actions
from sensor_live import decode_sample,rotation,transform,attitude
from sensor_views import bearing_point,sample_state,SensorAnimation,level_up,level_point,slerp

STEPS=[{'id':'a','title':'A','criterion':'A works'}, {'id':'b','title':'B','criterion':'B works'}]

class StateTests(unittest.TestCase):
    def test_resume_edit_finish(self):
        with tempfile.TemporaryDirectory() as d:
            a=Session(d,STEPS,'test-kernel');identifier=a.data['id']
            a.answer('pass',{'run':{'ok':True}})
            b=Session(d,STEPS,'test-kernel')
            self.assertEqual(b.data['id'],identifier);self.assertEqual(b.data['index'],1)
            b.back();b.answer('fail',{'run':{'ok':False}})
            self.assertEqual(b.data['answers']['a']['verdict'],'fail')
            self.assertEqual(len(b.data['history']),2)
            with self.assertRaises(ValueError):b.finish({})
            b.answer('skip',{});report=b.finish({'kernel':'test-kernel'})
            data=json.loads(report.read_text());self.assertTrue(data['completed'])
            self.assertEqual(b.counts(),{'pass':0,'fail':1,'skip':1})

    def test_new_kernel_preserves_old_session(self):
        with tempfile.TemporaryDirectory() as d:
            a=Session(d,STEPS,'old');a.answer('pass',{});old=a.data['id']
            b=Session(d,STEPS,'new')
            self.assertNotEqual(b.data['id'],old)
            self.assertTrue((Path(d)/'reports'/(old+'.json')).exists())

    def test_invalid_verdict_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            a=Session(d,STEPS,'test')
            with self.assertRaises(ValueError):a.answer('maybe',{})
            self.assertEqual(a.data['index'],0)

    def test_catalog_update_keeps_unchanged_answers(self):
        with tempfile.TemporaryDirectory() as d:
            a=Session(d,STEPS,'kernel');identifier=a.data['id'];a.answer('pass',{})
            changed=[STEPS[0],{'id':'imu','title':'IMU','criterion':'Tracks motion'},STEPS[1]]
            b=Session(d,changed,'kernel')
            self.assertEqual(b.data['id'],identifier);self.assertEqual(b.data['index'],1)
            self.assertEqual(b.data['answers']['a']['verdict'],'pass')
            self.assertEqual(b.data['test_count'],3)
            changed[0]={'id':'a','title':'A revised','criterion':'New criterion'}
            c=Session(d,changed,'kernel');self.assertFalse(c.data['answers'])
            self.assertTrue(list((Path(d)/'reports').glob('*-catalog-*.json')))

    def test_completed_report_survives_catalog_change(self):
        with tempfile.TemporaryDirectory() as d:
            a=Session(d,STEPS,'old');a.answer('pass',{});a.answer('skip',{});a.finish({})
            previous=a.data['id'];answers=a.data['answers'].copy()
            changed=STEPS+[{'id':'tilt','title':'Tilt','criterion':'Tilt works'}]
            resumed=Session(d,changed,'new')
            self.assertEqual(resumed.data['id'],previous)
            self.assertEqual(resumed.data['answers'],answers)
            self.assertTrue(resumed.data['completed'])
            with self.assertRaises(ValueError):resumed.answer('fail',{})
            fresh=Session(d,changed,'new',fresh=True)
            self.assertNotEqual(fresh.data['id'],previous)
            self.assertFalse(fresh.data['answers'])
            archived=json.loads((Path(d)/'reports'/(previous+'.json')).read_text())
            self.assertEqual(archived['answers'],answers)


class ArtifactTests(unittest.TestCase):
    def test_shortcut_does_not_finish_with_unanswered_checks(self):
        import app
        with tempfile.TemporaryDirectory() as d:
            ui=app.App(d,artifact_root=Path(d)/'mqa',capture_logs=False)
            ui.session.data['index']=len(app.STEPS)
            ui.finish_or_resume()
            self.assertFalse(ui.session.data['completed']);self.assertEqual(ui.mode,'welcome')
            self.assertEqual(ui.session.data['index'],0)

    def test_no_framebuffer_commit_during_panel_off_test(self):
        try:import app
        except ImportError:self.skipTest('Pillow is available on the phone')
        ui=app.App.__new__(app.App)
        ui.session=SimpleNamespace(data={'index':next(i for i,s in enumerate(app.STEPS) if s['action']=='panel_cycle')})
        ui.worker=object()
        self.assertFalse(ui.display_updates_allowed())
        ui.worker=None
        self.assertTrue(ui.display_updates_allowed())
        ui.worker=object();ui.session.data['index']=0
        self.assertTrue(ui.display_updates_allowed())

    def test_numbering_resume_and_saved_answers(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'mqa'
            session=Session(Path(d)/'state',STEPS,'kernel')
            first=ensure_run(session.data,root);session.save()
            self.assertEqual(first.name,'001')
            self.assertEqual(ensure_run(session.data,root),first)
            session.answer('pass',{'value':42})
            data=json.loads((first/'results.json').read_text())
            self.assertEqual(data['answers']['a']['verdict'],'pass')
            other=ensure_run({},root)
            self.assertEqual(other.name,'002')
            self.assertTrue((first/'photos').is_dir())
            self.assertTrue((first/'logs').is_dir())

    def test_full_dmesg_is_not_truncated(self):
        with tempfile.TemporaryDirectory() as d:
            directory=ensure_run({},Path(d)/'mqa')
            dump='kernel line\n'*20000
            def run(command,stdout,**kwargs):
                stdout.write(dump if command==['dmesg'] else 'service log\n')
                return SimpleNamespace(returncode=0)
            with patch('reports.subprocess.run',side_effect=run):
                result=collect_logs(directory)
            self.assertEqual((directory/'dmesg.log').read_text(),dump)
            self.assertEqual(result['files']['dmesg.log']['returncode'],0)

class SensorTests(unittest.TestCase):
    def test_level_marker_moves_toward_raised_edge(self):
        self.assertEqual(level_point(level_up((0,0,9.8))),(240,507))
        self.assertEqual(level_point(level_up((0,0,-9.8))),(240,507))
        self.assertGreater(level_point(level_up((3,0,9)))[0],240)
        self.assertLess(level_point(level_up((0,3,9)))[1],507)
        with self.assertRaises(ValueError):level_up((0,0,0))

    def test_compass_animation_crosses_north_without_spinning(self):
        def quat(heading):
            a=math.radians(-heading)/2;return [0,0,math.sin(a),math.cos(a)]
        animator=SensorAnimation()
        def frame(heading,stamp,now):
            state={'samples':{'compass':{'values':quat(heading),'time':stamp,'accuracy':3}}}
            original=json.dumps(state)
            result=animator.frame(state,True,now)
            self.assertEqual(json.dumps(state),original)
            return attitude(result['samples']['compass']['values'])[0]
        frame(359,10,10)
        h=frame(1,10.2,10.32)
        self.assertAlmostEqual((h+180)%360-180,0,places=5)
        self.assertAlmostEqual(frame(1,10.2,20),1,places=5)  # no extrapolation after a stall
        q=quat(30)
        self.assertAlmostEqual(attitude(slerp(q,[-v for v in q],.5))[0],30,places=5)

    def test_partial_framebuffer_updates_match_full_frame(self):
        import app
        from PIL import Image,ImageDraw
        fb=app.Framebuffer.__new__(app.Framebuffer)
        fb.width=90;fb.height=120;fb.stride=368;fb.fd=0;fb.screeninfo=bytes(160)
        fb.memory=bytearray([77])*(fb.stride*fb.height);fb.previous_image=None
        im=Image.new('RGB',(30,40),'#123456')
        with patch('app.fcntl.ioctl') as commit:
            for box,color in [((0,0,0,0),'#123456'),((5,8,12,19),'#ff3322'),((7,9,20,29),'#34bc78'),((0,0,29,39),'#123456')]:
                ImageDraw.Draw(im).rectangle(box,fill=color);fb.draw(im)
                actual=b''.join(fb.memory[y*fb.stride:y*fb.stride+fb.width*4] for y in range(fb.height))
                expected=im.resize((fb.width,fb.height),Image.Resampling.BILINEAR).tobytes('raw','BGRX')
                self.assertEqual(actual,expected)
                self.assertEqual(fb.memory[360:368],bytes([77])*8)
            count=commit.call_count;fb.draw(im);self.assertEqual(commit.call_count,count)

    def test_quaternion_and_acceleration_packet_decoding(self):
        payload=b'\x0a\x10'+struct.pack('<4f',0,0,0,1)+b'\x10\x03'
        q,accuracy=decode_sample(payload);self.assertEqual(q,[0,0,0,1]);self.assertEqual(accuracy,3)
        payload=b''.join(b'\x0d'+struct.pack('<f',v) for v in (1,2,3))+b'\x10\x02'
        self.assertEqual(decode_sample(payload),([1,2,3],2))
        for data in (b'\x0a\x10\x00',b'\x0a\x03abc',b'\x0d\x00',b'\x80'*11):
            with self.assertRaises(ValueError):decode_sample(data)
        with self.assertRaises(ValueError):decode_sample(b'\x0a\x04'+struct.pack('<f',float('nan')))

    def test_heading_cardinals_and_wraparound(self):
        # Clockwise yaw seen from above is negative quaternion rotation about +Z.
        for heading in (0,1,90,180,270,359):
            angle=math.radians(-heading)/2;q=(0,0,math.sin(angle),math.cos(angle))
            actual=attitude(q)[0]
            self.assertAlmostEqual((actual-heading+180)%360-180,0,places=5)
            self.assertAlmostEqual(attitude(tuple(-v for v in q))[0],actual,places=5)
        # Facing east, north must appear to the left, not the right.
        x,y=bearing_point(0,90,(0,0),100);self.assertAlmostEqual(x,-100);self.assertAlmostEqual(y,0)

    def test_orientation_preserves_vectors_and_gravity(self):
        q=(math.sin(math.pi/4),0,0,math.cos(math.pi/4));r=rotation(q)
        self.assertAlmostEqual(transform(r,(0,1,0))[2],1)
        self.assertAlmostEqual(transform(r,(0,0,1))[1],-1)
        self.assertAlmostEqual(sum(x*x for x in transform(r,(1,2,3))),14)
        for invalid in ((0,0,0,0),(0,0,0,float('nan'))):
            with self.assertRaises(ValueError):rotation(invalid)

    def test_missing_and_stale_streams_are_not_live(self):
        s={'samples':{'compass':{'time':time.monotonic()-10}}}
        self.assertFalse(sample_state(s,('compass','accelerometer'),True)[1])
        self.assertFalse(sample_state(s,('compass',),True)[1])
        self.assertTrue(sample_state(s,('compass',),False)[1])
        s['error']='failed';self.assertFalse(sample_state(s,('compass',),False)[1])

from test_camera import CameraTests
from test_usability import UsabilityTests, RadioTests
from test_display import DisplayTests
from test_gnss import ReceiverTests, WorkerTests, CardTests
from test_launcher import LockTests, LauncherTests

if __name__=='__main__':unittest.main()
