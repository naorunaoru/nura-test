"""Small libdrm atomic KMS binding for an independent external scanout.

The fbdev buffer remains the internal UI. Its plane is committed directly while
we own DRM master, because FBIOPAN_DISPLAY would restore fbdev's external mode.
"""
import ctypes as C
import mmap
import os
from pathlib import Path

U32=C.c_uint32;U64=C.c_uint64;I=C.c_int;P=C.POINTER
CONNECTOR=0xc0c0c0c0;CRTC=0xcccccccc;PLANE=0xeeeeeeee
TEST_ONLY=0x100;ALLOW_MODESET=0x400
XR24=0x34325258

class Mode(C.Structure):
    _fields_=[('clock',U32)]+[(n,C.c_uint16) for n in ('hdisplay','hsync_start','hsync_end','htotal','hskew','vdisplay','vsync_start','vsync_end','vtotal','vscan')]+[('vrefresh',U32),('flags',U32),('type',U32),('name',C.c_char*32)]
    def copy(self):return Mode.from_buffer_copy(bytes(self))
    @property
    def hz(self):
        denominator=self.htotal*self.vtotal*max(1,self.vscan)*(2 if self.flags&32 else 1)
        return self.clock*1000*(2 if self.flags&16 else 1)/denominator if denominator else 0
    @property
    def key(self):return (self.hdisplay,self.vdisplay,self.clock,self.htotal,self.vtotal,self.flags)
    def info(self):
        return dict(width=self.hdisplay,height=self.vdisplay,hz=round(self.hz,3),interlaced=bool(self.flags&16),preferred=bool(self.type&8))

class Resources(C.Structure):
    _fields_=[(n,t) for kind in ('fbs','crtcs','connectors','encoders') for n,t in [('count_'+kind,I),(kind,P(U32))]]+[(n,U32) for n in ('min_width','max_width','min_height','max_height')]
class Connector(C.Structure):
    _fields_=[(n,U32) for n in ('connector_id','encoder_id','connector_type','connector_type_id')]+[('connection',I),('mmWidth',U32),('mmHeight',U32),('subpixel',I),('count_modes',I),('modes',P(Mode)),('count_props',I),('props',P(U32)),('prop_values',P(U64)),('count_encoders',I),('encoders',P(U32))]
class Encoder(C.Structure):
    _fields_=[(n,U32) for n in ('encoder_id','encoder_type','crtc_id','possible_crtcs','possible_clones')]
class Crtc(C.Structure):
    _fields_=[(n,U32) for n in ('crtc_id','buffer_id','x','y','width','height')]+[('mode_valid',I),('mode',Mode),('gamma_size',I)]
class Plane(C.Structure):
    _fields_=[('count_formats',U32),('formats',P(U32))]+[(n,U32) for n in ('plane_id','crtc_id','fb_id','crtc_x','crtc_y','x','y','possible_crtcs','gamma_size')]
class Planes(C.Structure):
    _fields_=[('count_planes',U32),('planes',P(U32))]
class Properties(C.Structure):
    _fields_=[('count_props',U32),('props',P(U32)),('prop_values',P(U64))]
class Property(C.Structure):
    _fields_=[('prop_id',U32),('flags',U32),('name',C.c_char*32),('count_values',I),('values',P(U64)),('count_enums',I),('enums',C.c_void_p),('count_blobs',I),('blob_ids',P(U32))]
class Blob(C.Structure):
    _fields_=[('id',U32),('length',U32),('data',C.c_void_p)]
class Dumb(C.Structure):
    _fields_=[(n,U32) for n in ('height','width','bpp','flags','handle','pitch')]+[('size',U64)]
class MapDumb(C.Structure):
    _fields_=[('handle',U32),('pad',U32),('offset',U64)]

def mode_choices(modes):
    """Keep advertised timings, including refresh variants; omit sub-VGA modes."""
    choices={}
    for mode in modes:
        if mode.hdisplay>=640 and mode.vdisplay>=480 and not mode.flags & (0x1f<<14):
            old=choices.get(mode.key)
            if old is None or mode.type&8:choices[mode.key]=mode
    return sorted(choices.values(),key=lambda m:(m.hdisplay*m.vdisplay,m.hdisplay,m.vdisplay,m.hz,m.flags))

class DRM:
    def __init__(self,path):
        self.fd=os.open(path,os.O_RDWR|os.O_CLOEXEC);self.lib=C.CDLL('libdrm.so.2',use_errno=True)
        self.property_cache={};self.blobs=[];self.closed=False
        def bind(name,args,result=I):
            f=getattr(self.lib,name);f.argtypes=args;f.restype=result;setattr(self,name,f)
        bind('drmSetMaster',[I]);bind('drmDropMaster',[I]);bind('drmSetClientCap',[I,U64,U64]);bind('drmIoctl',[I,C.c_ulong,C.c_void_p])
        for name,typ,args in [('Resources',Resources,[I]),('Connector',Connector,[I,U32]),('Encoder',Encoder,[I,U32]),('Crtc',Crtc,[I,U32]),('PlaneResources',Planes,[I]),('Plane',Plane,[I,U32]),('Property',Property,[I,U32]),('PropertyBlob',Blob,[I,U32]),('ObjectProperties',Properties,[I,U32,U32])]:
            getter='drmModeObjectGetProperties' if name=='ObjectProperties' else 'drmModeGet'+name
            bind(getter,args,P(typ));bind('drmModeFree'+name,[P(typ)],None)
        bind('drmModeAtomicAlloc',[],C.c_void_p);bind('drmModeAtomicFree',[C.c_void_p],None)
        bind('drmModeAtomicAddProperty',[C.c_void_p,U32,U32,U64]);bind('drmModeAtomicCommit',[I,C.c_void_p,U32,C.c_void_p])
        bind('drmModeCreatePropertyBlob',[I,C.c_void_p,C.c_size_t,P(U32)])
        bind('drmModeDestroyPropertyBlob',[I,U32]);bind('drmModeAddFB2',[I,U32,U32,U32,P(U32),P(U32),P(U32),P(U32),U32]);bind('drmModeRmFB',[I,U32])
        try:
            self.check(self.drmSetClientCap(self.fd,3,1),'Enable atomic KMS')
            self.check(self.drmSetMaster(self.fd),'Acquire DRM master')
        except Exception:self.close();raise

    def check(self,result,operation):
        if result<0:raise OSError(C.get_errno(),operation+': '+os.strerror(C.get_errno()))
        return result

    def get(self,kind,*args):
        getter='drmModeObjectGetProperties' if kind=='ObjectProperties' else 'drmModeGet'+kind
        ptr=getattr(self,getter)(self.fd,*args)
        if not ptr:raise OSError(C.get_errno(),'Read DRM '+kind)
        return ptr

    def properties(self,obj,kind):
        ptr=self.get('ObjectProperties',obj,kind);values={}
        try:
            for i in range(ptr.contents.count_props):
                prop=self.get('Property',ptr.contents.props[i])
                try:
                    p=prop.contents;values[p.name.decode()]={'id':p.prop_id,'value':ptr.contents.prop_values[i],'flags':p.flags}
                finally:self.drmModeFreeProperty(prop)
        finally:self.drmModeFreeObjectProperties(ptr)
        self.property_cache[obj]=values;return values

    def blob(self,data):
        blob=U32();buffer=C.create_string_buffer(data)
        self.check(self.drmModeCreatePropertyBlob(self.fd,buffer,len(data),C.byref(blob)),'Create mode blob')
        self.blobs.append(blob.value);return blob.value

    def free_blob(self,blob):
        if blob in self.blobs:
            self.drmModeDestroyPropertyBlob(self.fd,blob);self.blobs.remove(blob)

    def atomic(self,changes,modeset=False,test=False):
        req=self.drmModeAtomicAlloc()
        if not req:raise MemoryError('Allocate KMS commit')
        try:
            for obj,properties in changes.items():
                known=self.property_cache[obj]
                for name,value in properties.items():
                    self.check(self.drmModeAtomicAddProperty(req,obj,known[name]['id'],value),'Set '+name)
            flags=(ALLOW_MODESET if modeset else 0)|(TEST_ONLY if test else 0)
            self.check(self.drmModeAtomicCommit(self.fd,req,flags,None),'Validate mode' if test else 'Commit display')
        finally:self.drmModeAtomicFree(req)

    def connectors(self):
        resources=self.get('Resources');result=[]
        try:
            self.crtcs=list(resources.contents.crtcs[:resources.contents.count_crtcs])
            for ident in resources.contents.connectors[:resources.contents.count_connectors]:
                ptr=self.get('Connector',ident)
                try:
                    c=ptr.contents;possible=0
                    for enc in c.encoders[:c.count_encoders]:
                        e=self.get('Encoder',enc)
                        try:possible|=e.contents.possible_crtcs
                        finally:self.drmModeFreeEncoder(e)
                    props=self.properties(ident,CONNECTOR)
                    result.append(dict(id=ident,type=c.connector_type,name={10:'DP',11:'HDMI',16:'DSI'}.get(c.connector_type,'Other')+'-'+str(c.connector_type_id),connected=c.connection==1,
                                       modes=[m.copy() for m in c.modes[:c.count_modes]],possible_crtcs=possible,properties=props))
                finally:self.drmModeFreeConnector(ptr)
        finally:self.drmModeFreeResources(resources)
        return result

    def primary(self,crtc):
        ptr=self.get('PlaneResources');candidates=[]
        try:
            for ident in ptr.contents.planes[:ptr.contents.count_planes]:
                plane=self.get('Plane',ident)
                try:
                    p=plane.contents;props=self.properties(ident,PLANE)
                    if p.possible_crtcs & (1<<self.crtcs.index(crtc)) and props.get('type',{}).get('value')==1 and p.crtc_id in (0,crtc):
                        candidates.append((p.crtc_id!=crtc,ident,props))
                finally:self.drmModeFreePlane(plane)
        finally:self.drmModeFreePlaneResources(ptr)
        if candidates:
            _,ident,props=min(candidates,key=lambda c:(c[0],c[1]));return ident,props
        raise RuntimeError('No primary plane for external display')

    def close(self):
        if self.closed:return
        for blob in self.blobs[:]:self.free_blob(blob)
        if hasattr(self,'drmDropMaster'):self.drmDropMaster(self.fd)
        os.close(self.fd);self.closed=True

class Buffer:
    def __init__(self,drm,width,height):
        self.drm=drm;self.fb=0;self.memory=None;self.dumb=Dumb(height=height,width=width,bpp=32)
        try:
            drm.check(drm.drmIoctl(drm.fd,0xc02064b2,C.byref(self.dumb)),'Allocate scanout')
            handles=(U32*4)(self.dumb.handle);pitches=(U32*4)(self.dumb.pitch);offsets=(U32*4)();fb=U32()
            drm.check(drm.drmModeAddFB2(drm.fd,width,height,XR24,handles,pitches,offsets,C.byref(fb),0),'Create framebuffer');self.fb=fb.value
            mapping=MapDumb(handle=self.dumb.handle)
            drm.check(drm.drmIoctl(drm.fd,0xc01064b3,C.byref(mapping)),'Map scanout')
            self.memory=mmap.mmap(drm.fd,self.dumb.size,flags=mmap.MAP_SHARED,prot=mmap.PROT_READ|mmap.PROT_WRITE,offset=mapping.offset)
        except Exception:self.close();raise

    def paint(self,image):
        if image.size!=(self.dumb.width,self.dumb.height):raise ValueError('Pattern must match scanout size')
        raw=image.convert('RGB').tobytes('raw','BGRX');row=self.dumb.width*4
        if row==self.dumb.pitch:self.memory[:len(raw)]=raw
        else:
            for y in range(self.dumb.height):self.memory[y*self.dumb.pitch:y*self.dumb.pitch+row]=raw[y*row:(y+1)*row]

    def close(self):
        if self.memory:self.memory.close();self.memory=None
        if self.fb:self.drm.drmModeRmFB(self.drm.fd,self.fb);self.fb=0
        if self.dumb.handle:
            handle=U32(self.dumb.handle);self.drm.drmIoctl(self.drm.fd,0xc00464b4,C.byref(handle));self.dumb.handle=0
