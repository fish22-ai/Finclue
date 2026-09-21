#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成 PWA 图标 → docs/icons/

设计 token 与 render.py 的 CSS 同源：宣纸底 #f8f4ed、朱红 #9e3d47、金 #c9a227。
图形语言 = 朱红印章「金」+ 一根穿过印章背后的金线（「金线索」的线索）。

产物：
  icon-192.png / icon-512.png      purpose=any，圆角宣纸卡
  icon-maskable-512.png            purpose=maskable，满幅纸底，图形收在安全区
  apple-touch-icon.png             180，满幅不透明（iOS 自己做圆角）
  favicon-32.png                   小尺寸简化版（只留印章）

重跑即可再生成；依赖 pillow。
"""

import os

from PIL import Image, ImageDraw, ImageFont

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(PROJECT, "docs", "icons")

PAPER = (248, 244, 237, 255)   # --bg
CARD = (255, 253, 249, 255)    # --card
LINE = (231, 222, 209, 255)    # --line
RED = (158, 61, 71, 255)       # --accent
GOLD = (201, 162, 39, 255)     # --gold
IVORY = (250, 246, 239, 255)   # 印章上的字

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\simkai.ttf",   # 楷体，最接近印章感
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyhbd.ttc",
]


def load_font(size):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    raise SystemExit("找不到中文字体，请把字体路径加进 FONT_CANDIDATES")


def thread_points(s, n=60):
    """金线：从左下到右上的缓弧线，中段绕一个小圈（结）。
    返回 [(x, y), ...]，坐标均为 0~1 比例，调用方乘以画布尺寸。"""
    pts = []
    for i in range(n + 1):
        t = i / n
        x = 0.06 + 0.88 * t
        y = 0.78 - 0.50 * t
        # 中段（t≈0.45~0.62）叠一个小环
        if 0.40 < t < 0.68:
            u = (t - 0.40) / 0.28
            x += 0.045 * (1 - abs(2 * u - 1)) * (1 if u < 0.5 else 1)
            y -= 0.10 * (1 - abs(2 * u - 1)) ** 1.5 * (-1 if u < 0.5 else 1)
        pts.append((x, y))
    return pts


def draw_card(draw, s, rounded=True):
    """宣纸卡底 + 细边框（对应页面的 card/line token）。"""
    pad = 0 if not rounded else int(s * 0.04)
    box = [pad, pad, s - pad, s - pad]
    r = int(s * 0.19) if rounded else 0
    draw.rounded_rectangle(box, radius=r, fill=CARD, outline=LINE,
                           width=max(2, s // 128))


def draw_seal(img, s, scale=0.40):
    """居中朱红印章 +「金」+ 背后的金线。scale = 印章边长 / 画布。"""
    draw = ImageDraw.Draw(img)
    # 金线在印章后面
    pts = [(x * s, y * s) for x, y in thread_points(s)]
    draw.line(pts, fill=GOLD, width=max(3, s // 64), joint="curve")
    # 印章
    side = s * scale
    x0 = (s - side) / 2
    y0 = (s - side) / 2
    draw.rounded_rectangle([x0, y0, x0 + side, y0 + side],
                           radius=side * 0.14, fill=RED)
    f = load_font(int(side * 0.62))
    draw.text((x0 + side / 2, y0 + side / 2 + side * 0.01), "金",
              font=f, fill=IVORY, anchor="mm")


def make_any(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw_card(ImageDraw.Draw(img), size, rounded=True)
    draw_seal(img, size, scale=0.42)
    return img


def make_maskable(size):
    """满幅纸底（无透明角），印章收到中心安全区（直径 80% 圆内）。"""
    img = Image.new("RGBA", (size, size), PAPER)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, size, size], fill=CARD)          # 满幅 card 底
    d.rectangle([0, int(size*0.985), size, size], fill=RED)  # 底部一道 accent 线
    draw_seal(img, size, scale=0.34)                     # 收进安全区
    return img


def make_square(size):
    """满幅不透明（apple-touch-icon 用）。"""
    img = Image.new("RGBA", (size, size), PAPER)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, size, size], fill=CARD)
    d.rectangle([0, 0, size, int(size * 0.015)], fill=RED)
    d.rectangle([0, int(size * 0.985), size, size], fill=RED)
    draw_seal(img, size, scale=0.44)
    return img


def main():
    os.makedirs(OUT, exist_ok=True)
    make_any(192).save(os.path.join(OUT, "icon-192.png"))
    make_any(512).save(os.path.join(OUT, "icon-512.png"))
    make_maskable(512).save(os.path.join(OUT, "icon-maskable-512.png"))
    make_square(180).save(os.path.join(OUT, "apple-touch-icon.png"))
    make_any(512).resize((32, 32), Image.LANCZOS).save(
        os.path.join(OUT, "favicon-32.png"))
    print("icons written to", OUT)


if __name__ == "__main__":
    main()
