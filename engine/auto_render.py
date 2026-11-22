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
  ]
}

y 只用来圈定“哪一行”，不需要精确；x / size 可省略，仅在检测失败时兜底。
用法：python auto_render.py <试卷图> <commands.json> <输出图> [--calib 校准图.png]
"""
import json
import re
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, r'D:\手写生成工具')
import api  # noqa: E402
import align  # noqa: E402

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


def place(canvas_size, gray, it, style, shift=(0, 0), fix_style='scribble'):
    """检测 + 落笔到透明层。返回 (透明层, 报告)。"""
    W, H = canvas_size
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    y = float(it['y'])
    typ = it.get('type', 'fill')
    pencil = style == 'pencil'
    ink = api.PENCIL if pencil else api.INK
    used_size = None
    seed = int(it.get('seed') or (abs(int(it.get('q') or 0)) * 131 + 17))
    dx, dy = shift
    line_y = None
    detected = False
    drift = [0.0, 0.0]
    fix_rng = np.random.default_rng(seed + 777)
    fix_info = None

    if typ == 'choice':
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
                                thicken=thk, jitter=True, seed=seed + 31)
            rw = api.text_layer(it['text'], size, FONT, err, ink, grain=pencil,
                                thicken=thk, jitter=True, seed=seed)
            gap = max(2, size * 0.10)
            total = lw.width + gap + rw.width
            if total > avail:
                k = avail / total
                size *= k
                lw = api.text_layer(decoy, size * 0.92, FONT, err, ink, grain=pencil,
                                    thicken=thk, jitter=True, seed=seed + 31)
                rw = api.text_layer(it['text'], size, FONT, err, ink, grain=pencil,
                                    thicken=thk, jitter=True, seed=seed)
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
                                   thicken=thk, jitter=True, seed=seed)
            if layer.width > avail:
                size *= avail / layer.width
                layer = api.text_layer(it['text'], size, FONT, err, ink, grain=pencil,
                                       thicken=thk, jitter=True, seed=seed)
            api.paste_center(img, layer, (x0 + x1) / 2, cy)
        used_size = size
        blank = (x0, cy - ph / 2, x1, cy + ph / 2)

    elif typ == 'fraction':
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
        api.draw_fraction(img, it['num'], it['den'], r, size, style, fit=False, thicken=0)
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
        fonts = [FALLBACK if any(ch in ln for ch in 'αβγθλμπσφω≥≤≠≈∫∑√∞→±×÷') else FONT
                 for ln in lines]
        widths = [api.text_layer(ln, size, fp, 0).width for ln, fp in zip(lines, fonts)]
        k = min(1.0, (width * 0.98) / max(max(widths), 1))
        size *= k
        lh = size * 1.85
        cur = y0
        for i, (ln, fp) in enumerate(zip(lines, fonts)):
            layer = api.text_layer(ln, size, fp, 0, ink, grain=pencil, thicken=0,
                                   jitter=True, seed=seed + i)
            img.paste(layer, (int(x0), int(cur)), layer)
            cur += lh
        used_size = size
        blank = (x0, y0 - size, x0 + width, cur)

    else:  # fill
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
                               thicken=thk, jitter=True, seed=seed)
        long_text = layer.width > avail_w * 1.05
        if long_text:
            allow = min(W * 0.55, max(avail_w * 4.2, W * 0.50))
            # 允许沿同一基线向右延伸，但不越过纸张右边界
            allow = max(20.0, min(allow, W * 0.96 - x0 - 2))
            if layer.width > allow * 0.97:
                size *= (allow * 0.97) / layer.width
                layer = api.text_layer(text, size, fp, err, ink, grain=pencil,
                                       thicken=thk, jitter=True, seed=seed)
            left = x0 + 1
            img.paste(layer, (int(left), int(line_y - 2 - layer.height)), layer)
            blank = (x0, line_y - layer.height - 4, min(W * 0.96, x0 + allow), line_y + 2)
        else:
            if layer.width > avail_w * 0.96:
                size *= (avail_w * 0.96) / layer.width
                layer = api.text_layer(text, size, fp, err, ink, grain=pencil,
                                       thicken=thk, jitter=True, seed=seed)
            decoy = None
            if it.get('_fix') and avail_w >= 90:
                decoy = _decoy_fill(text, fix_rng)
            if decoy:
                gap = max(3, size * 0.12)
                lw = api.text_layer(decoy, size, fp, err, ink, grain=pencil,
                                    thicken=thk, jitter=True, seed=seed + 41)
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
                                            thicken=thk, jitter=True, seed=seed + 41)
                        rw = api.text_layer(text, size, fp, err, ink, grain=pencil,
                                            thicken=thk, jitter=True, seed=seed)
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
        'size': round(used_size, 1), 'ink': [int(v) for v in ink_box],
        'blank': [int(v) for v in blank], 'ok': bool(ok),
        'detected': detected, 'drift': drift,
        'conf': 0.9 if detected else 0.3,
        'review': (not detected) or abs(drift[1]) > 0.02,
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
    if len(args) < 3:
        print('用法: python auto_render.py <试卷图> <commands.json> <输出图> [--style pencil] [--calib 校准图.png]')
        return
    cmd = json.loads(Path(args[1]).read_text(encoding='utf-8-sig'))
    if not style_set:
        style = cmd.get('style', style)
    items = cmd['items']
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
    im = Image.open(args[0]).convert('RGBA')
    W, H = im.size
    gray = cv2.cvtColor(np.array(im.convert('RGB')), cv2.COLOR_RGB2GRAY)

    # 校验层：估计整页漂移；小于 1.5px 视为 0，保证与人工标定逐像素一致
    off_y, det, verified = align.estimate_offset(gray, items)
    if abs(off_y) < 0.0015:
        off_y = 0.0

    report = []
    for idx, it in enumerate(items):
        y_hint = float(it['y'])
        it2 = dict(it)
        it2['y'] = min(0.995, max(0.005, y_hint + off_y))
        it2['_fix'] = idx in pick
        it2['_style'] = style_map.get(idx)
        limg, rep = place((W, H), gray, it2, style, fix_style=fix_style)
        rep['hint_px'] = [round(float(it.get('x', 0.5)) * W, 1), round(y_hint * H, 1)]
        rep['off_y'] = round(off_y, 4)
        if not verified:
            rep['review'] = True
        if not rep['ok']:
            for sdx, sdy in SHIFTS:
                l2, r2 = place((W, H), gray, it2, style, (sdx, sdy), fix_style=fix_style)
                if r2['ok']:
                    limg, rep = l2, r2
                    rep['hint_px'] = [round(float(it.get('x', 0.5)) * W, 1), round(y_hint * H, 1)]
                    rep['off_y'] = round(off_y, 4)
                    rep['retry'] = [sdx, sdy]
                    if not verified:
                        rep['review'] = True
                    break
        if limg.getbbox():
            im.paste(limg, (0, 0), limg)
        report.append(rep)

    out = Path(args[2])
    im.convert('RGB').save(out)
    if calib:
        draw_calib(im, report)
        im.convert('RGB').save(calib)
    print(json.dumps({'out': str(out), 'style': style, 'off_y': round(off_y, 4),
                      'offset_detail': det, 'items': report}, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
