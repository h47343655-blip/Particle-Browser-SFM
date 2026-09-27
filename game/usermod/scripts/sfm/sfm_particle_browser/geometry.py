# -*- coding: utf-8 -*-
"""Qt-free rectangle math for the viewport mirror (rects are ``(x, y, w, h)``)."""
from __future__ import absolute_import, division

MAX_ZOOM = 4.0


def clamp_zoom(zoom):
    try:
        zoom = float(zoom)
    except (TypeError, ValueError):
        return 1.0
    return max(1.0, min(MAX_ZOOM, zoom))


def center_crop(width, height, zoom, center=None):
    """Rect of ``1/zoom`` of the area around ``center`` (default: middle), kept inside."""
    zoom = clamp_zoom(zoom)
    crop_w = max(1, min(width, int(round(width / zoom))))
    crop_h = max(1, min(height, int(round(height / zoom))))
    cx, cy = (width / 2.0, height / 2.0) if center is None else center
    x = int(round(cx - crop_w / 2.0))
    y = int(round(cy - crop_h / 2.0))
    x = max(0, min(x, width - crop_w))
    y = max(0, min(y, height - crop_h))
    return x, y, crop_w, crop_h


def overlap_area(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    w = min(ax + aw, bx + bw) - max(ax, bx)
    h = min(ay + ah, by + bh) - max(ay, by)
    return w * h if w > 0 and h > 0 else 0


def choose_position(size, avoid, screen, current, margin=8):
    """Top-left for a window of ``size`` that covers as little of ``avoid`` as possible.

    Tries the current position and the four sides of ``avoid``, clamped to ``screen``;
    ties keep the position closest to ``current``.
    """
    w, h = size
    ax, ay, aw, ah = avoid
    sx, sy, sw, sh = screen

    def clamp(x, y):
        return (max(sx, min(x, sx + sw - w)), max(sy, min(y, sy + sh - h)))

    candidates = [clamp(*current),
                  clamp(ax + aw + margin, ay),
                  clamp(ax - w - margin, ay),
                  clamp(ax, ay + ah + margin),
                  clamp(ax, ay - h - margin)]

    def cost(position):
        covered = overlap_area((position[0], position[1], w, h), avoid)
        moved = abs(position[0] - current[0]) + abs(position[1] - current[1])
        return covered, moved

    return min(candidates, key=cost)
