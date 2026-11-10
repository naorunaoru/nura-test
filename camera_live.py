"""Camera diagnostics using the port's installed libcamera cam and SSC tools."""
import json
import math
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import time

from PIL import Image
from core import atomic_json, now
from launcher import managed_options, prctl

MODELS = {'camera_main':'imx689', 'camera_wide':'imx586', 'camera_front':'imx471',
          'camera_tele':'s5k3m5', 'camera_mono':'gc5035'}
PARAM = Path('/sys/module/lc898128/parameters/enable')
OIS_DEVICE = Path('/sys/bus/i2c/devices/20-0024')
MAIN_SENSOR = Path('/sys/bus/i2c/devices/20-001a')
OIS_LIMIT = 20  # This port blacklists the actuator after reports of sustained buzzing.
FRAME = re.compile(r'\(([\d.]+) fps\) (cam\d+-stream\d+) seq: (\d+) bytesused: (\d+)')
CONFIG = re.compile(r'^(?:0: |.*configuring streams: \(0\) )(\d+)x(\d+)-([^/\s]+)')
META = re.compile(r'^\s+([a-zA-Z]\w*) = (.+)$')


def scalar(text):
    text = text.strip()
    if text.lower() in ('true','false'): return text.lower() == 'true'
    try: return int(text)
    except ValueError:
        try: return float(text)
        except ValueError: return text


def capabilities(output):
    info = {'controls':{}, 'formats':{}, 'properties':{}}
    current = None
    for line in output.splitlines():
        m = re.match(r'^Property: (\w+) = (.+)',line)
        if m:
            info['properties'][m[1]] = m[2]
        m = re.match(r'^Control: \[([^]]*)\]\s+(\w+)::(\w+):\s*(.*)',line)
        if m:
            current = {'name':m[3], 'vendor':m[2], 'direction':m[1].strip(),
                       'description':m[4], 'choices':[], 'type':'unsupported'}
            info['controls'][m[3]] = current
            bounds = re.match(r'\[([^\s]+?)\.\.([^\s\]]+)\]',m[4])
            if bounds:
                lo, hi = (scalar(x) for x in bounds.groups())
                if isinstance(lo,(float,int)) and isinstance(hi,(float,int)):
                    current.update(min=lo,max=hi,type='bool' if isinstance(lo,bool) else
                                   'float' if isinstance(lo,float) or isinstance(hi,float) else 'int')
            continue
        m = re.match(r'^\s+- (\w+) \((-?\d+)\)(.*)',line)
        if current and m:
            current['choices'].append({'label':m[1], 'value':int(m[2])})
            current['type']='enum'
            if '[default]' in m[3]: current['default']=int(m[2])
        if current and line.strip().startswith('Size:'): current['type']='unsupported'
        m = re.match(r'^ \* Pixelformat: (\S+) \((\d+)x(\d+)\)-\((\d+)x(\d+)\)',line)
        if m: info['formats'][m[1]] = [int(m[4]),int(m[5])]
        m = CONFIG.match(line)
        if m: info['default_stream'] = [int(m[1]),int(m[2]),m[3]]
    info['model'] = info['properties'].get('Model','')
    dim = re.fullmatch(r'(\d+)x(\d+)',info['properties'].get('PixelArraySize',''))
    info['sensor'] = [int(n) for n in dim.groups()] if dim else None
    return info


def discover(kind, run, stopped):
    from actions import command
    ids = re.findall(r'^\d+: [^\n]*\((/[^)]+)\)$', command(['cam','-l'],full=True),re.M)
    cache = run/'camera-ids.json'
    try: hints=json.loads(cache.read_text())
    except (ValueError,OSError): hints={}
    model=MODELS[kind]
    ids.sort(key=lambda id_: id_ != hints.get(model))
    for id_ in ids:
        if stopped(): raise InterruptedError('Stopped')
        base=['cam','-c',id_]
        props=command(base+['--list-properties'],full=True)
        actual=capabilities(props)['model'];hints[actual]=id_
        if actual.lower()!=model: continue
        caps=capabilities(command(base+['--list-properties','--list-controls','--info','-s','role=still'],full=True))
        if caps['model'].lower()!=model: raise RuntimeError('Camera identity changed during discovery')
        raw=capabilities(command(base+['--info','-s','role=raw'],check=False,full=True))
        caps.update(id=id_,raw=raw.get('default_stream'),raw_formats=raw['formats'])
        atomic_json(cache,hints)
        return caps
    raise RuntimeError(f'{model} not found; discovered: '+', '.join(sorted(k for k,v in hints.items() if v in ids)))


def checked_value(control,value):
    if 'in' not in control['direction']: raise ValueError('Read-only control')
    kind=control['type']
    if kind=='enum' and type(value) is int and value in [c['value'] for c in control['choices']]: return value
    if kind=='bool' and type(value) is bool: return value
    if kind in ('int','float') and type(value) in (int,float) and math.isfinite(value):
        if control['min']<=value<=control['max'] and (kind=='float' or int(value)==value):
            return int(value) if kind=='int' else float(value)
    raise ValueError('Value is outside the advertised scalar control range')


def capture_spec(caps, target, raw):
    fmt=(caps.get('raw') or [None,None,None])[2] if raw else 'XRGB8888'
    if not fmt: raise ValueError('No raw stream advertised')
    if target=='sensor': size=caps['sensor']
    elif target=='maximum': size=(caps['raw_formats'] if raw else caps['formats']).get(fmt)
    elif target=='reference' and not raw:size=[1280,960]
    else: raise ValueError('Unknown capture target')
    if not size: raise ValueError('Camera did not advertise the requested dimensions')
    return {'target':target,'size':size,'format':fmt,'raw':raw}


def readback(control,value,metadata):
    if control['name'] not in metadata: return 'not reported'
    actual=metadata[control['name']]
    if isinstance(actual,(int,float)) and isinstance(value,(int,float)):
        return 'matches' if math.isclose(actual,value,rel_tol=1e-4,abs_tol=1e-5) else 'differs'
    return 'matches' if actual==value else 'differs'


def save_still(path,destination,spec,configured,bytesused):
    wanted=tuple(spec['size'])
    if configured!=[*wanted,spec['format']]: raise RuntimeError(f'Expected {wanted} {spec["format"]}; configured {configured}')
    if not path.exists() or path.stat().st_size==0: raise RuntimeError('No capture data received')
    if spec['raw']:
        if not bytesused or path.stat().st_size!=bytesused: raise RuntimeError('Incomplete raw buffer')
        bits=re.fullmatch(r'S(?:BGGR|GBRG|GRBG|RGGB)(\d+)_CSI2P',spec['format'])
        if bits and bytesused < math.ceil(wanted[0]*int(bits[1])/8)*wanted[1]:
            raise RuntimeError('Raw buffer is smaller than the requested Bayer image')
        shutil.copyfile(path,destination)
    else:
        with Image.open(path) as im:
            if im.size!=wanted: raise RuntimeError(f'Expected {wanted}; image is {im.size}')
            im.load();im.save(destination,format='PNG',compress_level=1)


def system_command(args):
    p=subprocess.run(args,capture_output=True,text=True,timeout=6,**managed_options())
    if p.returncode: raise RuntimeError((p.stderr or p.stdout).strip() or f'{args[0]} failed')


def recover_ois(runtime):
    journal=Path(runtime)/'camera-ois-restore.json'
    if not journal.exists(): return
    saved=json.loads(journal.read_text())
    # Allow the sensor's normal autosuspend to park the lens before removing
    # our driver. Never change a system-wide PM policy to make the test pass.
    pm=OIS_DEVICE/'power/runtime_status'
    deadline=time.monotonic()+2
    while (OIS_DEVICE/'driver').is_symlink() and pm.read_text().strip()=='active' and time.monotonic()<deadline:
        time.sleep(.04)
    if saved['loaded']:
        if not PARAM.exists(): raise RuntimeError('Previously loaded OIS driver is missing')
        PARAM.write_text(saved['enable'])
        if PARAM.read_text().strip()!=saved['enable']: raise RuntimeError('OIS parameter was not restored')
    elif PARAM.parent.parent.exists():
        system_command(['modprobe','-r','lc898128'])
        if PARAM.parent.parent.exists(): raise RuntimeError('Test-loaded OIS module is still present')
    journal.unlink()


def request_ois(runtime,enabled=True):
    journal=Path(runtime)/'camera-ois-restore.json'
    loaded=PARAM.parent.parent.exists()
    old=PARAM.read_text().strip() if loaded else None
    if loaded and old not in ('Y','N','1','0'): raise RuntimeError('Unknown OIS enable parameter')
    if enabled != (old in ('Y','1')):
        atomic_json(journal,{'loaded':loaded,'enable':old})
        if not loaded: system_command(['modprobe','lc898128','enable=1'])
        else: PARAM.write_text('1' if enabled else '0')
    if loaded or enabled:
        if PARAM.read_text().strip() not in (('Y','1') if enabled else ('N','0')):
            raise RuntimeError('Driver did not accept the OIS parameter')
    if enabled and not (OIS_DEVICE/'driver').is_symlink():
        raise RuntimeError('OIS requested but driver did not bind to the lens')


def wait_main_standby(cancelled):
    # The OIS device link joins on the sensor's next power-up. cam can exit
    # while the IMX689 is still in its one-second autosuspend grace period.
    if (MAIN_SENSOR/'name').read_text().strip()!='imx689':
        raise RuntimeError('Cannot identify the OIS-associated main sensor')
    deadline=time.monotonic()+4
    while (MAIN_SENSOR/'power/runtime_status').read_text().strip()!='suspended':
        if cancelled():raise InterruptedError('Stopped')
        if time.monotonic()>deadline:raise RuntimeError('IMX689 still powered; cannot switch OIS reliably')
        time.sleep(.04)


def child(args,**options):
    # External camera/gyro tools must stop even if their worker alone is killed.
    return subprocess.Popen([sys.executable,__file__,str(os.getpid()),*args],
                            stdin=subprocess.DEVNULL,**managed_options(),**options)


def end_child(p,sig=signal.SIGINT):
    if p and p.poll() is None:
        p.send_signal(sig)
        try:p.wait(timeout=2)
        except subprocess.TimeoutExpired:p.kill();p.wait(timeout=2)


class Camera:
    def __init__(self,kind,runtime,artifacts):
        from actions import numbered_file
        self.kind=kind;self.run=Path(runtime);self.art=Path(artifacts);self.numbered=numbered_file
        self.events=numbered_file(self.art/'logs',kind+'-events','.jsonl')
        self.scratch=self.run/'camera-stream';self.scratch.mkdir(exist_ok=True)
        self.proc=None;self.gyro=None;self.gyrofd=None;self.kmsg=None;self.rawlog=None
        self.pending=b'';self.image=None;self.spec=None;self.configured=None;self.capture_path=None
        self.settings={};self.trigger={};self.published=0;self.last_image=0;self.bytesused=0
        self.frame_seq=None;self.image_seq=None;self.image_metadata={};self.frame_metadata={};self.next_image=None
        self.last_frame=0;self.started=0;self.diagnostics=[];self.attempt=None
        self.state={'phase':'probing','camera':kind,'message':'Reading camera capabilities…',
                    'events':str(self.events.relative_to(self.art)),'captures':[],'verdicts':[],
                    'frames':0,'metadata':{},'requested':{},'ack':0,'ois_requested':False}
        self.publish()

    def event(self,event,**values):
        with self.events.open('a') as f:f.write(json.dumps({'time':now(),'event':event,**values})+'\n')

    def publish(self):
        atomic_json(self.run/'camera.json',self.state);self.published=time.monotonic()

    def stop_requested(self):
        return (self.run/'stop').exists()

    def init(self):
        self.caps=discover(self.kind,self.run,self.stop_requested)
        path=self.numbered(self.art/'logs',self.kind+'-capabilities','.json')
        atomic_json(path,self.caps)
        self.state.update(caps=self.caps,inventory=str(path.relative_to(self.art)),phase='idle',message='Ready')
        ctl=self.caps['controls'].get('AfMode',{})
        if any(c['value']==2 for c in ctl.get('choices',[])):self.settings['AfMode']=2
        self.event('capabilities',file=self.state['inventory'],model=self.caps['model'],id=self.caps['id'])
        self.publish()

    def sample_gyro(self):
        if self.gyro is None:return
        if self.gyro.poll() is not None: raise RuntimeError('SSC gyroscope stream ended')
        while select.select([self.gyrofd],[],[],0)[0]:
            data=os.read(self.gyrofd,8192)
            if not data:break
            self.gyro_tail+=data
            lines=self.gyro_tail.split(b'\n');self.gyro_tail=lines.pop()
            self.state['gyro_samples']+=sum(b'Gyroscope sensor measurement:' in line for line in lines)
        if self.state['gyro_samples']!=self.gyro_count:
            self.gyro_count=self.state['gyro_samples'];self.gyro_last=time.monotonic()
        if time.monotonic()-self.gyro_last>3: raise RuntimeError('No fresh SSC gyroscope samples for OIS')

    def start_ois(self):
        if self.kind!='camera_main':raise ValueError('This port only exposes main-camera OIS')
        self.gyrofd,slave=pty.openpty();self.gyro_tail=b'';self.gyro_count=0;self.gyro_last=time.monotonic()
        try:self.gyro=child(['ssccli','--sensor','gyroscope','--timeout','86400'],stdout=slave,stderr=slave)
        finally:os.close(slave)
        self.state['gyro_samples']=0
        while not self.state['gyro_samples']:
            if self.stop_requested() or (self.run/'camera-abort').exists():raise InterruptedError('Stopped')
            self.sample_gyro();time.sleep(.02)
        try:
            self.kmsg=os.open('/dev/kmsg',os.O_RDONLY|os.O_NONBLOCK);os.lseek(self.kmsg,0,os.SEEK_END)
        except OSError:
            if self.kmsg is not None:os.close(self.kmsg);self.kmsg=None
        if self.stop_requested() or (self.run/'camera-abort').exists():raise InterruptedError('Stopped')
        request_ois(self.run)
        self.event('ois-request',enabled=True,driver_bound=True,gyro_samples=self.state['gyro_samples'])

    def kernel_notes(self):
        if self.kmsg is None:return
        while True:
            try:data=os.read(self.kmsg,16384).decode(errors='replace')
            except BlockingIOError:break
            except OSError:break
            msg=data.partition(';')[2].strip().partition('\n')[0]
            if 'lc898128' in msg.lower():
                self.event('ois-kernel',message=msg)
                if any(x in msg.lower() for x in ('not taken','failed','timed out','timeout')):
                    self.state['ois_warning']=msg
        if self.state.get('ois_warning'):raise RuntimeError(self.state['ois_warning'])

    def start(self,spec=None,ois=False):
        self.stop()
        self.spec=spec;self.configured=None;self.image=None;self.bytesused=0;self.pending=b'';self.diagnostics=[]
        self.frame_seq=self.image_seq=None;self.image_metadata={};self.frame_metadata={};self.next_image=None
        self.attempt={'operation':'still' if spec else 'preview','requested':spec or {'size':[640,480],'format':'XRGB8888'},
                      'ois_requested':bool(ois),'controls':{**self.settings,**self.trigger}}
        self.event('request',**self.attempt)
        self.state.update(phase='starting',frames=0,metadata={},requested=self.attempt['controls'],ois_requested=bool(ois),
                          ois_warning='',ois_pm='',gyro_samples=0,message='Opening stream…',error='',fps=0,configured=None,
                          preview_seq=None,metadata_seq=None)
        self.publish()
        if self.kind=='camera_main' and (ois or PARAM.exists()):
            wait_main_standby(lambda:self.stop_requested() or (self.run/'camera-abort').exists())
        if ois:self.start_ois()
        elif self.kind=='camera_main':request_ois(self.run,False)
        if self.stop_requested() or (self.run/'camera-abort').exists():raise InterruptedError('Stopped')
        (self.run/'camera-preview.jpg').unlink(missing_ok=True)
        fmt=spec['format'] if spec else 'XRGB8888';w,h=spec['size'] if spec else (640,480)
        raw=bool(spec and spec['raw']);role='raw' if raw else 'still' if spec else 'viewfinder'
        args=['cam','-c',self.caps['id'],'-s',f'role={role},width={w},height={h},pixelformat={fmt}',
              '--strict-formats','--metadata','--capture=1' if spec else '--capture']
        controls={**self.settings,**self.trigger};self.trigger={}
        if controls:
            script=self.scratch/'controls.yaml'
            script.write_text('frames:\n  - 0:\n'+''.join(f'      {name}: {json.dumps(checked_value(self.caps["controls"][name],val))}\n' for name,val in controls.items()))
            args+=['--script='+str(script)]
        self.extension='bin' if raw else 'ppm'
        args+=['--file='+str(self.scratch/('frame-#.'+self.extension))]
        rawpath=self.numbered(self.art/'logs',self.kind+'-stream','.log')
        self.rawlog=rawpath.open('wb');self.attempt['log']=str(rawpath.relative_to(self.art))
        self.rawlog.write((' '.join(args)+'\n').encode())
        self.proc=child(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        self.started=self.last_frame=time.monotonic();self.capture_path=None
        self.state.update(phase='capture' if spec else 'preview',log=self.attempt['log'])
        self.publish()

    def line(self,line):
        m=CONFIG.match(line)
        if m:self.configured=[int(m[1]),int(m[2]),m[3]];self.state['configured']=self.configured
        m=FRAME.search(line)
        if m:
            self.finish_frame()
            self.last_frame=time.monotonic();self.state['frames']+=1;self.state['fps']=float(m[1]);self.bytesused=int(m[4])
            self.frame_seq=int(m[3]);self.frame_metadata={}
            path=self.scratch/f'frame-{m[2]}-{int(m[3]):06d}.{self.extension}'
            if self.spec:self.capture_path=path
            else:
                try:
                    if self.last_frame-self.last_image>=.09:
                        with Image.open(path) as im:self.next_image=im.copy()
                        self.last_image=self.last_frame
                finally:path.unlink(missing_ok=True)
            self.state['message']='Receiving frames'
        m=META.match(line)
        if m:self.frame_metadata[m[1]]=scalar(m[2])
        if not line.startswith('\t') and re.search("error|failed|disallowed|invalid|not supported|timed.out|adjusted|can't allocate",line,re.I):
            self.diagnostics=(self.diagnostics+[line.strip()])[-8:]

    def finish_frame(self):
        # cam writes metadata after the sequence line, often in a separate pipe read.
        if self.frame_seq is None:return
        self.state.update(metadata=self.frame_metadata,metadata_seq=self.frame_seq)
        if self.next_image is not None:
            self.image=self.next_image;self.next_image=None;self.image_seq=self.frame_seq
            self.image_metadata=self.frame_metadata;self.preview_image(self.image);self.state['preview_seq']=self.frame_seq

    def preview_image(self,im):
        thumb=im.copy();thumb.thumbnail((432,250))
        tmp=self.scratch/'preview.tmp';thumb.save(tmp,format='JPEG',quality=85);os.replace(tmp,self.run/'camera-preview.jpg')

    def save_frame(self,tag):
        if self.image is None:raise ValueError('No preview frame yet')
        p=self.numbered(self.art/'photos',self.kind+'-'+tag,'.png')
        self.image.save(p,compress_level=1);return str(p.relative_to(self.art))

    def tick(self):
        if not self.proc:return
        self.sample_gyro();self.kernel_notes()
        if time.monotonic()-self.last_frame>(18 if self.spec else 8):
            raise RuntimeError('Timed out waiting for a camera frame')
        if self.state['ois_requested']:
            remaining=OIS_LIMIT-(time.monotonic()-self.started)
            self.state['ois_seconds_left']=max(0,math.ceil(remaining))
            if remaining<=0:
                self.stop();self.state['message']='OIS trial ended; driver restored';self.publish();return
            try:self.state['ois_pm']=(OIS_DEVICE/'power/runtime_status').read_text().strip()
            except OSError:self.state['ois_pm']='unavailable'
            if self.state['frames'] and time.monotonic()-self.started>5 and self.state['ois_pm']!='active':
                raise RuntimeError('OIS requested but actuator power is '+self.state['ois_pm'])
        fd=self.proc.stdout.fileno()
        if select.select([fd],[],[],.03)[0]:
            data=os.read(fd,65536)
            if data:
                self.rawlog.write(data);self.pending+=data
                lines=self.pending.split(b'\n');self.pending=lines.pop()
                for line in lines:self.line(line.decode(errors='replace').rstrip('\r'))
                return
        if self.proc.poll() is not None:
            self.finish_frame()
            if not self.spec or self.proc.returncode or not self.capture_path:
                specific=[line for line in self.diagnostics if line not in ('Failed to start camera session','Failed to create camera session')]
                detail=(specific or self.diagnostics or ['No completed capture'])[-1]
                if 'disallowed by --strict-formats' in detail:detail='Exact configuration rejected: camera would adjust the request'
                raise RuntimeError(detail+f' (cam exit {self.proc.returncode})')
            suffix='.bin' if self.spec['raw'] else '.png'
            dest=self.numbered(self.art/'photos',self.kind+'-'+self.spec['target'],suffix)
            try:save_still(self.capture_path,dest,self.spec,self.configured,self.bytesused)
            except Exception:dest.unlink(missing_ok=True);raise
            if not self.spec['raw']:
                with Image.open(dest) as im:self.preview_image(im)
            record={**self.attempt,'file':str(dest.relative_to(self.art)),'configured':self.configured,
                    'bytesused':self.bytesused,'metadata':self.state['metadata'],'status':'captured'}
            if self.spec['raw'] and self.bytesused%self.configured[1]==0:
                record['bytes_per_row']=self.bytesused//self.configured[1]
            atomic_json(dest.with_suffix('.json'),record);self.event('capture',**record)
            self.state['captures'].append(record);self.attempt=None;self.stop()
            self.state['message']='Saved '+dest.name;self.publish()

    def stop(self):
        try:
            if self.proc:
                end_child(self.proc)
                self.proc.stdout.close();self.proc=None
            if self.rawlog:self.rawlog.close();self.rawlog=None
            if self.attempt:
                self.event('stream-end',**self.attempt,frames=self.state['frames'],metadata=self.state['metadata'],
                           configured=self.configured,gyro_samples=self.state.get('gyro_samples',0),ois_pm=self.state.get('ois_pm'))
        finally:
            try:recover_ois(self.run)
            finally:
                if self.gyro:end_child(self.gyro,signal.SIGTERM);self.gyro=None
                for name in ('gyrofd','kmsg'):
                    fd=getattr(self,name)
                    if fd is not None:os.close(fd);setattr(self,name,None)
                for p in self.scratch.glob('frame-*'):p.unlink(missing_ok=True)
                self.attempt=None;self.state.update(phase='idle',ois_requested=False)

    def dispatch(self,cmd):
        op=cmd['op']
        if op=='stop':
            self.stop();(self.run/'camera-abort').unlink(missing_ok=True);self.state.update(message='Stopped',error='')
        elif op=='preview':self.start(ois=cmd.get('ois',False))
        elif op=='still':self.start(capture_spec(self.caps,cmd['target'],bool(cmd['raw'])))
        elif op=='apply':
            name=cmd['name'];control=self.caps['controls'][name];val=checked_value(control,cmd['value'])
            if name=='AfTrigger':
                self.trigger[name]=val
                if 'AfMode' in self.caps['controls']:self.settings['AfMode']=1
            else:self.settings[name]=val
            self.start(ois=cmd.get('ois',False))
        elif op=='snapshot':
            path=self.save_frame('preview');self.event('snapshot',file=path,seq=self.image_seq,metadata=self.image_metadata)
            self.state['snapshot']=path
        elif op=='verdict':
            name=cmd['name'];verdict=cmd['verdict']
            if verdict not in ('works','no effect') or name not in self.state['requested'] or self.image is None or not self.proc:
                raise ValueError('Apply this control and inspect the live image first')
            val=self.state['requested'][name];meta=self.image_metadata
            record={'name':name,'requested':val,'readback':meta.get(name),'comparison':readback(self.caps['controls'][name],val,meta),
                    'verdict':verdict,'seq':self.image_seq,'metadata':dict(meta),'file':self.save_frame('control')}
            self.event('control-verdict',**record);self.state['verdicts'].append(record);self.state['message']=f'{name}: {verdict}'
        else:raise ValueError('Unknown camera command')

    def failed(self,error):
        msg=str(error)
        self.event('error',detail=msg,attempt=self.attempt,diagnostics=self.diagnostics,
                   configured=self.configured,metadata=self.state['metadata'])
        if self.attempt and self.spec:
            self.state['captures'].append({**self.attempt,'status':'failed','error':msg,'configured':self.configured})
        self.stop();self.state.update(error=msg,message=msg);self.publish()

    def loop(self):
        try:
            self.init()
            if not self.stop_requested():
                try:self.start()
                except Exception as error:self.failed(error)
            while not self.stop_requested():
                try:
                    p=self.run/'camera-command.json'
                    if p.exists():
                        cmd=json.loads(p.read_text())
                        if cmd['seq']>self.state['ack']:
                            try:self.dispatch(cmd)
                            finally:self.state['ack']=cmd['seq'];self.publish()
                    self.tick()
                except Exception as error:
                    self.failed(error)
                if self.proc:
                    if time.monotonic()-self.published>.1:self.publish()
                else:time.sleep(.04)
        finally:
            self.stop();self.publish();shutil.rmtree(self.scratch,ignore_errors=True)
        return {k:self.state.get(k) for k in ('inventory','events','captures','verdicts','error')}


if __name__=='__main__':
    parent=int(sys.argv[1]);prctl(1,signal.SIGTERM)
    if os.getppid()!=parent:sys.exit(1)
    os.execvp(sys.argv[2],sys.argv[2:])
