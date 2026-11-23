# -*- coding: utf-8 -*-
"""透视矫正：检测纸张四角，拉平成扫描件；再把透明手写层投回原照片。

流程：
1. detect_page_corners 找纸张四角（外轮廓四边形，失败再用亮纸二值化兜底）；
2. warp_to_scan 用单应矩阵把纸张拉平，行线变水平，坐标可直接量；
3. 手写层在扫描件坐标系里渲染，最后 warp_layer_back 按逆矩阵贴回照片。

角点不可信时返回 None，由调用方回退到直接在照片上落笔的旧流程。
"""
import cv2
import numpy as np


def _order(pts):
    """按 左上、右上、右下、左下 排序（页面大致竖直，允许小角度旋转）。"""
    pts = sorted([tuple(map(float, p)) for p in np.array(pts).reshape(4, 2)], key=lambda p: p[1])
    top = sorted(pts[:2], key=lambda p: p[0])
    bot = sorted(pts[2:], key=lambda p: p[0])
    return np.array([top[0], top[1], bot[1], bot[0]], dtype=np.float32)


def _angles_ok(quad):
    """四个内角都要像纸角：45°~135°，避免把长条/三角形误当纸。"""
    q = quad
    for i in range(4):
        a, b, c = q[(i - 1) % 4], q[i], q[(i + 1) % 4]
        v1, v2 = a - b, c - b
        n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
        if n1 < 20 or n2 < 20:
            return False
        cos = float(np.clip(np.dot(v1, v2) / (n1 * n2), -1, 1))
        ang = np.degrees(np.arccos(cos))
        if not (45.0 <= ang <= 135.0):
            return False
    return True


def _quad_from_mask(mask, min_area):
    cs, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in cs:
        area = cv2.contourArea(c)
        if area < min_area:
            continue
        peri = cv2.arcLength(c, True)
        ap = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(ap) != 4 or not cv2.isContourConvex(ap):
            continue
        if best is None or area > best[0]:
            best = (area, ap.reshape(4, 2))
    return best


def detect_page_corners(img, min_ratio=0.35):
    """返回 (四角 | None, 面积占比)。

    先用“光照归一化 + Otsu”找纸张轮廓，能扛住页面底部阴影；
    角点贴住画面边缘（整张已是扫描件）时返回 None，不做无意义的矫正。
    """
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    g = cv2.GaussianBlur(gray, (5, 5), 0)
    min_area = min_ratio * w * h

    # 主路径：去掉大尺度明暗差异后再二值化，避免底部阴影把纸切掉一块
    bg = cv2.GaussianBlur(g, (0, 0), max(30, min(h, w) // 8))
    norm = cv2.divide(g, bg, scale=255)
    _, bw = cv2.threshold(norm, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    cs, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    if cs:
        c = max(cs, key=cv2.contourArea)
        area = cv2.contourArea(c)
        peri = cv2.arcLength(c, True)
        for eps in (0.01, 0.02, 0.03, 0.05):
            ap = cv2.approxPolyDP(cv2.convexHull(c), eps * peri, True)
            if len(ap) == 4:
                best = (area, ap.reshape(4, 2).astype(np.float32))
                break

    if best is None:
        edges = cv2.Canny(g, 40, 120)
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
        best = _quad_from_mask(edges, min_area)

    if best is None:
        return None, 0.0
    area, quad = best
    ratio = area / float(w * h)
    if ratio < min_ratio or ratio > 0.94:
        return None, ratio
    quad = _order(quad)
    if not _angles_ok(quad):
        return None, ratio
    # 四角都贴边 = 已经是扫描件，不需要再拉一次
    mx, my = 0.015 * w, 0.015 * h
    if all((p[0] <= mx or p[0] >= w - mx) and (p[1] <= my or p[1] >= h - my) for p in quad):
        return None, ratio
    return quad, ratio


def warp_to_scan(img, quad):
    """把纸张四角拉平；输出尺寸沿用照片里的实际像素尺寸，避免字号比例变化。"""
    tl, tr, br, bl = quad
    wa = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2.0
    ha = (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)) / 2.0
    sw, sh = int(round(wa)), int(round(ha))
    if sw < 80 or sh < 80:
        return None
    dst = np.array([[0, 0], [sw - 1, 0], [sw - 1, sh - 1], [0, sh - 1]], np.float32)
    H = cv2.getPerspectiveTransform(quad, dst)
    scan = cv2.warpPerspective(img, H, (sw, sh), flags=cv2.INTER_CUBIC)
    return scan, H


def transform_points(pts, M):
    arr = np.array(pts, dtype=np.float32).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(arr, M).reshape(-1, 2)


def box_back(box, H_inv, W, H):
    """把扫描件坐标的框投回照片坐标（取外接矩形）。"""
    x0, y0, x1, y1 = [float(v) for v in box]
    pts = transform_points([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], H_inv)
    xs, ys = pts[:, 0], pts[:, 1]
    return [int(round(np.clip(xs.min(), 0, W))), int(round(np.clip(ys.min(), 0, H))),
            int(round(np.clip(xs.max(), 0, W))), int(round(np.clip(ys.max(), 0, H)))]


def warp_layer_back(layer_rgba, H_inv, size):
    """RGBA 手写层按逆矩阵投回照片；先预乘 alpha，避免黑边。"""
    arr = np.array(layer_rgba, dtype=np.float32)
    a = arr[..., 3:4] / 255.0
    pre = np.dstack([arr[..., :3] * a, arr[..., 3]])
    out = cv2.warpPerspective(pre, H_inv, (int(size[0]), int(size[1])),
                              flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
                              borderValue=(0, 0, 0, 0))
    oa = out[..., 3:4] / 255.0
    rgb = np.where(oa > 1e-3, out[..., :3] / np.maximum(oa, 1e-3), 0)
    out = np.dstack([rgb, out[..., 3]])
    return np.clip(out, 0, 255).astype(np.uint8)
