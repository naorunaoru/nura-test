"""Live SSC samples for the framebuffer views; no Android services required."""
import ctypes as C
import json, math, os, signal, struct, time
from pathlib import Path

G = 9.80665

def decode_sample(data):
    """Decode standard SSC packed float samples and their accuracy enum."""
    i=0;values=[];accuracy=None
    def varint():
        nonlocal i
        value=0
        for shift in range(0,70,7):
            if i>=len(data):raise ValueError('Truncated sensor packet')
            byte=data[i];i+=1;value|=(byte&127)<<shift
            if not byte&128:return value
        raise ValueError('Invalid sensor varint')
    while i<len(data):
        tag=varint();field=tag>>3;wire=tag&7
        if wire==0:
            value=varint()
            if field==2:accuracy=value
        elif wire==2:
            size=varint();end=i+size
            if end>len(data):raise ValueError('Truncated sensor payload')
            if field==1:
                if size%4:raise ValueError('Invalid float array')
                values.extend(struct.unpack('<'+'f'*(size//4),data[i:end]))
            i=end
        elif wire in (1,5):
            size=8 if wire==1 else 4
            if i+size>len(data):raise ValueError('Truncated fixed field')
            if field==1 and wire==5:values.append(struct.unpack_from('<f',data,i)[0])
            i+=size
        else:raise ValueError('Unsupported sensor wire type')
    if not all(math.isfinite(v) for v in values):raise ValueError('Non-finite sensor sample')
    return values,accuracy

def rotation(q):
    if len(q)!=4 or not all(math.isfinite(v) for v in q):raise ValueError('Invalid quaternion')
    length=math.sqrt(sum(v*v for v in q))
    if length<.5 or length>1.5:raise ValueError('Invalid quaternion length')
    x,y,z,w=(v/length for v in q)
    # Device axes -> East, magnetic North, Up, matching SSC rotv / Android.
    return ((1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)),
            (2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)),
            (2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)))

def transform(matrix,v):return tuple(sum(a*b for a,b in zip(row,v)) for row in matrix)

def attitude(q):
    r=rotation(q)
    heading=math.degrees(math.atan2(r[0][1],r[1][1]))%360
    pitch=math.degrees(math.asin(max(-1,min(1,-r[2][1]))))
    roll=math.degrees(math.atan2(-r[2][0],r[2][2]))
    return heading,pitch,roll

def publish(path,data):
    path=Path(path);tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data,allow_nan=False));os.replace(tmp,path)

class GArray(C.Structure):_fields_=[('data',C.c_void_p),('len',C.c_uint)]
class GError(C.Structure):_fields_=[('domain',C.c_uint),('code',C.c_int),('message',C.c_char_p)]
Report=C.CFUNCTYPE(None,C.c_void_p,C.c_uint,C.c_uint64,C.c_uint64,C.POINTER(GArray),C.c_void_p)

class NativeSensors:
    """Use libssc's typed public sensors and their client report signal.

    The report signal retains the rotation quaternion and accuracy, which the
    compass measurement callback reduces to a heading. Read-only properties
    supply the UID; reports from other sensors are ignored.
    """
    def __init__(self,receive):
        self.receive=receive;self.sensors=[];self.callbacks=[];self.error=None
        self.ssc=C.CDLL('libssc.so.2');self.gobj=C.CDLL('libgobject-2.0.so.0');self.glib=C.CDLL('libglib-2.0.so.0')
        def bind(lib,name,args,result):
            f=getattr(lib,name);f.argtypes=args;f.restype=result;return f
        ptr=C.c_void_p
        self.iteration=bind(self.glib,'g_main_context_iteration',[ptr,C.c_int],C.c_int)
        self.free_error=bind(self.glib,'g_error_free',[C.POINTER(GError)],None)
        self.get=bind(self.gobj,'g_object_get',[ptr,C.c_char_p],None)
        self.unref=bind(self.gobj,'g_object_unref',[ptr],None)
        self.connect=bind(self.gobj,'g_signal_connect_data',[ptr,C.c_char_p,Report,ptr,ptr,C.c_int],C.c_ulong)
        self.disconnect=bind(self.gobj,'g_signal_handler_disconnect',[ptr,C.c_ulong],None)
        self.api={}
        for kind in ('compass','accelerometer'):
            self.api[kind]={}
            for op in ('new','open','close'):
                args=[ptr,C.POINTER(C.POINTER(GError))] if op=='new' else [ptr,ptr,C.POINTER(C.POINTER(GError))]
                self.api[kind][op]=bind(self.ssc,f'ssc_sensor_{kind}_{op}_sync',args,ptr if op=='new' else C.c_int)

    def call(self,kind,op,sensor=None):
        error=C.POINTER(GError)();args=[None,C.byref(error)]
        if op!='new':args.insert(0,sensor)
        value=self.api[kind][op](*args)
        if error:
            message=error.contents.message.decode(errors='replace');self.free_error(error)
            raise RuntimeError(f'{kind}: {message}')
        if not value:raise RuntimeError(f'{kind} unavailable; check sensor firmware and motion-detector setup')
        return value

    def open(self,kind):
        sensor=self.call(kind,'new');client=C.c_void_p();high=C.c_uint64();low=C.c_uint64()
        self.get(sensor,b'client',C.byref(client),None)
        self.get(sensor,b'uid-high',C.byref(high),None);self.get(sensor,b'uid-low',C.byref(low),None)
        entry={'kind':kind,'sensor':sensor,'client':client,'handler':None,'opened':False}
        self.sensors.append(entry)
        @Report
        def report(_client,msg,hi,lo,array,_user):
            if msg!=1025 or (hi,lo)!=(high.value,low.value):return
            try:
                if not array or not 0<array.contents.len<4096:raise ValueError('Invalid sensor report size')
                values,accuracy=decode_sample(C.string_at(array.contents.data,array.contents.len))
                required=4 if kind=='compass' else 3
                if len(values)<required:raise ValueError('Incomplete sensor sample')
                self.receive(kind,values[:required],accuracy)
            except Exception as error:self.error=str(error)
        self.callbacks.append(report)
        entry['handler']=self.connect(client,b'report',report,None,None,0)
        if not entry['handler']:raise RuntimeError('libssc report interface is unavailable')
        self.call(kind,'open',sensor);entry['opened']=True

    def pump(self):
        for _ in range(50):
            if not self.iteration(None,False):break
        if self.error:raise RuntimeError(self.error)

    def close(self):
        errors=[]
        for s in reversed(self.sensors):
            if s['handler']:self.disconnect(s['client'],s['handler'])
            if s['opened']:
                try:self.call(s['kind'],'close',s['sensor'])
                except Exception as error:errors.append(str(error))
            self.unref(s['client']);self.unref(s['sensor'])
        self.sensors.clear();self.callbacks.clear()
        if errors:raise RuntimeError('; '.join(errors))

def capture(kind,runtime,sample_path,seconds=None):
    state={'kind':kind,'phase':'starting','samples':{},'counts':{}}
    stop=False;started=time.monotonic();last_publish=0;last_record=None
    def finish(*_):
        nonlocal stop
        stop=True
    old=signal.signal(signal.SIGTERM,finish)
    def receive(sensor,values,accuracy):
        stamp=time.monotonic()
        if sensor=='compass':rotation(values)
        state['samples'][sensor]={'values':values,'accuracy':accuracy,'time':stamp}
        state['counts'][sensor]=state['counts'].get(sensor,0)+1
    backend=None;required=('accelerometer',) if kind=='imu' else ('compass',)
    path=Path(runtime)/'sensors.json';publish(path,state)
    try:
        backend=NativeSensors(receive)
        for sensor in required:
            if stop:break
            backend.open(sensor)
        with Path(sample_path).open('a') as log:
            while not stop and not (Path(runtime)/'stop').exists() and (seconds is None or time.monotonic()-started<seconds):
                backend.pump();now=time.monotonic()
                if now-last_publish>=.02:
                    state['phase']='live';state['updated']=now;publish(path,state);last_publish=now
                    marker=tuple(state['counts'].get(s,0) for s in required)
                    if marker!=last_record:
                        log.write(json.dumps(state,allow_nan=False)+'\n');log.flush();last_record=marker
                time.sleep(.01)
        missing=[s for s in required if s not in state['samples']]
        if missing:raise RuntimeError('No samples from '+', '.join(missing))
        stale=[s for s in required if time.monotonic()-state['samples'][s]['time']>2]
        if stale:raise RuntimeError('Sensor stream stopped: '+', '.join(stale))
        state['phase']='recorded';publish(path,state)
        return {'samples_file':str(sample_path),'counts':state['counts'],'last_samples':state['samples'],'duration_seconds':round(time.monotonic()-started,2)}
    finally:
        try:
            if backend:backend.close()
        finally:signal.signal(signal.SIGTERM,old)
