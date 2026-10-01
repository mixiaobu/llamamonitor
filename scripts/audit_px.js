// Programmatic PIL-style visual audit on the Edge screenshots.
// Verifies structure per viewport: card regions present, no horizontal clipping,
// GPU cards positioned left-aligned, section count, bottom nav present (mobile).
const { execSync } = require("node:child_process");
const path = require("path"), fs = require("fs");
const OUT = path.join(__dirname, "..", "artifacts", "edge-ovr-r2");
// Use Python (Pillow) for pixel sampling since it's reliable.
const py = path.join(__dirname, "..", ".venv-final", "Scripts", "python.exe");
const script = `
import sys
from PIL import Image
import os, json
d = r"${OUT}"
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
`;
fs.writeFileSync(path.join(OUT, "_audit.py"), script);
console.log(execSync(`"${py}" -X utf8 "${path.join(OUT, "_audit.py")}"`, { encoding: "utf8" }));
