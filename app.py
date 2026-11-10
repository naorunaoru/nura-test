#!/usr/bin/env python3
"""One-test-at-a-time framebuffer UI with controls only at the bottom."""
from pathlib import Path
import argparse, fcntl, json, mmap, os, platform, select, signal, struct, subprocess, sys, time
from PIL import Image, ImageDraw, ImageFont, ImageChops
from core import Session, atomic_json, now
import actions
from sensor_views import draw_imu, draw_compass, SensorAnimation
from reports import ensure_run, collect_logs

ROOT=Path(__file__).resolve().parent
STATE=Path('/var/lib/nura-tests');RUN=Path('/run/nura-tests')
STEPS=json.loads((ROOT/'steps.json').read_text())
BG='#0d121a';FG='#e9eff8';MUTED='#9aa9bf';ACCENT='#80e5bd'
LIVE_ACTIONS=('imu','compass')
PASSIVE_ACTIONS=actions.PASSIVE_ACTIONS
SAFE_TOP=64  # 192 physical pixels; leave the camera cutout clear.
FONT=Path('/usr/share/fonts/droid')

def restore_console():
    try:
        from external_display import recover
        recover(RUN);actions.recover()
    except Exception as error:print('Restoration needs attention:',error,flush=True)
    try:
        state=json.loads((RUN/'console.json').read_text())
        fd=os.open('/dev/tty8',os.O_RDWR|os.O_NOCTTY)
        try:fcntl.ioctl(fd,0x4b3a,0);fcntl.ioctl(fd,0x5606,state['previous_vt'])
        finally:os.close(fd)
    except (OSError,ValueError,KeyError):pass
    for p in Path('/sys/class/leds').glob('white:flash-*/brightness'):
        try:p.write_text('0')
        except OSError:pass
    try:Path('/sys/class/graphics/fb0/blank').write_text('0')
    except OSError:pass

class Framebuffer:
    def __init__(self):
        RUN.mkdir(mode=0o700,parents=True,exist_ok=True)
        from external_display import recover
        recover(RUN)
        self.external=None
        previous=int(Path('/sys/class/tty/tty0/active').read_text().strip()[3:])
        if previous==8 and (RUN/'console.json').exists():previous=json.loads((RUN/'console.json').read_text())['previous_vt']
        atomic_json(RUN/'console.json',{'previous_vt':previous})
        self.tty=os.open('/dev/tty8',os.O_RDWR|os.O_NOCTTY)
        fcntl.ioctl(self.tty,0x5606,8);fcntl.ioctl(self.tty,0x5607,8)
        fcntl.ioctl(self.tty,0x4b3a,1)
        self.fd=os.open('/dev/fb0',os.O_RDWR)
        v=bytearray(160);f=bytearray(80)
        fcntl.ioctl(self.fd,0x4600,v);fcntl.ioctl(self.fd,0x4602,f)
        self.screeninfo=v
        values=struct.unpack('40I',v);self.width,self.height=values[:2]
        if values[6]!=32 or (values[8],values[11],values[14])!=(16,8,0):raise RuntimeError('Unsupported framebuffer pixel layout')
        if self.width!=1440 or self.height!=3168:raise RuntimeError('Internal display must be in portrait 1440×3168 mode')
        self.stride=struct.unpack_from('I',f,48)[0]
        self.memory=mmap.mmap(self.fd,self.stride*self.height,flags=mmap.MAP_SHARED,prot=mmap.PROT_READ|mmap.PROT_WRITE)
        self.previous_image=None

    def draw(self,image):
        box=None if self.previous_image is None else ImageChops.difference(image,self.previous_image).getbbox()
        if self.previous_image is not None and box is None:return
        if box is None:box=(0,0,image.width,image.height)
        else:box=(max(0,box[0]-2),max(0,box[1]-2),min(image.width,box[2]+2),min(image.height,box[3]+2))
        self.previous_image=image.copy()
        sx=self.width/image.width;sy=self.height/image.height
        x0,y0,x1,y1=(round(box[0]*sx),round(box[1]*sy),round(box[2]*sx),round(box[3]*sy))
        # Sample beyond the write rectangle so bilinear filtering matches a
        # full-frame resize even when an unchanged edge lies on its boundary.
        padded=(max(0,box[0]-1),max(0,box[1]-1),min(image.width,box[2]+1),min(image.height,box[3]+1))
        px0,py0,px1,py1=(round(padded[0]*sx),round(padded[1]*sy),round(padded[2]*sx),round(padded[3]*sy))
        scaled=image.crop(padded).resize((px1-px0,py1-py0),Image.Resampling.BILINEAR)
        scaled=scaled.crop((x0-px0,y0-py0,x1-px0,y1-py0))
        raw=scaled.tobytes('raw','BGRX');row_bytes=(x1-x0)*4
        if x0==0 and row_bytes==self.stride:self.memory[y0*self.stride:y1*self.stride]=raw
        else:
            for row,y in enumerate(range(y0,y1)):
                offset=y*self.stride+x0*4;self.memory[offset:offset+row_bytes]=raw[row*row_bytes:(row+1)*row_bytes]
        # The internal command-mode DSI panel needs a display commit after
        # mmap writes. External DP scans the same memory continuously.
        if getattr(self,'external',None):self.external.flush_internal()
        else:fcntl.ioctl(self.fd,0x4606,self.screeninfo)

    def close(self):
        self.memory.close();os.close(self.fd);restore_console();os.close(self.tty)

class Renderer:
    width=480;height=1056
    def __init__(self):
        self.fonts={}

    def font(self,size,bold=False):
        key=(size,bold)
        if key not in self.fonts:self.fonts[key]=ImageFont.truetype(str(FONT/('DroidSans-Bold.ttf' if bold else 'DroidSans.ttf')),size)
        return self.fonts[key]

    def text(self,draw,text,xy,size=21,color=FG,width=432,max_lines=7,bold=False):
        x,y=xy;font=self.font(size,bold);lines=[]
        for para in str(text).split('\n'):
            line=''
            for word in para.split():
                candidate=(line+' '+word).strip()
                if line and draw.textlength(candidate,font=font)>width:lines.append(line);line=word
                else:line=candidate
            lines.append(line)
        overflow=len(lines)>max_lines;lines=lines[:max_lines]
        for i,line in enumerate(lines):
            clipped=draw.textlength(line,font=font)>width or (overflow and i==len(lines)-1)
            if clipped:
                while line and draw.textlength(line+'…',font=font)>width:line=line[:-1]
                line+='…'
            draw.text((x,y),line,font=font,fill=color);y+=int(size*1.4)
        return y

    def buttons(self,app):
        if app.mode=='welcome':
            return [('jump_imu','IMU',(16,882,156,950),'#254f68',True),('jump_compass','Compass',(168,882,308,950),'#254f68',True),('jump_display','Display',(320,882,464,950),'#254f68',True),
                    ('exit','Close',(16,968,137,1040),'#263449',True),('start','Resume' if app.session.data['answers'] else 'Start',(148,968,464,1040),'#275f4c',True)]
        if app.mode=='summary':
            complete=app.session.data['completed']
            return [('new' if complete else 'review','New run' if complete else 'Review',(16,968,234,1040),'#263449',True),('exit','Close',(246,968,464,1040),'#275f4c',True)]
        step=STEPS[app.session.data['index']];busy=app.worker is not None
        available=app.pending_action is None
        passive=step['action'] in PASSIVE_ACTIONS
        buttons=[('back','Back',(16,882,119,950),'#263449',available),
                 ('pause','Pause',(361,882,464,950),'#263449',available),
                 ('pass','Pass',(16,968,156,1040),'#275f4c',available and (passive or bool(app.result and app.result.get('ok')))),
                 ('fail','Fail',(168,968,308,1040),'#76333e',available),
                 ('skip','Skip',(320,968,464,1040),'#344052',available)]
        if step.get('external_display'):
            connected=bool(app.external and app.external.connected and app.external.modes)
            buttons += [('ext_prev','< Res',(16,790,120,860),'#254f68',available and connected),
                        ('ext_next','Res >',(130,790,234,860),'#254f68',available and connected),
                        ('ext_hz','Hz',(244,790,348,860),'#254f68',available and connected),
                        ('ext_apply','Apply',(358,790,464,860),'#275f4c',available and connected)]
            if passive:
                if not busy and app.result:buttons.append(('run','Retry',(130,882,350,950),'#254f68',available))
                else:buttons.append(('ext_native','Preferred' if connected else 'Refresh',(130,882,350,950),'#254f68',available))
        if not passive or (not busy and app.result and not step.get('external_display')):
            buttons.append(('run','Stop' if busy else 'Retry' if app.result else 'Run',(130,882,350,950),'#7c3843' if busy else '#254f68',available))
        elif step['action'] in ('bluetooth','wifi') and len(app.scan_state.get('devices',[]))>6:
            buttons.append(('more','Next page',(130,882,350,950),'#254f68',available))
        return buttons

    def external_view(self,image,draw,app,y):
        from external_display import label
        ext=app.external
        if ext is None:
            self.text(draw,app.external_error or 'Opening display…',(24,y),21,MUTED,max_lines=6);return
        if not ext.connected:
            self.text(draw,'Connect an external display',(24,y),23,MUTED,max_lines=2);return
        self.text(draw,'Output: '+label(ext.current),(24,y),19,ACCENT,max_lines=2)
        self.text(draw,'Selected: '+label(ext.selected),(24,y+56),23,bold=True,max_lines=2)
        self.text(draw,ext.hdr,(24,y+116),16,MUTED,max_lines=1)
        if getattr(ext,'preview',None):image.paste(ext.preview,((480-ext.preview.width)//2,y+154))
        error=ext.error or (app.result or {}).get('error','')
        self.text(draw,error or app.status_text,(24,700),17,'#ff9b9b' if error else MUTED,max_lines=3)

    def scan(self,draw,app,y):
        rows=app.scan_state.get('devices',[]);pages=max(1,(len(rows)+5)//6)
        page=app.scan_page%pages;kind=STEPS[app.session.data['index']]['action']
        self.text(draw,f'{len(rows)} '+('devices' if kind=='bluetooth' else 'networks')+f' · {page+1}/{pages}',(24,y),17,MUTED,max_lines=1)
        if not rows:self.text(draw,'No devices seen yet' if kind=='bluetooth' else 'No networks found yet',(24,y+45),22,MUTED,max_lines=2)
        for row in rows[page*6:page*6+6]:
            y+=68
            name=row.get('name') or ('Unnamed device' if kind=='bluetooth' else 'Hidden SSID')
            self.text(draw,name,(24,y),21,width=300,max_lines=1,bold=True)
            signal=(str(row['rssi_dbm'])+' dBm' if 'rssi_dbm' in row else '—') if kind=='bluetooth' else str(row['signal_percent'])+'%'
            self.text(draw,signal,(346,y+2),16,ACCENT,width=100,max_lines=1)
            detail=row['address']
            if kind=='wifi':detail+=f" · ch {row['channel']} · {row['security']}"+(' · connected' if row.get('connected') else '')
            self.text(draw,detail,(24,y+30),14,MUTED,max_lines=1)
            draw.line((24,y+61,456,y+61),fill='#243144')

    def render(self,app):
        image=Image.new('RGB',(self.width,self.height),BG);draw=ImageDraw.Draw(image)
        if app.mode=='welcome':
            self.text(draw,'Hardware checks',(24,SAFE_TOP),30,bold=True)
            self.text(draw,'OnePlus 8 Pro',(24,115),21,MUTED)
            self.text(draw,app.session.data['kernel'],(24,151),16,MUTED,max_lines=2)
            counts=app.session.counts();count=len(app.session.data['answers'])
            total=app.session.data.get('test_count',len(STEPS))
            self.text(draw,f'{count} / {total} checks saved',(24,249),26,bold=True)
            self.text(draw,f"{counts['pass']} pass · {counts['fail']} fail · {counts['skip']} skip",(24,293),21,MUTED)
            if app.session.data['index']<len(STEPS):
                self.text(draw,'Next: '+STEPS[app.session.data['index']]['title'],(24,364),23,max_lines=2)
            self.text(draw,app.message,(24,650),19,ACCENT,max_lines=3)
            self.text(draw,str(app.output_directory),(24,780),16,MUTED,max_lines=3)
        elif app.mode=='summary':
            counts=app.session.counts()
            self.text(draw,'Results',(24,SAFE_TOP),30,bold=True)
            self.text(draw,f"{counts['pass']} pass · {counts['fail']} fail · {counts['skip']} skip",(24,125),23,ACCENT,max_lines=2)
            failures=[a['title'] for a in app.session.data['answers'].values() if a['verdict']=='fail']
            self.text(draw,'Failed checks' if failures else 'No failed checks',(24,230),23,bold=True)
            self.text(draw,'\n'.join(failures),(24,280),21,MUTED,max_lines=15)
            self.text(draw,app.message or app.session.data['output_directory'],(24,786),17,MUTED,max_lines=3)
        else:
            index=app.session.data['index'];step=STEPS[index];action=step['action']
            self.text(draw,f'{index+1:02d} / {len(STEPS)}',(24,SAFE_TOP),16,MUTED,max_lines=1)
            y=self.text(draw,step['title'],(24,103),28,bold=True,max_lines=2)
            if action in LIVE_ACTIONS:
                view=draw_imu if action=='imu' else draw_compass
                view(self,draw,app.sensor_animation.frame(app.sensor_state,app.worker is not None),app.worker is not None)
            else:
                y=self.text(draw,step['instruction'],(24,y+18),21,max_lines=3)
                y=self.text(draw,step.get('expected',step['criterion']),(24,y+12),18,MUTED,max_lines=3)
                y=max(y+24,300)
                if step.get('external_display'):
                    self.external_view(image,draw,app,280)
                elif action in ('bluetooth','wifi'):
                    self.scan(draw,app,min(y,320))
                elif action=='touch':
                    for x in range(16,465,32):draw.line((x,y,x,860),fill='#243144')
                    for yy in range(y,861,32):draw.line((16,yy,464,yy),fill='#243144')
                    if app.touch_coverage:image.paste(app.touch_coverage,(0,0),app.touch_coverage)
                elif action=='pattern':
                    for i,c in enumerate(('#f3f3f3','#e34646','#5cb775','#4678df','#242424','#868686')):
                        x=24+(i%3)*144;yy=y+(i//3)*180
                        draw.rectangle((x,yy,x+136,yy+172),fill=c)
                elif action.startswith('camera_') and (RUN/'preview.png').exists():
                    try:
                        with Image.open(RUN/'preview.png') as im:
                            im.thumbnail((432,400));image.paste(im,((480-im.width)//2,y))
                    except OSError:pass
                else:
                    self.text(draw,app.status_text or app.message,(24,y),22,ACCENT,max_lines=12)
            # Keep errors visible even on pages with a dedicated sensor/scan view.
            footer=app.message or (app.status_text if action in ('bluetooth','wifi') else '')
            if app.result and not app.result.get('ok'):footer=app.result.get('error','Check failed')
            previous=app.session.data['answers'].get(step['id'])
            if not footer and previous:footer='Previous: '+previous['verdict']
            if not step.get('external_display'):
                self.text(draw,footer,(24,818),16,'#ff9b9b' if app.result and not app.result.get('ok') else MUTED,max_lines=2)
        for _,label,rect,color,enabled in self.buttons(app):
            draw.rounded_rectangle(rect,radius=9,fill=color if enabled else '#18212d')
            size=22 if len(label)<10 else 17;font=self.font(size,True)
            w=draw.textlength(label,font=font);draw.text(((rect[0]+rect[2]-w)/2,(rect[1]+rect[3]-size)/2-2),label,font=font,fill=FG if enabled else '#647186')
        return image

class App:
    def __init__(self,directory=STATE,artifact_root=Path('/tmp/mqa'),capture_logs=True):
        self.session=Session(directory,STEPS,platform.release());self.mode='summary' if self.session.data['completed'] else 'welcome'
        self.worker=None;self.pending_action=None;self.epoch=0;self.result=None;self.status_text='';self.message='';self.started=0
        self.dirty=True;self.stop=False;self.cancelled=False;self.cancel_time=0
        self.renderer=Renderer();self.touch_trace=None;self.touch_axes=None;self.touch_start=0;self.touch_coverage=None;self.touch_points=[];self.touch_marker=None;self.last_action=0;self.status_mtime=0
        self.sensor_state={};self.sensor_mtime=0;self.sensor_animation=SensorAnimation()
        self.scan_state={};self.scan_mtime=0;self.scan_page=0
        self.external=None;self.external_error='';self.framebuffer=None
        self.artifact_root=Path(artifact_root);self.capture_logs=capture_logs
        self.prepare_run()

    def generation(self):return self.mode,self.session.data['index'],self.epoch

    def display_updates_allowed(self):
        # FBIOPAN_DISPLAY commits the fbdev modeset and turns a blanked
        # command-mode panel back on. Let the blanking worker own the display.
        return not (self.worker is not None and
                    STEPS[self.session.data['index']]['action']=='panel_cycle')

    def button_at(self,x,y):
        for key,_,r,_,enabled in self.renderer.buttons(self):
            if enabled and r[0]<=x<=r[2] and r[1]<=y<=r[3]:return key
        return None

    def clear(self):
        self.result=None;self.status_text='';self.message='';self.touch_points=[];self.touch_marker=None;self.status_mtime=0
        self.sensor_state={};self.sensor_mtime=0;self.sensor_animation=SensorAnimation()
        self.scan_state={};self.scan_mtime=0;self.scan_page=0
        self.touch_coverage=None;self.touch_start=self.touch_trace.count if self.touch_trace else 0
        for name in ('sensors.json','preview.png','status.json','scan.json','result.json','stop'):
            (RUN/name).unlink(missing_ok=True)

    def enter_test(self):
        self.clear();self.mode='test';self.epoch+=1
        if STEPS[self.session.data['index']]['action'] in PASSIVE_ACTIONS:self.start_worker()

    def start_worker(self):
        self.clear();self.cancelled=False;self.started=time.monotonic();self.epoch+=1
        RUN.mkdir(mode=0o700,parents=True,exist_ok=True)
        if STEPS[self.session.data['index']]['action']=='touch':
            self.touch_coverage=Image.new('RGBA',(480,1056),(0,0,0,0))
        try:
            actions.recover()
            self.worker=subprocess.Popen([sys.executable,str(ROOT/'actions.py'),STEPS[self.session.data['index']]['action'],'--out',str(RUN/'result.json'),'--artifacts',str(self.output_directory),'--runtime',str(RUN)],start_new_session=True)
        except Exception as error:self.result={'ok':False,'error':str(error)}

    def act(self,key,force=False):
        if not force and time.monotonic()-self.last_action<.35:return
        if self.pending_action is not None:return
        self.last_action=time.monotonic();self.dirty=True
        if key.startswith('ext_'):
            if not self.external:
                self.external_error='';self.sync_external();return
            if key=='ext_prev':self.external.select_resolution(-1)
            elif key=='ext_next':self.external.select_resolution(1)
            elif key=='ext_hz':self.external.select_refresh()
            elif key=='ext_apply':self.external.apply()
            elif key=='ext_native':
                if self.external.connected:self.external.native()
                else:self.external.poll(force=True)
            return
        if key=='more':self.scan_page+=1;return
        if self.worker:
            if key=='run':self.cancel();return
            if key in ('pass','fail','skip','back','pause','exit'):
                self.pending_action=key;self.cancel();return
            return
        if key=='exit':self.stop=True;return
        if key=='start':
            if self.session.data['completed'] or self.session.data['index']>=len(STEPS):self.mode='summary'
            else:self.enter_test()
            return
        if key=='pause':self.mode='welcome';return
        if key in ('jump_imu','jump_compass','jump_display'):
            self.session.data['index']=next(i for i,s in enumerate(STEPS) if (s['id']=='dp-video' if key=='jump_display' else s['action']==key[5:]))
            self.session.save();self.enter_test();return
        if key=='review':self.session.data['index']=0;self.session.save();self.enter_test();return
        if key=='new':
            self.session=Session(self.session.directory,STEPS,platform.release(),fresh=True)
            self.prepare_run();self.reset_touch_trace();self.clear();self.mode='welcome';return
        if key=='back':self.session.back();self.enter_test();return
        if key=='run':self.start_worker();return
        if key in ('pass','fail','skip'):
            if key=='pass' and STEPS[self.session.data['index']]['action'] not in PASSIVE_ACTIONS and not (self.result and self.result.get('ok')):return
            evidence={'run':self.result,'status':self.status_text,'device':actions.snapshot()}
            if self.external:evidence['external_display']=self.external.evidence()
            if self.scan_state:evidence['scan']=self.scan_state
            if self.touch_trace:
                evidence['touch_trace']={'file':str(self.touch_trace.path.relative_to(self.output_directory)),
                                         'after_seq':self.touch_start,'through_seq':self.touch_trace.count}
            if self.touch_coverage:
                path=actions.numbered_file(self.output_directory/'logs','touch-coverage','.png')
                self.touch_coverage.save(path);evidence['touch_coverage']=str(path.relative_to(self.output_directory))
            self.session.answer(key,evidence);self.clear();self.save_logs()
            self.finish_or_resume()
            if self.mode=='test':self.enter_test()

    def sync_external(self):
        if self.framebuffer is None:return
        wanted=self.mode=='test' and STEPS[self.session.data['index']].get('external_display',False)
        if not wanted:
            self.close_external();self.external_error='';return
        try:
            if self.external is None and not self.external_error:
                from external_display import ExternalDisplay
                self.external=ExternalDisplay(RUN,self.output_directory)
                self.framebuffer.external=self.external;self.dirty=True
            elif self.external and self.external.poll():self.dirty=True
        except Exception as error:
            self.external_error=str(error)
            if self.external:self.external.error=str(error)
            self.dirty=True

    def close_external(self):
        if self.external:
            try:self.external.close()
            finally:
                self.external=None
                if self.framebuffer:
                    self.framebuffer.external=None;self.framebuffer.previous_image=None
                self.dirty=True

    def reset_touch_trace(self):
        if self.touch_trace:self.touch_trace.close();self.touch_trace=None
        if self.touch_axes:
            from touch_trace import TouchTrace
            self.touch_trace=TouchTrace(self.output_directory,'s6sy761',*self.touch_axes)

    def touch_context(self):
        return {'mode':self.mode,'step':STEPS[self.session.data['index']]['id'] if self.mode=='test' else None}

    def touch_frame(self,slots,event_time):
        active=sum(p['active'] for p in slots.values())
        context=self.touch_context();action=None
        for slot,point in slots.items():
            if point['raw_x'] is None or point['raw_y'] is None:continue
            x=(point['raw_x']-self.touch_axes[0][0])*479/(self.touch_axes[0][1]-self.touch_axes[0][0])
            y=(point['raw_y']-self.touch_axes[1][0])*1055/(self.touch_axes[1][1]-self.touch_axes[1][0])
            if point['active'] and point['begin'] is None:
                point['begin']=(self.button_at(x,y),self.generation(),x,y)
            begin=point['begin']
            if begin and (active>1 or abs(x-begin[2])>16 or abs(y-begin[3])>16):point['moved']=True
            if point.get('changed') and (point['active'] or point.get('release')):
                phase='up' if point.get('release') else 'down' if point.get('new') else 'move'
                if self.touch_trace:
                    self.touch_trace.record(slot,point,phase,{**context,'control':self.button_at(x,y)},event_time)
                if self.mode=='test' and STEPS[self.session.data['index']]['action']=='touch' and self.touch_coverage:
                    # Dots record observed positions; joining points could paint over a dead zone.
                    ImageDraw.Draw(self.touch_coverage).ellipse((x-2,y-2,x+2,y+2),fill='#fbe28b')
                    self.dirty=True
                point['changed']=False;point['new']=False
            if point.get('release'):
                point['release']=False;point['begin']=None
                if begin and begin[0] and not point.get('moved') and begin[1]==self.generation() and begin[0]==self.button_at(x,y):action=begin[0]
        if action:self.act(action)

    def finish_or_resume(self):
        if self.session.data['index']<len(STEPS):return
        missing=[i for i,s in enumerate(STEPS) if s['id'] not in self.session.data['answers']]
        if missing:
            self.session.data['index']=missing[0];self.session.save();self.mode='welcome'
            self.message='Unanswered checks remain.'
        else:
            self.session.finish(actions.snapshot());self.mode='summary';self.save_logs()

    def cancel(self):
        if self.worker and self.worker.poll() is None:
            self.cancelled=True;self.cancel_time=time.monotonic();self.message='Stopping…'
            if STEPS[self.session.data['index']]['action'] in PASSIVE_ACTIONS:(RUN/'stop').touch()
            else:
                try:os.killpg(self.worker.pid,signal.SIGTERM)
                except ProcessLookupError:pass

    def poll_worker(self):
        if not self.worker:return
        live=STEPS[self.session.data['index']]['action'] in LIVE_ACTIONS
        if STEPS[self.session.data['index']]['action'] not in PASSIVE_ACTIONS and time.monotonic()-self.started>35 and not self.cancelled:self.cancel()
        if self.cancelled and time.monotonic()-self.cancel_time>4:
            try:os.killpg(self.worker.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        p=RUN/'status.json'
        try:
            if p.stat().st_mtime_ns!=self.status_mtime:
                self.status_mtime=p.stat().st_mtime_ns;self.status_text=json.loads(p.read_text())['text'];self.dirty=True
        except (OSError,ValueError):pass
        if live:
            p=RUN/'sensors.json'
            try:
                if p.stat().st_mtime_ns!=self.sensor_mtime:
                    self.sensor_mtime=p.stat().st_mtime_ns;self.sensor_state=json.loads(p.read_text());self.dirty=True
            except (OSError,ValueError):pass
        p=RUN/'scan.json'
        try:
            if p.stat().st_mtime_ns!=self.scan_mtime:
                self.scan_mtime=p.stat().st_mtime_ns;self.scan_state=json.loads(p.read_text());self.dirty=True
        except (OSError,ValueError):pass
        if self.worker.poll() is not None:
            code=self.worker.returncode;self.worker=None
            try:self.result=json.loads((RUN/'result.json').read_text())
            except (OSError,ValueError):self.result={'ok':False,'error':f'Test stopped (exit {code}).'}
            try:actions.recover()
            except Exception as error:self.result={'ok':False,'cleanup_failed':True,'error':'Restoration failed: '+str(error)}
            if not self.result.get('ok'):
                if live:self.sensor_state['error']=self.result.get('error','Sensor test failed')
                self.status_text=self.result.get('error','Check failed')
            elif not self.status_text:self.status_text='Finished'
            pending=self.pending_action;self.pending_action=None;self.message='';self.dirty=True
            if pending and not self.result.get('cleanup_failed'):self.act(pending,force=True)

    def prepare_run(self):
        first_output=not self.session.data.get('output_directory')
        self.output_directory=ensure_run(self.session.data,self.artifact_root)
        if first_output and self.session.data.get('app_version') in (None,'1.0','1.1'):
            self.session.data['artifact_note']='Imported an earlier run. Its camera previews were already deleted; photos are retained for new camera tests.'
        self.session.save()
        if self.capture_logs:
            if not (self.output_directory/'dmesg-start.log').exists():
                collect_logs(self.output_directory,initial=True)
            self.save_logs()

    def save_logs(self):
        if self.capture_logs:
            self.session.data['log_capture']=collect_logs(self.output_directory)
            self.session.save()

    def run(self):
        from evdev import InputDevice,list_devices,ecodes
        fb=None;dev=None
        try:
            fb=Framebuffer();self.framebuffer=fb
            for path in list_devices():
                d=InputDevice(path)
                if d.name=='s6sy761':dev=d;break
                d.close()
            if dev is None:raise RuntimeError('Internal touchscreen not found')
            self.touch_axes=tuple((info.min,info.max) for info in
                                  (dev.absinfo(ecodes.ABS_MT_POSITION_X),dev.absinfo(ecodes.ABS_MT_POSITION_Y)))
            if any(hi<=lo for lo,hi in self.touch_axes):raise RuntimeError('Invalid touch axis ranges')
            self.reset_touch_trace()
            dev.grab();slots={};slot=0;last_frame=0;dropped=False
            def stop(*_):self.stop=True
            signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
            while not self.stop:
                self.poll_worker()
                self.sync_external()
                live=self.mode=='test' and STEPS[self.session.data['index']]['action'] in LIVE_ACTIONS
                interval=1/30 if live else 0
                if self.display_updates_allowed() and time.monotonic()-last_frame>=interval and (self.dirty or (self.worker and (live or time.monotonic()-last_frame>1))):
                    last_frame=time.monotonic()
                    fb.draw(self.renderer.render(self));self.dirty=False
                    atomic_json(RUN/'ui.json',{'mode':self.mode,'index':self.session.data['index'],'busy':self.worker is not None,'updated':now()})
                delay=max(.001,min(.01,interval-(time.monotonic()-last_frame))) if live else .1
                if not select.select([dev],[],[],delay)[0]:continue
                try:events=dev.read()
                except BlockingIOError:continue
                for event in events:
                    if event.type==ecodes.EV_SYN and event.code==ecodes.SYN_DROPPED:
                        if self.touch_trace:self.touch_trace.dropped(self.touch_context())
                        slots={};dropped=True;continue
                    if dropped:
                        if event.type==ecodes.EV_SYN and event.code==ecodes.SYN_REPORT:dropped=False
                        continue
                    if event.type==ecodes.EV_ABS:
                        if event.code==ecodes.ABS_MT_SLOT:slot=event.value;continue
                        point=slots.setdefault(slot,{'raw_x':None,'raw_y':None,'active':False,'begin':None,'tracking_id':None})
                        if event.code==ecodes.ABS_MT_TRACKING_ID:
                            if event.value<0:point.update(release=True,active=False,changed=True)
                            else:point.update(active=True,begin=None,release=False,new=True,changed=True,moved=False,tracking_id=event.value)
                        elif event.code==ecodes.ABS_MT_POSITION_X:point.update(raw_x=event.value,changed=True)
                        elif event.code==ecodes.ABS_MT_POSITION_Y:point.update(raw_y=event.value,changed=True)
                    if event.type==ecodes.EV_SYN and event.code==ecodes.SYN_REPORT:
                        self.touch_frame(slots,event.timestamp())
        finally:
            if self.worker:
                self.cancel()
                try:self.worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:os.killpg(self.worker.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
            if dev:
                try:dev.ungrab()
                except OSError:pass
                dev.close()
            try:self.close_external()
            except Exception as error:print('Display restoration:',error,flush=True)
            if fb:fb.close()
            else:restore_console()
            (RUN/'preview.png').unlink(missing_ok=True)
            if self.touch_trace:self.touch_trace.close()
            self.save_logs()

def main():
    p=argparse.ArgumentParser();p.add_argument('--preview');p.add_argument('--step',type=int,default=-1);p.add_argument('--restore',action='store_true');a=p.parse_args()
    if a.restore:restore_console();return
    if a.preview:
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            app=App(directory,artifact_root=Path(directory)/'mqa',capture_logs=False)
            if a.step>=0:app.mode='test';app.session.data['index']=a.step
            app.renderer.render(app).save(a.preview)
        return
    App().run()

if __name__=='__main__':main()
