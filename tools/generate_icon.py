"""
tools/generate_icon.py — 生成 assets/LlamaMonitor.ico（开发图标，无网络依赖）

深色圆角背景 + 浅蓝色 "LM" 字样（与 Dashboard 深色主题一致）。
输出多尺寸 ICO：256/128/64/48/32/16（PyInstaller --icon 与托盘共用同一资源）。

运行（项目根目录）：
    python tools/generate_icon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "assets" / "LlamaMonitor.ico"
SIZES = [256, 128, 64, 48, 32, 16]

BG = (24, 30, 40, 255)        # 深色背景（接近 Dashboard 主题 #161b22）
FG = (108, 180, 255, 255)     # 浅蓝 "LM"
RADIUS_RATIO = 0.22           # 圆角半径 = 边长 * 0.22


def _font(size: int) -> ImageFont.ImageFont:
    """优先 Windows Arial 粗体，缺失时用 Pillow 内置默认字体。"""
    for name in ("arialbd.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def render(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    radius = max(1, int(size * RADIUS_RATIO))
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=BG)
    text = "LM"
    font_size = int(size * 0.42)
    font = _font(font_size)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    # 视觉居中（补偿字体的 baseline 偏移）
    x = (size - tw) / 2 - bbox[0]
    y = (size - th) / 2 - bbox[1]
    draw.text((x, y), text, font=font, fill=FG)
    return img


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    images = [render(s) for s in SIZES]
    images[0].save(
        OUT,
        format="ICO",
        sizes=[(s, s) for s in SIZES],
        append_images=images[1:],
    )
    print(f"written: {OUT} ({OUT.stat().st_size} bytes, sizes={SIZES})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
