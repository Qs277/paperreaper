# -*- coding: utf-8 -*-
"""阅卷作答 · 手写落笔工具
用法：
    python render.py [试卷图片] [输出图片]
不传参数时，读取同目录下的 目标试卷.jpg，输出 作答_结果.png。
"""
import sys
import random
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

random.seed(11)
BASE = Path(__file__).resolve().parent
FONT = str(BASE / 'fonts' / 'xiongdi.ttf')
FALLBACK = str(BASE / 'fonts' / 'cicada.ttf')
INK = (14, 14, 14)
S = 2

SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / '目标试卷.jpg'
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else BASE / '作答_结果.png'

im = Image.open(SRC).convert('RGB')
W, H = im.size


def text_layer(text, size, font_path, erode):
    font = ImageFont.truetype(font_path, size * S)
    box = font.getbbox(text)
    layer = Image.new('RGBA', (box[2] - box[0] + 40 * S, box[3] - box[1] + 40 * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.text((20 * S - box[0], 20 * S - box[1]), text, font=font, fill=INK + (255,))
    a = layer.split()[3]
    for _ in range(erode):
        a = a.filter(ImageFilter.MinFilter(3))
    layer.putalpha(a)
    layer = layer.resize((layer.width // S, layer.height // S), Image.LANCZOS)
    b = layer.getbbox()
    return layer.crop(b) if b else layer


def put(layer, cx, cy):
    im.paste(layer, (int(cx * W - layer.width / 2), int(cy * H - layer.height / 2)), layer)


def write(text, cx, cy, size, font_path=None, erode=1):
    put(text_layer(text, size, font_path or FONT, erode), cx, cy)


def fraction(num, den, cx, cy, size):
    """上下结构分数：分子、横向分数线、分母。"""
    gap = size * 0.38
    put(text_layer(num, size, FONT, 0), cx, cy - gap / H)
    put(text_layer(den, size, FONT, 0), cx, cy + gap / H)
    bw = size * 0.50
    layer = Image.new('RGBA', (int(bw * S) + 20, 20), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.line([(10, 10), (int(bw * S) + 10, 10 + random.uniform(-1, 1) * S)],
           fill=INK + (255,), width=3 * S)
    layer = layer.resize((layer.width // S, layer.height // S), Image.LANCZOS)
    b = layer.getbbox()
    if b:
        layer = layer.crop(b)
    im.paste(layer, (int(cx * W - layer.width / 2), int(cy * H - layer.height / 2)), layer)


# 选择题：保持第五题 B 的大小与笔重
write('D', 0.620, 0.120, 78)
write('C', 0.706, 0.228, 78)
write('D', 0.625, 0.331, 78)
write('D', 0.858, 0.459, 78)
write('B', 0.620, 0.601, 78)
write('C', 0.465, 0.653, 78)

# 填空题：加粗、字号放大 30%
fraction('1', '3', 0.616, 0.820, 50)
write('α≥3', 0.557, 0.889, 46, font_path=FALLBACK, erode=0)
fraction('1', '2', 0.620, 0.922, 50)

im.save(OUT)
print(OUT)
