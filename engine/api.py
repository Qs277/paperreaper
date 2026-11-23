# -*- coding: utf-8 -*-
"""本机落笔服务：网页把试卷和矩形发过来，返回写好字的图片。
启动：python api.py   默认 http://127.0.0.1:9130
"""
import base64
import io
import json
import math
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

BASE = os.path.dirname(os.path.abspath(__file__))
FONT = os.path.join(BASE, 'fonts', 'xiongdi.ttf')
FALLBACK = os.path.join(BASE, 'fonts', 'cicada.ttf')
INK = (14, 14, 14)
PENCIL = (74, 74, 74)
S = 2

# 同一字的不同写法：少量字符换用同风格手写字体，避免整页一个模子
VARIANTS = [os.path.join(BASE, 'fonts', 'newleaf.otf'),
            os.path.join(BASE, 'fonts', 'qishan.ttf')]
_CMAP_CACHE = {}


def _supports(fp, ch):
    try:
        from fontTools.ttLib import TTFont
    except Exception:
        return False
    if fp not in _CMAP_CACHE:
        try:
            f = TTFont(fp, fontNumber=0)
            c = set()
            for t in f['cmap'].tables:
                c.update(t.cmap.keys())
            _CMAP_CACHE[fp] = c
        except Exception:
            _CMAP_CACHE[fp] = set()
    return ord(ch) in _CMAP_CACHE[fp]


def _pick_font(ch, font_path, rng, p=0.20):
    """基础字体为主；约 20% 的字换成同风格变体，且只换该字确实存在的字体。"""
    if os.path.abspath(font_path) != os.path.abspath(FONT):
        return font_path
    if rng.random() >= p:
        return font_path
    opts = [v for v in VARIANTS if _supports(v, ch)]
    return opts[int(rng.integers(0, len(opts)))] if opts else font_path


def _grain(layer, strength=0.16):
    """给铅笔字加纸面颗粒，让笔画有轻重不均的铅笔感。"""
    a = np.array(layer.split()[3], dtype=np.float32)
    rng = np.random.default_rng(11)
    noise = np.clip(rng.normal(1.0, strength, a.shape), 0.75, 1.0)
    layer.putalpha(Image.fromarray(np.clip(a * noise, 0, 255).astype(np.uint8)))
    return layer


def text_layer(text, size, font_path, erode, ink=INK, grain=False, thicken=0, jitter=False, seed=None,
               profile=None):
    if jitter and text:
        return _jitter_layer(text, size, font_path, erode, ink, grain, thicken, seed, profile)
    font = ImageFont.truetype(font_path, max(8, int(size * S)))
    box = font.getbbox(text)
    layer = Image.new('RGBA', (box[2] - box[0] + 40 * S, box[3] - box[1] + 40 * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.text((20 * S - box[0], 20 * S - box[1]), text, font=font, fill=ink + (255,))
    a = layer.split()[3]
    for _ in range(erode):
        a = a.filter(ImageFilter.MinFilter(3))
    for _ in range(thicken):
        a = a.filter(ImageFilter.MaxFilter(3))
    layer.putalpha(a)
    layer = layer.resize((max(1, layer.width // S), max(1, layer.height // S)), Image.LANCZOS)
    # 与逐字路径一致：压深抗锯齿边缘，避免分数等非逐字文本发浅
    la = np.array(layer.split()[3], dtype=np.float32)
    layer.putalpha(Image.fromarray(np.clip(255.0 * np.power(la / 255.0, 0.65), 0, 255).astype(np.uint8)))
    b = layer.getbbox()
    layer = layer.crop(b) if b else layer
    if grain:
        layer = _grain(layer)
    return layer


def _elastic(g, rng, amp):
    """用 3x3 网格做低频弹性形变，改变笔画弧度和内部比例。"""
    w, h = g.size
    if w < 14 or h < 14 or amp < 0.3:
        return g
    xs = [0.0, w / 3.0, 2.0 * w / 3.0, float(w)]
    ys = [0.0, h / 3.0, 2.0 * h / 3.0, float(h)]
    px = [row[:] for row in [xs, xs, xs, xs]]
    py = [[ys[i]] * 4 for i in range(4)]
    for iy in (1, 2):
        for ix in (1, 2):
            px[iy][ix] += float(rng.uniform(-amp, amp))
            py[iy][ix] += float(rng.uniform(-amp, amp))
    mesh = []
    for iy in range(3):
        for ix in range(3):
            src = tuple(int(round(v)) for v in (xs[ix], ys[iy], xs[ix + 1], ys[iy + 1]))
            dst = tuple(int(round(v)) for v in (
                px[iy][ix], py[iy][ix],
                px[iy + 1][ix], py[iy + 1][ix],
                px[iy + 1][ix + 1], py[iy + 1][ix + 1],
                px[iy][ix + 1], py[iy][ix + 1]))
            mesh.append((src, dst))
    return g.transform((w, h), Image.MESH, mesh, resample=Image.BICUBIC)


def _jitter_layer(text, size, font_path, erode, ink, grain, thicken, seed, profile=None):
    """逐字做仿射 + 弹性形变 + 风格指纹 + 笔压，避免同一个字母写得一模一样。

    profile（可选）是“这个人的书写习惯”，整行共用，不逐字随机：
      slant     整体倾斜偏置（>0 右倾，约 ±0.10）
      x_height  竖向整体比例（0.94~1.06）
      spacing   字距系数（0.95~1.06）
      stroke    额外笔重档位（+1 更粗 / -1 更细）
      ink       覆盖墨色 (r,g,b)
      pressure  笔压起伏强度（0~0.25，0 关闭）
      baseline  基线起伏系数（0.6~1.4）
    """
    prof = profile or {}
    rng = np.random.default_rng(seed if seed is not None else (abs(hash(text)) % (2 ** 32)))
    base_font = ImageFont.truetype(font_path, max(8, int(size * S)))
    asc, desc = base_font.getmetrics()
    pad = 12 * S
    gh = asc + desc + 2 * pad + 10 * S
    glyphs, pen = [], 0
    pvar = 0.0   # 统一笔重：不再逐字换字体，避免个别字明显偏细偏浅
    period = max(3.0, float(rng.uniform(6.0, 10.0)))  # 基线起伏的“波长”
    phase = float(rng.uniform(0, 2 * math.pi))
    baseline_k = float(prof.get('baseline', 1.0))
    base_amp = 0.10 * size * S * baseline_k            # 基线起伏幅度（整行的低频波）
    drift = 0.0
    slant_bias = float(prof.get('slant', 0.0))
    xh = float(prof.get('x_height', 1.0))
    space = float(prof.get('spacing', 1.0))
    stroke_extra = int(prof.get('stroke', 0))
    if prof.get('ink') and not grain:
        ink = tuple(prof['ink'])
    thicken = max(0, thicken + stroke_extra)
    for i, ch in enumerate(text):
        fp = _pick_font(ch, font_path, rng, pvar)
        fs = max(8, int(size * S * float(rng.uniform(0.97, 1.03))))
        fnt = ImageFont.truetype(fp, fs)
        asc_i = fnt.getmetrics()[0]
        adv = fnt.getlength(ch)
        gw = max(int(adv) + 2 * pad, 2)
        g = Image.new('RGBA', (gw, gh), (0, 0, 0, 0))
        ImageDraw.Draw(g).text((pad, pad + max(0, asc - asc_i)), ch, font=fnt, fill=ink + (255,))
        a = g.split()[3]
        for _ in range(max(0, erode)):
            a = a.filter(ImageFilter.MinFilter(3))
        # 整行统一笔重：不再逐字随机加粗，避免粗细忽胖忽瘦
        for _ in range(thicken):
            a = a.filter(ImageFilter.MaxFilter(3))
        g.putalpha(a)

        # 独立横纵缩放：同一个字有时偏瘦、有时偏扁
        sx = float(rng.uniform(0.97, 1.03))
        sy = xh * float(rng.uniform(0.97, 1.03))
        if abs(sx - 1) > 0.004 or abs(sy - 1) > 0.004:
            g = g.resize((max(2, int(g.width * sx)), max(2, int(g.height * sy))), Image.LANCZOS)
        # 轻微右倾/左倾：整行倾斜偏置 + 逐字小抖动
        sh = slant_bias + float(rng.uniform(-0.07, 0.07))
        if abs(sh) > 0.008:
            g = g.transform(g.size, Image.AFFINE, (1, sh, -sh * g.height / 2, 0, 1, 0),
                            resample=Image.BICUBIC)
        # 小角度旋转
        g = g.rotate(float(rng.uniform(-3.0, 3.0)), resample=Image.BICUBIC, expand=False)
        # 低频弹性形变：让两个相同字母的轮廓也不一样（幅度独立，不能覆盖基线幅度）
        e_amp = max(1.0, min(3.0, 0.05 * min(g.size)))
        g = _elastic(g, rng, e_amp)

        # 基线：低频正弦 + 缓慢漂移 + 极小逐字噪声，避免一条死直线
        drift += float(rng.normal(0.0, 0.010 * size * S))
        drift = float(np.clip(drift, -base_amp, base_amp))
        wave = base_amp * math.sin(2 * math.pi * i / period + phase)
        dy = int(wave + drift + rng.integers(-S, S + 1))
        glyphs.append((g, pen, dy))
        pen += int(adv * space * float(rng.uniform(0.98, 1.02))) + int(rng.integers(-S, S + 1))
    line = Image.new('RGBA', (max(pen + 2 * pad, 2), gh + 6 * S), (0, 0, 0, 0))
    for g, px, dy in glyphs:
        line.alpha_composite(g, (px, 3 * S + dy))
    b = line.getbbox()
    if b:
        line = line.crop(b)
    line = line.resize((max(1, line.width // S), max(1, line.height // S)), Image.LANCZOS)

    # 笔压：沿行方向做低频轻重起伏 + 上浅下深，模拟落笔/收笔压力，不逐字跳动
    pressure = float(prof.get('pressure', 0.0))
    if pressure > 0 and line.width > 4:
        la = np.array(line.split()[3], dtype=np.float32)
        lw, lh = line.size
        xs = np.arange(lw, dtype=np.float32)
        lam = max(36.0, lw * 0.55)
        field = 1.0 - pressure * 0.5 * (1.0 - np.sin(2 * np.pi * xs / lam + phase))
        vgrad = np.linspace(1.0 - pressure * 0.45, 1.0, lh, dtype=np.float32)[:, None]
        la = la * field[None, :] * vgrad
        line.putalpha(Image.fromarray(np.clip(la, 0, 255).astype(np.uint8)))

    # 压深抗锯齿边缘：保证所有字（尤其数字）墨色一致，不发浅
    la = np.array(line.split()[3], dtype=np.float32)
    line.putalpha(Image.fromarray(np.clip(255.0 * np.power(la / 255.0, 0.65), 0, 255).astype(np.uint8)))
    if grain:
        line = _grain(line)
    return line


def fit_layer(layer, w, h):
    """把字缩到矩形内，绝不越界。"""
    k = min(1.0, max(0.02, w) / layer.width, max(0.02, h) / layer.height)
    if k < 1.0:
        layer = layer.resize((max(1, int(layer.width * k)), max(1, int(layer.height * k))), Image.LANCZOS)
    return layer


def paste_center(img, layer, cx, cy):
    img.paste(layer, (int(cx - layer.width / 2), int(cy - layer.height / 2)), layer)


def draw_text(img, text, rect, bold, size=None, style='pen', fit=False, thicken=0, profile=None):
    x, y, w, h = rect
    if size is None:
        size = min(h * 0.86, w * 1.3, 120)
    else:
        size = float(size)
    if not text:
        return None
    fp = FALLBACK if any(ch in text for ch in 'αβγθλμπσφω≥≤≠≈') else FONT
    pencil = style == 'pencil'
    layer = text_layer(text, size, fp, 1 if pencil or not bold else 0,
                       PENCIL if pencil else INK, grain=pencil, thicken=thicken, profile=profile)
    if fit:
        layer = fit_layer(layer, w, h)
    elif layer.width > max(w * 1.25, 24):
        k = (w * 1.25) / layer.width
        layer = layer.resize((max(1, int(layer.width * k)), max(1, int(layer.height * k))), Image.LANCZOS)
    paste_center(img, layer, x + w / 2, y + h / 2)
    return layer.size


def draw_fraction(img, num, den, rect, size=None, style='pen', fit=False, thicken=0, profile=None):
    x, y, w, h = rect
    cx, cy = x + w / 2, y + h / 2
    if size is None:
        size = max(20, min(h * 0.46, w * 1.1, 64))
    else:
        size = float(size)
    pencil = style == 'pencil'
    ink = PENCIL if pencil else INK
    if (profile or {}).get('ink') and not pencil:
        ink = tuple(profile['ink'])
    gap = size * 0.46
    a = text_layer(num, size, FONT, 0, ink, grain=pencil, thicken=thicken, profile=profile)
    b = text_layer(den, size, FONT, 0, ink, grain=pencil, thicken=thicken, profile=profile)
    bw = max(12, min(w * 0.5, size * 1.5))
    bar_h = 3
    total_h = a.height + b.height + int(gap * 2) + bar_h
    total_w = max(a.width, b.width, bw)
    if fit and (total_h > h or total_w > w):
        k = min(h / max(total_h, 1), w / max(total_w, 1))
        a = a.resize((max(1, int(a.width * k)), max(1, int(a.height * k))), Image.LANCZOS)
        b = b.resize((max(1, int(b.width * k)), max(1, int(b.height * k))), Image.LANCZOS)
        gap *= k
        bw *= k
    paste_center(img, a, cx, cy - gap)
    paste_center(img, b, cx, cy + gap)
    bar = Image.new('RGBA', (int(bw * S) + 20, 20), (0, 0, 0, 0))
    d = ImageDraw.Draw(bar)
    d.line([(10, 10), (10 + int(bw * S), 10)], fill=ink + (255,), width=max(2, int(1.5 * S)))
    bar = bar.resize((max(1, bar.width // S), max(1, bar.height // S)), Image.LANCZOS)
    bb = bar.getbbox()
    if bb:
        bar = bar.crop(bb)
    if pencil:
        bar = _grain(bar)
    paste_center(img, bar, cx, cy)


def render(payload):
    raw = payload['image']
    b64 = raw.split(',', 1)[1] if ',' in raw else raw
    img = Image.open(io.BytesIO(base64.b64decode(b64))).convert('RGB')
    W, H = img.size
    for item in payload.get('items') or []:
        r = item.get('rect') or {}
        rect = (
            float(r.get('x', 0)) * W,
            float(r.get('y', 0)) * H,
            max(8, float(r.get('w', 0.02)) * W),
            max(8, float(r.get('h', 0.02)) * H),
        )
        style = item.get('style') or payload.get('style') or 'pen'
        fit = bool(item.get('fit') if item.get('fit') is not None else payload.get('fit'))
        if item.get('type') == 'fraction':
            draw_fraction(img, str(item.get('num', '')), str(item.get('den', '')), rect,
                          item.get('size'), style, fit)
        else:
            draw_text(img, str(item.get('text', '')), rect, bool(item.get('bold')),
                      item.get('size'), style, fit)
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.startswith('/health'):
            body = json.dumps({'ok': True}).encode()
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if not self.path.startswith('/render'):
            self.send_response(404)
            self._cors()
            self.end_headers()
            return
        try:
            n = int(self.headers.get('Content-Length', 0))
            payload = json.loads(self.rfile.read(n).decode('utf-8'))
            image = render(payload)
            body = json.dumps({'image': image}).encode()
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            body = json.dumps({'error': str(e)}).encode()
            self.send_response(500)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9130
    ThreadingHTTPServer.allow_reuse_address = True
    print('http://127.0.0.1:%d' % port)
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
