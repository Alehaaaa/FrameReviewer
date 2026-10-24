# -*- coding: utf-8 -*-
"""Canonical, JSON-safe annotation item data and legacy normalization."""
from __future__ import absolute_import


DEFAULT_COLOR = '#ef5350'
DRAWABLE_KINDS = frozenset(('brush', 'rectangle', 'ellipse', 'arrow'))


def _clamp(value, low, high):
    return max(low, min(high, value))


def _with_id(source, item):
    """Carry a stable shape ``id`` through canonicalization when present."""
    if isinstance(source, dict) and source.get('id') is not None:
        item['id'] = str(source['id'])
    return item


def copy_strokes(strokes):
    """Detach note dictionaries and their mutable geometry from UI state."""
    copied = []
    for stroke in strokes:
        item = dict(stroke)
        item['points'] = [tuple(point) for point in stroke.get('points', [])]
        if 'pos' in item:
            item['pos'] = list(item['pos'])
        copied.append(item)
    return copied


def copy_drawings(drawings):
    return {int(frame): copy_strokes(strokes)
            for frame, strokes in drawings.items()}


def _points(value, allow_empty=False):
    if not isinstance(value, (list, tuple)):
        raise ValueError('Annotation points must be a list.')
    result = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise ValueError('Every annotation point needs x and y values.')
        pressure = point[2] if len(point) > 2 else 1.0
        result.append((float(point[0]), float(point[1]),
                       _clamp(float(pressure), 0.0, 1.0)))
    if not result and not allow_empty:
        return []
    return result


def normalize_stroke(stroke):
    """Return one canonical note item, or ``None`` for an empty item.

    Text geometry may be empty for legacy files.  The Qt renderer upgrades it
    to a four-corner box because font metrics belong to the presentation layer.
    """
    if not isinstance(stroke, dict):
        raise ValueError('Every annotation stroke must be an object.')
    kind = str(stroke.get('kind', 'brush'))
    color = str(stroke.get('color', DEFAULT_COLOR))
    opacity = _clamp(float(stroke.get('opacity', 1.0)), 0.01, 1.0)
    width = max(1.0, float(stroke.get('width', 3.0)))
    if kind == 'text':
        text = str(stroke.get('text', ''))
        if not text.strip():
            return None
        pos = stroke.get('pos') or (0.0, 0.0)
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            raise ValueError('Text annotation position needs x and y values.')
        try:
            points = _points(stroke.get('points', []), allow_empty=True)
        except (TypeError, ValueError):
            # Old files did not store text geometry.  Treat malformed optional
            # geometry the same way and let the renderer reconstruct it.
            points = []
        return _with_id(stroke, dict(
            kind='text', text=text,
            pos=[float(pos[0]), float(pos[1])],
            color=color, opacity=opacity,
            size=max(2.0, float(stroke.get('size', 24.0))),
            points=points, width=width,
            pressure_size=False, pressure_opacity=False))
    points = _points(stroke.get('points', []))
    if not points:
        return None
    return _with_id(stroke, dict(
        points=points, color=color, width=width, opacity=opacity,
        pressure_size=bool(stroke.get('pressure_size', False)),
        pressure_opacity=bool(stroke.get('pressure_opacity', False)),
        kind=kind if kind in DRAWABLE_KINDS else 'brush'))


def normalize_drawings(drawings):
    """Validate and normalize a frame-to-items mapping.

    Empty frames are retained because they intentionally stop OpenRV-style
    held annotations from carrying onto later frames.
    """
    if not isinstance(drawings, dict):
        raise ValueError('Notes must contain a frame-to-strokes mapping.')
    loaded = {}
    for frame, strokes in drawings.items():
        frame = int(frame)
        if frame < 0:
            continue
        if not isinstance(strokes, list):
            raise ValueError('Annotation strokes must be a list.')
        clean = []
        for stroke in strokes:
            item = normalize_stroke(stroke)
            if item is not None:
                clean.append(item)
        loaded[frame] = clean
    return loaded
