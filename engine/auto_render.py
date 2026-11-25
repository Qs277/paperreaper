# -*- coding: utf-8 -*-
"""AI 只给“第几题 + 答案 + 大概哪一行”，工具自己找括号/横线、缩放到不出格、贴着线落笔，并回检。

坐标校验由 align.py 完成：
- 用选择题的高置信检测估计整页 dy（中位数，抗单题误检）；
- 把 dy 回代给所有题，再走原检测路径；
- 偏移为 0 时与人工逐题标定逐像素一致；偏移 2%~4% 时仍落到同一空档。

输入：
{
  "style": "pen",
  "items": [
    {"q": 1, "type": "choice", "text": "D", "y": 0.113},
    {"q": 7, "type": "fraction", "num": "1", "den": "2", "y": 0.765},
    {"q": 9, "type": "work", "x": 0.10, "y": 0.30, "w": 0.82, "lines": ["…", "…"]}
  ],
  "traces": [
    {"q": 1, "type": "circle", "box": [0.57, 0.10, 0.63, 0.13]},
    {"q": 2, "type": "tick",   "box": [0.64, 0.18, 0.67, 0.21]}
  ]
}

y 只用来圈定“哪一行”，不需要精确；x / size 可省略，仅在检测失败时兜底。
默认会自动检测纸张四角并先做透视矫正（--rectify auto）；整张已是扫描件时自动跳过。
用法：python auto_render.py <试卷图> <commands.json> <输出图> [--calib 校准图.png]
      [--rectify auto|off|force] [--scan 扫描件.png]
"""
import json
import math
import re
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import api  # noqa: E402
import align  # noqa: E402
import rectify  # noqa: E402

FONT = api.FONT
FALLBACK = api.FALLBACK
SHIFTS = ((0, -3), (0, 3), (-3, 0), (3, 0), (-6, 0), (6, 0), (0, -6), (0, 6))


def _decoy_choice(ans, rng):
    ans = (ans or '').strip().upper()[:1]
    opts = [c for c in 'ABCD' if c != ans]
    return opts[int(rng.integers(0, len(opts)))] if opts else None


def _decoy_fill(text, rng):
    m = re.search(r'\d', text or '')
    if not m:
        return None
    old = int(m.group())
    new = (old + 2) % 10
    if new == old:
        new = (old + 1) % 10
    return text[:m.start()] + str(new) + text[m.end():]


def _fix_eligible(it):
    typ = it.get('type', 'fill')
    if typ == 'choice':
        return True
    if typ == 'fill' and len(str(it.get('text', ''))) <= 6:
        return True
    return False


STYLES = ('double', 'cross', 'single', 'horizontal', 'zigzag', 'circle')

# 做题痕迹：圈画 / 波浪下划线 / 双下划线 / 对勾 / 涂改
TRACE_KINDS = ('circle', 'wave', 'double', 'tick', 'scribble')


def _pick_style(fix_style, rng):
    if fix_style in ('mix', None, ''):
        return STYLES[int(rng.integers(0, len(STYLES)))]
    return 'zigzag' if fix_style == 'scribble' else fix_style


def _strike(img, box, ink, rng, style='double'):
    """六种手写涂改：双斜杠 / 交叉 / 单斜杠 / 横线 / 锯齿 / 圈划。"""
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    h, span = max(1, y1 - y0), max(1, x1 - x0)
    w = max(3, int(h * 0.09))          # 与字迹同粗
    if style in ('single', 'line'):
        d.line([(x0 + span * 0.12, y1 + 2), (x0 + span * 0.88, y0 - 2)],
               fill=ink + (255,), width=w)
    elif style == 'cross':
        d.line([(x0 + 1, y1 + 1), (x1 - 1, y0 - 1)], fill=ink + (255,), width=w)
        d.line([(x0 + 1, y0 - 1), (x1 - 1, y1 + 1)], fill=ink + (255,), width=w)
    elif style == 'horizontal':
        for k in (0, 1):
            yy = y0 + h * (0.30 + 0.40 * k)
            pts = [(x0 - 2, yy + float(rng.uniform(-2, 2))),
                   ((x0 + x1) / 2, yy + float(rng.uniform(-3, 3))),
                   (x1 + 2, yy + float(rng.uniform(-2, 2)))]
            d.line(pts, fill=ink + (255,), width=w, joint='curve')
    elif style == 'zigzag':
        n = 5
        for k in range(1 if span < 60 else 2):
            pts = []
            for i in range(n):
                xx = x0 - 2 + (span + 4) * i / (n - 1)
                yy = y0 + 2 + (h - 4) * (0.18 if (i + k) % 2 == 0 else 0.82)
                pts.append((xx, yy + float(rng.uniform(-2, 2))))
            d.line(pts, fill=ink + (255,), width=max(3, int(h * 0.075)), joint='curve')
    elif style == 'circle':
        d.ellipse([x0 - 1, y0 - 1, x1 + 1, y1 + 1], outline=ink + (255,), width=w)
        d.line([(x0 + 2, y1), (x1 - 2, y0)], fill=ink + (255,), width=w)
    else:  # double：两条平行斜杠
        for k in (0, 1):
            ox = (k - 0.5) * span * 0.30 + float(rng.uniform(-1.5, 1.5))
            d.line([(x0 + span * 0.15 + ox, y1 + 2),
                    (x0 + span * 0.85 + ox, y0 - 2)],
                   fill=ink + (255,), width=w)


def _trace_points(kind, w, h, rng):
    """返回若干条折线（局部坐标，原点在痕迹框左上角）。"""
    lines = []
    if kind == 'circle':
        cx, cy = w / 2.0, h / 2.0
        rx, ry = w * 0.52, h * 0.60
        n = max(48, int((rx + ry)))
        start, end = -0.22 * math.pi, 1.96 * math.pi
        pts = []
        for i in range(n):
            t = start + (end - start) * i / (n - 1)
            wob = 1.0 + 0.045 * math.sin(3.1 * t + 0.8) + float(rng.uniform(-0.015, 0.015))
            pts.append((cx + rx * wob * math.cos(t), cy + ry * wob * math.sin(t)))
        lines.append(pts)
        lines.append([pts[-1], (pts[-1][0] + rx * 0.16, pts[-1][1] + ry * 0.16)])
    elif kind == 'wave':
        n = max(26, int(w / 7))
        amp = max(2.0, h * 0.45)
        cyc = max(2.2, w / 85.0)
        lines.append([(w * i / (n - 1),
                       h / 2.0 + amp * math.sin(2 * math.pi * cyc * i / (n - 1) + 0.45))
                      for i in range(n)])
    elif kind == 'double':
        lines.append([(0.0, h * 0.28), (w * 0.34, h * 0.22), (w * 0.70, h * 0.27), (w, h * 0.21)])
        lines.append([(1.0, h * 0.80), (w * 0.32, h * 0.84), (w * 0.68, h * 0.79), (w - 1.0, h * 0.83)])
    elif kind == 'tick':
        lines.append([(w * 0.10, h * 0.54), (w * 0.33, h * 0.88), (w * 0.95, h * 0.10)])
    elif kind == 'scribble':
        n = max(12, int(w / 14))
        lines.append([(w * i / (n - 1), h * (0.16 if i % 2 == 0 else 0.84)) for i in range(n)])
        lines.append([(w * 0.04, h * 0.58), (w * 0.34, h * 0.32),
                      (w * 0.66, h * 0.72), (w * 0.96, h * 0.42)])
    return lines


def _draw_trace(im, trace, ink, seed):
    """把一种做题痕迹画到照片坐标里；box 为照片归一化坐标。"""
    kind = str(trace.get('type') or 'circle').lower()
    if kind not in TRACE_KINDS:
        kind = 'circle'
    box = trace.get('box')
    if not box or len(box) < 4:
        return None
    W, H = im.size
    nx0, ny0, nx1, ny1 = [float(v) for v in box[:4]]
    x0, y0 = nx0 * W, ny0 * H
    cw, ch = max(2.0, (nx1 - nx0) * W), max(2.0, (ny1 - ny0) * H)
    pad = int(max(9.0, max(cw, ch) * 0.24))
    lw, lh = int(round(cw)) + 2 * pad, int(round(ch)) + 2 * pad
    if lw < 6 or lh < 6 or lw > W or lh > H:
        return None
    S2 = 2
    layer = Image.new('RGBA', (lw * S2, lh * S2), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    rng = np.random.default_rng(seed)
    width = float(trace.get('w') or max(2.6, min(cw, ch) * 0.085))
    width = max(2.0, min(width, 8.0)) * S2
    for pts in _trace_points(kind, cw, ch, rng):
        for pi in range(2):
            ox = float(rng.uniform(-1.0, 1.0)) if pi else 0.0
            oy = float(rng.uniform(-1.0, 1.0)) if pi else 0.0
            P = [((px + pad + ox) * S2 + float(rng.uniform(-0.8, 0.8)) * S2,
                  (py + pad + oy) * S2 + float(rng.uniform(-0.8, 0.8)) * S2) for px, py in pts]
            d.line(P, fill=ink + (236 if pi == 0 else 110,), width=int(round(width)), joint='curve')
    layer = layer.resize((lw, lh), Image.LANCZOS)
    layer = layer.rotate(float(rng.uniform(-2.2, 2.2)), resample=Image.BICUBIC, expand=False)
    a = np.array(layer.split()[3], dtype=np.float32)
    layer.putalpha(Image.fromarray(np.clip(255.0 * np.power(a / 255.0, 0.72), 0, 255).astype(np.uint8)))
    left, top = int(round(x0)) - pad, int(round(y0)) - pad
    if left < 0 or top < 0 or left + lw > W or top + lh > H:
        sx0, sy0 = max(0, left), max(0, top)
        crop = layer.crop((sx0 - left, sy0 - top,
                           min(lw, W - left), min(lh, H - top)))
        im.alpha_composite(crop, (sx0, sy0))
        layer = crop
    else:
        im.alpha_composite(layer, (left, top))
    bb = layer.getbbox()
    return {
        'q': trace.get('q'), 'kind': kind, 'box': [x0, y0, x0 + cw, y0 + ch],
        'ink': [int(left + bb[0]), int(top + bb[1]),
                int(left + bb[2]), int(top + bb[3])] if bb else None,
    }


def _near_offsets(max_off=0.032, step=0.006):
    offs = [0.0]
    k = step
    while k <= max_off + 1e-9:
        offs.extend([-k, k])
        k += step
    return offs


def _detect_choice_near(gray, y, max_off=0.032, step=0.006):
    """提示行没检出括号时，在提示行附近逐档搜索，取离提示行最近的一对。"""
    best = None
    for off in _near_offsets(max_off, step):
        yy = y + off
        if yy <= 0.01 or yy >= 0.99:
            continue
        b = align.detect_choice(gray, yy)
        if not b:
            continue
        d = abs(b['cy'] - y)
        if d > max_off + 0.004:
            continue
        if best is None or d < best[0]:
            best = (d, b)
    return best[1] if best else None


def _detect_underline_near(gray, y, anchor_x=None, max_off=0.028, step=0.006):
    """提示行没检出横线时，在附近逐档搜索，取离提示行最近的一条。"""
    best = None
    for off in _near_offsets(max_off, step):
        yy = y + off
        if yy <= 0.01 or yy >= 0.995:
            continue
        b = align.detect_underline(gray, yy, anchor_x=anchor_x)
        if not b:
            continue
        d = abs(b['cy'] - y)
        if d > max_off + 0.004:
            continue
        if best is None or d < best[0]:
            best = (d, b)
    return best[1] if best else None


def _ink_frac(gray, box):
    """区域内的印刷墨迹占比，用来判断目标位置是否压到题干。"""
    H, W = gray.shape
    y0, y1, x0, x1 = [int(round(v)) for v in box]
    y0, y1 = max(0, y0), min(H, y1)
    x0, x1 = max(0, x0), min(W, x1)
    if x1 <= x0 or y1 <= y0:
        return 1.0
    return float((gray[y0:y1, x0:x1] < 115).mean())


_BREAK_AFTER = set('=+-×÷±,，、；:： ')
_UNARY_PREV = set('([{=+-×÷±,，、；:： ')
# 字体缺字的安全替换（借鉴 inko 的符号替换表）：只换会变成豆腐块的字符
_SAFE_SUBS = {'²': '^2', '³': '^3', '·': '*', '−': '-', '–': '-', '—': '-'}


def _clean_text(s):
    if not s:
        return s
    return ''.join(_SAFE_SUBS.get(ch, ch) for ch in str(s))


def _wrap_math_line(line, size, fp, max_w, profile=None):
    """超宽行优先在 = + - × , 等位置折行，再按字符兜底；避免整段被缩小。

    借鉴 inko：长公式在等号/加减号处续行，续行仍与首行同字号。
    """
    if not line or max_w <= 20:
        return [line]
    font = ImageFont.truetype(fp, max(8, int(size)))

    def w(s):
        return font.getlength(s)

    if w(line) <= max_w:
        return [line]
    out, rest = [], line
    while rest:
        if w(rest) <= max_w:
            out.append(rest)
            break
        # 优先在等号/逗号处断（语义完整），退而求其次在加减乘除处断
        best = -1
        for strong in (True, False):
            for i, ch in enumerate(rest[:-1]):
                if ch not in _BREAK_AFTER:
                    continue
                if strong != (ch in '=，、；:：'):
                    continue
                if ch in '+-' and (i == 0 or rest[i - 1] in _UNARY_PREV):
                    continue                      # 一元正负号不折行
                cand = rest[:i + 1].rstrip()
                cw = w(cand)
                if len(cand) >= 2 and cw <= max_w and cw >= max_w * 0.35:
                    best = i + 1
            if best > 0:
                break
        if best <= 0:
            lo, hi = 1, len(rest)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if w(rest[:mid]) <= max_w:
                    lo = mid
                else:
                    hi = mid - 1
            best = max(1, lo)
        out.append(rest[:best].rstrip())
        rest = rest[best:].lstrip()
    return out or [line]


def place(canvas_size, gray, it, style, shift=(0, 0), fix_style='scribble', profile=None):
    """检测 + 落笔到透明层。返回 (透明层, 报告)。"""
    W, H = canvas_size
    prof = dict(profile or {})
    prof.update(it.get('profile') or {})
    it = dict(it)
    if it.get('text'):
        it['text'] = _clean_text(it['text'])
    if it.get('lines'):
        it['lines'] = [_clean_text(x) for x in it['lines']]
    if it.get('num') is not None:
        it['num'] = _clean_text(it['num'])
    if it.get('den') is not None:
        it['den'] = _clean_text(it['den'])
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    y = float(it['y'])
    typ = it.get('type', 'fill')
    pencil = style == 'pencil'
    ink = api.PENCIL if pencil else api.INK
    if prof.get('ink') and not pencil:
        ink = tuple(prof['ink'])
    used_size = None
    seed = int(it.get('seed') or (abs(int(it.get('q') or 0)) * 131 + 17))
    dx, dy = shift
    line_y = None
    detected = False
    drift = [0.0, 0.0]
    fix_rng = np.random.default_rng(seed + 777)
    fix_info = None

    if typ == 'choice':
        lock = it.get('lock')
        if lock:
            # 用户在第一轮确认过的框：直接按框写，不再重新检测
            x0, x1 = float(lock[0]) * W + dx, float(lock[2]) * W + dx
            cy = (float(lock[1]) + float(lock[3])) / 2 * H + dy
            ph = max(12.0, (float(lock[3]) - float(lock[1])) * H)
            detected = True
        else:
            b = align.detect_choice(gray, y)
            if not b:
                b = _detect_choice_near(gray, y)
            if b:
                detected = True
                drift = [0.0, round(b['cy'] - y, 4)]
                x0, x1 = b['x0'] * W + dx, b['x1'] * W + dx
                cy, ph = b['cy'] * H + dy, max(12.0, b['h'] * H)
            else:
                cx = float(it.get('x', 0.70)) * W
                x0, x1 = cx - 35, cx + 35
                cy, ph = y * H, 26
        # 只受横向限制：字号回到 78，纵向允许超出括号
        err, thk = (0, 0) if pencil else (1, 0)
        size = float(it.get('size', 78))
        avail = (x1 - x0) * 0.98
        decoy = None
        if it.get('_fix') and (x1 - x0) >= 62:
            decoy = _decoy_choice(it['text'], fix_rng)
        if decoy:
            size = min(size, avail / 1.45)
            lw = api.text_layer(decoy, size * 0.92, FONT, err, ink, grain=pencil,
                                thicken=thk, jitter=True, seed=seed + 31, profile=prof)
            rw = api.text_layer(it['text'], size, FONT, err, ink, grain=pencil,
                                thicken=thk, jitter=True, seed=seed, profile=prof)
            gap = max(2, size * 0.10)
            total = lw.width + gap + rw.width
            if total > avail:
                k = avail / total
                size *= k
                lw = api.text_layer(decoy, size * 0.92, FONT, err, ink, grain=pencil,
                                    thicken=thk, jitter=True, seed=seed + 31, profile=prof)
                rw = api.text_layer(it['text'], size, FONT, err, ink, grain=pencil,
                                    thicken=thk, jitter=True, seed=seed, profile=prof)
                gap = max(2, size * 0.10)
                total = lw.width + gap + rw.width
            left = x0 + ((x1 - x0) - total) / 2
            api.paste_center(img, lw, left + lw.width / 2, cy)
            st = it.get('_style') or _pick_style(fix_style, fix_rng)
            _strike(img, (left, cy - lw.height / 2, left + lw.width, cy + lw.height / 2),
                    ink, fix_rng, st)
            api.paste_center(img, rw, left + lw.width + gap + rw.width / 2, cy)
            fix_info = {'applied': True, 'from': decoy, 'to': it['text'], 'style': st}
        else:
            layer = api.text_layer(it['text'], size, FONT, err, ink, grain=pencil,
                                   thicken=thk, jitter=True, seed=seed, profile=prof)
            if layer.width > avail:
                size *= avail / layer.width
                layer = api.text_layer(it['text'], size, FONT, err, ink, grain=pencil,
                                       thicken=thk, jitter=True, seed=seed, profile=prof)
            api.paste_center(img, layer, (x0 + x1) / 2, cy)
        used_size = size
        blank = (x0, cy - ph / 2, x1, cy + ph / 2)

    elif typ == 'fraction':
        lock = it.get('lock')
        if lock:
            x0, x1 = float(lock[0]) * W + dx, float(lock[2]) * W + dx
            line_y = float(lock[3]) * H + dy
            detected = True
        else:
            ax = float(it['x']) if it.get('x') is not None else None
            b = align.detect_underline(gray, y, anchor_x=ax)
            if not b:
                b = _detect_underline_near(gray, y, anchor_x=ax)
            if b:
                detected = True
                drift = [round((b['x0'] + b['x1']) / 2 - (ax if ax is not None else (b['x0'] + b['x1']) / 2), 4),
                         round(b['cy'] - y, 4)]
                x0, x1 = b['x0'] * W + dx, b['x1'] * W + dx
                line_y = b['cy'] * H + dy
            else:
                cx = float(it.get('x', 0.6)) * W
                x0, x1, line_y = cx - 60, cx + 60, y * H
        avail_w = max(20, (x1 - x0) * 0.96)
        size = float(it.get('size', 50))
        if size * 1.5 > avail_w:
            size *= avail_w / (size * 1.5)
        th = size * 0.46 * 2 + size * 0.95 + 3
        cy = line_y - 2 - th / 2
        r = ((x0 + x1) / 2 - avail_w / 2, cy - th / 2, avail_w, th)
        api.draw_fraction(img, it['num'], it['den'], r, size, style, fit=False, thicken=0,
                          profile=prof)
        used_size = size
        blank = (x0, line_y - th - 4, x1, line_y + 2)

    elif typ == 'work':
        x0 = float(it.get('x', 0.12)) * W
        y0 = float(it.get('y', 0.30)) * H
        width = float(it.get('w', 0.82)) * W
        size = float(it.get('size', 38))
        lines = it.get('lines') or [it.get('text', '')]
        if isinstance(lines, str):
            lines = lines.split('\n')
        # 超宽的行优先在 = + - × , 处折行（inko 的做法），避免整段被压到很小
        wrapped = []
        for ln in lines:
            fp = FALLBACK if any(ch in ln for ch in 'αβγθλμπσφω≥≤≠≈∫∑√∞→±×÷') else FONT
            parts = _wrap_math_line(ln, size, fp, width * 0.94, prof)
            for j, part in enumerate(parts):
                pfp = FALLBACK if any(ch in part for ch in 'αβγθλμπσφω≥≤≠≈∫∑√∞→±×÷') else FONT
                wrapped.append((part, pfp, j > 0))
        widths = [api.text_layer(ln, size, fp, 0, profile=prof).width
                  for ln, fp, _ in wrapped]
        k = min(1.0, (width * 0.98) / max(max(widths), 1))
        size *= k
        lh = size * 1.85
        cur = y0
        edge_rng = np.random.default_rng(seed + 991)
        for i, (ln, fp, cont) in enumerate(wrapped):
            layer = api.text_layer(ln, size, fp, 0, ink, grain=pencil, thicken=0,
                                   jitter=True, seed=seed + i, profile=prof)
            # 行首留一点自然漂移，避免左边一条死直线；续行缩进，保持公式从属关系
            lx = x0 + (size * 1.35 if cont else 0) + float(edge_rng.uniform(-1.5, 1.5))
            lx = min(max(x0, lx), max(x0, x0 + width - layer.width))
            img.paste(layer, (int(lx), int(cur)), layer)
            cur += lh
        used_size = size
        blank = (x0, y0 - size, x0 + width, cur)

    else:  # fill
        lock = it.get('lock')
        if lock:
            x0, x1 = float(lock[0]) * W + dx, float(lock[2]) * W + dx
            line_y = float(lock[3]) * H + dy
            detected = True
        else:
            ax = float(it['x']) if it.get('x') is not None else None
            b = align.detect_underline(gray, y, anchor_x=ax)
            if not b:
                b = _detect_underline_near(gray, y, anchor_x=ax)
            if b:
                detected = True
                drift = [round((b['x0'] + b['x1']) / 2 - (ax if ax is not None else (b['x0'] + b['x1']) / 2), 4),
                         round(b['cy'] - y, 4)]
                x0, x1 = b['x0'] * W + dx, b['x1'] * W + dx
                line_y = b['cy'] * H + dy
            else:
                cx = float(it.get('x', 0.6)) * W
                x0, x1, line_y = cx - 60, cx + 60, y * H
        avail_w = max(20, x1 - x0)
        text = it['text']
        fp = FALLBACK if any(ch in text for ch in 'αβγθλμπσφω≥≤≠≈') else FONT
        size = float(it.get('size', 50))
        err, thk = (0, 0) if pencil else (0, 1)
        layer = api.text_layer(text, size, fp, err, ink, grain=pencil,
                               thicken=thk, jitter=True, seed=seed, profile=prof)
        long_text = layer.width > avail_w * 1.05
        if long_text and not lock:
            allow = min(W * 0.55, max(avail_w * 4.2, W * 0.50))
            # 允许沿同一基线向右延伸，但不越过纸张右边界
            allow = max(20.0, min(allow, W * 0.96 - x0 - 2))
            if layer.width > allow * 0.97:
                size *= (allow * 0.97) / layer.width
                layer = api.text_layer(text, size, fp, err, ink, grain=pencil,
                                       thicken=thk, jitter=True, seed=seed, profile=prof)
            # 先看右侧延伸区是否压到题干；压到就改到同题右侧空白
            zone = (line_y - layer.height - 2, line_y - 3, x0 + 1, x0 + 1 + layer.width)
            if _ink_frac(gray, zone) > 0.030 and x0 < W * 0.62:
                msize = min(size, 26.0)
                mlayer = api.text_layer(text, msize, fp, err, ink, grain=pencil,
                                        thicken=thk, jitter=True, seed=seed, profile=prof)
                mx = W * 0.78
                mzone = (line_y - mlayer.height - 2, line_y - 3, mx, mx + mlayer.width)
                if mx + mlayer.width <= W * 0.965 and _ink_frac(gray, mzone) < 0.020:
                    img.paste(mlayer, (int(mx), int(line_y - 2 - mlayer.height)), mlayer)
                    blank = (mx, line_y - mlayer.height - 4, mx + mlayer.width, line_y + 2)
                    size = msize
                else:
                    # 右侧也占用了：缩进空档，绝不压字
                    size *= (avail_w * 0.96) / layer.width
                    layer = api.text_layer(text, size, fp, err, ink, grain=pencil,
                                           thicken=thk, jitter=True, seed=seed, profile=prof)
                    api.paste_center(img, layer, (x0 + x1) / 2, line_y - 2 - layer.height / 2)
                    blank = (x0, line_y - layer.height - 4, x1, line_y + 2)
                    used_size = size
            else:
                left = x0 + 1
                img.paste(layer, (int(left), int(line_y - 2 - layer.height)), layer)
                blank = (x0, line_y - layer.height - 4, min(W * 0.96, x0 + allow), line_y + 2)
        else:
            if layer.width > avail_w * 0.96:
                size *= (avail_w * 0.96) / layer.width
                layer = api.text_layer(text, size, fp, err, ink, grain=pencil,
                                       thicken=thk, jitter=True, seed=seed, profile=prof)
            decoy = None
            if it.get('_fix') and avail_w >= 90:
                decoy = _decoy_fill(text, fix_rng)
            if decoy:
                gap = max(3, size * 0.12)
                lw = api.text_layer(decoy, size, fp, err, ink, grain=pencil,
                                    thicken=thk, jitter=True, seed=seed + 41, profile=prof)
                rw = layer
                total = lw.width + gap + rw.width
                limit = avail_w * 0.80
                if total > limit:
                    k = limit / total
                    if size * k < 26:
                        decoy = None
                    else:
                        size *= k
                        lw = api.text_layer(decoy, size, fp, err, ink, grain=pencil,
                                            thicken=thk, jitter=True, seed=seed + 41, profile=prof)
                        rw = api.text_layer(text, size, fp, err, ink, grain=pencil,
                                            thicken=thk, jitter=True, seed=seed, profile=prof)
                        gap = max(3, size * 0.12)
                if decoy:
                    total = lw.width + gap + rw.width
                    # 居中放在横线中段，避开左侧印刷题干
                    center = (x0 + x1) / 2
                    left = min(max(x0 + 1, center - total / 2), max(x0 + 1, x1 - total - 1))
                    bottom = line_y - 2
                    img.paste(lw, (int(left), int(bottom - lw.height)), lw)
                    st = it.get('_style') or _pick_style(fix_style, fix_rng)
                    _strike(img, (left, bottom - lw.height, left + lw.width, bottom),
                            ink, fix_rng, st)
                    img.paste(rw, (int(left + lw.width + gap), int(bottom - rw.height)), rw)
                    fix_info = {'applied': True, 'from': decoy, 'to': text, 'style': st}
            if not fix_info:
                api.paste_center(img, layer, (x0 + x1) / 2, line_y - 2 - layer.height / 2)
            blank = (x0, line_y - layer.height - 4, x1, line_y + 2)
        used_size = size

    bb = img.getbbox()
    ink_box = bb if bb else (0, 0, 0, 0)
    ok = bool(bb) and ink_box[0] >= blank[0] - 2 and ink_box[2] <= blank[2] + 2
    if typ == 'fill' and line_y is not None:
        ok = ok and abs(ink_box[3] - (line_y - 2)) <= 4
    return img, {
        'q': it.get('q'), 'type': typ,
        'text': it.get('text') or (f"{it.get('num')}/{it.get('den')}" if it.get('num') is not None else '过程'),
        'size': 0.0 if used_size is None else round(float(used_size), 1),
        'ink': [int(v) for v in ink_box],
        'blank': [int(v) for v in blank], 'ok': bool(ok),
        'detected': bool(detected), 'drift': [float(v) for v in drift],
        'conf': 0.9 if detected else 0.3,
        'review': bool((not detected) or abs(float(drift[1])) > 0.02),
        'fix': fix_info or {'applied': False},
    }


def draw_calib(img, report):
    d = ImageDraw.Draw(img)
    for r in report:
        x0, y0, x1, y1 = r['blank']
        d.rectangle([x0, y0, x1, y1], outline=(30, 110, 220), width=2)
        ix0, iy0, ix1, iy1 = r['ink']
        d.rectangle([ix0, iy0, ix1, iy1], outline=(220, 40, 40), width=1)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        d.line([cx - 8, cy, cx + 8, cy], fill=(220, 40, 40), width=2)
        d.line([cx, cy - 8, cx, cy + 8], fill=(220, 40, 40), width=2)
        hx, hy = r.get('hint_px', (None, None))
        if hx is not None:
            d.line([hx - 6, hy, hx + 6, hy], fill=(230, 170, 0), width=2)
            d.line([hx, hy - 6, hx, hy + 6], fill=(230, 170, 0), width=2)
            if r.get('review'):
                d.rectangle([x0 - 4, y0 - 4, x1 + 4, y1 + 5], outline=(240, 140, 0), width=2)


def _map_item_to_scan(it, Hf, W, H, SW, SH):
    """把 commands 里的照片坐标提示投到拉平后的扫描件坐标。"""
    it2 = dict(it)
    x = float(it.get('x', 0.5))
    y = float(it.get('y'))
    pt = rectify.transform_points([(x * W, y * H)], Hf)[0]
    it2['y'] = float(np.clip(pt[1] / SH, 0.005, 0.995))
    if it.get('x') is not None:
        it2['x'] = float(np.clip(pt[0] / SW, 0.01, 0.99))
    lk = it.get('lock')
    if lk:
        # 锁定框也要投到扫描件平面，否则透视矫正后会漂
        p0, p1 = rectify.transform_points(
            [(float(lk[0]) * W, float(lk[1]) * H),
             (float(lk[2]) * W, float(lk[3]) * H)], Hf)
        it2['lock'] = [float(np.clip(p0[0] / SW, 0.0, 1.0)),
                       float(np.clip(p0[1] / SH, 0.0, 1.0)),
                       float(np.clip(p1[0] / SW, 0.0, 1.0)),
                       float(np.clip(p1[1] / SH, 0.0, 1.0))]
    return it2


def main():
    args = sys.argv[1:]
    style, style_set = 'pen', False
    if '--style' in args:
        i = args.index('--style')
        style = args[i + 1] if i + 1 < len(args) else 'pen'
        style_set = True
        del args[i:i + 2]
    calib = None
    if '--calib' in args:
        i = args.index('--calib')
        calib = args[i + 1] if i + 1 < len(args) else 'calib.png'
        del args[i:i + 2]
    rect_mode = 'auto'
    if '--rectify' in args:
        i = args.index('--rectify')
        rect_mode = args[i + 1] if i + 1 < len(args) else 'auto'
        del args[i:i + 2]
    scan_out = None
    if '--scan' in args:
        i = args.index('--scan')
        scan_out = args[i + 1] if i + 1 < len(args) else 'scan.png'
        del args[i:i + 2]
    no_align = '--no-align' in args
    if no_align:
        args.remove('--no-align')
    if len(args) < 3:
        print('用法: python auto_render.py <试卷图> <commands.json> <输出图> '
              '[--style pencil] [--calib 校准图.png] [--rectify auto|off|force] '
              '[--scan 扫描件.png] [--no-align]')
        return
    cmd = json.loads(Path(args[1]).read_text(encoding='utf-8-sig'))
    if not style_set:
        style = cmd.get('style', style)
    items = cmd['items']
    profile = dict(cmd.get('profile') or {})
    fix_rate = float(cmd.get('fix_rate', 0))
    fix_style = cmd.get('fix_style', 'scribble')
    # 按比例精确抽题：25% 就是 11 题里固定 3 题，避免随机成 1 题看不出效果
    k = int(round(fix_rate * len(items)))
    elig = [i for i, it in enumerate(items) if _fix_eligible(it)]
    pick = set()
    if fix_rate > 0 and elig:
        order = np.random.default_rng(20240601).permutation(len(elig))
        pick = {elig[j] for j in order[:min(k, len(elig))]}
    style_map = {}
    if pick:
        sp = np.random.default_rng(20240602).permutation(len(STYLES))
        style_map = {idx: STYLES[int(sp[j % len(STYLES)])] for j, idx in enumerate(sorted(pick))}
    # 命令里写了 "fix": true 的题强制涂改（给示例/人工指定用）
    for i, it in enumerate(items):
        if it.get('fix') and _fix_eligible(it):
            pick.add(i)
    im = Image.open(args[0]).convert('RGBA')
    W, H = im.size
    bgr = cv2.cvtColor(np.array(im.convert('RGB')), cv2.COLOR_RGB2BGR)

    # 透视矫正：先把纸拉平，在扫描件坐标里找线落笔，最后把手写层投回照片
    quad, qratio = (None, 0.0)
    if rect_mode != 'off':
        quad, qratio = rectify.detect_page_corners(bgr)
    warped = rectify.warp_to_scan(bgr, quad) if quad is not None else None
    if rect_mode == 'force' and quad is None:
        print('警告：未检测到纸张四角，回退为直接落笔')
    use_rect = warped is not None
    if use_rect:
        scan, Hf = warped
        Hi = np.linalg.inv(Hf)
        SH, SW = scan.shape[:2]
        gray = cv2.cvtColor(scan, cv2.COLOR_BGR2GRAY)
        canvas_size = (SW, SH)
        items_work = [_map_item_to_scan(it, Hf, W, H, SW, SH) for it in items]
        ink_img = Image.new('RGBA', (SW, SH), (0, 0, 0, 0))
        if scan_out:
            cv2.imwrite(scan_out, scan)
    else:
        gray = cv2.cvtColor(np.array(im.convert('RGB')), cv2.COLOR_RGB2GRAY)
        canvas_size = (W, H)
        items_work = items
        ink_img = None

    # 校验层：估计整页漂移；小于 1.5px 视为 0，保证与人工标定逐像素一致
    if no_align:
        # 位置已由用户在框选图上确认，禁止再做整页漂移修正
        off_y, det, verified = 0.0, [], True
    else:
        off_y, det, verified = align.estimate_offset(gray, items_work)
        if abs(off_y) < 0.0015:
            off_y = 0.0

    report = []
    for idx, it in enumerate(items_work):
        it0 = items[idx]
        y_hint = float(it['y'])
        it2 = dict(it)
        it2['y'] = min(0.995, max(0.005, y_hint + off_y))
        it2['_fix'] = idx in pick
        it2['_style'] = style_map.get(idx)
        limg, rep = place(canvas_size, gray, it2, style, fix_style=fix_style, profile=profile)
        rep['hint_px'] = [round(float(it0.get('x', 0.5)) * W, 1), round(float(it0['y']) * H, 1)]
        rep['off_y'] = round(off_y, 4)
        if not verified:
            rep['review'] = True
        if not rep['ok']:
            for sdx, sdy in SHIFTS:
                l2, r2 = place(canvas_size, gray, it2, style, (sdx, sdy),
                               fix_style=fix_style, profile=profile)
                if r2['ok']:
                    limg, rep = l2, r2
                    rep['hint_px'] = [round(float(it0.get('x', 0.5)) * W, 1),
                                      round(float(it0['y']) * H, 1)]
                    rep['off_y'] = round(off_y, 4)
                    rep['retry'] = [sdx, sdy]
                    if not verified:
                        rep['review'] = True
                    break
        if limg.getbbox():
            if ink_img is not None:
                ink_img.alpha_composite(limg)
            else:
                im.paste(limg, (0, 0), limg)
        report.append(rep)

    if use_rect:
        back = rectify.warp_layer_back(ink_img, Hi, (W, H))
        im.alpha_composite(Image.fromarray(back, 'RGBA'))
        for rep in report:
            rep['ink'] = rectify.box_back(rep['ink'], Hi, W, H)
            rep['blank'] = rectify.box_back(rep['blank'], Hi, W, H)

    # 做题痕迹：直接用照片坐标落笔，不再做任何换算，保证位置不漂
    traces = cmd.get('traces') or []
    trace_report = []
    if traces:
        tink = api.PENCIL if style == 'pencil' else tuple(profile.get('ink') or api.INK)
        for i, tr in enumerate(traces):
            if not isinstance(tr, dict):
                continue
            seed = abs(hash('%s|%s|%s' % (tr.get('q'), tr.get('type'), i))) % (2 ** 32)
            rep = _draw_trace(im, tr, tink, seed)
            if rep:
                trace_report.append(rep)

    out = Path(args[2])
    im.convert('RGB').save(out)
    if calib:
        draw_calib(im, report)
        if trace_report:
            dc = ImageDraw.Draw(im)
            for r in trace_report:
                x0, y0, x1, y1 = r['box']
                dc.rectangle([x0, y0, x1, y1], outline=(150, 80, 200), width=2)
                dc.text((x0 + 2, max(0, y0 - 14)), str(r.get('kind')), fill=(150, 80, 200))
        im.convert('RGB').save(calib)
    summary = {'out': str(out), 'style': style, 'off_y': round(off_y, 4),
               'rectified': bool(use_rect), 'page_ratio': round(qratio, 3),
               'offset_detail': det, 'items': report, 'traces': trace_report}
    if use_rect:
        summary['scan_size'] = [SW, SH]
        summary['corners'] = [[round(float(x), 1), round(float(y), 1)] for x, y in quad]
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
