"""Hardware checks with cooperative stopping and device restoration."""
from pathlib import Path
import argparse, array, codecs, json, math, os, platform, pty, re, select, shlex, signal, subprocess, time, wave
from core import atomic_json
from launcher import managed_options, arm_parent_death

RUN=Path('/run/nura-tests')
BACKLIGHT=Path('/sys/class/backlight/ae94000.dsi.0/brightness')
ARTIFACTS=None
ACTION_LOG=None
PASSIVE_ACTIONS={'pattern','touch','imu','compass','accelerometer','tilt','light',
                 'proximity','connection','external_touch','bluetooth','wifi','nfc'}

def stopped():return (RUN/'stop').exists()

def wait_live(seconds):
    end=time.monotonic()+seconds
    while not stopped() and time.monotonic()<end:time.sleep(.05)

def log(text):
    if ACTION_LOG:
        with ACTION_LOG.open('a') as target:target.write(text)

def numbered_file(directory,stem,suffix):
    directory.mkdir(parents=True,exist_ok=True)
    number=1
    while True:
        path=directory/f'{stem}-{number:03d}{suffix}'
        try:
            with path.open('x'):pass
            return path
        except FileExistsError:number+=1

def read(path):
    try:return Path(path).read_text().strip()
    except OSError:return None

def command(args, timeout=8, check=True, cancellable=False, full=False):
    log('$ '+shlex.join(args)+'\n')
    p=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,
                       env={**os.environ,'LC_ALL':'C'},**managed_options())
    start=time.monotonic()
    try:
        while True:
            try:output=p.communicate(timeout=.1)[0];break
            except subprocess.TimeoutExpired:
                if cancellable and stopped():return ''
                if time.monotonic()-start>timeout:raise RuntimeError(f'{args[0]} timed out')
        log(output+'\n')
        if check and p.returncode:raise RuntimeError(output[-1200:] or f'{args[0]} exited {p.returncode}')
        return output if full else output[-7000:]
    finally:
        if p.poll() is None:
            p.terminate()
            try:output=p.communicate(timeout=1)[0]
            except subprocess.TimeoutExpired:p.kill();output=p.communicate()[0]
            log(output+'\n')

def snapshot():
    result={'kernel':platform.release(),'brightness':read(BACKLIGHT),'power':[], 'usb':[], 'displays':[]}
    for p in Path('/sys/class/power_supply').glob('*'):
        result['power'].append({'name':p.name,**{k:read(p/k) for k in ('status','online','capacity','voltage_now','current_now','usb_type')}})
    for p in Path('/sys/bus/usb/devices').glob('*'):
        if (p/'idVendor').exists():
            result['usb'].append({'name':read(p/'product'),'vid':read(p/'idVendor'),'pid':read(p/'idProduct'),'speed_mbps':read(p/'speed')})
    for p in Path('/sys/class/drm').glob('card*-*'):
        if (p/'status').exists():result['displays'].append({'connector':p.name,'status':read(p/'status'),'enabled':read(p/'enabled')})
    # Count complete kernel output rather than relying on the truncated command result.
    p=subprocess.run(['dmesg'],capture_output=True,text=True,timeout=5)
    result['kernel_error_counts']={k:len(re.findall(pattern,p.stdout)) for k,pattern in {
        'warnings':r'WARNING:', 'dsi_lock_failure':r'DSI PLL\(\d+\) lock failed',
        'qrtr_rejections':r'failed while handling packet','phy_timeout':r'phy initialization timed-out'}.items()}
    return result

def status(text):
    atomic_json(RUN/'status.json',{'text':text,'time':time.time()})

def stream(args,seconds=None,on_line=None,interactive=False):
    log('$ '+shlex.join(args)+'\n')
    master,slave=pty.openpty()
    try:
        p=subprocess.Popen(args,stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
                           stdout=slave,stderr=slave,env={**os.environ,'LC_ALL':'C'},**managed_options())
    except Exception:
        os.close(slave);os.close(master);raise
    os.close(slave);end=time.monotonic()+seconds if seconds is not None else float('inf');data='';pending=''
    decoder=codecs.getincrementaldecoder('utf-8')(errors='replace')
    try:
        if interactive:p.stdin.write(b'scan on\n');p.stdin.flush()
        while not stopped() and time.monotonic()<end:
            if select.select([master],[],[],.15)[0]:
                try:raw=os.read(master,4096)
                except OSError:break
                if not raw:break
                chunk=decoder.decode(raw)
                log(chunk)
                pending+=chunk.replace('\r','');lines=pending.split('\n');pending=lines.pop()
                for line in lines:
                    line=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',line).strip()
                    if not line:continue
                    data=(data+line+'\n')[-7000:]
                    if on_line:on_line(line)
                    else:status('\n'.join(data.splitlines()[-4:]))
            elif p.poll() is not None:break
        if not stopped() and (p.poll() is not None or seconds is None):
            raise RuntimeError('Stream ended: '+data[-1000:])
        return data
    finally:
        if p.poll() is None:p.terminate()
        try:p.wait(timeout=2)
        except subprocess.TimeoutExpired:p.kill();p.wait()
        if p.stdin:p.stdin.close()
        os.close(master)

def mixer_set(name,value,saved):
    if name not in saved:
        old=command(['amixer','-c','0','cget','name='+name])
        saved[name]=re.search(r': values=(.*)',old).group(1).strip()
        atomic_json(RUN/'mixer-restore.json',saved)
    command(['amixer','-c','0','cset','name='+name,str(value)])

def recover():
    """Finish restoration if a worker was interrupted or killed."""
    p=RUN/'restore.json'
    if p.exists():
        values=json.loads(p.read_text())
        allowed={str(BACKLIGHT),'/sys/class/graphics/fb0/blank',
                 '/sys/class/leds/white:flash-0/brightness',
                 '/sys/class/leds/white:flash-1/brightness'}
        for path,value in values.items():
            if path not in allowed:raise ValueError('Invalid restoration path')
            Path(path).write_text(str(value))
        p.unlink()
    p=RUN/'mixer-restore.json'
    if p.exists():
        for name,value in json.loads(p.read_text()).items():
            command(['amixer','-c','0','cset','name='+name,value],timeout=3)
        p.unlink()
    (RUN/'microphone.wav').unlink(missing_ok=True)
    (RUN/'audio.wav').unlink(missing_ok=True)
    p=RUN/'bluetooth-restore.json'
    if p.exists():
        command(['bluetoothctl','power','off'])
        p.unlink()
    p=RUN/'nfc-restore.json'
    if p.exists():
        values=json.loads(p.read_text());base=['busctl','--system']
        dest=['org.neard','/org/neard/nfc0','org.neard.Adapter']
        if not values['polling'] and 'true' in command(base+['get-property',*dest,'Polling'],timeout=3):
            command(base+['call',*dest,'StopPollLoop'],timeout=3)
        if not values['powered']:command(base+['set-property',*dest,'Powered','b','false'],timeout=3)
        p.unlink()

def set_restore(path,value):
    atomic_json(RUN/'restore.json',{str(path):value})

def audio(kind):
    saved={};wav=RUN/'audio.wav';capture=RUN/'microphone.wav'
    def c(name,value):mixer_set(name,value,saved)
    try:
        if kind=='microphone':
            c('MultiMedia3 Mixer VA_CODEC_DMA_TX_0',1)
            for idx,dmic in [(0,'DMIC2'),(1,'DMIC1')]:
                c(f'VA_AIF1_CAP Mixer DEC{idx}',1);c(f'VA DEC{idx} MUX','VA_DMIC')
                c(f'VA DMIC MUX{idx}',dmic);c(f'VA_DEC{idx} Volume',108)
            status('Recording for three seconds. Speak now.')
            command(['arecord','-q','-D','hw:0,2','-f','S16_LE','-r','48000','-c','2','-d','3',str(capture)],timeout=8)
            c('TERT_MI2S_RX Audio Mixer MultiMedia1',1)
            status('Playing your recording through the phone.')
            command(['aplay','-q','-D','hw:0,0',str(capture)],timeout=8)
            return {'recorded_seconds':3,'recording_retained':False}
        dp=kind=='dp_audio'
        if dp and not any(read(p)=='connected' for p in Path('/sys/class/drm').glob('card*-DP-*/status')):
            raise RuntimeError('No DisplayPort monitor is connected.')
        c('DISPLAY_PORT_RX Audio Mixer MultiMedia8' if dp else 'TERT_MI2S_RX Audio Mixer MultiMedia1',1)
        rate=48000;seconds=3 if dp else 1.2;samples=array.array('h')
        for i in range(int(rate*seconds)):
            t=i/rate;envelope=max(0,min(1,t/.03,(seconds-t)/.03))
            left=(t<1 or t>=2) if dp else kind=='speaker_left'
            right=t>=1 if dp else kind=='speaker_right'
            samples.extend((int(1800*envelope*math.sin(2*math.pi*440*t)) if left else 0,
                            int(1800*envelope*math.sin(2*math.pi*660*t)) if right else 0))
        with wave.open(str(wav),'wb') as f:
            f.setparams((2,2,rate,0,'NONE','not compressed'));f.writeframes(samples.tobytes())
        status('Playing left, right, then both.' if dp else 'Playing a short tone.')
        command(['aplay','-q','-D','hw:0,7' if dp else 'hw:0,0',str(wav)],timeout=10)
        return {'stream_completed':True,'route':kind}
    finally:
        errors=[]
        for name,value in reversed(list(saved.items())):
            try:command(['amixer','-c','0','cset','name='+name,value],timeout=3)
            except Exception:errors.append(name)
        wav.unlink(missing_ok=True);capture.unlink(missing_ok=True)
        if errors:raise RuntimeError('Could not restore audio controls: '+', '.join(errors))
        (RUN/'mixer-restore.json').unlink(missing_ok=True)

def haptic():
    from evdev import InputDevice,list_devices,ecodes,ff
    devices=[InputDevice(p) for p in list_devices()]
    dev=next((d for d in devices if d.name=='aw8695-haptics'),None)
    if not dev:raise RuntimeError('Haptics input device is unavailable.')
    effect=ff.Effect(ecodes.FF_RUMBLE,-1,0,ff.Trigger(0,0),ff.Replay(80,0),ff.EffectType(ff_rumble_effect=ff.Rumble(0x1800,0)))
    eid=dev.upload_effect(effect)
    try:dev.write(ecodes.EV_FF,eid,1);time.sleep(.15)
    finally:
        dev.write(ecodes.EV_FF,eid,0);dev.erase_effect(eid)
        for d in devices:d.close()
    return {'pulse_ms':80,'magnitude':0x1800}

def external_touch():
    from evdev import InputDevice,list_devices,ecodes
    devices=[];counts={};points=[]
    for path in list_devices():
        d=InputDevice(path)
        if d.info.bustype==ecodes.BUS_USB and ecodes.ABS_MT_POSITION_X in d.capabilities(absinfo=False).get(ecodes.EV_ABS,[]):
            devices.append(d);counts[d.path]=0
        else:d.close()
    if not devices:raise RuntimeError('No USB touchscreen accessory was found.')
    try:
        while not stopped():
            for d in select.select(devices,[],[],.2)[0]:
                for e in d.read():
                    if e.type==ecodes.EV_ABS and e.code in (ecodes.ABS_MT_POSITION_X,ecodes.ABS_MT_POSITION_Y):
                        counts[d.path]+=1;points.append([e.code,e.value]);points=points[-100:]
                        status(f'USB touch: {sum(counts.values())} coordinate events\n{d.name}: {e.value}')
        return {'scope':'USB peripheral','devices':[d.name for d in devices],'coordinate_events':sum(counts.values()),'recent_coordinates':points}
    finally:
        for d in devices:d.close()

def nfc():
    base=['busctl','--system'];dest=['org.neard','/org/neard/nfc0','org.neard.Adapter']
    powered='true' in command(base+['get-property',*dest,'Powered'])
    polling='true' in command(base+['get-property',*dest,'Polling'])
    seen=set()
    atomic_json(RUN/'nfc-restore.json',{'powered':powered,'polling':polling})
    try:
        if not powered:command(base+['set-property',*dest,'Powered','b','true'])
        if not polling:command(base+['call',*dest,'StartPollLoop','s','Initiator'])
        while not stopped():
            tree=command(base+['tree','org.neard'],cancellable=True)
            seen.update(re.findall(r'/org/neard/nfc0/tag\d+',tree))
            status('Tag detected.' if seen else 'Hold the tag near the rear of the phone.')
            wait_live(.5)
        return {'tag_detected':bool(seen),'tag_contents_read':False}
    finally:
        recover()

def camera(kind):
    from PIL import Image
    if 'lc898128 ' in (read('/proc/modules') or ''):raise RuntimeError('OIS is active; stop it before camera testing.')
    ids={'camera_main':'/base/soc@0/cci@ac4f000/i2c-bus@0/camera@1a',
         'camera_wide':'/base/soc@0/cci@ac4f000/i2c-bus@1/camera@10',
         'camera_front':'/base/soc@0/cci@ac50000/i2c-bus@1/camera@10',
         'camera_tele':'/base/soc@0/cci@ac4f000/i2c-bus@1/camera@37',
         'camera_mono':'/base/soc@0/cci@ac50000/i2c-bus@0/camera@2d'}
    ppm=RUN/'camera.ppm'
    try:
        status('Capturing a local preview. OIS remains off.')
        output=command(['cam','-c',ids[kind],'-s','role=still,width=1280,height=960,pixelformat=XRGB8888','--capture=15','--file='+str(ppm)],timeout=22)
        with Image.open(ppm) as im:
            photo=numbered_file(ARTIFACTS/'photos',kind,'.png')
            im.convert('RGB').save(photo)
            im.thumbnail((1200,900));im.convert('RGB').save(RUN/'preview.png')
        return {'preview_created':True,'camera':kind,'output':output[-1200:],'photo':str(photo.relative_to(ARTIFACTS))}
    finally:ppm.unlink(missing_ok=True)

def scan_update(kind,rows):
    state={'kind':kind,'devices':rows,'updated':time.time()}
    atomic_json(RUN/'scan.json',state)
    return state

def bluetooth_scan():
    from radio_scan import BluetoothDevices
    devices=BluetoothDevices();scan_update('bluetooth',[])
    status('Scanning…')
    old=command(['bluetoothctl','show'])
    if 'Powered:' not in old:raise RuntimeError('No Bluetooth adapter')
    powered='Powered: yes' in old
    published=[]
    def receive(line):
        nonlocal published
        devices.feed(line);rows=devices.rows()
        if rows!=published:scan_update('bluetooth',rows);published=rows
    try:
        if not powered:
            atomic_json(RUN/'bluetooth-restore.json',{'powered':False})
            command(['bluetoothctl','power','on'])
        # An interactive client owns discovery until it exits; no global scan-off
        # command, which could interfere with another client's discovery.
        stream(['bluetoothctl'],on_line=receive,interactive=True)
        return {'devices':devices.rows(),'paired':False}
    finally:
        if not powered:recover()

def wifi_scan():
    from radio_scan import wifi_rows
    rows=[];scan_update('wifi',rows)
    if command(['nmcli','radio','wifi']).strip()!='enabled':
        raise RuntimeError('Wi-Fi is off. Enable it, then Retry.')
    while not stopped():
        status('Scanning…')
        output=command(['nmcli','--colors','no','--terse','--escape','yes',
                        '--fields','BSSID,SSID,CHAN,SIGNAL,SECURITY,IN-USE',
                        'device','wifi','list','--rescan','yes'],timeout=20,cancellable=True,full=True)
        if stopped():break
        rows=wifi_rows(output);scan_update('wifi',rows)
        status('Scanning · updated '+time.strftime('%H:%M:%S'));wait_live(5)
    return {'devices':rows,'connected_by_test':False}

def perform(action):
    if action in ('pattern','touch'):
        status('' if action=='pattern' else 'Touch recording active')
        while not stopped():wait_live(.2)
        return {'observed':True}
    if action=='brightness':
        old=read(BACKLIGHT)
        set_restore(BACKLIGHT,old or '32')
        try:
            for value in (96,256,32):BACKLIGHT.write_text(str(value));time.sleep(.8)
        finally:recover()
        return {'levels':[96,256,32],'restored':read(BACKLIGHT)}
    if action=='panel_cycle':
        p=Path('/sys/class/graphics/fb0/blank')
        set_restore(p,'0')
        try:
            p.write_text('4');start=time.monotonic();time.sleep(2)
            elapsed=time.monotonic()-start
        finally:recover()
        return {'requested_off_seconds':2,'waited_seconds':round(elapsed,3),'restored':read(p)=='0'}
    if action=='haptic':return haptic()
    if action in ('torch0','torch1'):
        p=Path('/sys/class/leds/white:flash-'+action[-1]+'/brightness')
        if read(p)!='0':raise RuntimeError('LED is already in use; turn it off first.')
        set_restore(p,'0')
        try:p.write_text('10');time.sleep(.25)
        finally:recover()
        return {'led':action[-1],'level':10,'duration_ms':250,'off':read(p)=='0'}
    if action in ('speaker_left','speaker_right','dp_audio','microphone'):return audio(action)
    if action in ('imu','compass'):
        from sensor_live import capture
        path=numbered_file(ARTIFACTS/'logs',action+'-samples','.jsonl')
        result=capture(action,RUN,path)
        result['samples_file']=str(path.relative_to(ARTIFACTS))
        return result
    if action in ('accelerometer','tilt','light','proximity'):
        option='accel' if action in ('accelerometer','tilt') else action
        status('Waiting for sensor data…')
        events=stream(['monitor-sensor','--'+option])
        if action=='tilt':
            labels=re.findall(r'(?:tilt: |Tilt changed: )([a-z-]+)',events)
            labels=[label for i,label in enumerate(labels) if label!='undefined' and (i==0 or label!=labels[i-1])]
            status('Recorded tilt labels:\n'+' → '.join(labels))
        return {'sensor_events':events}
    if action=='external_touch':return external_touch()
    if action=='connection':
        state={}
        while not stopped():
            state=snapshot();parts=[]
            for p in state['power']:
                if p['capacity'] is not None:parts.append(f"Battery {p['capacity']}%: {p['status']}")
                elif p['online']=='1':parts.append(f"USB {p['voltage_now']} uV, {p['current_now']} uA")
            parts += [f"{u['name'] or 'USB device'}: {u['speed_mbps']} Mb/s" for u in state['usb'] if u['vid']!='1d6b']
            parts += [f"{p['connector']}: {p['status']}" for p in state['displays'] if 'Writeback' not in p['connector']]
            status('\n'.join(parts[-10:]));wait_live(1)
        return state
    if action=='bluetooth':return bluetooth_scan()
    if action=='wifi':return wifi_scan()
    if action=='nfc':return nfc()
    if action.startswith('camera_'):return camera(action)
    raise ValueError('Unknown test action')

def main():
    global ARTIFACTS,ACTION_LOG,RUN
    arm_parent_death()
    p=argparse.ArgumentParser();p.add_argument('action');p.add_argument('--out',required=True);p.add_argument('--artifacts',type=Path,required=True);p.add_argument('--runtime',type=Path,default=RUN);a=p.parse_args()
    RUN=a.runtime
    ARTIFACTS=a.artifacts;ACTION_LOG=numbered_file(ARTIFACTS/'logs',a.action,'.log')
    RUN.mkdir(mode=0o700,exist_ok=True)
    def cancel(*_):
        if a.action in PASSIVE_ACTIONS:(RUN/'stop').touch()
        else:raise InterruptedError('Stopped by user')
    signal.signal(signal.SIGTERM,cancel)
    start=time.time();result={'action':a.action,'started':start,'log':str(ACTION_LOG.relative_to(ARTIFACTS))}
    try:result.update(ok=True,result=perform(a.action))
    except Exception as error:result.update(ok=False,error=str(error)[-1500:])
    result['seconds']=round(time.time()-start,2)
    log('\n'+json.dumps(result,indent=2)+'\n')
    atomic_json(a.out,result)

if __name__=='__main__':main()
