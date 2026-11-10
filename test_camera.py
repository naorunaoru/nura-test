import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import camera_live as cam
import camera_view


STILL = '''Property: Model = imx689
Property: PixelArraySize = 8000x6000
Control: [inout] libcamera::AfMode:
  - AfModeAuto (1)
  - AfModeContinuous (2) [default]
Control: [inout] libcamera::Gamma: [0.100000..10.000000]
Control: [  out] libcamera::Result: [0..7]
0: 3992x3000-XRGB8888/sRGB
 * Pixelformat: XRGB8888 (4x2)-(3992x3000)/(+4,+2)
'''
RAW = '''0: 4000x3000-SBGGR10_CSI2P/RAW
 * Pixelformat: SBGGR10_CSI2P (4000x3000)-(4000x3000)/(+1,+1)
'''


class CameraTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.caps=cam.capabilities(STILL)
        raw=cam.capabilities(RAW)
        self.caps.update(raw=raw['default_stream'],raw_formats=raw['formats'],id='/main')

    def test_identify_sensor_by_model_instead_of_index_or_bus(self):
        def command(args,**opts):
            if args==['cam','-l']:return '1: colour (/path/a)\n2: tele (/path/b)'
            model={'/path/a':'gc5035','/path/b':'s5k3m5'}[args[2]]
            return RAW if 'role=raw' in args else STILL.replace('imx689',model)
        with patch('actions.command',side_effect=command):
            result=cam.discover('camera_tele',self.root,lambda:False)
        self.assertEqual(result['id'],'/path/b')
        self.assertEqual(result['sensor'],[8000,6000])
        self.assertEqual(result['raw'][2],'SBGGR10_CSI2P')

    def test_native_and_maximum_requests_are_distinct(self):
        for raw,size in ((False,[3992,3000]),(True,[4000,3000])):
            self.assertEqual(cam.capture_spec(self.caps,'sensor',raw)['size'],[8000,6000])
            self.assertEqual(cam.capture_spec(self.caps,'maximum',raw)['size'],size)

    def test_frame_file_and_configuration_must_both_match(self):
        path=self.root/'input.ppm';photo=self.root/'still.png'
        Image.new('RGB',(80,60),(20,80,140)).save(path)
        spec={'size':[80,60],'format':'XRGB8888','raw':False}
        cam.save_still(path,photo,spec,[80,60,'XRGB8888'],19200)
        with Image.open(photo) as im:self.assertEqual(im.getpixel((79,59)),(20,80,140))
        for config in ([80,60,'RGB888'],[40,30,'XRGB8888']):
            with self.assertRaises(RuntimeError):cam.save_still(path,photo,spec,config,19200)
        spec['size']=[160,120]
        with self.assertRaises(RuntimeError):cam.save_still(path,photo,spec,[160,120,'XRGB8888'],19200)

    def test_raw_capture_rejects_truncated_buffer(self):
        inp=self.root/'in';inp.write_bytes(b'123456')
        spec={'size':[4,1],'raw':True,'format':'SBGGR10_CSI2P'};config=[*spec['size'],spec['format']]
        with self.assertRaises(RuntimeError):cam.save_still(inp,self.root/'out',spec,config,8)
        cam.save_still(inp,self.root/'out',spec,config,6)
        self.assertEqual((self.root/'out').read_bytes(),b'123456')
        with self.assertRaisesRegex(RuntimeError,'smaller'):
            cam.save_still(inp,self.root/'out',{**spec,'size':[8,2]},[8,2,spec['format']],6)

    def test_image_control_verdict_uses_that_frames_complete_metadata(self):
        camera=cam.Camera('camera_main',self.root,self.root);camera.caps=self.caps
        camera.proc=object();camera.extension='ppm'
        camera.state['requested']={'Gamma':3.5}
        for seq in (0,1):
            Image.new('RGB',(16,8),(seq,0,0)).save(camera.scratch/f'frame-cam0-stream0-{seq:06d}.ppm')
            camera.line(f'(30.00 fps) cam0-stream0 seq: {seq:06d} bytesused: 512')
            camera.line('\tGamma = '+('3.500000' if seq==0 else '2.200000'))
        camera.dispatch({'op':'verdict','name':'Gamma','verdict':'no effect'})
        record=camera.state['verdicts'][-1]
        self.assertEqual((record['seq'],record['readback'],record['comparison'],record['verdict']),(0,3.5,'matches','no effect'))
        with Image.open(self.root/record['file']) as image:self.assertEqual(image.getpixel((0,0)),(0,0,0))

    def test_ois_trial_times_out_without_ending_camera_page(self):
        camera=cam.Camera('camera_main',self.root,self.root);camera.proc=object()
        camera.state['ois_requested']=True;camera.last_frame=100;camera.started=79
        with patch('camera_live.time.monotonic',return_value=100),patch.object(camera,'stop') as stop:
            camera.tick()
        stop.assert_called_once()
        self.assertIn('trial ended',camera.state['message'])

    def test_controls_enforce_advertised_values_and_preserve_missing_readback(self):
        af,gamma,out=self.caps['controls'].values()
        self.assertEqual(af['default'],2)
        self.assertEqual(camera_view.change_value(af,2,1),1)
        self.assertEqual(cam.checked_value(gamma,3.5),3.5)
        for ctl,val in ((gamma,float('nan')),(gamma,10.1),(af,5),(af,True),(out,2)):
            with self.assertRaises(ValueError):cam.checked_value(ctl,val)
        self.assertEqual(cam.readback(af,2,{'AfState':2}),'not reported')
        self.assertEqual(cam.readback(gamma,3.5,{'Gamma':3.50001}),'matches')
        self.assertEqual(cam.readback(gamma,3.5,{'Gamma':1.5}),'differs')

    def test_failed_strict_request_leaves_evidence_without_photo(self):
        camera=cam.Camera('camera_main',self.root,self.root)
        camera.spec=cam.capture_spec(self.caps,'sensor',False)
        camera.attempt={'operation':'still','requested':camera.spec}
        camera.failed(RuntimeError('Stream configuration adjusted, disallowed by --strict-formats'))
        record=camera.state['captures'][0]
        self.assertEqual((record['requested']['size'],record['status']),([8000,6000],'failed'))
        self.assertNotIn('file',record)
        self.assertIn('strict-formats',(self.root/camera.state['events']).read_text())

    def test_ois_baseline_and_cleanup_restore_loaded_driver(self):
        param=self.root/'module/lc898128/parameters/enable';param.parent.mkdir(parents=True);param.write_text('Y')
        with patch.object(cam,'PARAM',param),patch.object(cam,'system_command') as mod:
            cam.request_ois(self.root,False)
            self.assertEqual(param.read_text(),'0')
            cam.recover_ois(self.root)
            self.assertEqual(param.read_text(),'Y');mod.assert_not_called()

    def test_ois_cleanup_journal_survives_failed_unload(self):
        param=self.root/'module/lc898128/parameters/enable'
        def mod(args):
            if '-r' in args:raise RuntimeError('busy')
            param.parent.mkdir(parents=True);param.write_text('Y')
        with patch.object(cam,'PARAM',param),patch.object(cam,'OIS_DEVICE',self.root/'missing'),patch.object(cam,'system_command',side_effect=mod):
            cam.request_ois(self.root,False)
            self.assertFalse(param.exists())
            with self.assertRaisesRegex(RuntimeError,'bind'):cam.request_ois(self.root,True)
            with self.assertRaisesRegex(RuntimeError,'busy'):cam.recover_ois(self.root)
            self.assertEqual(json.loads((self.root/'camera-ois-restore.json').read_text()),{'loaded':False,'enable':None})
