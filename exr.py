# -*- coding: utf-8 -*-
"""Dedicated OpenEXR reading for image-sequence playback.

Qt has no built-in EXR reader, so EXR frames are decoded here through
``OpenEXR`` + ``Imath`` + NumPy. Multi-channel / layered EXRs are handled by
resolving the display R/G/B channels by name (``R``/``red`` ... possibly with a
layer prefix), reading them as 32-bit float, and tone-mapping linear scene
values to an 8-bit sRGB pixmap for display. Everything degrades gracefully: if
the modules are missing the caller falls back and EXR frames simply do not
decode.
"""
from __future__ import absolute_import

from .qtcompat import QtCore, QtGui

try:
    import numpy as np
    _HAVE_NUMPY = True
except Exception:
    np = None
    _HAVE_NUMPY = False

try:
    import OpenEXR
    import Imath
    _HAVE_OPENEXR = True
except Exception:
    OpenEXR = None
    Imath = None
    _HAVE_OPENEXR = False


def available():
    return _HAVE_NUMPY and _HAVE_OPENEXR


def is_exr(path):
    return bool(path) and path.lower().endswith('.exr')


def _pick_channel(channels, names):
    """Resolve a display channel among possibly layer-prefixed EXR channels."""
    lowered = {name.lower(): name for name in channels}
    # Exact match first (single-layer EXR: 'R', 'G', 'B').
    for candidate in names:
        if candidate in lowered:
            return lowered[candidate]
    # Then a layered match: '<layer>.R', '<beauty>.red', ...
    for candidate in names:
        for lower, original in lowered.items():
            if lower.endswith('.' + candidate):
                return original
    return None


def read_rgb_float(path):
    """Return an HxWx3 float32 linear RGB array for an EXR file, or None."""
    if not available():
        return None
    try:
        exr = OpenEXR.InputFile(path)
    except Exception:
        return None
    try:
        header = exr.header()
        window = header['dataWindow']
        width = window.max.x - window.min.x + 1
        height = window.max.y - window.min.y + 1
        channels = list(header['channels'].keys())
        red = _pick_channel(channels, ('r', 'red'))
        green = _pick_channel(channels, ('g', 'green'))
        blue = _pick_channel(channels, ('b', 'blue'))
        if not (red and green and blue):
            return None
        pixel_type = Imath.PixelType(Imath.PixelType.FLOAT)

        def read(name):
            raw = exr.channel(name, pixel_type)
            return np.frombuffer(raw, dtype=np.float32).reshape(height, width)

        rgb = np.stack([read(red), read(green), read(blue)], axis=-1)
    except Exception:
        return None
    finally:
        try:
            exr.close()
        except Exception:
            pass
    return np.nan_to_num(rgb, copy=False)


def read_size(path):
    """Return (width, height) for an EXR without decoding pixels, or None."""
    if not _HAVE_OPENEXR:
        return None
    try:
        exr = OpenEXR.InputFile(path)
        window = exr.header()['dataWindow']
        exr.close()
        return (window.max.x - window.min.x + 1,
                window.max.y - window.min.y + 1)
    except Exception:
        return None


def read_pixmap(path, decode_width=None):
    """Decode an EXR to a display QPixmap (linear -> sRGB), or None."""
    rgb = read_rgb_float(path)
    if rgb is None:
        return None
    try:
        linear = np.clip(rgb, 0.0, None)
        # Linear scene-referred float -> sRGB display encoding.
        srgb = np.where(linear <= 0.0031308,
                        linear * 12.92,
                        1.055 * np.power(linear, 1.0 / 2.4) - 0.055)
        data = np.clip(srgb * 255.0 + 0.5, 0, 255).astype(np.uint8)
        data = np.ascontiguousarray(data)
        height, width = data.shape[:2]
        image = QtGui.QImage(data.data, width, height, width * 3,
                             QtGui.QImage.Format_RGB888)
        pixmap = QtGui.QPixmap.fromImage(image.copy())
    except Exception:
        return None
    if decode_width and pixmap.width() > decode_width:
        pixmap = pixmap.scaledToWidth(decode_width, QtCore.Qt.SmoothTransformation)
    return pixmap
