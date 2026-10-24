# -*- coding: utf-8 -*-
"""One bounded, deterministic, Qt-free display raster for intentional holes.

``raster_placeholder(policy, width, height, channels=4)`` produces packed
top-to-bottom RGB8 or RGBA8 pixels.  All placeholders are explicitly opaque;
the bytes are display palette values, never native Source/Working HDR samples.
Display PNG16 converts each RGB8 value ``n`` to ``n * 257`` exactly.

Error draws an unmistakable red header and light X.  Font/layout-dependent
Qt text is intentionally not rasterized: signed source frame identity belongs
to SequenceSource.frame_status and the managed export frame_map, not invented
pixels.  A failed decode of a *present* file must never call this module.
"""
from __future__ import absolute_import


BLACK = (0, 0, 0)
DARK = (41, 46, 52)       # #292e34
LIGHT = (75, 81, 89)     # #4b5159
ERROR_RED = (187, 85, 78)  # #bb554e
ERROR_X = (227, 215, 213)  # #e3d7d5

MAX_RASTER_BYTES = 64 * 1024 * 1024


def checker_cell(width):
    """The original Maya check size, computed once in native raster pixels."""
    if type(width) is not int or width < 1:
        raise ValueError('Missing-frame raster width must be a positive integer.')
    return max(8, min(32, width // 12 or 8))


def raster_placeholder(policy, width, height, channels=4):
    """Return canonical opaque RGB(A) u8, independent of Qt, NumPy and fonts.

    A placeholder is a bounded *display* construction; ``hold`` is a separate
    source-frame sampling policy, and ``decode_error`` is an actual media fault.
    Neither is eligible for a synthetic slate here.
    """
    if policy not in ('black', 'checker', 'error'):
        raise ValueError('Only intentional Black/Checker/Error holes have display rasters.')
    if (type(width) is not int or type(height) is not int or
            min(width, height) < 1 or width > 16384 or height > 16384 or
            type(channels) is not int or channels not in (3, 4) or
            width * height * channels > MAX_RASTER_BYTES):
        raise ValueError('Missing-frame raster exceeds safe positive CPU pixel bounds.')
    alpha = b'\xff' if channels == 4 else b''
    colors = {name: bytes(rgb) + alpha for name, rgb in (
        ('black', BLACK), ('dark', DARK), ('light', LIGHT),
        ('red', ERROR_RED), ('x', ERROR_X))}
    if policy == 'black':
        return colors['black'] * (width * height)
    if policy == 'checker':
        cell = checker_cell(width)
        # Construct each native scanline by whole cells; no Qt/DPI anti-alias.
        rows = []
        for y in range(height):
            row = bytearray()
            for x in range(0, width, cell):
                parity = ((x // cell) + (y // cell)) % 2
                row.extend(colors['light' if parity else 'dark'] *
                           min(cell, width - x))
            rows.append(bytes(row))
        return b''.join(rows)
    # A single exact, readable geometric slate at any raster shape.  The
    # original Qt glyphs are deliberately omitted: font hinting is platform-
    # dependent and cannot be proven equal to a managed PNG16 pixel buffer.
    header = min(height, max(3, height // 16))
    rows = []
    for y in range(height):
        if y < header:
            rows.append(colors['red'] * width)
            continue
        row = bytearray(colors['dark'] * width)
        cross_left = (y * (width - 1)) // max(1, height - 1)
        cross_right = ((height - 1 - y) * (width - 1)) // max(1, height - 1)
        for center in (cross_left, cross_right):
            for x in range(max(0, center - 1), min(width, center + 2)):
                row[x * channels:(x + 1) * channels] = colors['x']
        rows.append(bytes(row))
    return b''.join(rows)


__all__ = ['BLACK', 'DARK', 'LIGHT', 'ERROR_RED', 'ERROR_X',
           'MAX_RASTER_BYTES', 'checker_cell', 'raster_placeholder']
