
import sys
from PIL import Image
import os, json
d = r"C:\Users\mixiaobu\Desktop\ai\LlamaMonitor\artifacts\edge-ovr-r2"
views = [("ovr-1920-top.png","1920",1920),("ovr-1065-top.png","1065",1065),("ovr-390-top.png","390",390),("ovr-320-top.png","320",320),("ovr-390-full.png","390F",390),("ovr-1920-full.png","1920F",1920),("ovr-1065-full.png","1065F",1065),("ovr-320-full.png","320F",320)]
res = {}
for fn,tag,vw in views:
    p = os.path.join(d, fn)
    if not os.path.exists(p): res[tag]="MISSING"; continue
    im = Image.open(p).convert("RGB")
    W,H = im.size
    # sample a vertical strip at right edge (x=W-3) to detect dark content near edge (clipping) vs scrollbar
    px = im.load()
    # count distinct "card" horizontal bands by sampling mid column brightness transitions
    col = [px[W//2, y][0] for y in range(0,H,4)]
    bands = 0; prev = None
    for v in col:
        if prev is None: prev=v; continue
        if abs(v-prev) > 24: bands += 1
        prev = v
    res[tag] = {"W":W,"H":H,"bands":bands}
print(json.dumps(res, indent=1))
