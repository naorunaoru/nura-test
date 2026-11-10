"""Persistent manual-test state; independent of the framebuffer and hardware."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib, json, os, uuid

def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def atomic_json(path, data):
    path=Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    with tmp.open('w') as f:
        json.dump(data,f,indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path)
    fd=os.open(path.parent,os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)

class Session:
    def __init__(self, directory, steps, kernel, fresh=False):
        self.directory=Path(directory); self.directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.path=self.directory/'current.json'; self.steps=steps
        catalog=hashlib.sha256(json.dumps(steps,sort_keys=True).encode()).hexdigest()
        self.data=None
        if self.path.exists():
            previous=json.loads(self.path.read_text())
            if not fresh and previous.get('completed'):
                self.data=previous
            elif not fresh and previous.get('kernel')==kernel and previous.get('catalog')==catalog:
                self.data=previous
            elif not fresh and previous.get('kernel')==kernel:
                # Keep unchanged answers when the suite gains or revises tests.
                # Archive the old catalogue's report before changing its cursor.
                atomic_json(self.directory/'reports'/(previous['id']+'-catalog-'+previous.get('catalog','unknown')[:12]+'.json'),previous)
                self.data=previous
                current={s['id']:s for s in steps}
                self.data['answers']={k:a for k,a in previous['answers'].items() if k in current and
                                      a['criterion']==current[k]['criterion'] and a['title']==current[k]['title']}
                self.data.update(catalog=catalog,test_count=len(steps),app_version='1.8',
                                 index=next((i for i,s in enumerate(steps) if s['id'] not in self.data['answers']),len(steps)))
            else:
                atomic_json(self.directory/'reports'/(previous['id']+'.json'),previous)
        if self.data is None:
            self.data=dict(schema=1,id=str(uuid.uuid4()),created=now(),kernel=kernel,
                           catalog=catalog,index=0,answers={},history=[],completed=False,
                           app_version='1.8',device='OnePlus 8 Pro',test_count=len(steps))
        self.save()

    def save(self):
        self.data['updated']=now(); atomic_json(self.path,self.data)
        if self.data.get('output_directory') and Path(self.data['output_directory']).is_dir():
            atomic_json(Path(self.data['output_directory'])/'results.json',self.data)

    def answer(self, verdict, evidence):
        if self.data['completed']:raise ValueError('Start a new run to record new results')
        if verdict not in ('pass','fail','skip'):raise ValueError('Invalid verdict')
        index=self.data['index']
        if not 0<=index<len(self.steps):raise ValueError('No active test')
        step=self.steps[index]
        entry=dict(id=step['id'],title=step['title'],criterion=step['criterion'],
                   verdict=verdict,time=now(),evidence=evidence,group=step.get('group','Hardware'))
        self.data['answers'][step['id']]=entry
        self.data['history'].append(entry)
        self.data['index']+=1; self.save()

    def back(self):
        self.data['index']=max(0,self.data['index']-1);self.save()

    def finish(self, snapshot):
        missing=[s['id'] for s in self.steps if s['id'] not in self.data['answers']]
        if missing:raise ValueError('Finish each test or mark it Skip first')
        self.data.update(completed=True,finished=now(),final_snapshot=snapshot)
        self.save();path=self.directory/'reports'/(self.data['id']+'.json')
        atomic_json(path,self.data);return path

    def counts(self):
        return {k:sum(a['verdict']==k for a in self.data['answers'].values()) for k in ('pass','fail','skip')}
