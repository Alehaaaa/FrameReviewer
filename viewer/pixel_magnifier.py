# -*- coding: utf-8 -*-
"""Bounded Qt-only pixel grid of the *presented* pixmap, never the scene.

No annotations, guides, OCIO processing, native-sample synthesis, GPU readback,
timers, saved state or interactive overlay. Target the original QPixmap pixel
using PlaybackSurface's exact item/viewport transform first; rotation and
flips affect this read-only grid's orientation, not its native sample IDs.
"""
from __future__ import absolute_import

from ..core.qtcompat import QtCore, QtGui

SCALES = (8, 12, 16)
RADII = (4, 5)  # 9x9 / 11x11


def grid_offset(dx, dy, rotation=0, flip_h=False, flip_v=False):
    """Oriented display-grid cell offset -> actual source-pixmap cell offset.

    PAR governs pixel-center viewport targeting in QGraphicsView. A cell here
    deliberately means exactly one native image sample regardless of PAR.
    """
    if rotation not in (0, 90, 180, 270):
        raise ValueError('Magnifier needs cardinal image rotation.')
    if rotation == 90:
        x, y = dy, -dx
    elif rotation == 180:
        x, y = -dx, -dy
    elif rotation == 270:
        x, y = -dy, dx
    else:
        x, y = dx, dy
    return (-x if flip_h else x, -y if flip_v else y)


def render_magnifier(image, x, y, scale=12, radius=4, rotation=0,
                     flip_h=False, flip_v=False):
    """Return detached ARGB32 QImage of exact nearest source pixmap pixels.

    Outside-source cells are neutral hatchless grey, never interpolated edge
    pixels. Grid lines frame cells; each cell's inner color is the verbatim
    QImage.pixelColor at its integer sample coordinate. Maximum 176x176 px.
    """
    if (not isinstance(image, QtGui.QImage) or image.isNull() or
            type(x) is not int or type(y) is not int or
            type(scale) is not int or scale not in SCALES or
            type(radius) is not int or radius not in RADII or
            not (0 <= x < image.width() and 0 <= y < image.height())):
        raise ValueError('Magnifier needs nonempty image, actual pixel and bounded 9/11 grid.')
    width = (2 * radius + 1) * scale
    output = QtGui.QImage(width, width, QtGui.QImage.Format_ARGB32)
    output.fill(QtGui.QColor(30, 32, 36, 255))
    painter = QtGui.QPainter(output)
    try:
        painter.setRenderHint(QtGui.QPainter.Antialiasing, False)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
        for j in range(2 * radius + 1):
            for i in range(2 * radius + 1):
                ix, iy = grid_offset(i-radius, j-radius, rotation,
                                     flip_h, flip_v)
                sx, sy = x + ix, y + iy
                if 0 <= sx < image.width() and 0 <= sy < image.height():
                    painter.fillRect(i * scale, j * scale, scale, scale,
                                     image.pixelColor(sx, sy))
        painter.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver)
        painter.setPen(QtGui.QPen(QtGui.QColor(32, 32, 32, 185), 1))
        for cell in range(1, 2 * radius + 1):
            offset = cell * scale
            painter.drawLine(offset, 0, offset, width-1)
            painter.drawLine(0, offset, width-1, offset)
        # Mark the selected pixel, but leave the cell centre unpainted so the
        # grid never obscures its actual sRGB/alpha presentation value.
        painter.setPen(QtGui.QPen(QtGui.QColor('#f2c86d'), 1))
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawRect(radius*scale+1, radius*scale+1, scale-2, scale-2)
    finally:
        painter.end()
    return output


__all__ = ['render_magnifier', 'grid_offset', 'SCALES', 'RADII']
