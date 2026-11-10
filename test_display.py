import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
import app
from drm_display import Mode,mode_choices
from external_display import ExternalDisplay,restore
from display_pattern import Pattern


def mode(w,h,hz=60,preferred=False):
    m=Mode();m.hdisplay=w;m.vdisplay=h;m.htotal=w+160;m.vtotal=h+40
    m.clock=round(m.htotal*m.vtotal*hz/1000);m.type=8 if preferred else 64
    m.name=f'{w}x{h}'.encode();return m


class DisplayTests(unittest.TestCase):
    def test_primary_plane_selection_preserves_internal_plane(self):
        import ctypes as C
        from drm_display import DRM,Plane,Planes,U32
        drm=DRM.__new__(DRM);drm.crtcs=[94,95,96]
        ids=(U32*3)(46,52,58);resources=Planes(count_planes=3,planes=ids)
        planes={i:Plane(plane_id=i,crtc_id=c,possible_crtcs=7) for i,c in [(46,94),(52,95),(58,0)]}
        drm.get=lambda kind,*args:C.pointer(resources) if kind=='PlaneResources' else C.pointer(planes[args[0]])
        drm.properties=lambda ident,kind:{'type':{'value':1}}
        drm.drmModeFreePlane=lambda p:None;drm.drmModeFreePlaneResources=lambda p:None
        self.assertEqual(drm.primary(95)[0],52)
        self.assertEqual(drm.primary(94)[0],46)
        self.assertEqual(drm.primary(96)[0],58)

    def test_hotplug_reprobes_modes_and_releases_disconnected_output(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            ext=ExternalDisplay.__new__(ExternalDisplay)
            ext.status_path=Path(directory)/'status';ext.status_path.write_text('disconnected')
            ext.signature=None;ext.last_poll=0;ext.saved=None;ext.external={'id':38};ext.hdr='Unavailable'
            connector={'id':38,'name':'DP-1','connected':False,'modes':[]}
            ext.drm=SimpleNamespace(connectors=lambda:[connector]);ext.record=Mock();ext.attach=Mock();ext.apply=Mock(return_value=True);ext.release=Mock()
            ext.poll(force=True);self.assertFalse(ext.connected);self.assertEqual(ext.modes,[])
            connector.update(connected=True,modes=[mode(640,480),mode(2880,1800,120,True)])
            ext.status_path.write_text('connected');ext.poll(force=True)
            self.assertTrue(ext.connected);self.assertEqual(len(ext.modes),2);ext.attach.assert_called_once();ext.apply.assert_called_once()
            ext.saved={'snapshot':True};connector['connected']=False;ext.status_path.write_text('disconnected');ext.poll(force=True)
            ext.release.assert_called_once_with(connected=False);self.assertIsNone(ext.current)

    def test_leaving_external_check_restores_before_fbdev_resumes(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as d:
            ui=app.App(Path(d)/'state',artifact_root=Path(d)/'artifacts',capture_logs=False)
            ext=Mock();ui.external=ext;ui.framebuffer=SimpleNamespace(external=ext,previous_image=object())
            ui.mode='welcome';ui.sync_external()
            ext.close.assert_called_once();self.assertIsNone(ui.external);self.assertIsNone(ui.framebuffer.external)
            self.assertIsNone(ui.framebuffer.previous_image)

    def test_native_modes_survive_internal_framebuffer_limits(self):
        modes=mode_choices([mode(640,480),mode(2880,1800,120),mode(2880,1800,100,True),mode(640,350),mode(640,480)])
        self.assertEqual(len(modes),3)
        self.assertEqual((modes[-1].hdisplay,modes[-1].vdisplay),(2880,1800))
        self.assertAlmostEqual(modes[-1].hz,120,places=2)
        self.assertTrue(modes[-2].type&8)

    def test_selection_preserves_refresh_and_native_uses_preferred_timing(self):
        ext=ExternalDisplay.__new__(ExternalDisplay)
        ext.modes=mode_choices([mode(640,480),mode(1920,1080),mode(2880,1800,60),mode(2880,1800,100,True),mode(2880,1800,120)])
        ext.selected=ext.modes[1]
        ext.select_resolution(1);self.assertAlmostEqual(ext.selected.hz,60,places=2)
        ext.select_refresh();self.assertAlmostEqual(ext.selected.hz,100,places=2)
        ext.select_refresh();self.assertAlmostEqual(ext.selected.hz,120,places=2)
        ext.select_resolution(1);self.assertEqual(ext.selected.hdisplay,640)
        ext.native();self.assertTrue(ext.selected.type&8)

    def test_vector_pattern_has_native_pixel_details(self):
        for w,h in [(640,480),(2880,1800)]:
            pattern=Pattern(w,h,f'{w}×{h}')
            image=pattern.image()
            self.assertEqual(image.size,(w,h))
            svg=pattern.svg();self.assertIn(f'viewBox="0 0 {w} {h}"',svg)
            self.assertIn('<circle',svg);self.assertNotIn('<image',svg)
            y=round(h*.82)+10;x=w//2+10
            self.assertNotEqual(image.getpixel((x,y)),image.getpixel((x+1,y)))
            self.assertEqual(image.getpixel((0,0)),(255,255,255))

    def test_rejected_mode_keeps_current_scanout(self):
        ext=ExternalDisplay.__new__(ExternalDisplay)
        ext.connected=True;ext.selected=mode(2880,1800,120);ext.current=mode(640,480)
        old=object();ext.buffer=old;ext.mode_blob=5;ext.connector=38;ext.crtc=95;ext.plane=52
        ext.record=lambda *args,**kwargs:None
        drm=SimpleNamespace(blob=lambda data:99,atomic=lambda *args,**kwargs:(_ for _ in ()).throw(OSError('mode rejected')),free_blob=lambda b:None)
        ext.drm=drm
        with patch('external_display.Buffer') as buffer:
            buffer.return_value.fb=101
            self.assertFalse(ext.apply());buffer.return_value.close.assert_called_once()
        self.assertIs(ext.buffer,old);self.assertEqual(ext.current.hdisplay,640)
        self.assertIn('mode rejected',ext.error)

    def test_disconnect_restore_never_touches_internal_pipeline(self):
        commits=[]
        drm=SimpleNamespace(properties=lambda *args:None,free_blob=lambda b:None,atomic=lambda changes,**kwargs:commits.append(changes))
        saved={'connector':38,'crtc':95,'plane':52,'mode':'','changes':{'38':{'CRTC_ID':95},'95':{'ACTIVE':1,'MODE_ID':9},'52':{'FB_ID':97}}}
        restore(drm,saved,connected=False)
        self.assertEqual(set(commits[0]),{38,95,52})
        self.assertEqual(commits[0][95],{'MODE_ID':0,'ACTIVE':0})
        self.assertEqual(commits[0][52]['FB_ID'],0)

    def test_internal_redraw_uses_its_plane_not_fbdev_modeset(self):
        from unittest.mock import Mock
        fb=app.Framebuffer.__new__(app.Framebuffer)
        fb.width=90;fb.height=120;fb.stride=360;fb.fd=0;fb.screeninfo=bytes(160)
        fb.memory=bytearray(fb.stride*fb.height);fb.previous_image=None
        fb.external=Mock()
        with patch('app.fcntl.ioctl') as pan:
            fb.draw(Image.new('RGB',(30,40),'red'))
            pan.assert_not_called();fb.external.flush_internal.assert_called_once()

    def test_external_controls_work_while_connection_worker_runs(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as d:
            with patch.object(app,'RUN',Path(d)/'runtime'):
                ui=app.App(Path(d)/'state',artifact_root=Path(d)/'artifacts',capture_logs=False)
                ui.mode='test';ui.session.data['index']=next(i for i,s in enumerate(app.STEPS) if s['id']=='dp-video')
                ui.external=Mock();ui.worker=object()
                ui.act('ext_next',force=True);ui.external.select_resolution.assert_called_once_with(1)
                ui.act('ext_apply',force=True);ui.external.apply.assert_called_once()
                self.assertIsNotNone(ui.worker);self.assertIsNone(ui.pending_action)
                controls={key:enabled for key,_,_,_,enabled in ui.renderer.buttons(ui)}
                self.assertTrue(controls['ext_apply']);self.assertTrue(controls['pass'])
                self.assertNotIn('run',controls)

    def test_unexplained_display_failure_is_reported_once_not_retried(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(app,'RUN',Path(d)/'runtime'):
                ui=app.App(Path(d)/'state',artifact_root=Path(d)/'artifacts',capture_logs=False)
            ui.mode='test';ui.session.data['index']=next(i for i,s in enumerate(app.STEPS) if s['id']=='dp-video')
            ui.framebuffer=SimpleNamespace(external=None,previous_image=None)
            # An exception without a message must still count as an error.
            with patch('external_display.ExternalDisplay',side_effect=StopIteration()) as opened:
                for _ in range(3):ui.sync_external()
            self.assertEqual(opened.call_count,1)
            self.assertTrue(ui.external_error)

    def test_kernel_without_external_connector_is_explained(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'sys/class/drm/card0-DSI-1').mkdir(parents=True)
            internal={'id':32,'type':16,'name':'DSI-1','connected':True,'modes':[],'possible_crtcs':1,'properties':{'CRTC_ID':{'value':94}}}
            drm=Mock();drm.connectors.return_value=[internal];drm.primary.return_value=(46,{'FB_ID':{'value':97}})
            sysfs=lambda p:root/str(p).lstrip('/') if str(p).startswith('/sys') else Path(p)
            with patch('external_display.DRM',return_value=drm),patch('external_display.Path',side_effect=sysfs):
                with self.assertRaises(RuntimeError) as failure:
                    ExternalDisplay(root/'runtime',root/'artifacts')
            self.assertIn('external',str(failure.exception).lower())
            drm.close.assert_called_once()

if __name__=='__main__':unittest.main()
