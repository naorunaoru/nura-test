"""Software-rendered 3D orientation and magnetic compass for the internal display."""
import math,time
from collections import deque
from sensor_live import G, attitude, rotation, transform

AXES=('#ff7e89','#80e5bd','#7cabff');GOLD='#ffd077';CYAN='#62def2'
FG='#e9eff8';MUTED='#9aa9bf'
ACCURACY={0:'Uncalibrated',1:'Low',2:'Medium',3:'High'}

def slerp(a,b,fraction):
    """Shortest-arc quaternion interpolation, including the 359°/0° crossing."""
    a=tuple(v/math.sqrt(sum(x*x for x in a)) for v in a)
    b=tuple(v/math.sqrt(sum(x*x for x in b)) for v in b)
    dot=sum(x*y for x,y in zip(a,b))
    if dot<0:b=tuple(-v for v in b);dot=-dot
    dot=max(-1,min(1,dot));fraction=max(0,min(1,fraction))
    if dot>.9995:
        q=tuple(x+(y-x)*fraction for x,y in zip(a,b));n=math.sqrt(sum(v*v for v in q))
        return tuple(v/n for v in q)
    angle=math.acos(dot);den=math.sin(angle)
    return tuple((x*math.sin((1-fraction)*angle)+y*math.sin(fraction*angle))/den for x,y in zip(a,b))

class SensorAnimation:
    """Presentation only: logged samples and verdict evidence remain untouched."""
    def __init__(self):
        self.orientations=deque(maxlen=10);self.accel=None;self.accel_time=None

    def frame(self,state,running,now=None):
        if not running:return state
        now=time.monotonic() if now is None else now
        result=dict(state);samples={k:dict(v) for k,v in state.get('samples',{}).items()};result['samples']=samples
        if 'compass' in samples:
            sample=samples['compass'];stamp=sample['time']
            if not self.orientations or stamp>self.orientations[-1][0]:self.orientations.append((stamp,tuple(sample['values'])))
            # Buffer one 5 Hz sample interval. Interpolate measured poses rather
            # than extrapolating a heading after the phone stops or data stalls.
            target=now-.22;value=self.orientations[0][1]
            for (t0,q0),(t1,q1) in zip(self.orientations,list(self.orientations)[1:]):
                if target<t0:break
                value=q1 if target>=t1 else slerp(q0,q1,(target-t0)/(t1-t0))
                if target<t1:break
            sample['values']=value
        if 'accelerometer' in samples:
            sample=samples['accelerometer'];stamp=sample['time'];values=sample['values']
            if self.accel_time is None or stamp-self.accel_time>1:
                self.accel=tuple(values);self.accel_time=stamp
            elif stamp>self.accel_time:
                alpha=1-math.exp(-(stamp-self.accel_time)/.045)
                self.accel=tuple(a+(b-a)*alpha for a,b in zip(self.accel,values));self.accel_time=stamp
            sample['values']=self.accel
        return result

def level_up(acceleration):
    length=math.sqrt(sum(v*v for v in acceleration))
    if not math.isfinite(length) or length<1:raise ValueError('Gravity direction unavailable')
    return tuple(v/length for v in acceleration)

def level_point(up,center=(240,507),radius=150):
    # Screen-relative view: the bubble moves toward the physically raised edge.
    return center[0]+radius*up[0],center[1]-radius*up[1]

def bearing_point(bearing,heading,center,radius):
    angle=math.radians(bearing-heading)
    return center[0]+radius*math.sin(angle),center[1]-radius*math.cos(angle)

def cardinal(heading):return ('N','NE','E','SE','S','SW','W','NW')[int((heading+22.5)//45)%8]

def arrow(draw,start,end,color,width=3):
    dx=end[0]-start[0];dy=end[1]-start[1];length=math.hypot(dx,dy)
    if length<3:draw.ellipse((end[0]-2,end[1]-2,end[0]+2,end[1]+2),fill=color);return
    ux,uy=dx/length,dy/length;size=min(10,length*.35)
    draw.line((start,end),fill=color,width=width)
    draw.polygon((end,(end[0]-size*ux+size*.45*uy,end[1]-size*uy-size*.45*ux),
                  (end[0]-size*ux-size*.45*uy,end[1]-size*uy+size*.45*ux)),fill=color)

def project(v,center=(240,502),radius=145):
    # Fixed oblique observer; east, north and up remain anchored in the scene.
    right=(.83205,.55470,0);up=(-.27735,.416025,.866025);eye=(.480384,-.720577,.5)
    dot=lambda a:sum(x*y for x,y in zip(a,v))
    return (center[0]+radius*dot(right),center[1]-radius*dot(up)),dot(eye)

def sample_state(state,required,running):
    samples=state.get('samples',{});now=time.monotonic()
    if state.get('error'):return 'Sensor test failed',False
    if any(k not in samples for k in required):return 'Waiting for sensor data',False
    if running and any(now-samples[k]['time']>2 for k in required):return 'Sensor data is stale',False
    return 'Live' if running else 'Recorded',True

def calibration_text(samples):
    accuracy=samples.get('compass',{}).get('accuracy')
    return 'Calibration: '+ACCURACY.get(accuracy,'Unknown'),accuracy

def draw_imu(renderer,draw,state,running):
    text=renderer.text;mode,ready=sample_state(state,('accelerometer',),running)
    text(draw,'Bubble follows the raised edge.',(24,253),20,MUTED,max_lines=1)
    center=(240,507);radius=150
    for size in range(154,0,-3):
        shade=int(18+21*(1-size/154))
        draw.ellipse((240-size,507-size,240+size,507+size),fill=(shade,shade+13,shade+22))
    draw.ellipse((86,353,394,661),outline='#527186',width=2)
    a=state.get('samples',{}).get('accelerometer',{}).get('values',(0,0,G))
    try:up=level_up(a)
    except ValueError:ready=False;mode='Keep the phone still';up=(0,0,1)
    # Latitude rings on a gravity-aligned sphere; the marked pole is always up
    # in the physical world, while the view stays attached to the phone screen.
    ux,uy,uz=up;xy=math.hypot(ux,uy)
    e=(-uy/xy,ux/xy,0) if xy>1e-6 else (1,0,0)
    f=(uy*e[2]-uz*e[1],uz*e[0]-ux*e[2],ux*e[1]-uy*e[0])
    for latitude in (-45,0,45):
        lat=math.radians(latitude);points=[]
        for i in range(97):
            angle=i*math.tau/96
            v=tuple(math.sin(lat)*u+math.cos(lat)*(math.cos(angle)*x+math.sin(angle)*y) for u,x,y in zip(up,e,f))
            points.append((level_point(v,center,radius),v[2]))
        for p0,p1 in zip(points,points[1:]):
            draw.line((p0[0],p1[0]),fill=('#7796a7' if latitude==0 else '#456477') if p0[1]>=0 else '#263d50',width=2 if latitude==0 else 1)
    # Fixed center target, independent of north or phone yaw.
    draw.line((215,507,265,507),fill='#90a5b4',width=1)
    draw.line((240,482,240,532),fill='#90a5b4',width=1)
    draw.ellipse((221,488,259,526),outline='#90a5b4',width=2)
    if not ready:
        text(draw,state.get('error',mode),(24,710),23,GOLD,max_lines=3)
        return
    tilt=math.degrees(math.acos(max(0,min(1,abs(uz)))))
    bubble=level_point(up,center,radius);x,y=bubble;color=AXES[1] if tilt<1 else GOLD
    if uz>=0:
        draw.ellipse((x-14,y-14,x+14,y+14),fill='#132a32',outline=color,width=3)
        draw.ellipse((x-7,y-7,x+7,y+7),fill=color)
        draw.ellipse((x-4,y-5,x+1,y),fill='#fff5ce')
    else:
        draw.ellipse((x-12,y-12,x+12,y+12),outline=color,width=3)
        draw.line((x-5,y-5,x+5,y+5),fill=color,width=2)
        draw.line((x-5,y+5,x+5,y-5),fill=color,width=2)
    text(draw,'UP',(max(90,min(366,x-10)),max(357,min(637,y-34))),14,color,width=45,max_lines=1,bold=True)
    right=math.degrees(math.asin(max(-1,min(1,ux))))
    top=math.degrees(math.asin(max(-1,min(1,uy))))
    text(draw,f'Right edge {right:+.1f}°   Top {top:+.1f}°',(24,319),18,FG,max_lines=1)
    side='Screen up' if uz>.15 else 'Screen down' if uz<-.15 else 'On edge'
    text(draw,f'{tilt:.1f}° from level · {side}',(24,684),23,color,max_lines=1,bold=True)
    text(draw,'Solid bubble: front · Hollow: behind sphere',(24,723),15,MUTED,max_lines=1)
    text(draw,f'a  {a[0]:+.1f}  {a[1]:+.1f}  {a[2]:+.1f} m/s²',(24,753),20,FG,max_lines=1)
    text(draw,mode,(24,790),16,MUTED,max_lines=1)

def draw_compass(renderer,draw,state,running):
    text=renderer.text;mode,ready=sample_state(state,('compass',),running)
    text(draw,'Compare with magnetic north on the other device.',(24,253),18,MUTED,max_lines=2)
    text(draw,'Keep flat and align the two top edges.',(24,307),18,MUTED,max_lines=1)
    center=(240,533);radius=153
    draw.ellipse((78,371,402,695),fill='#142130',outline='#385269',width=2)
    heading=pitch=roll=0
    if ready:heading,pitch,roll=attitude(state['samples']['compass']['values'])
    for bearing in range(0,360,5):
        outer=bearing_point(bearing,heading,center,radius)
        inner=bearing_point(bearing,heading,center,radius-(16 if bearing%30==0 else 7))
        draw.line((inner,outer),fill=FG if bearing%30==0 else '#576b82',width=2 if bearing%30==0 else 1)
        if bearing%90==0:
            point=bearing_point(bearing,heading,center,116);label={0:'N',90:'E',180:'S',270:'W'}[bearing]
            font=renderer.font(25,True);width=draw.textlength(label,font=font)
            draw.text((point[0]-width/2,point[1]-14),label,font=font,fill=AXES[0] if bearing==0 else FG)
    draw.polygon(((240,365),(231,350),(249,350)),fill=GOLD)
    if ready:
        north=bearing_point(0,heading,center,87);south=bearing_point(180,heading,center,87)
        left=bearing_point(270,heading,center,15);right=bearing_point(90,heading,center,15)
        draw.polygon((north,left,right),fill=AXES[0]);draw.polygon((south,left,right),fill='#90a1bb')
        draw.ellipse((235,528,245,538),fill=FG)
        label=f'{heading:05.1f}°  {cardinal(heading)}';font=renderer.font(39,True)
        draw.text(((480-draw.textlength(label,font=font))/2,708),label,font=font,fill=FG)
        cal,accuracy=calibration_text(state['samples'])
        text(draw,mode+' · '+cal,(24,765),18,AXES[1] if accuracy==3 else GOLD,max_lines=1)
        advice='Move through figure-eights to calibrate.' if accuracy!=3 else ('Hold the phone flatter.' if max(abs(pitch),abs(roll))>20 else '')
        text(draw,advice,(24,796),16,MUTED,max_lines=1)
    else:
        text(draw,state.get('error',mode),(24,719),23,GOLD,max_lines=3)
