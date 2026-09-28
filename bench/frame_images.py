"""Render captured host frames. Raw bytes stay untouched; only header pixels are masked."""
import struct, zlib, binascii
from pathlib import Path

def chunk(kind,data):
    return struct.pack('>I',len(data))+kind+data+struct.pack('>I',binascii.crc32(kind+data)&0xffffffff)

def png(raw,meta,path):
    w,h=meta['width'],meta['height'];typ=meta['type'];size=meta['header_size']
    if not (0<w<=4096 and 0<h<=4096):raise ValueError('Invalid dimensions')
    data=bytearray(raw);data[:size]=bytes(size)
    rows=[]
    if typ==2:
        if len(data)<w*h*2:raise ValueError('Truncated depth frame')
        # YUYV is the transport label; module payload is little-endian uint16 depth in mm.
        for y in range(h):
            row=bytearray()
            for x in range(w):
                d=struct.unpack_from('<H',data,(y*w+x)*2)[0]
                if d==0:row.extend((0,0,0));continue
                t=max(0,min(1,(d-100)/1400));row.extend((int(255*(1-t)),int(180*(1-abs(t*2-1))),int(255*t)))
            rows.append(b'\0'+row)
        color=2;meta['preview']={'encoding':'RGB','depth_range_mm':[100,1500],'invalid_depth':'black'}
    elif typ==3:
        # Combined IR = [Y0 UV0 Y1 UV1], not one conventional NV12 image.
        eye=w*(h//2);gray=data[:eye]+data[eye*3//2:eye*3//2+eye]
        if len(gray)!=w*h:raise ValueError('Truncated combined IR')
        rows=[b'\0'+gray[y*w:(y+1)*w] for y in range(h)];color=0
        meta['preview']={'encoding':'grayscale','layout':'top=first IR eye; bottom=second IR eye'}
    else:
        if len(data)<w*h*3//2:raise ValueError('Truncated RGB NV12')
        def clip(v):return max(0,min(255,v))
        for y in range(h):
            row=bytearray()
            for x in range(w):
                yy=max(0,data[y*w+x]-16);uv=w*h+(y//2)*w+(x//2)*2
                u=data[uv]-128;v=data[uv+1]-128
                row.extend((clip((298*yy+409*v+128)>>8),clip((298*yy-100*u-208*v+128)>>8),clip((298*yy+516*u+128)>>8)))
            rows.append(b'\0'+row)
        color=2;meta['preview']={'encoding':'RGB','conversion':'NV12 BT.601 limited range'}
    meta['preview']['masked_header_bytes']=size
    content=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',w,h,8,color,0,0,0))+chunk(b'IDAT',zlib.compress(b''.join(rows),3))+chunk(b'IEND',b'')
    Path(path).write_bytes(content)
