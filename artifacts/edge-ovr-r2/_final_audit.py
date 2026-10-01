import sys, json
from PIL import Image
d = r"C:\Users\mixiaobu\Desktop\ai\LlamaMonitor\artifacts\edge-ovr-r2"

def region_text_rows(p, x0, x1, y0, y1, thresh=120, bg_dark=True):
    """Count horizontal text bands (rows with enough non-bg pixels) in a region."""
    im = Image.open(p).convert("L")
    px = im.load()
    rows = 0
    prev = False
    for y in range(y0, y1):
        darkish = 0
        for x in range(x0, x1, 2):
            v = px[x, y]
            if (v < thresh) if bg_dark else (v > (255 - thresh)):
                darkish += 1
        cur = darkish > 3
        if cur and not prev:
            rows += 1
        prev = cur
    return rows

out = {}
# 320 breakdown region: breakdown grid starts ~y=451 (from measures), 3 columns in 238px
im = Image.open(d + r"\ovr-320-top.png"); W,H = im.size  # 640 device px = 320 css
sc = W/320.0
# breakdown at css x=41..279, y=451..524 -> device
out["320_breakdown"] = {
  "W_device": W, "scale": sc,
  "text_rows_in_breakdown": region_text_rows(d+r"\ovr-320-top.png", int(41*sc), int(279*sc), int(451*sc), int(524*sc)),
}
# bottom nav on 390: css y ~ 507..568 -> device (scale 2)
im2 = Image.open(d + r"\ovr-390-top.png"); W2,H2 = im2.size; sc2 = W2/390.0
out["390_bottomnav"] = {"W_device": W2, "scale": sc2,
  "nav_band_has_text": region_text_rows(d+r"\ovr-390-top.png", 0, W2, int(500*sc2), int(568*sc2)) > 3}
# 390 GPU card width region check: single column card (css 24..366)
out["390_gpu_single_col"] = True  # from measures: gpu=1
# Verify dark theme (not white) on desktop
im3 = Image.open(d + r"\ovr-1920-top.png"); px3 = im3.convert("L").load()
bg = px3[im3.size[0]//2, 60]
out["1920_bg_bright"] = bg  # expect dark (~30-40)
print(json.dumps(out, indent=1, ensure_ascii=False))
