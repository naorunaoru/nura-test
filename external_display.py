"""External pattern/mode session and crash recovery; internal timing is unchanged."""
import base64
import ctypes as C
import json
import time
from pathlib import Path
from core import atomic_json
from drm_display import DRM,Buffer,CONNECTOR,CRTC,PLANE,mode_choices
from display_pattern import Pattern

PLANE_FIELDS=('FB_ID','CRTC_ID','SRC_X','SRC_Y','SRC_W','SRC_H','CRTC_X','CRTC_Y','CRTC_W','CRTC_H')

def label(mode):
    if mode is None:return 'None'
    return f'{mode.hdisplay}×{mode.vdisplay} · {mode.hz:.2f} Hz'+(' interlaced' if mode.flags&16 else '')

def restore(drm,saved,connected=True):
    changes={int(obj):dict(props) for obj,props in saved['changes'].items()}
    crtc=saved['crtc'];connector=saved['connector'];plane=saved['plane']
    for obj,kind in ((crtc,CRTC),(connector,CONNECTOR),(plane,PLANE)):drm.properties(obj,kind)
    blob=0
    try:
        if saved['mode'] and connected:
            blob=drm.blob(base64.b64decode(saved['mode']));changes[crtc]['MODE_ID']=blob
        if not connected:
            changes={connector:{'CRTC_ID':0},crtc:{'MODE_ID':0,'ACTIVE':0},plane:{'FB_ID':0,'CRTC_ID':0}}
        drm.atomic(changes,modeset=True)
    finally:drm.free_blob(blob)

def recover(runtime):
    journal=Path(runtime)/'display-restore.json'
    if not journal.exists():return
    saved=json.loads(journal.read_text());drm=DRM(saved['card'])
    try:
        connectors=drm.connectors()
        connected=any(c['id']==saved['connector'] and c['connected'] for c in connectors)
        restore(drm,saved,connected);journal.unlink()
    finally:drm.close()

class ExternalDisplay:
    def __init__(self,runtime,artifacts):
        self.runtime=Path(runtime);self.artifacts=Path(artifacts)
        self.drm=None;self.buffer=None;self.mode_blob=0;self.saved=None
        self.modes=[];self.selected=None;self.current=None;self.error='';self.hdr='HDR unavailable in driver'
        self.connected=False;self.last_poll=0;self.signature=None;self.history=[];self.pattern_file=None
        self.internal_plane=None;self.internal_fb=None;self.connector=None;self.crtc=None;self.plane=None
        self.journal=self.runtime/'display-restore.json'
        self.history_file=self.numbered('external-modes','.jsonl')
        try:
            for card in sorted(Path('/sys/class/drm').glob('card*-DSI-*')):
                self.card='/dev/dri/'+card.name.split('-')[0];break
            else:raise RuntimeError('Internal DRM connector not found')
            self.drm=DRM(self.card)
            connectors=self.drm.connectors()
            internal=next(c for c in connectors if c['type']==16 and c['connected'])
            internal_crtc=internal['properties']['CRTC_ID']['value']
            self.internal_plane,props=self.drm.primary(internal_crtc);self.internal_fb=props['FB_ID']['value']
            self.internal_crtc=internal_crtc
            self.external=next(c for c in connectors if c['type'] in (10,11))
            self.status_path=Path('/sys/class/drm')/(Path(self.card).name+'-'+self.external['name'])/'status'
            self.poll(force=True)
        except Exception:
            self.close();raise

    def numbered(self,stem,suffix):
        from actions import numbered_file
        return numbered_file(self.artifacts/'logs',stem,suffix)

    def record(self,event,**data):
        row={'event':event,'time':time.time(),**data};self.history.append(row)
        with self.history_file.open('a') as log:log.write(json.dumps(row)+'\n')

    def poll(self,force=False):
        if not force and time.monotonic()-self.last_poll<.5:return False
        self.last_poll=time.monotonic()
        signature=self.status_path.read_text().strip()
        if not force and signature==self.signature:return False
        self.signature=signature
        connectors=self.drm.connectors();self.external=next(c for c in connectors if c['id']==self.external['id'])
        self.connected=self.external['connected'];self.error=''
        if not self.connected:
            if self.saved:self.release(connected=False)
            self.modes=[];self.selected=None;self.current=None
            self.record('disconnected');return True
        self.modes=mode_choices(self.external['modes'])
        if not self.modes:raise RuntimeError('Display has no modes at or above 640×480')
        if not self.saved:self.attach(connectors)
        self.selected=next((m for m in self.modes if m.type&8),self.modes[-1])
        self.record('connected',connector=self.external['name'],modes=[m.info() for m in self.modes],hdr=self.hdr)
        # Prefer EDID's preferred timing, then lower rates of that size, then VGA.
        candidates=[self.selected]+sorted([m for m in self.modes if (m.hdisplay,m.vdisplay)==(self.selected.hdisplay,self.selected.vdisplay) and m.key!=self.selected.key],key=lambda m:m.hz,reverse=True)
        candidates+=sorted([m for m in self.modes if (m.hdisplay,m.vdisplay)==(640,480)],key=lambda m:abs(m.hz-60))
        for mode in candidates:
            self.selected=mode
            if self.apply():break
        return True

    def attach(self,connectors):
        external=self.external;drm=self.drm;self.connector=external['id']
        used={c['properties']['CRTC_ID']['value'] for c in connectors if c['id']!=self.connector}
        current=external['properties']['CRTC_ID']['value']
        candidates=[c for i,c in enumerate(drm.crtcs) if external['possible_crtcs']&(1<<i) and c not in used]
        self.crtc=current if current and current!=self.internal_crtc else next(iter(candidates),None)
        if not self.crtc:raise RuntimeError('No independent external display pipeline')
        self.plane,plane=drm.primary(self.crtc);crtc=drm.properties(self.crtc,CRTC)
        mode_id=crtc['MODE_ID']['value'];mode=b''
        if mode_id:
            ptr=drm.get('PropertyBlob',mode_id)
            try:mode=C.string_at(ptr.contents.data,ptr.contents.length)
            finally:drm.drmModeFreePropertyBlob(ptr)
        self.saved={'card':self.card,'connector':self.connector,'crtc':self.crtc,'plane':self.plane,'mode':base64.b64encode(mode).decode(),
                    'changes':{str(self.connector):{'CRTC_ID':current},str(self.crtc):{n:crtc[n]['value'] for n in ('MODE_ID','ACTIVE')},str(self.plane):{n:plane[n]['value'] for n in PLANE_FIELDS}}}
        atomic_json(self.journal,self.saved)
        self.hdr='HDR unavailable in driver' if 'HDR_OUTPUT_METADATA' not in external['properties'] else 'HDR not implemented (SDR pattern)'

    def apply(self):
        if not self.connected or self.selected is None:return False
        drm=self.drm;mode=self.selected;buffer=None;blob=0
        try:
            buffer=Buffer(drm,mode.hdisplay,mode.vdisplay);blob=drm.blob(bytes(mode))
            changes={self.connector:{'CRTC_ID':self.crtc},self.crtc:{'ACTIVE':1,'MODE_ID':blob},self.plane:{'FB_ID':buffer.fb,'CRTC_ID':self.crtc,'SRC_X':0,'SRC_Y':0,'SRC_W':mode.hdisplay<<16,'SRC_H':mode.vdisplay<<16,'CRTC_X':0,'CRTC_Y':0,'CRTC_W':mode.hdisplay,'CRTC_H':mode.vdisplay}}
            drm.atomic(changes,modeset=True,test=True)
            pattern=Pattern(mode.hdisplay,mode.vdisplay,label(mode));image=pattern.image();buffer.paint(image)
            drm.atomic(changes,modeset=True)
        except Exception as error:
            if buffer:buffer.close()
            drm.free_blob(blob);self.error=str(error);self.record('mode-rejected',mode=mode.info(),error=self.error);return False
        old=self.buffer;old_blob=self.mode_blob
        self.buffer=buffer;self.mode_blob=blob;self.current=mode;self.error=''
        self.preview=image;self.preview.thumbnail((432,240))
        if old:old.close()
        drm.free_blob(old_blob)
        path=self.numbered('external-pattern','.svg');path.write_text(pattern.svg());self.pattern_file=str(path.relative_to(self.artifacts))
        self.record('mode-accepted',mode=mode.info(),pattern=self.pattern_file)
        return True

    def select_resolution(self,direction):
        if not self.modes:return
        sizes=list(dict.fromkeys((m.hdisplay,m.vdisplay) for m in self.modes));size=(self.selected.hdisplay,self.selected.vdisplay)
        chosen=sizes[(sizes.index(size)+direction)%len(sizes)]
        self.selected=min((m for m in self.modes if (m.hdisplay,m.vdisplay)==chosen),key=lambda m:abs(m.hz-self.selected.hz));self.error=''

    def select_refresh(self):
        if not self.modes:return
        modes=[m for m in self.modes if (m.hdisplay,m.vdisplay)==(self.selected.hdisplay,self.selected.vdisplay)]
        self.selected=modes[(modes.index(self.selected)+1)%len(modes)];self.error=''

    def native(self):
        if self.modes:self.selected=next((m for m in self.modes if m.type&8),self.modes[-1])

    def flush_internal(self):
        self.drm.atomic({self.internal_plane:{'FB_ID':self.internal_fb}})

    def evidence(self):
        return {'connector':self.external['name'],'current_mode':self.current.info() if self.current else None,
                'history_file':str(self.history_file.relative_to(self.artifacts)),'pattern':self.pattern_file,'hdr':self.hdr,'error':self.error}

    def release(self,connected=True):
        if self.saved:
            restore(self.drm,self.saved,connected);self.journal.unlink(missing_ok=True);self.saved=None
        if self.buffer:self.buffer.close();self.buffer=None
        if self.drm:self.drm.free_blob(self.mode_blob)
        self.mode_blob=0;self.current=None

    def close(self):
        if self.drm:
            try:self.release(self.status_path.read_text().strip()=='connected' if hasattr(self,'status_path') else False)
            finally:self.drm.close();self.drm=None
