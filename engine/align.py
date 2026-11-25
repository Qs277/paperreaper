# -*- coding: utf-8 -*-
"""坐标校验模块：估计并修正 AI 坐标的整页系统性漂移。

设计要点
1. AI 的 x/y 只当“搜索先验”，落点仍由原检测器在图像里找；
2. 用选择题的高置信检测结果估计整页 dy（中位数，抗单题误检）；
3. 把 dy 回代给每一题，再交给原检测器；
4. 修正量限幅 ±0.04，避免把“指错题”硬拉成另一行。

这样在偏移为 0 时，输出与人工逐题标定逐像素一致；
偏移 2%~4% 时，回代后仍走同一检测路径，输出保持一致。
"""
import cv2
import numpy as np


def _choice_pairs(gray, y_norm, half, min_x):
    """收集行带内所有“答题括号对”，返回 (x0,x1,cy,h) 归一化结果。"""
    h, w = gray.shape
    y0 = max(0, int((y_norm - half) * h))
    y1 = min(h, int((y_norm + half) * h))
    if y1 - y0 < 8:
        return []
    crop = gray[y0:y1, :]
    _, bw = cv2.threshold(crop, 115, 255, cv2.THRESH_BINARY_INV)
    cs, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = [cv2.boundingRect(c) for c in cs]
    cands = [(x, yy, ww, hh) for x, yy, ww, hh in boxes
             if 12 <= hh <= 65 and 2 <= ww <= 22 and hh / max(ww, 1) >= 1.3 and x > min_x * w]
    out = []
    for i in range(len(cands)):
        for j in range(i + 1, len(cands)):
            a, b = sorted((cands[i], cands[j]), key=lambda t: t[0])
            ya, yb = a[1] + a[3] / 2, b[1] + b[3] / 2
            if abs(ya - yb) > 16:
                continue
            x0, x1 = a[0] + a[2], b[0]
            gap = x1 - x0
            if not (45 <= gap <= 100):
                continue
            inner = bw[int(max(ya, yb) - 9):int(min(ya, yb) + 9), int(x0):int(x1)]
            density = float(inner.mean()) / 255 if inner.size else 1.0
            if density > 0.10:          # 括号内部应是空白，排除 f(0) 这类函数括号
                continue
            out.append({'x0': x0 / w, 'x1': x1 / w,
                        'cy': ((ya + yb) / 2 + y0) / h,
                        'h': (a[3] + b[3]) / 2 / h})
    return out


def detect_choice(gray, y_norm, half=0.018, min_x=0.42):
    """找该行最像“答题括号”的一对：细长、间距够、内部空白、取最右。"""
    h, w = gray.shape
    y0 = max(0, int((y_norm - half) * h))
    y1 = min(h, int((y_norm + half) * h))
    if y1 - y0 < 8:
        return None
    crop = gray[y0:y1, :]
    _, bw = cv2.threshold(crop, 115, 255, cv2.THRESH_BINARY_INV)
    cs, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = [cv2.boundingRect(c) for c in cs]
    cands = [(x, yy, ww, hh) for x, yy, ww, hh in boxes
             if 12 <= hh <= 65 and 2 <= ww <= 22 and hh / max(ww, 1) >= 1.3 and x > min_x * w]
    best = None
    for i in range(len(cands)):
        for j in range(i + 1, len(cands)):
            a, b = sorted((cands[i], cands[j]), key=lambda t: t[0])
            ya, yb = a[1] + a[3] / 2, b[1] + b[3] / 2
            if abs(ya - yb) > 16:
                continue
            x0, x1 = a[0] + a[2], b[0]
            gap = x1 - x0
            if not (45 <= gap <= 100):
                continue
            inner = bw[int(max(ya, yb) - 9):int(min(ya, yb) + 9), int(x0):int(x1)]
            density = float(inner.mean()) / 255 if inner.size else 1.0
            if density > 0.10:
                continue
            score = (x0, gap)
            if best is None or score > best[0]:
                best = (score, x0, x1, (ya + yb) / 2 + y0, (a[3] + b[3]) / 2)
    if best is None:
        return None
    _, x0, x1, cy, ph = best
    return {'x0': x0 / w, 'x1': x1 / w, 'cy': cy / h, 'h': ph / h}


def _morph_underlines(gray, y_norm, half, max_w=0.24, kern=25):
    """形态学找印刷下划线：横向开运算保留长横线，滤掉文字笔画。

    教材页的文字笔画会被 Hough 当成短线，且行距只有 0.02；先做横向开运算，
    只留下真正的下划线/分数线，再按长度和页边距过滤。
    扫描件横线通常有 1~2° 倾斜，25px 核会把它切断；版面扫描用 kern=16 更稳。
    """
    h, w = gray.shape
    y0 = max(0, int((y_norm - half) * h))
    y1 = min(h, int((y_norm + half) * h))
    if y1 - y0 < 8:
        return []
    crop = gray[y0:y1, :]
    bw = (crop < 122).astype(np.uint8) * 255
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (max(8, int(kern)), 1))
    op = cv2.morphologyEx(bw, cv2.MORPH_OPEN, k)
    n, _, st, _ = cv2.connectedComponentsWithStats(op, 8)
    out = []
    for i in range(1, n):
        x, y, ww, hh, _ = st[i]
        if hh > 8 or not (w * 0.035 <= ww <= w * max_w):
            continue
        if x < 0.14 * w or x + ww > 0.93 * w:
            continue
        out.append({'x0': x / w, 'x1': (x + ww) / w,
                    'cx': (x + ww / 2) / w, 'cy': (y + y0 + hh / 2) / h})
    return out


def detect_underline(gray, y_norm, half=0.024, anchor_x=None):
    """找该行答题横线（允许扫描倾斜）：优先形态学真线，失败再走 Hough。"""
    morph = _morph_underlines(gray, y_norm, half)
    # 先按行距过滤，再做“最右/最近”选择：教材页两行只隔 0.02，不过滤会取到上一行
    morph = [c for c in morph if abs(c['cy'] - y_norm) <= 0.013]
    if morph:
        if anchor_x is None:
            pick = max(morph, key=lambda c: c['x1'])
        else:
            pick = min(morph, key=lambda c: abs(c['cx'] - anchor_x))
        # 只接受离锚点不太远的真线
        if anchor_x is None or abs(pick['cx'] - anchor_x) <= 0.24:
            return {'x0': pick['x0'], 'x1': pick['x1'], 'cy': pick['cy']}
    return _detect_underline_hough(gray, y_norm, half, anchor_x)


def _detect_underline_hough(gray, y_norm, half=0.024, anchor_x=None):
    """原 Hough 路径：找该行答题横线，合并短段，取离锚点最近/最右一条。"""
    h, w = gray.shape
    y0 = max(0, int((y_norm - half) * h))
    y1 = min(h, int((y_norm + half) * h))
    if y1 - y0 < 8:
        return None
    crop = gray[y0:y1, :]
    edges = cv2.Canny(crop, 15, 50)
    ls = cv2.HoughLinesP(edges, 1, np.pi / 360, threshold=14,
                         minLineLength=int(w * 0.04), maxLineGap=18)
    segs = []
    if ls is not None:
        for x1, ya, x2, yb in ls.reshape(-1, 4):
            x1, x2, ya, yb = int(x1), int(x2), int(ya), int(yb)
            if x2 < x1:
                x1, x2, ya, yb = x2, x1, yb, ya
            dx = x2 - x1
            if dx < w * 0.04 or x1 < 0.28 * w or x2 > 0.90 * w:
                continue
            if abs(yb - ya) > 0.12 * dx + 3:
                continue
            segs.append([x1, x2, (ya + yb) / 2])
    if not segs:
        return None
    segs.sort(key=lambda s: s[0])
    merged = []
    for s in segs:
        if merged and s[0] <= merged[-1][1] + 28 and abs(s[2] - merged[-1][2]) <= 6:
            merged[-1][1] = max(merged[-1][1], s[1])
            merged[-1][2] = (merged[-1][2] + s[2]) / 2
        else:
            merged.append(list(s))
    cands = [m for m in merged
             if w * 0.045 <= (m[1] - m[0]) <= w * 0.22 and m[1] <= 0.88 * w and m[0] >= 0.30 * w]
    if not cands:
        return None
    if anchor_x is None:
        # 排除页脚/页边阴影被误检成的短横线：位于页面底部且明显偏短。
        keep = [c for c in cands
                if not ((c[2] + y0) / h > 0.945 and (c[1] - c[0]) < 0.08 * w)]
        if keep:
            cands = keep

        # 排除纸面纹理造成的“假线”：真印刷线在列方向上有明显暗像素。
        def _dark(c):
            xa, xb, yy = int(c[0]), int(c[1]), int(round(c[2]))
            lo = max(0, yy - 12)
            hi = min(crop.shape[0], yy + 13)
            if xb - xa < 4 or hi <= lo:
                return 255.0
            mn = crop[lo:hi, xa:xb].min(axis=0)
            return float(np.percentile(mn, 90))

        paper = float(np.percentile(crop, 75))
        thr = min(115.0, paper - 25.0)
        dark = [c for c in cands if _dark(c) <= thr]
        if dark:
            cands = dark
    if anchor_x is not None:
        x1, x2, yy = min(cands, key=lambda s: abs((s[0] + s[1]) / 2 / w - anchor_x))
    else:
        x1, x2, yy = max(cands, key=lambda s: s[1])
    return {'x0': x1 / w, 'x1': x2 / w, 'cy': (yy + y0) / h}


def estimate_offset(gray, items, limit=0.04):
    """用选择题的高置信检测估计整页 dy；没有选择题时退回填空。

    返回 (off_y, detail)。detail 里记录每道题的“检测行 - AI 行”，便于排查。
    """
    det = []
    for it in items:
        typ = it.get('type', 'fill')
        if typ != 'choice':
            continue
        y = float(it.get('y', 0.5))
        pairs = _choice_pairs(gray, y, 0.045, 0.42)
        if pairs:
            b = min(pairs, key=lambda c: (abs(c['cy'] - y), -c['x0'], -c['x1']))
            det.append((it.get('q'), round(float(b['cy']) - y, 4)))
    if not det:
        for it in items:
            if it.get('type', 'fill') not in ('fill', 'fraction'):
                continue
            y = float(it.get('y', 0.5))
            x = float(it['x']) if it.get('x') is not None else None
            b = detect_underline(gray, y, anchor_x=x)
            if b:
                det.append((it.get('q'), round(float(b['cy']) - y, 4)))
    vals = [d for _, d in det if abs(d) <= limit]
    if len(vals) < 3:
        return 0.0, det, False
    # 取最大一致簇，容忍个别题被错误括号带偏（例如选项/函数括号）
    best = []
    for v in vals:
        cl = [u for u in vals if abs(u - v) <= 0.006]
        if len(cl) > len(best):
            best = cl
    if len(best) < 3:
        return 0.0, det, False
    off = float(np.clip(np.median(best), -limit, limit))
    return round(off, 3), det, True
