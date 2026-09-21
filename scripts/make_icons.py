#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成 PWA 图标 → docs/icons/

设计 token 与 render.py 的 CSS 同源：宣纸底 #f8f4ed、朱红 #9e3d47、金 #c9a227。
图形语言 = 一根金线（「线索」的意象）串起几颗金星，一颗小红星点缀。
纯图形、无文字。

产物：
  icon-192.png / icon-512.png      purpose=any，圆角宣纸卡
  icon-maskable-512.png            purpose=maskable，满幅纸底，图形收在安全区
  apple-touch-icon.png             180，满幅不透明（iOS 自己做圆角）
  favicon-32.png                   小尺寸简化版

重跑即可再生成；依赖 pillow。
"""

import math
import os

from PIL import Image, ImageDraw

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(PROJECT, "docs", "icons")

PAPER = (248, 244, 237, 255)   # --bg
CARD = (255, 253, 249, 255)    # --card
LINE = (231, 222, 209, 255)    # --line
RED = (158, 61, 71, 255)       # --accent
GOLD = (201, 162, 39, 255)     # --gold
GOLD_INK = (138, 108, 18, 255)  # --gold-ink，星星描边


def thread_points(s, n=80):
    """金线：从左下到右上的缓弧线（「线索」）。返回 0~1 比例坐标列表。"""
    pts = []
    for i in range(n + 1):
        t = i / n
        x = 0.08 + 0.84 * t
        y = 0.74 - 0.46 * t
        # 轻微的下弯弧度，像一根挂起来的线
        y += 0.06 * math.sin(math.pi * t)
        pts.append((x, y))
    return pts


def star_pts(cx, cy, r, rot=-90.0):
    """五角星顶点：外接圆半径 r，rot=-90 让一个角朝上。"""
    pts = []
    for i in range(10):
        rad = math.radians(rot + i * 36)
        rr = r if i % 2 == 0 else r * 0.42
        pts.append((cx + rr * math.cos(rad), cy + rr * math.sin(rad)))
    return pts


def draw_composition(img, s, k=1.0):
    """整个图形：金线 + 三颗金星 + 一颗小红星。k 是整体缩放（maskable 用 <1 收安全区）。"""
    draw = ImageDraw.Draw(img)

    def P(x, y):
        return (s / 2 + (x - 0.5) * s * k, s / 2 + (y - 0.5) * s * k)

    lw = max(2, int(s * 0.016 * k))

    # 金线（线索）
    pts = [P(x, y) for x, y in thread_points(s)]
    draw.line(pts, fill=GOLD, width=lw, joint="curve")

    # 金星：一大两小，都落在金线附近；红星点缀
    stars = [
        (0.575, 0.405, 0.165, GOLD, GOLD_INK),   # 主星
        (0.265, 0.285, 0.085, GOLD, GOLD_INK),
        (0.335, 0.640, 0.055, GOLD, GOLD_INK),
        (0.780, 0.690, 0.050, RED, None),        # 红星
    ]
    for fx, fy, fr, fill, outline in stars:
        cx, cy = P(fx, fy)
        poly = star_pts(cx, cy, fr * s * k)
        draw.polygon(poly, fill=fill, outline=outline,
                     width=max(1, lw // 2) if outline else 0)


def draw_card(draw, s, rounded=True):
    """宣纸卡底 + 细边框（对应页面的 card/line token）。"""
    pad = 0 if not rounded else int(s * 0.04)
    box = [pad, pad, s - pad, s - pad]
    r = int(s * 0.19) if rounded else 0
    draw.rounded_rectangle(box, radius=r, fill=CARD, outline=LINE,
                           width=max(2, s // 128))


def make_any(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw_card(ImageDraw.Draw(img), size, rounded=True)
    draw_composition(img, size, k=1.0)
    return img


def make_maskable(size):
    """满幅纸底（无透明角），图形整体缩到中心安全区（直径 80% 圆内）。"""
    img = Image.new("RGBA", (size, size), CARD)
    d = ImageDraw.Draw(img)
    d.rectangle([0, int(size * 0.985), size, size], fill=RED)  # 底部一道 accent 线
    draw_composition(img, size, k=0.72)
    return img


def make_square(size):
    """满幅不透明（apple-touch-icon 用）。"""
    img = Image.new("RGBA", (size, size), CARD)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, size, int(size * 0.015)], fill=RED)
    d.rectangle([0, int(size * 0.985), size, size], fill=RED)
    draw_composition(img, size, k=0.92)
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
