# -*- coding: utf-8 -*-
"""MarkedTimelineSlider: scrub track with two optional, stackable lanes.

Layout, top to bottom, each lane independently toggleable:
    [ thumbnail strip ]   (optional)
    [ waveform        ]   (optional)
    [ scrub track     ]   (always; range highlight + playhead)

All lanes share one time axis, so a thumbnail, its waveform column and the
playhead line up at the same frame.
"""
from __future__ import absolute_import

import math

from .qtcompat import QtCore, QtGui, QtWidgets, Signal
from . import style, utils

MARGIN = 8
TRACK_H = 16
CACHE_H = 3  # VC_SEAMLESS_CACHE_OVERLAY_V4 (in-track overlay)
WAVE_H = 24
MIN_FRAME_TICK_GAP = 3.0   # Thin frame dividers remain readable at this spacing.
MIN_SECOND_TICK_GAP = 10.0  # At smaller scales, omit the ruler altogether.


class MarkedTimelineSlider(QtWidgets.QWidget):
    scrubbed = Signal(int)

    def __init__(self, parent=None):
        QtWidgets.QWidget.__init__(self, parent)
        self._count = 1
        self._fps = 24.0
        self._frame = 0
        self._in = 0
        self._out = 0
        self._peaks = []
        self._markers = set()
        self._annotation_frames = {}  # frame -> most prominent note color
        self._cache_ranges = []
        self._cache_target = None
        self._buffer_progress = None
        self._thumbs = {}
        self._thumb_total = 0
        self._thumb_h = 36
        self._cached_frames = ()
        self._show_thumbs = True
        self._show_wave = True
        self.setMouseTracking(True)
        self._relayout()

    # -- configuration ----------------------------------------------------
    def set_frame_count(self, count, fps=None):
        self._count = max(1, int(count))
        self._out = self._count - 1
        if fps is not None:
            self.set_fps(fps)
        self.update()

    def set_fps(self, fps):
        """Set source frame rate for time-based ruler ticks (including fractional fps)."""
        value = float(fps)
        self._fps = value if math.isfinite(value) and value > 0.0 else 24.0
        self.update()

    def set_frame(self, frame):
        self._frame = int(utils.clamp(frame, 0, self._count - 1))
        self.update()

    def set_range(self, in_frame, out_frame):
        self._in, self._out = int(in_frame), int(out_frame)
        self.update()

    def set_waveform(self, peaks):
        values = [float(value) for value in (peaks or [])]
        if len(values) >= 3:
            # A short triangular low-pass filter keeps adjacent resize pixels
            # from jumping between unrelated peak buckets.
            padded = [values[0], values[0]] + values + [values[-1], values[-1]]
            values = [
                (padded[i] + 2.0 * padded[i + 1] + 3.0 * padded[i + 2] +
                 2.0 * padded[i + 3] + padded[i + 4]) / 9.0
                for i in range(len(values))
            ]
        self._peaks = values
        self.update()

    def clear_waveform(self):
        self._peaks = []
        self.update()

    def set_markers(self, frames):
        self._markers = set(int(frame) for frame in (frames or ()))
        self.update()

    def set_annotation_frames(self, frames):
        """Set drawing keyframes and their dominant colors (or legacy frame list)."""
        if hasattr(frames, 'items'):
            self._annotation_frames = {int(frame): color
                                       for frame, color in frames.items()}
        else:
            self._annotation_frames = {int(frame): None for frame in (frames or ())}
        self.update()

    def set_cached_frames(self, frames):
        # At most 64 resident frames, not an unbounded history.
        self._cached_frames = tuple(sorted(set(int(f) for f in frames)))
        self.update()

    def clear_cached_frames(self):
        self._cached_frames = ()
        self.update()

    def set_cache_ranges(self, decoded_ranges, desired_window=None):
        """Paint the exact decoded frames and the rolling prefetch target."""
        ranges = [(int(a), int(b)) for a, b in (decoded_ranges or [])]
        self._cache_ranges = ranges
        self._cache_target = tuple(desired_window) if desired_window else None
        if ranges:
            self._buffer_progress = None
        self.update()

    def set_buffer_progress(self, percent):
        """Show Qt's reported native-player buffer amount when ranges are unavailable."""
        self._buffer_progress = max(0.0, min(100.0, float(percent)))
        self._cache_ranges = []
        self._cache_target = None
        self.update()

    def clear_cache_ranges(self):
        self._cache_ranges = []
        self._cache_target = None
        self._buffer_progress = None
        self.update()

    def begin_thumbnails(self, total, thumb_h):
        self._thumbs = {}
        self._thumb_total = max(1, total)
        self._thumb_h = max(24, int(thumb_h))
        self._relayout()

    def add_thumbnail(self, ordinal, pixmap):
        self._thumbs[int(ordinal)] = pixmap
        self.update()

    def clear_thumbnails(self):
        self._thumbs = {}
        self._thumb_total = 0
        self.update()

    def set_thumbnails_visible(self, visible):
        self._show_thumbs = bool(visible)
        self._relayout()

    def set_waveform_visible(self, visible):
        self._show_wave = bool(visible)
        self._relayout()

    def thumbnails_visible(self):
        return self._show_thumbs

    def waveform_visible(self):
        return self._show_wave

    # -- geometry ---------------------------------------------------------
    def _relayout(self):
        height = TRACK_H
        if self._show_wave:
            height += WAVE_H
        if self._show_thumbs:
            height += self._thumb_h
        self.setFixedHeight(height)
        self.updateGeometry()
        self.update()

    def _inner_width(self):
        return max(1, self.width() - 2 * MARGIN)

    def _x_for(self, frame):
        if self._count <= 1:
            return MARGIN
        return MARGIN + self._inner_width() * frame / float(self._count - 1)

    def _frame_for(self, x):
        ratio = (x - MARGIN) / float(self._inner_width())
        return int(round(utils.clamp(ratio, 0.0, 1.0) * (self._count - 1)))

    def _lane_rects(self):
        """Return (thumb_rect, wave_rect, track_rect); empty rects when hidden."""
        top = 0
        empty = QtCore.QRect()
        thumb = empty
        wave = empty
        if self._show_thumbs:
            thumb = QtCore.QRect(0, top, self.width(), self._thumb_h)
            top += self._thumb_h
        if self._show_wave:
            wave = QtCore.QRect(0, top, self.width(), WAVE_H)
            top += WAVE_H
        track = QtCore.QRect(0, top, self.width(), TRACK_H)
        return thumb, wave, track

    # -- painting ---------------------------------------------------------
    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        thumb_rect, wave_rect, track_rect = self._lane_rects()
        if not thumb_rect.isEmpty():
            self._paint_thumbs(painter, thumb_rect)
        if not wave_rect.isEmpty():
            self._paint_wave(painter, wave_rect)
        self._paint_track(painter, track_rect)
        self._paint_cache(painter, track_rect)
        self._paint_frame_ticks(painter, track_rect)
        self._paint_markers(painter, track_rect)
        self._paint_annotation_marks(painter, track_rect)
        self._paint_playhead(painter)
        painter.end()

    def _paint_thumbs(self, painter, rect):
        painter.fillRect(rect, QtGui.QColor(style.THUMB_FRAME))
        if not self._thumbs:
            return
        inner = self._inner_width()
        cell = inner / float(self._thumb_total)
        for ordinal, pixmap in self._thumbs.items():
            if pixmap.isNull() or pixmap.width() <= 0 or pixmap.height() <= 0:
                continue
            x = MARGIN + ordinal * cell
            available_w = max(1.0, cell)
            available_h = max(1.0, rect.height() - 2.0)
            source_aspect = pixmap.width() / float(pixmap.height())
            target_w = min(available_w, available_h * source_aspect)
            target_h = target_w / source_aspect
            if target_h > available_h:
                target_h = available_h
                target_w = target_h * source_aspect
            target = QtCore.QRectF(
                x + (available_w - target_w) / 2.0,
                rect.top() + 1.0 + (available_h - target_h) / 2.0,
                target_w, target_h)
            painter.drawPixmap(target, pixmap, QtCore.QRectF(pixmap.rect()))

    def _paint_wave(self, painter, rect):
        painter.fillRect(rect, QtGui.QColor(style.TRACK))
        if not self._peaks:
            return
        inner = max(1, int(self._inner_width()))
        mid = rect.top() + rect.height() / 2.0
        half = rect.height() / 2.0 - 2
        count = len(self._peaks)
        amplitudes = []
        for px in range(inner + 1):
            position = px * (count - 1) / float(inner) if count > 1 else 0.0
            left = int(position)
            right = min(count - 1, left + 1)
            blend = position - left
            peak = self._peaks[left] * (1.0 - blend) + self._peaks[right] * blend
            amplitudes.append(max(0.0, min(1.0, peak)) * half)

        # One antialiased envelope is visually stable while the widget width
        # changes; nearest-neighbour vertical bars visibly popped on resize.
        path = QtGui.QPainterPath()
        path.moveTo(QtCore.QPointF(MARGIN, mid - amplitudes[0]))
        for px, amp in enumerate(amplitudes[1:], 1):
            path.lineTo(QtCore.QPointF(MARGIN + px, mid - amp))
        for px in range(inner, -1, -1):
            path.lineTo(QtCore.QPointF(MARGIN + px, mid + amplitudes[px]))
        path.closeSubpath()
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor(style.WAVE))
        painter.drawPath(path)
        painter.restore()

    def _paint_frame_ticks(self, painter, track_rect):
        """Show frame or second divisions only at legible timeline scales.

        Both branches are bounded by the widget width. In particular, painting
        does not iterate through every frame of a long video.
        """
        if self._count <= 1:
            return
        frame_gap = self._inner_width() / float(self._count - 1)
        show_frames = frame_gap >= MIN_FRAME_TICK_GAP
        show_seconds = frame_gap * self._fps >= MIN_SECOND_TICK_GAP
        if not show_frames and not show_seconds:
            return

        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing, False)
        top = track_rect.top() + 1
        if show_frames:
            color = QtGui.QColor(style.TEXT_DIM)
            color.setAlpha(95)
            painter.setPen(QtGui.QPen(color, 1))
            for frame in range(1, self._count - 1):
                x = round(self._x_for(frame))
                painter.drawLine(QtCore.QPointF(x, top),
                                 QtCore.QPointF(x, top + 7))

        if show_seconds:
            color = QtGui.QColor(style.TEXT_DIM)
            color.setAlpha(160)
            painter.setPen(QtGui.QPen(color, 1))
            # The first and last frame already define the ruler boundaries.
            duration = (self._count - 1) / self._fps
            for second in range(1, int(math.ceil(duration))):
                x = round(self._x_for(second * self._fps))
                painter.drawLine(QtCore.QPointF(x, top),
                                 QtCore.QPointF(x, top + 11))
        painter.restore()

    def _paint_markers(self, painter, track_rect):
        if not self._markers:
            return
        painter.save()
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor(style.CROP))
        # Bookmark pennants occupy only the upper half of the ruler. Drawing
        # annotations occupy the lower half, so both are visible on one frame.
        top = track_rect.top() + 1.0
        gap = self._inner_width() / float(max(1, self._count - 1))
        half_width = min(3.5, max(0.5, gap * 0.36))
        for frame in sorted(self._markers):
            if 0 <= frame < self._count:
                x = self._x_for(frame)
                pennant = QtGui.QPolygonF([
                    QtCore.QPointF(x - half_width, top),
                    QtCore.QPointF(x + half_width, top),
                    QtCore.QPointF(x, top + 5.5)])
                painter.drawPolygon(pennant)
        painter.restore()

    def _paint_annotation_marks(self, painter, track_rect):
        if not self._annotation_frames:
            return
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        # Lower-row color bars never intersect upper-row bookmark pennants.
        # Use each note's most prominent stroke color, not a global amber.
        gap = self._inner_width() / float(max(1, self._count - 1))
        width = min(3.0, max(1.0, gap * 0.7))
        top = track_rect.top() + 8.0
        height = 4.0
        for frame, note_color in sorted(self._annotation_frames.items()):
            if 0 <= frame < self._count:
                x = self._x_for(frame)
                color = QtGui.QColor(note_color) if note_color else QtGui.QColor(style.ANNOTATION)
                if not color.isValid():
                    color = QtGui.QColor(style.ANNOTATION)
                painter.setPen(QtCore.Qt.NoPen)
                painter.setBrush(color)
                if gap < 1.5:
                    painter.drawRect(QtCore.QRectF(x - 0.5, top, 1.0, height))
                else:
                    rect = QtCore.QRectF(x - width / 2.0, top, width, height)
                    painter.drawRect(rect)
        painter.restore()

    def _paint_track(self, painter, rect):
        well = QtCore.QRectF(MARGIN, rect.top() + rect.height() / 2.0 - 2,
                             self._inner_width(), 4)
        painter.fillRect(well, QtGui.QColor(style.TRACK))
        x_in, x_out = self._x_for(self._in), self._x_for(self._out)
        painter.fillRect(QtCore.QRectF(x_in, well.top(), max(1.0, x_out - x_in), well.height()),
                         QtGui.QColor(style.ACCENT))
        # Blue segments depict actual RAM-resident frames.
        if self._cached_frames:
            cached_y = rect.top() + 1
            ordered = [f for f in self._cached_frames if 0 <= f < self._count]
            if ordered:
                painter.setPen(QtCore.Qt.NoPen)
                painter.setBrush(QtGui.QColor(style.ACCENT_HI))
                start = previous = ordered[0]
                for frame in ordered[1:] + [None]:
                    if frame is None or frame > previous + 1:
                        x0 = self._x_for(start)
                        x1 = self._x_for(previous + 1) if previous + 1 < self._count else self._x_for(previous)
                        painter.drawRect(QtCore.QRectF(x0, cached_y, max(2.0, x1-x0), 2))
                        start = frame
                    previous = frame

    def _paint_cache(self, painter, track_rect):
        """Draw resident cache inside the range track, below the playhead.

        There is NO separate lane or background. Horizontal geometry uses the
        same MARGIN/inner width as the existing In/Out range timeline.
        """  # VC_SEAMLESS_CACHE_OVERLAY_V4
        if not self._cache_ranges and self._buffer_progress is None:
            return
        height = 4
        y = track_rect.bottom() - height + 1
        start_x = MARGIN
        width = max(1, self._inner_width())
        total = max(1, self._count)
        painter.save()
        painter.setClipRect(QtCore.QRectF(start_x, y, width, height))
        color = QtGui.QColor(style.ACCENT_HI)
        if self._buffer_progress is not None:
            backing = QtGui.QColor(color)
            backing.setAlpha(45)
            painter.fillRect(QtCore.QRectF(start_x, y, width, height), backing)
            buffered_width = width * self._buffer_progress / 100.0
            if buffered_width > 0.0:
                painter.fillRect(
                    QtCore.QRectF(start_x, y, max(1.0, buffered_width), height), color)
        for first, last in self._cache_ranges:
            first = max(0, min(total - 1, int(first)))
            last = max(first, min(total - 1, int(last)))
            x1 = start_x + width * (first / float(total))
            x2 = start_x + width * ((last + 1) / float(total))
            painter.fillRect(QtCore.QRectF(x1, y, max(1., x2 - x1), height), color)
        painter.restore()


    def _paint_playhead(self, painter):
        x = self._x_for(self._frame)
        painter.setPen(QtGui.QPen(QtGui.QColor(style.ACCENT_HI), 1))
        painter.drawLine(QtCore.QPointF(x, 0), QtCore.QPointF(x, self.height()))
        handle = QtGui.QPolygonF([
            QtCore.QPointF(x, self.height() - TRACK_H + 2),
            QtCore.QPointF(x - 5, self.height() - TRACK_H - 4),
            QtCore.QPointF(x + 5, self.height() - TRACK_H - 4)])
        painter.setBrush(QtGui.QColor(style.ACCENT_HI))
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawPolygon(handle)

    # -- interaction ------------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.scrubbed.emit(self._frame_for(event.pos().x()))
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() & QtCore.Qt.LeftButton:
            self.scrubbed.emit(self._frame_for(event.pos().x()))
            event.accept()

    def resizeEvent(self, event):
        QtWidgets.QWidget.resizeEvent(self, event)
        self.update()
