"""Resolution-independent test card: the same primitives produce SVG and scanout."""
from html import escape
from PIL import Image,ImageDraw,ImageFont
from pathlib import Path

class Pattern:
    def __init__(self,width,height,label):
        self.width=width;self.height=height;self.items=[]
        w=width;h=height
        self.rect(0,0,w,h,'#101010')
        colors=('#bfbfbf','#bfbf00','#00bfbf','#00bf00','#bf00bf','#bf0000','#0000bf')
        top=round(h*.10);bottom=round(h*.66)
        for i,color in enumerate(colors):
            x=round(i*w/7);end=round((i+1)*w/7)
            self.rect(x,top,end-x,bottom-top,color)
        # A true circle reveals aspect distortion. Grid spacing is equal in X/Y.
        radius=round(min(w,h)*.22);cx=w//2;cy=(top+bottom)//2
        for color,stroke in (('#000000',max(3,round(h/180))),('#ffffff',max(1,round(h/500)))):
            self.items.append(('circle',(cx,cy,radius,color,stroke)))
        for x in range(w//2%max(1,h//8),w,max(1,h//8)):self.line(x,top,x,bottom,'#707070',1)
        for y in range(cy%max(1,h//8)+top,bottom,max(1,h//8)):self.line(0,y,w,y,'#707070',1)
        self.line(cx,top,cx,bottom,'#ffffff',1);self.line(0,cy,w,cy,'#ffffff',1)
        # 256-step luminance ramp: regenerate at each mode, never scale a bitmap.
        y=round(h*.69);end=round(h*.79)
        for i in range(256):
            x=round(i*w/256);xx=round((i+1)*w/256);c=f'#{i:02x}{i:02x}{i:02x}'
            self.rect(x,y,xx-x,end-y,c)
        y=round(h*.82);hh=round(h*.10)
        for i,value in enumerate((0,4,8,16,235,247,251,255)):
            x=round(i*w/16);xx=round((i+1)*w/16)
            self.rect(x,y,xx-x,hh,f'#{value:02x}{value:02x}{value:02x}')
        for x in range(w//2,3*w//4):self.rect(x,y,1,hh,'#ffffff' if x%2 else '#000000')
        for yy in range(y,y+hh):self.rect(3*w//4,yy,w-3*w//4,1,'#ffffff' if yy%2 else '#000000')
        self.text(w//2,round(h*.055),label,max(15,round(h*.032)),'#ffffff')
        self.text(w//2,round(h*.955),'75% RGB  |  Gray ramp  |  Near black / white  |  1 px lines',max(10,round(h*.020)),'#ffffff')
        # One-pixel perimeter plus inset corner marks expose overscan.
        for inset,color in ((0,'#ffffff'),(max(5,round(min(w,h)*.025)),'#ff4040')):
            self.line(inset,inset,w-1-inset,inset,color,1)
            self.line(inset,h-1-inset,w-1-inset,h-1-inset,color,1)
            self.line(inset,inset,inset,h-1-inset,color,1)
            self.line(w-1-inset,inset,w-1-inset,h-1-inset,color,1)

    def rect(self,x,y,w,h,color):self.items.append(('rect',(x,y,w,h,color)))
    def line(self,x1,y1,x2,y2,color,stroke):self.items.append(('line',(x1,y1,x2,y2,color,stroke)))
    def text(self,x,y,text,size,color):self.items.append(('text',(x,y,text,size,color)))

    def svg(self):
        parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{self.height}" viewBox="0 0 {self.width} {self.height}">']
        for kind,a in self.items:
            if kind=='rect':
                x,y,w,h,c=a;parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{c}"/>')
            elif kind=='line':
                x,y,xx,yy,c,s=a;parts.append(f'<line x1="{x}" y1="{y}" x2="{xx}" y2="{yy}" stroke="{c}" stroke-width="{s}"/>')
            elif kind=='circle':
                x,y,r,c,s=a;parts.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="none" stroke="{c}" stroke-width="{s}"/>')
            else:
                x,y,text,size,c=a;parts.append(f'<text x="{x}" y="{y}" text-anchor="middle" dominant-baseline="middle" font-family="sans-serif" font-size="{size}" fill="{c}">{escape(text)}</text>')
        return '\n'.join(parts+['</svg>'])

    def image(self,font_path=Path('/usr/share/fonts/droid/DroidSans.ttf')):
        image=Image.new('RGB',(self.width,self.height));draw=ImageDraw.Draw(image)
        for kind,a in self.items:
            if kind=='rect':
                x,y,w,h,c=a
                if w>0 and h>0:draw.rectangle((x,y,x+w-1,y+h-1),fill=c)
            elif kind=='line':
                x,y,xx,yy,c,s=a;draw.line((x,y,xx,yy),fill=c,width=s)
            elif kind=='circle':
                x,y,r,c,s=a;draw.ellipse((x-r,y-r,x+r,y+r),outline=c,width=s)
            else:
                x,y,text,size,c=a
                try:font=ImageFont.truetype(str(font_path),size)
                except OSError:font=ImageFont.load_default(size=size)
                draw.text((x,y),text,font=font,fill=c,anchor='mm')
        return image
