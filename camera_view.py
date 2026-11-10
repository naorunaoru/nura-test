"""Camera bench controls shared by the five sensor pages."""
import time
from PIL import Image
from core import atomic_json
from camera_live import readback

BLUE='#254f68';GREEN='#275f4c';RED='#76333e';FG='#e9eff8';MUTED='#9aa9bf'


def reset(app):
    app.camera_state={};app.camera_mtime=0;app.camera_tab='preview'
    app.camera_cursor=0;app.camera_value=None;app.camera_raw=False;app.camera_ois=False;app.camera_seq=0


def selected(app):
    controls=app.camera_state.get('caps',{}).get('controls',{})
    if not controls:return None,None
    ctl=list(controls.values())[app.camera_cursor%len(controls)]
    value=app.camera_value
    if value is None:
        value=app.camera_state.get('requested',{}).get(ctl['name'])
        if value is None:value=app.camera_state.get('metadata',{}).get(ctl['name'])
        if value is None:value=ctl.get('default',ctl.get('min'))
        if value is None and ctl['choices']:value=ctl['choices'][0]['value']
    return ctl,value


def change_value(ctl,value,direction):
    if ctl['type']=='enum':
        values=[c['value'] for c in ctl['choices']]
        return values[((values.index(value) if value in values else 0)+direction)%len(values)]
    if ctl['type']=='bool':return not value
    lo,hi=ctl['min'],ctl['max']
    step=(hi-lo)/10 if ctl['type']=='float' else max(1,round((hi-lo)/10))
    return max(lo,min(hi,round(value+direction*step,5)))


def buttons(app,step,available):
    st=app.camera_state;caps=st.get('caps',{});phase=st.get('phase');active=phase in ('starting','preview','capture')
    ready=available and app.worker is not None and bool(caps) and app.camera_seq<=st.get('ack',0)
    def row(names,y):
        width=448/len(names)
        return [(key,label,(round(16+i*width),y,round(12+(i+1)*width),y+58),color,enabled)
                for i,(key,label,color,enabled) in enumerate(names)]
    result=row([('cam_tab_'+k,label,GREEN if app.camera_tab==k else BLUE,available)
                for k,label in [('preview','Preview'),('capture','Capture'),('controls','Controls')]],180)
    if app.camera_tab=='preview':
        result+=row([('cam_ois','OIS: '+('on' if app.camera_ois else 'off'),BLUE,ready and step['action']=='camera_main'),
                     ('cam_focus','AF scan',BLUE,ready and 'AfTrigger' in caps.get('controls',{})),
                     ('cam_snapshot','Save frame',BLUE,ready and phase=='preview' and st.get('preview_seq') is not None)],790)
    elif app.camera_tab=='capture':
        result+=row([('cam_reference','1280×960 RGB',BLUE,ready and not app.camera_raw)],715)
        result+=row([('cam_format','RAW' if app.camera_raw else 'RGB',BLUE,ready and bool(caps.get('raw'))),
                     ('cam_sensor','Full sensor',GREEN,ready and bool(caps.get('sensor'))),
                     ('cam_maximum','Max stream',BLUE,ready and bool(caps.get('raw') if app.camera_raw else caps.get('formats',{}).get('XRGB8888')))],790)
    else:
        ctl,value=selected(app)
        editable=bool(ready and ctl and 'in' in ctl['direction'] and ctl['type']!='unsupported')
        judged=bool(ready and ctl and phase=='preview' and st.get('preview_seq') is not None and ctl['name'] in st.get('requested',{}))
        result+=row([('cam_prev','< Ctrl',BLUE,bool(ctl)),('cam_next','Ctrl >',BLUE,bool(ctl)),
                     ('cam_down','−',BLUE,editable),('cam_up','+',BLUE,editable),('cam_apply','Apply',GREEN,editable)],715)
        result+=row([('cam_works','Works',GREEN,judged),('cam_noeffect','No effect',RED,judged)],790)
    if app.worker is None:
        result.append(('run','Retry',(130,882,350,950),BLUE,available))
    else:
        result.append(('cam_stop' if active else 'cam_start','Stop' if active else 'Start preview',
                       (130,882,350,950),RED if active else GREEN,available if active else ready))
    return result


def draw(renderer,image,draw,app,runtime):
    st=app.camera_state;caps=st.get('caps',{});tab=app.camera_tab
    text=lambda s,y,color=MUTED,lines=1,size=17:renderer.text(draw,s,(24,y),size,color,max_lines=lines)
    text(caps.get('model','Probing')+'  ·  '+st.get('phase','opening'),148)
    preview=runtime/'camera-preview.jpg'
    if preview.exists():
        try:
            with Image.open(preview) as im:image.paste(im,((480-im.width)//2,250))
        except OSError:pass
    metadata=st.get('metadata',{});af=metadata.get('AfState')
    af_name={0:'idle',1:'scanning',2:'focused',3:'failed'}.get(af,str(af) if af is not None else '—')
    fmt=st.get('configured') or []
    text(('×'.join(str(x) for x in fmt[:2])+'  ·  ' if fmt else '')+
         f"{st.get('frames',0)} frames  ·  {st.get('fps',0):.1f} sensor fps",507,FG)
    if tab=='preview':
        ctl=caps.get('controls',{});afmode=st.get('requested',{}).get('AfMode')
        text('AF: '+({0:'manual',1:'auto',2:'continuous'}.get(afmode,'available')+' / '+af_name if 'AfMode' in ctl else 'not exposed'),545)
        if st.get('ois_requested'):
            text(f"OIS: {st.get('ois_pm') or 'starting'}  ·  gyro {st.get('gyro_samples',0)}  ·  {st.get('ois_seconds_left',20)}s left",578,FG,2)
        else:text('OIS: '+('selected for next preview' if app.camera_ois else 'off for preview') if st.get('camera')=='camera_main' else 'OIS: not supported on this camera',578)
        text('Change focus distance; stop OIS if the lens buzzes continuously.',620,MUTED,2)
        if st.get('snapshot'):text('Saved: '+st['snapshot'].split('/')[-1],727,FG)
    elif tab=='capture':
        dims=lambda v:'×'.join(str(x) for x in v[:2]) if v else 'not advertised'
        text('Sensor array: '+dims(caps.get('sensor')),545,FG)
        fmt=(caps.get('raw') or [0,0,None])[2] if app.camera_raw else 'XRGB8888'
        maximum=(caps.get('raw_formats',{}) if app.camera_raw else caps.get('formats',{})).get(fmt)
        text(f'Max {fmt or "raw"}: '+dims(maximum),577)
        if st.get('captures'):
            last=st['captures'][-1];req=last['requested']
            text(f"Last: {dims(req['size'])} {'RAW' if req['raw'] else 'RGB'}  ·  {last['status']}",616,FG)
        else:text('Exact size required. An adjusted size is a failure.',616)
    else:
        ctl,val=selected(app)
        if ctl:
            name=ctl['name'];entry=next((x['label'] for x in ctl['choices'] if x['value']==val),None)
            text(f"{app.camera_cursor%len(caps['controls'])+1}/{len(caps['controls'])}  {name}: {entry or val}",542,FG,1,20)
            text(f"{ctl['direction']}  ·  "+(ctl['description'] or ' / '.join(str(x['value']) for x in ctl['choices'])),576)
            req=st.get('requested',{}).get(name)
            text(f"Requested: {req if req is not None else '—'}   Reported: {metadata.get(name,'—')}",606)
            text((readback(ctl,req,metadata) if req is not None else 'Not applied')+'  ·  AF '+af_name,631)
            prior=next((v for v in reversed(st.get('verdicts',[])) if v['name']==name),None)
            if prior:text(f"Last verdict: {prior['verdict']} at {prior['requested']}",686,FG)
        else:text('No controls reported',545)
    msg=st.get('error') or (app.result or {}).get('error') or st.get('message') or app.status_text
    text(msg,659,'#ff9b9b' if st.get('error') or (app.result and not app.result.get('ok')) else '#80e5bd',2,16)


def action(app,key,runtime):
    if key.startswith('cam_tab_'):app.camera_tab=key[8:];return
    if key=='cam_format':app.camera_raw=not app.camera_raw;return
    ctl,val=selected(app)
    if key in ('cam_prev','cam_next'):
        app.camera_cursor+=1 if key=='cam_next' else -1;app.camera_value=None;return
    if key in ('cam_up','cam_down') and ctl:
        app.camera_value=change_value(ctl,val,1 if key=='cam_up' else -1);return
    if key=='cam_ois':
        app.camera_ois=not app.camera_ois
        if app.camera_state.get('phase')!='preview':return
        key='cam_start'
    if key=='cam_start':cmd={'op':'preview','ois':app.camera_ois}
    elif key=='cam_stop':cmd={'op':'stop'};(runtime/'camera-abort').touch()
    elif key in ('cam_sensor','cam_maximum','cam_reference'):cmd={'op':'still','target':key[4:],'raw':app.camera_raw}
    elif key=='cam_focus':cmd={'op':'apply','name':'AfTrigger','value':0,'ois':app.camera_ois}
    elif key=='cam_snapshot':cmd={'op':'snapshot'}
    elif key=='cam_apply' and ctl:cmd={'op':'apply','name':ctl['name'],'value':val,'ois':app.camera_ois}
    elif key in ('cam_works','cam_noeffect') and ctl:cmd={'op':'verdict','name':ctl['name'],'verdict':'works' if key=='cam_works' else 'no effect'}
    else:return
    app.camera_seq=time.monotonic_ns()
    atomic_json(runtime/'camera-command.json',{'seq':app.camera_seq,**cmd})
