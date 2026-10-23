# -*- coding: utf-8 -*-
"""The viewport: a QGraphicsView whose scene is in native video pixels.

A single pixmap item displays decoded frames from the native video sink,
FFmpeg streaming fallback, or an image sequence. Using one item for every
source means playback renders through the same reliable path as the poster,
instead of a QGraphicsVideoItem that some drivers leave black. The crop
rectangle sits on top, in native pixel coordinates, so there is no hand-rolled
coordinate mapping anywhere.
"""
from __future__ import absolute_import

import os
import math

from .qtcompat import QtCore, QtGui, QtWidgets, Signal
from . import style, utils, color


class PlaybackSurface(QtWidgets.QGraphicsView):
    cropChanged = Signal(object)        # QRectF in native pixels, or None
    scrubRequested = Signal(int)        # +/- frame delta from the wheel
    mediaDropped = Signal(str)          # path dropped onto the viewport
    importRequested = Signal()          # placeholder button clicked
    drawingChanged = Signal(int)        # number of strokes on the current frame
    annotationFramesChanged = Signal(object)
    activeAnnotationChanged = Signal(int, int)  # source keyframe, visible strokes
    tabletPressureChanged = Signal(float)
    edited = Signal(int)                # a committed drawing change, on this frame

    def __init__(self, parent=None):
        QtWidgets.QGraphicsView.__init__(self, parent)
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        self.setRenderHint(QtGui.QPainter.Antialiasing, True)
        self.setBackgroundBrush(QtGui.QColor(style.BG))
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        # Navigation: zoom toward the cursor, pan via the scrollbars (kept hidden
        # for a clean viewport), and receive macOS trackpad pinch gestures.
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._panning = False
        self._pan_last = QtCore.QPoint()

        self._native = QtCore.QSizeF(0, 0)
        self._flip_h = False
        self._flip_v = False

        self.frame_item = QtWidgets.QGraphicsPixmapItem()
        self.frame_item.setTransformationMode(QtCore.Qt.SmoothTransformation)
        self.scene().addItem(self.frame_item)

        self._crop = QtWidgets.QGraphicsRectItem()
        pen = QtGui.QPen(QtGui.QColor(style.CROP))
        pen.setCosmetic(True)
        pen.setWidth(1)
        self._crop.setPen(pen)
        self._crop.setBrush(QtGui.QColor(82, 133, 166, 40))
        self._crop.setZValue(10)
        self.scene().addItem(self._crop)
        self._crop.hide()
        self._crop_hidden = False
        self._has_crop = False

        # Perspective tracking uses a four-corner overlay. It is a display-only
        # scene item, so the existing crop tool remains interactive beneath it.
        self._tracked_quad = QtWidgets.QGraphicsPolygonItem()
        tracked_pen = QtGui.QPen(QtGui.QColor(style.CROP))
        tracked_pen.setWidth(2)
        tracked_pen.setCosmetic(True)
        self._tracked_quad.setPen(tracked_pen)
        self._tracked_quad.setBrush(QtGui.QColor(82, 133, 166, 26))
        self._tracked_quad.setZValue(12)
        self._tracked_quad.setAcceptedMouseButtons(QtCore.Qt.NoButton)
        self.scene().addItem(self._tracked_quad)
        self._tracked_quad.hide()

        self._drag = None
        self._tool = 'select'
        self._current_frame = 0
        self._drawings = {}
        self._drawing_items = []
        self._selected_strokes = set()
        self._selection_key = -1
        self._selection_drag = None
        self._selection_box = QtWidgets.QGraphicsRectItem()
        self._selection_box.setZValue(35)
        self._selection_box.setPen(QtGui.QPen(QtGui.QColor(style.SELECTION), 1, QtCore.Qt.DashLine))
        self._selection_box.setBrush(QtGui.QBrush(QtCore.Qt.NoBrush))
        self._selection_box.setAcceptedMouseButtons(QtCore.Qt.NoButton)
        self.scene().addItem(self._selection_box)
        self._selection_box.hide()
        self._selection_handles = {}
        for handle in ('nw', 'ne', 'sw', 'se', 'rotate'):
            item = QtWidgets.QGraphicsEllipseItem()
            item.setZValue(36)
            item.setBrush(QtGui.QColor(style.SELECTION) if handle != 'rotate'
                          else QtGui.QColor(style.HANDLE_ROTATE))
            item.setPen(QtGui.QPen(QtGui.QColor(style.HANDLE_OUTLINE)))
            item.setAcceptedMouseButtons(QtCore.Qt.NoButton)
            self.scene().addItem(item)
            item.hide()
            self._selection_handles[handle] = item
        self._selection_marquee = QtWidgets.QGraphicsRectItem()
        self._selection_marquee.setZValue(34)
        self._selection_marquee.setPen(QtGui.QPen(QtGui.QColor(style.SELECTION), 1, QtCore.Qt.DashLine))
        self._selection_marquee.setBrush(QtGui.QColor(100, 201, 255, 24))
        self._selection_marquee.setAcceptedMouseButtons(QtCore.Qt.NoButton)
        self.scene().addItem(self._selection_marquee)
        self._selection_marquee.hide()
        self._onion_items = []
        self._onion_enabled = False
        self._onion_before = 2
        self._onion_after = 2
        self._onion_opacity = 0.35
        self._onion_falloff = True
        self._onion_tint = True
        self._active_item = None
        self._active_points = []
        self._pending_strokes = None
        self._erasing = False
        self._erase_changed = False
        self._erase_last_point = None
        self._pen_color = QtGui.QColor(style.DRAW_DEFAULT)
        self._pen_width = 3.0
        # Pen widths are cosmetic viewport pixels, independent of the native
        # resolution. Give the eraser its own screen-pixel diameter.
        self._eraser_size = 24.0
        self._eraser_pressure = 1.0
        self._pen_opacity = 1.0
        self._pressure_size = True
        self._pressure_opacity = False
        self._smoothing = 20.0
        self._draw_mode = 'brush'
        self._shape_constrain = False
        self._tablet_active = False
        self._composition_mode = 'off'
        self._source_pixmap = None          # ungraded frame, for re-grading
        self._color_params = None           # active display color correction
        self._guides = QtWidgets.QGraphicsItemGroup()
        self._guides.setZValue(8)
        self.scene().addItem(self._guides)
        self._guide_items = []

        # The cursor and its geometric clipping both use the same viewport-
        # pixel radius, converted to scene units at the current zoom.
        self._eraser_cursor = QtWidgets.QGraphicsEllipseItem()
        self._eraser_cursor.setZValue(30)
        eraser_pen = QtGui.QPen(QtGui.QColor(245, 245, 245, 230))
        eraser_pen.setCosmetic(True)
        self._eraser_cursor.setPen(eraser_pen)
        self._eraser_cursor.setBrush(QtGui.QColor(255, 255, 255, 36))
        self._eraser_cursor.hide()
        self.scene().addItem(self._eraser_cursor)

        self._placeholder = self._build_placeholder()

    # -- empty-state placeholder -----------------------------------------
    def _build_placeholder(self):
        icons = os.path.join(os.path.dirname(__file__), 'icons')
        frame = QtWidgets.QFrame(self)
        frame.setObjectName('vcPlaceholder')
        frame.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        frame.setStyleSheet('#vcPlaceholder { background: transparent; }')
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(40, 32, 40, 32)
        layout.setSpacing(18)
        layout.setAlignment(QtCore.Qt.AlignCenter)

        icon = QtWidgets.QLabel()
        icon.setAlignment(QtCore.Qt.AlignCenter)
        icon.setFixedSize(112, 112)
        icon.setPixmap(QtGui.QIcon(os.path.join(icons, 'placeholder.png')).pixmap(104, 104))
        layout.addWidget(icon, 0, QtCore.Qt.AlignCenter)

        title = QtWidgets.QLabel('Drop a video to review')
        title.setAlignment(QtCore.Qt.AlignCenter)
        title.setStyleSheet('font-size: 17pt; font-weight: bold; color: %s;' % style.TEXT)
        layout.addWidget(title)

        caption = QtWidgets.QLabel(
            'Drag a clip onto the viewport, or import one from disk. '
            'Image sequences and stills work too.')
        caption.setAlignment(QtCore.Qt.AlignCenter)
        caption.setWordWrap(True)
        caption.setFixedWidth(330)
        caption.setStyleSheet('font-size: 10pt; color: %s;' % style.TEXT_DIM)
        layout.addWidget(caption, 0, QtCore.Qt.AlignCenter)

        button = QtWidgets.QPushButton('Import Media…')
        button.setCursor(QtCore.Qt.PointingHandCursor)
        button.setFixedSize(180, 34)
        button.setIcon(QtGui.QIcon(os.path.join(icons, 'media.png')))
        button.clicked.connect(self.importRequested.emit)
        layout.addWidget(button, 0, QtCore.Qt.AlignCenter)
        frame.adjustSize()
        return frame

    def set_placeholder_visible(self, visible):
        self._placeholder.setVisible(visible)
        self._position_placeholder()

    def _position_placeholder(self):
        size = self._placeholder.sizeHint()
        self._placeholder.resize(size)
        self._placeholder.move((self.width() - size.width()) // 2,
                               (self.height() - size.height()) // 2)

    # -- drag and drop ----------------------------------------------------
    def _dropped_path(self, event):
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            path = url.toLocalFile()
            if path and os.path.splitext(path)[1].lower() in (utils.IMAGE_EXTS + utils.VIDEO_EXTS):
                return path
        return None

    def dragEnterEvent(self, event):
        event.acceptProposedAction() if self._dropped_path(event) else event.ignore()

    def dragMoveEvent(self, event):
        event.acceptProposedAction() if self._dropped_path(event) else event.ignore()

    def dropEvent(self, event):
        path = self._dropped_path(event)
        if path:
            event.acceptProposedAction()
            self.mediaDropped.emit(path)
        else:
            event.ignore()

    # -- media geometry / display ----------------------------------------
    def set_native_size(self, width, height):
        self._native = QtCore.QSizeF(width, height)
        self.scene().setSceneRect(0, 0, width, height)
        self._rebuild_guides()
        self._fit()

    def set_display(self, pixmap):
        """Show a pixmap (live frame or poster still), scaled+flipped to native."""
        if pixmap is None or pixmap.isNull():
            return
        self._source_pixmap = pixmap
        self.frame_item.setPixmap(self._graded(pixmap))
        self._apply_frame_transform()

    def set_color_correction(self, params):
        """Apply a non-destructive display grade (dict), or None to disable."""
        self._color_params = params
        if self._source_pixmap is not None and not self._source_pixmap.isNull():
            self.frame_item.setPixmap(self._graded(self._source_pixmap))

    def _graded(self, pixmap):
        if not self._color_params:
            return pixmap
        return color.grade_pixmap(pixmap, self._color_params)

    def clear(self):
        self._source_pixmap = None
        self.frame_item.setPixmap(QtGui.QPixmap())
        self.set_tracked_quad(None)
        self.set_crop(None)
        self.clear_drawings()

    def set_flip(self, flip_h, flip_v):
        flip_h, flip_v = bool(flip_h), bool(flip_v)
        change_h = self._flip_h != flip_h
        change_v = self._flip_v != flip_v
        if (change_h or change_v) and not self._native.isEmpty():
            # The scene uses displayed (post-flip) pixel coordinates, which is
            # also the frame of reference for FFmpeg's flip-then-crop export and
            # for optical flow tracking. Mirror existing overlays with the
            # image so they remain attached to the same visible subject.
            width, height = self._native.width(), self._native.height()

            def mirror_point(x, y):
                return (width - x if change_h else x,
                        height - y if change_v else y)

            def mirror_strokes(strokes):
                mirrored = []
                for stroke in strokes:
                    points = [(*mirror_point(x, y), pressure)
                              for x, y, pressure in stroke.get('points', ())]
                    updated = dict(stroke, points=points)
                    if stroke.get('kind') == 'text' and stroke.get('pos'):
                        px, py = stroke['pos']
                        updated['pos'] = list(mirror_point(float(px), float(py)))
                    mirrored.append(updated)
                return mirrored

            self._drawings = {frame: mirror_strokes(strokes)
                              for frame, strokes in self._drawings.items()}
            if self._pending_strokes is not None:
                self._pending_strokes = mirror_strokes(self._pending_strokes)
            self._active_points = [(*mirror_point(x, y), pressure)
                                   for x, y, pressure in self._active_points]
            if self._active_item is not None:
                self.scene().removeItem(self._active_item)
                self._active_item = None
                if self._active_points:
                    self._refresh_active_stroke()

            if self._tracked_quad.isVisible():
                polygon = QtGui.QPolygonF([
                    QtCore.QPointF(*mirror_point(point.x(), point.y()))
                    for point in self._tracked_quad.polygon()])
                self._tracked_quad.setPolygon(polygon)
            if self._has_crop:
                rect = self._crop.rect()
                x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
                if change_h:
                    x = width - (x + w)
                if change_v:
                    y = height - (y + h)
                self.set_crop(QtCore.QRectF(x, y, w, h))
            self._drag = None
            self._eraser_cursor.hide()
            self._rebuild_drawing_items()
            self.annotationFramesChanged.emit(self.annotation_frame_colors())
        self._flip_h, self._flip_v = flip_h, flip_v
        self._apply_frame_transform()

    def _apply_frame_transform(self):
        """Map the pixmap onto the native rect, mirrored as needed. The crop
        item stays in native coords, matching ffmpeg's flip-then-crop order."""
        pixmap = self.frame_item.pixmap()
        if pixmap.isNull() or self._native.isEmpty() or not pixmap.width() or not pixmap.height():
            return
        sx = self._native.width() / pixmap.width()
        sy = self._native.height() / pixmap.height()
        transform = QtGui.QTransform()
        if self._flip_h:
            transform.translate(self._native.width(), 0)
            transform.scale(-1, 1)
        if self._flip_v:
            transform.translate(0, self._native.height())
            transform.scale(1, -1)
        transform.scale(sx, sy)
        self.frame_item.setTransform(transform)

    # -- crop rectangle (native pixels) -----------------------------------
    def crop(self):
        if not self._has_crop:
            return None
        rect = self._crop.rect()
        return (int(round(rect.x())), int(round(rect.y())),
                int(round(rect.width())), int(round(rect.height())))

    def set_crop(self, rect, notify=True):
        """rect is (x, y, w, h) in native pixels, a QRectF, or None."""
        bounds = QtCore.QRectF(0, 0, self._native.width(), self._native.height())
        if rect is None or bounds.isEmpty():
            self._has_crop = False
            self._crop.hide()
            self.set_tracked_quad(None)
            if notify:
                self.cropChanged.emit(None)
            return
        if not isinstance(rect, QtCore.QRectF):
            x, y, w, h = rect
            rect = QtCore.QRectF(x, y, w, h)
        rect = rect.normalized().intersected(bounds)
        if rect.width() < 2 or rect.height() < 2:
            self._has_crop = False
            self._crop.hide()
            if notify:
                self.cropChanged.emit(None)
            return
        self._has_crop = True
        self._crop.setRect(rect)
        self._crop.setVisible(not self._crop_hidden)
        if notify:
            self.cropChanged.emit(QtCore.QRectF(rect))

    def set_crop_visible(self, visible):
        self._crop_hidden = not visible
        self._crop.setVisible(visible and self._has_crop)
        self._tracked_quad.setVisible(visible and len(self._tracked_quad.polygon()) == 4)

    def set_tracked_quad(self, points_or_none):
        """Show a tracking quadrilateral in post-flip native-pixel coordinates.

        Accepts four (x, y) tuples or QPointF objects; ``None`` removes the
        overlay. The polygon never intercepts crop/annotation mouse events.
        """
        if points_or_none is None:
            self._tracked_quad.hide()
            self._tracked_quad.setPolygon(QtGui.QPolygonF())
            self._crop.setOpacity(1.0)
            return
        points = []
        for point in points_or_none:
            if isinstance(point, QtCore.QPointF):
                points.append(QtCore.QPointF(point))
            else:
                x, y = point
                points.append(QtCore.QPointF(float(x), float(y)))
        if len(points) != 4:
            self.set_tracked_quad(None)
            return
        self._tracked_quad.setPolygon(QtGui.QPolygonF(points))
        self._tracked_quad.setVisible(not self._crop_hidden)
        # Keep the editable rectangular crop's hit-testing/handles available,
        # but make its box unobtrusive under the actual corner-pin polygon.
        self._crop.setOpacity(.22)

    # -- review tools ----------------------------------------------------
    def set_tool(self, tool):
        self._tool = str(tool)
        self._update_selection_box()
        self._apply_draw_cursor()

    def _apply_draw_cursor(self):
        """Pick the viewport cursor; the eraser hides it for the circle overlay."""
        erasing = self._tool == 'draw' and self._draw_mode == 'eraser'
        if erasing:
            self.viewport().setCursor(QtCore.Qt.BlankCursor)
        elif self._tool == 'draw' and self._draw_mode == 'text':
            self.viewport().setCursor(QtCore.Qt.IBeamCursor)
        elif self._tool in ('crop', 'draw'):
            self.viewport().setCursor(QtCore.Qt.CrossCursor)
        else:
            self.viewport().setCursor(QtCore.Qt.ArrowCursor)
        if not erasing:
            self._eraser_cursor.hide()

    def set_drawing_style(self, color=None, width=None, opacity=None,
                          pressure_size=None, pressure_opacity=None,
                          smoothing=None, eraser_size=None):
        if color is not None:
            self._pen_color = QtGui.QColor(color)
        if width is not None:
            self._pen_width = max(1.0, float(width))
        if opacity is not None:
            self._pen_opacity = utils.clamp(float(opacity), 0.01, 1.0)
        if pressure_size is not None:
            self._pressure_size = bool(pressure_size)
        if pressure_opacity is not None:
            self._pressure_opacity = bool(pressure_opacity)
        if smoothing is not None:
            self._smoothing = utils.clamp(float(smoothing), 0.0, 100.0)
        if eraser_size is not None:
            self._eraser_size = max(1.0, float(eraser_size))
            if self._eraser_cursor.isVisible():
                self._update_eraser_cursor(self._eraser_cursor.rect().center())

    def set_draw_mode(self, mode):
        mode = str(mode)
        self._draw_mode = mode if mode in (
            'select', 'brush', 'eraser', 'rectangle', 'ellipse', 'arrow',
            'text') else 'brush'
        self._update_selection_box()
        self._apply_draw_cursor()

    def set_current_frame(self, frame):
        if int(frame) != self._current_frame:
            self._selected_strokes.clear()
            self._selection_key = -1
            self._selection_drag = None
        self._current_frame = int(frame)
        self._rebuild_drawing_items()

    def set_onion_skin(self, enabled=None, before=None, after=None,
                       opacity=None, falloff=None, tint=None):
        """Display neighboring annotation keys without changing stored strokes."""
        if enabled is not None:
            self._onion_enabled = bool(enabled)
        if before is not None:
            self._onion_before = max(0, min(20, int(before)))
        if after is not None:
            self._onion_after = max(0, min(20, int(after)))
        if opacity is not None:
            self._onion_opacity = utils.clamp(float(opacity), 0.0, 1.0)
        if falloff is not None:
            self._onion_falloff = bool(falloff)
        if tint is not None:
            self._onion_tint = bool(tint)
        self._rebuild_drawing_items()

    def drawing_frames(self):
        return sorted(self._drawings)

    def annotation_frame_colors(self):
        """Per-key dominant visible stroke color for timeline indicators."""
        result = {}
        for frame, strokes in self._drawings.items():
            strengths = {}
            for stroke in strokes:
                points = stroke.get('points', [])
                if not points:
                    continue
                color = str(stroke.get('color', style.DRAW_DEFAULT))
                width = max(0.5, float(stroke.get('width', 3.0)))
                opacity = max(0.01, float(stroke.get('opacity', 1.0)))
                contours = self._stroke_polylines(stroke)
                length = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                             for contour in contours
                             for a, b in zip(contour, contour[1:]))
                strengths[color] = strengths.get(color, 0.0) + max(1.0, length) * width * opacity
            result[frame] = max(strengths, key=strengths.get) if strengths else None
        return result

    def drawing_data(self):
        """Return a detached, JSON-safe copy of all annotation keys."""
        return {int(frame): self._copy_strokes(strokes)
                for frame, strokes in self._drawings.items()}

    def set_drawing_data(self, drawings, replace=True):
        """Load validated annotation keys, replacing or merging existing notes."""
        if not isinstance(drawings, dict):
            raise ValueError('Notes must contain a frame-to-strokes mapping.')
        loaded = {}
        for frame, strokes in drawings.items():
            frame = int(frame)
            if frame < 0:
                continue
            clean_strokes = []
            for stroke in strokes:
                if not isinstance(stroke, dict):
                    raise ValueError('Every annotation stroke must be an object.')
                if stroke.get('kind') == 'text':
                    text = str(stroke.get('text', ''))
                    if not text.strip():
                        continue
                    pos = stroke.get('pos') or (0.0, 0.0)
                    clean_strokes.append(dict(
                        kind='text', text=text,
                        pos=[float(pos[0]), float(pos[1])],
                        color=str(stroke.get('color', style.DRAW_DEFAULT)),
                        opacity=utils.clamp(float(stroke.get('opacity', 1.0)), 0.01, 1.0),
                        size=max(2.0, float(stroke.get('size', 24.0))),
                        points=[], width=max(1.0, float(stroke.get('width', 3.0))),
                        pressure_size=False, pressure_opacity=False))
                    continue
                points = [(float(x), float(y), utils.clamp(float(pressure), 0.0, 1.0))
                          for x, y, pressure in stroke.get('points', [])]
                if not points:
                    continue
                clean_strokes.append(dict(
                    points=points,
                    color=str(stroke.get('color', style.DRAW_DEFAULT)),
                    width=max(1.0, float(stroke.get('width', 3.0))),
                    opacity=utils.clamp(float(stroke.get('opacity', 1.0)), 0.01, 1.0),
                    pressure_size=bool(stroke.get('pressure_size', False)),
                    pressure_opacity=bool(stroke.get('pressure_opacity', False)),
                    kind=(stroke.get('kind', 'brush') if stroke.get('kind', 'brush') in
                          ('brush', 'rectangle', 'ellipse', 'arrow') else 'brush')))
            # Empty keys intentionally interrupt a held annotation. In
            # particular, erasing its final fragment must not reveal the old
            # stroke again on subsequent frames.
            loaded[frame] = clean_strokes
        if replace:
            self._selected_strokes.clear()
            self._selection_drag = None
            self._drawings = loaded
        else:
            for frame, strokes in loaded.items():
                self._drawings.setdefault(frame, []).extend(strokes)
        self.annotationFramesChanged.emit(self.annotation_frame_colors())
        self._rebuild_drawing_items()

    def active_drawing_frame(self, frame=None):
        """Closest drawing key on the left, including the requested frame."""
        current = self._current_frame if frame is None else int(frame)
        keys = [key for key in self.drawing_frames() if key <= current]
        return max(keys) if keys else -1

    def clear_current_drawing(self):
        key = self.active_drawing_frame()
        if key >= 0:
            self._drawings.pop(key, None)
            self.annotationFramesChanged.emit(self.annotation_frame_colors())
            self._rebuild_drawing_items()
            self.edited.emit(key)
        else:
            self._rebuild_drawing_items()

    def create_blank_drawing(self):
        """Create an empty key on the playhead, ending any held annotation."""
        frame = self._current_frame
        if frame in self._drawings and not self._drawings[frame]:
            return
        self._drawings[frame] = []
        self._selected_strokes.clear()
        self._selection_drag = None
        self.annotationFramesChanged.emit(self.annotation_frame_colors())
        self._rebuild_drawing_items()
        self.edited.emit(frame)

    def undo_drawing_stroke(self):
        key = self.active_drawing_frame()
        strokes = self._drawings.get(key, []) if key >= 0 else []
        if strokes:
            strokes.pop()
            if not strokes:
                self._drawings.pop(key, None)
            self.annotationFramesChanged.emit(self.annotation_frame_colors())
            self._rebuild_drawing_items()
            self.edited.emit(key)

    def clear_drawings(self, record=True):
        if self._active_item is not None:
            self.scene().removeItem(self._active_item)
        self._active_item = None
        self._active_points = []
        self._pending_strokes = None
        self._erasing = False
        self._erase_changed = False
        self._erase_last_point = None
        had_drawings = bool(self._drawings)
        self._selected_strokes.clear()
        self._selection_key = -1
        self._selection_drag = None
        self._selection_marquee.hide()
        self._drawings.clear()
        self.annotationFramesChanged.emit({})
        self._rebuild_drawing_items()
        if record and had_drawings:
            self.edited.emit(self._current_frame)

    def _rebuild_drawing_items(self):
        self._remove_drawing_items()
        self._rebuild_onion_items()
        key = self.active_drawing_frame()
        strokes = self._drawings.get(key, []) if key >= 0 else []
        for stroke in strokes:
            item = self._make_stroke_item(stroke)
            self._drawing_items.append(item)
        if self._selection_key != key:
            self._selected_strokes.clear()
        self._selection_key = key
        self._selected_strokes.intersection_update(range(len(self._drawing_items)))
        self._update_selection_box()
        self.drawingChanged.emit(len(self._drawing_items))
        self.activeAnnotationChanged.emit(key, len(self._drawing_items))

    def _rebuild_onion_items(self):
        for item in self._onion_items:
            self.scene().removeItem(item)
        self._onion_items = []
        if not self._onion_enabled or self._onion_opacity <= 0:
            return
        # Held drawings are not new keys. Exclude the active key so the
        # current annotation never appears twice while it is held.
        active = self.active_drawing_frame()
        keys = self.drawing_frames()
        previous = [k for k in keys if k < self._current_frame and k != active]
        following = [k for k in keys if k > self._current_frame]
        previous_frames = (previous[-self._onion_before:]
                           if self._onion_before else [])
        following_frames = following[:self._onion_after]
        for frames, tint_color in ((previous_frames, style.ONION_PREV),
                                   (following_frames, style.ONION_NEXT)):
            frames = list(reversed(frames)) if tint_color == style.ONION_PREV else frames
            for distance, key in enumerate(frames, 1):
                factor = (1.0 / distance if self._onion_falloff else 1.0)
                for stroke in self._drawings[key]:
                    ghost = dict(stroke)
                    if self._onion_tint:
                        ghost['color'] = tint_color
                    item = self._make_stroke_item(ghost)
                    item.setOpacity(self._onion_opacity * factor)
                    item.setZValue(18)
                    item.setAcceptedMouseButtons(QtCore.Qt.NoButton)
                    self._onion_items.append(item)

    def _remove_drawing_items(self):
        """Remove only the committed annotation graphics from the scene."""
        for item in self._drawing_items:
            self.scene().removeItem(item)
        self._drawing_items = []

    def _copy_strokes(self, strokes):
        return [dict(stroke, points=list(stroke.get('points', [])))
                for stroke in strokes]

    def snapshot_drawings(self):
        """A deep copy of every frame's strokes, for the undo stack."""
        return {frame: self._copy_strokes(strokes)
                for frame, strokes in self._drawings.items()}

    def restore_drawings(self, snapshot):
        """Replace all drawings with a snapshot. Does not emit ``edited``."""
        self._selected_strokes.clear()
        self._selection_drag = None
        self._drawings = {frame: self._copy_strokes(strokes)
                          for frame, strokes in dict(snapshot or {}).items()}
        self.annotationFramesChanged.emit(self.annotation_frame_colors())
        self._rebuild_drawing_items()

    def _update_selection_box(self):
        key = self._selection_key
        strokes = self._drawings.get(key, []) if key >= 0 else []
        selected = [strokes[i] for i in sorted(self._selected_strokes)
                    if i < len(strokes)]
        visible = bool(selected) and self._tool == 'draw' and self._draw_mode == 'select'
        self._selection_box.setVisible(visible)
        for handle in self._selection_handles.values():
            handle.setVisible(visible)
        if not visible:
            return
        bounds = None
        for stroke in selected:
            points = [point for contour in self._stroke_polylines(stroke)
                      for point in contour]
            if not points:
                continue
            left = min(point[0] for point in points)
            right = max(point[0] for point in points)
            top = min(point[1] for point in points)
            bottom = max(point[1] for point in points)
            padding = self._scene_units(float(stroke.get('width', 3.0)) * 0.5)
            stroke_bounds = QtCore.QRectF(left - padding, top - padding,
                                          right - left + padding * 2.0,
                                          bottom - top + padding * 2.0)
            bounds = stroke_bounds if bounds is None else bounds.united(stroke_bounds)
        if bounds is None:
            self._selection_box.hide()
            for handle in self._selection_handles.values():
                handle.hide()
            return
        self._selection_box.setRect(bounds)
        radius = 5.0 / max(0.001, abs(self.transform().m11()))
        locations = {'nw': bounds.topLeft(), 'ne': bounds.topRight(),
                     'sw': bounds.bottomLeft(), 'se': bounds.bottomRight(),
                     'rotate': QtCore.QPointF(bounds.center().x(),
                         bounds.top() - 25.0 / max(0.001, abs(self.transform().m11())))}
        for name, point in locations.items():
            self._selection_handles[name].setRect(
                point.x()-radius, point.y()-radius, 2*radius, 2*radius)

    def _scene_units(self, viewport_pixels):
        """Convert a viewport-pixel measurement to native scene units."""
        return float(viewport_pixels) / max(0.001, abs(self.transform().m11()))

    @staticmethod
    def _point_segment_distance(point, first, second):
        """Shortest distance from ``point`` to a 2-D line segment."""
        px, py = point.x(), point.y()
        ax, ay = float(first[0]), float(first[1])
        bx, by = float(second[0]), float(second[1])
        dx, dy = bx - ax, by - ay
        length_squared = dx * dx + dy * dy
        if length_squared <= 1e-12:
            return math.hypot(px - ax, py - ay)
        amount = utils.clamp(((px - ax) * dx + (py - ay) * dy) /
                             length_squared, 0.0, 1.0)
        return math.hypot(px - (ax + amount * dx),
                          py - (ay + amount * dy))

    def _stroke_hit(self, stroke, point, tolerance_pixels=9.0):
        """Hit-test stored stroke geometry in screen pixels.

        QGraphicsItemGroup.shape() is empty or incomplete with cosmetic child
        pens in some Maya Qt builds. Selection must therefore use the same
        stored contours that draw and erase tools use, not Qt item internals.
        """
        tolerance = self._scene_units(
            float(tolerance_pixels) + float(stroke.get('width', 3.0)) * 0.5)
        contours = self._stroke_polylines(stroke)
        for contour in contours:
            if len(contour) == 1:
                if math.hypot(point.x() - contour[0][0],
                              point.y() - contour[0][1]) <= tolerance:
                    return True
            for first, second in zip(contour, contour[1:]):
                if self._point_segment_distance(point, first, second) <= tolerance:
                    return True
        return False

    @staticmethod
    def _segment_intersects_rect(first, second, rect):
        """Liang-Barsky segment/rectangle test without Qt enum differences."""
        x1, y1 = float(first[0]), float(first[1])
        x2, y2 = float(second[0]), float(second[1])
        dx, dy = x2 - x1, y2 - y1
        low, high = 0.0, 1.0
        for direction, distance in ((-dx, x1 - rect.left()),
                                    (dx, rect.right() - x1),
                                    (-dy, y1 - rect.top()),
                                    (dy, rect.bottom() - y1)):
            if abs(direction) <= 1e-12:
                if distance < 0.0:
                    return False
                continue
            ratio = distance / direction
            if direction < 0.0:
                low = max(low, ratio)
            else:
                high = min(high, ratio)
            if low > high:
                return False
        return True

    def _stroke_intersects_rect(self, stroke, rect):
        """Return whether a marquee touches any visible part of a stroke."""
        padding = self._scene_units(float(stroke.get('width', 3.0)) * 0.5)
        target = rect.adjusted(-padding, -padding, padding, padding)
        for contour in self._stroke_polylines(stroke):
            if any(target.contains(QtCore.QPointF(point[0], point[1]))
                   for point in contour):
                return True
            if any(self._segment_intersects_rect(first, second, target)
                   for first, second in zip(contour, contour[1:])):
                return True
        return False

    def _selection_hit(self, point):
        tolerance = self._scene_units(11.0)
        if self._selected_strokes:
            for name, handle in self._selection_handles.items():
                if handle.isVisible() and handle.rect().adjusted(
                        -tolerance, -tolerance, tolerance, tolerance).contains(point):
                    return name, None
        key = self.active_drawing_frame()
        strokes = self._drawings.get(key, []) if key >= 0 else []
        for index in reversed(range(len(strokes))):
            if self._stroke_hit(strokes[index], point):
                return 'stroke', index
        return 'empty', None

    def _selection_press(self, event):
        self._selection_press_at(self.mapToScene(event.pos()), event.modifiers())

    def _selection_press_at(self, point, modifiers=QtCore.Qt.NoModifier):
        """Begin a selection gesture from either a mouse or tablet event."""
        self.setFocus(QtCore.Qt.MouseFocusReason)
        action, index = self._selection_hit(point)
        add = bool(modifiers & QtCore.Qt.ShiftModifier)
        if action == 'stroke':
            if add:
                if index in self._selected_strokes:
                    self._selected_strokes.remove(index)
                else:
                    self._selected_strokes.add(index)
                self._update_selection_box()
                return
            if index not in self._selected_strokes:
                self._selected_strokes = {index}
                self._update_selection_box()
            action = 'move'
        elif action == 'empty':
            if not add:
                self._selected_strokes.clear()
                self._update_selection_box()
            self._selection_drag = ('marquee', point, add, None, None)
            self._selection_marquee.setRect(QtCore.QRectF(point, point))
            self._selection_marquee.show()
            return
        if not self._selected_strokes:
            return
        key = self.active_drawing_frame()
        originals = {i: self._copy_strokes([self._drawings[key][i]])[0]
                     for i in self._selected_strokes}
        box = QtCore.QRectF(self._selection_box.rect())
        self._selection_drag = (action, point, False, originals, box)

    def _selection_move(self, event):
        self._selection_move_at(self.mapToScene(event.pos()), event.modifiers())

    def _selection_move_at(self, point, modifiers=QtCore.Qt.NoModifier):
        """Update a selection gesture from either a mouse or tablet event."""
        if self._selection_drag is None:
            return
        action, origin, _add, originals, box = self._selection_drag
        if action == 'marquee':
            self._selection_marquee.setRect(QtCore.QRectF(origin, point).normalized())
            return
        center = box.center()
        if action == 'move':
            transform = lambda x,y: (x+point.x()-origin.x(), y+point.y()-origin.y())
        elif action == 'rotate':
            a0 = math.atan2(origin.y()-center.y(), origin.x()-center.x())
            a1 = math.atan2(point.y()-center.y(), point.x()-center.x())
            angle = a1-a0
            if modifiers & QtCore.Qt.ShiftModifier:
                increment = math.pi / 12.0
                angle = round(angle / increment) * increment
            cs, sn = math.cos(angle), math.sin(angle)
            transform = lambda x,y: (center.x()+(x-center.x())*cs-(y-center.y())*sn,
                                     center.y()+(x-center.x())*sn+(y-center.y())*cs)
        else:
            opposite = {'nw': box.bottomRight(), 'ne': box.bottomLeft(),
                        'sw': box.topRight(), 'se': box.topLeft()}[action]
            origin_dx = origin.x() - opposite.x()
            origin_dy = origin.y() - opposite.y()
            sx = ((point.x() - opposite.x()) / origin_dx
                  if abs(origin_dx) > 0.0001 else 1.0)
            sy = ((point.y() - opposite.y()) / origin_dy
                  if abs(origin_dy) > 0.0001 else 1.0)
            if modifiers & QtCore.Qt.ShiftModifier:
                value = sx if abs(sx) >= abs(sy) else sy
                sx = sy = value
            transform = lambda x,y: (opposite.x()+(x-opposite.x())*sx,
                                     opposite.y()+(y-opposite.y())*sy)
        key = self._selection_key
        if key not in self._drawings:
            return
        for index, stroke in originals.items():
            edited = self._copy_strokes([stroke])[0]
            edited['points'] = [tuple(transform(float(x),float(y))) + (p,)
                                for x,y,p in stroke.get('points', [])]
            self._drawings[key][index] = edited
        self._rebuild_drawing_items()

    def _selection_release(self, event):
        drag = self._selection_drag
        self._selection_drag = None
        if drag is None:
            return
        if drag[0] == 'marquee':
            bounds = self._selection_marquee.rect()
            self._selection_marquee.hide()
            key = self.active_drawing_frame()
            strokes = self._drawings.get(key, []) if key >= 0 else []
            # A click on empty space produces a zero-size marquee and should
            # only clear selection, never select a nearby bounding box.
            picked = ({i for i, stroke in enumerate(strokes)
                       if self._stroke_intersects_rect(stroke, bounds)}
                      if bounds.width() >= self._scene_units(2.0) or
                      bounds.height() >= self._scene_units(2.0) else set())
            self._selected_strokes.update(picked)
            self._update_selection_box()
        else:
            self._selection_move_final_notify(drag)

    def _selection_move_final_notify(self, drag):
        if drag[0] not in ('move','rotate','nw','ne','sw','se'):
            return
        key = self._selection_key
        originals = drag[3] or {}
        strokes = self._drawings.get(key, [])
        changed = any(index < len(strokes) and
                      strokes[index].get('points', []) != original.get('points', [])
                      for index, original in originals.items())
        if key >= 0 and changed:
            self.annotationFramesChanged.emit(self.annotation_frame_colors())
            self.edited.emit(key)

    def _make_stroke_item(self, stroke):
        group = QtWidgets.QGraphicsItemGroup()
        group.setZValue(20)
        self.scene().addItem(group)
        if stroke.get('kind') == 'text':
            color = QtGui.QColor(stroke.get('color', style.DRAW_DEFAULT))
            color.setAlphaF(utils.clamp(float(stroke.get('opacity', 1.0)), 0.02, 1.0))
            pos = stroke.get('pos') or (0.0, 0.0)
            item = QtWidgets.QGraphicsSimpleTextItem(str(stroke.get('text', '')))
            font = item.font()
            font.setPixelSize(max(2, int(round(float(stroke.get('size', 24.0))))))
            item.setFont(font)
            item.setBrush(QtGui.QBrush(color))
            item.setPos(float(pos[0]), float(pos[1]))
            group.addToGroup(item)
            return group
        points = stroke.get('points', [])
        if not points:
            return group
        base_width = float(stroke.get('width', 3.0))
        base_opacity = float(stroke.get('opacity', 1.0))
        use_size = bool(stroke.get('pressure_size', False))
        use_opacity = bool(stroke.get('pressure_opacity', False))
        kind = stroke.get('kind', 'brush')

        def values(pressure):
            pressure = utils.clamp(float(pressure), 0.0, 1.0)
            width = base_width * (0.1 + 0.9 * pressure) if use_size else base_width
            opacity = base_opacity * pressure if use_opacity else base_opacity
            return max(0.5, width), max(0.02, opacity)

        if kind in ('rectangle', 'ellipse', 'arrow') and len(points) >= 2:
            x1, y1, p1 = points[0]
            x2, y2, p2 = points[-1]
            width, opacity = values((p1 + p2) * 0.5)
            color = QtGui.QColor(stroke.get('color', style.DRAW_DEFAULT))
            color.setAlphaF(opacity)
            pen = QtGui.QPen(color, width, QtCore.Qt.SolidLine,
                             QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin)
            pen.setCosmetic(True)
            if kind == 'rectangle':
                item = QtWidgets.QGraphicsRectItem(
                    QtCore.QRectF(QtCore.QPointF(x1, y1),
                                  QtCore.QPointF(x2, y2)).normalized(), group)
                item.setPen(pen)
            elif kind == 'ellipse':
                item = QtWidgets.QGraphicsEllipseItem(
                    QtCore.QRectF(QtCore.QPointF(x1, y1),
                                  QtCore.QPointF(x2, y2)).normalized(), group)
                item.setPen(pen)
            else:
                shaft = QtGui.QPainterPath(QtCore.QPointF(x1, y1))
                shaft.lineTo(QtCore.QPointF(x2, y2))
                item = QtWidgets.QGraphicsPathItem(shaft, group)
                item.setPen(pen)
                angle = math.atan2(y2 - y1, x2 - x1)
                # Grow smoothly from zero for short arrows, then cap at a
                # restrained size so the head never overwhelms the shaft.
                # Arrowheads are capped in on-screen pixels like their
                # cosmetic outline, even when reviewing 4K/8K sources.
                screen_scale = max(0.0001, abs(self.transform().m11()))
                head = min(18.0 / screen_scale,
                           math.hypot(x2 - x1, y2 - y1) * 0.22)
                if head > 0.1:
                    for offset in (math.pi * 0.84, -math.pi * 0.84):
                        hx = x2 + math.cos(angle + offset) * head
                        hy = y2 + math.sin(angle + offset) * head
                        wing_path = QtGui.QPainterPath(QtCore.QPointF(x2, y2))
                        wing_path.lineTo(QtCore.QPointF(hx, hy))
                        wing = QtWidgets.QGraphicsPathItem(wing_path, group)
                        wing.setPen(pen)
            return group
        if len(points) == 1:
            # Shape tools have no meaningful geometry until the pointer moves;
            # do not flash a brush-sized dot on their initial press.
            if kind in ('rectangle', 'ellipse', 'arrow'):
                return group
            x, y, pressure = points[0]
            width, opacity = values(pressure)
            color = QtGui.QColor(stroke.get('color', style.DRAW_DEFAULT))
            color.setAlphaF(opacity)
            # Match the freehand stroke's cosmetic, round-cap pen exactly.
            # A tiny segment produces a stable dot without scene-scale jumps.
            path = QtGui.QPainterPath(QtCore.QPointF(x, y))
            path.lineTo(QtCore.QPointF(x + 0.01, y))
            dot = QtWidgets.QGraphicsPathItem(path, group)
            pen = QtGui.QPen(color, width, QtCore.Qt.SolidLine,
                             QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin)
            pen.setCosmetic(True)
            dot.setPen(pen)
            return group
        for first, second in zip(points, points[1:]):
            x1, y1, p1 = first
            x2, y2, p2 = second
            width, opacity = values((p1 + p2) * 0.5)
            color = QtGui.QColor(stroke.get('color', style.DRAW_DEFAULT))
            color.setAlphaF(opacity)
            path = QtGui.QPainterPath(QtCore.QPointF(x1, y1))
            path.lineTo(QtCore.QPointF(x2, y2))
            segment = QtWidgets.QGraphicsPathItem(path, group)
            pen = QtGui.QPen(color, width, QtCore.Qt.SolidLine,
                             QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin)
            pen.setCosmetic(True)
            segment.setPen(pen)
        return group

    def _start_stroke(self, point, pressure=1.0, eraser=False, constrain=False):
        if eraser or self._draw_mode == 'eraser':
            self._eraser_pressure = float(pressure)
            self._erasing = True
            self._erase_changed = False
            self._erase_last_point = QtCore.QPointF(point)
            self._erase_at(point)
            return
        if self._current_frame in self._drawings:
            self._pending_strokes = self._copy_strokes(
                self._drawings[self._current_frame])
        else:
            # Held annotations are display-only.  Beginning a note on a later
            # frame creates a new, clean annotation key instead of baking the
            # closest-left note into it.  Remove the held graphics immediately
            # so only the stroke currently being authored remains visible.
            self._pending_strokes = []
            self._remove_drawing_items()
            self.drawingChanged.emit(0)
            self.activeAnnotationChanged.emit(self._current_frame, 0)
        self._active_points = [(point.x(), point.y(), float(pressure))]
        self._shape_constrain = bool(constrain)
        self._refresh_active_stroke()

    def _continue_stroke(self, point, pressure=1.0, eraser=False, constrain=None):
        if eraser or self._draw_mode == 'eraser':
            self._eraser_pressure = float(pressure)
            self._update_eraser_cursor(point)
            previous = self._erase_last_point
            if previous is not None:
                length = math.hypot(point.x() - previous.x(),
                                    point.y() - previous.y())
                steps = max(1, int(math.ceil(length /
                                            max(1.0, self._eraser_radius() * 0.5))))
                samples = []
                for index in range(1, steps + 1):
                    t = index / float(steps)
                    samples.append(QtCore.QPointF(
                        previous.x() + (point.x() - previous.x()) * t,
                        previous.y() + (point.y() - previous.y()) * t))
                self._erase_path(samples)
            else:
                self._erase_at(point)
            self._erase_last_point = QtCore.QPointF(point)
            return
        if not self._active_points:
            return
        if self._draw_mode in ('rectangle', 'ellipse', 'arrow'):
            start = self._active_points[0]
            if constrain is not None:
                self._shape_constrain = bool(constrain)
            x, y = point.x(), point.y()
            if self._shape_constrain and self._draw_mode in ('rectangle', 'ellipse'):
                dx, dy = x - start[0], y - start[1]
                size = max(abs(dx), abs(dy))
                x = start[0] + (size if dx >= 0 else -size)
                y = start[1] + (size if dy >= 0 else -size)
            self._active_points = [start, (x, y, float(pressure))]
            self._refresh_active_stroke()
            return
        last_x, last_y, last_pressure = self._active_points[-1]
        strength = self._smoothing / 100.0
        blend = max(0.05, 1.0 - strength * 0.95)
        x = last_x + (point.x() - last_x) * blend
        y = last_y + (point.y() - last_y) * blend
        pressure = last_pressure + (float(pressure) - last_pressure) * blend
        if abs(x - last_x) + abs(y - last_y) < 0.15:
            return
        self._active_points.append((x, y, pressure))
        self._refresh_active_stroke()

    def _place_text(self, point):
        """Prompt for text and drop a text annotation at the scene point."""
        if self._native.isEmpty():
            return
        text, ok = QtWidgets.QInputDialog.getMultiLineText(
            self, 'Text Annotation', 'Annotation text:')
        if not ok or not text.strip():
            return
        # Size the text relative to the frame so it reads at any resolution.
        size = max(12.0, self._native.height() * 0.04)
        stroke = dict(kind='text', text=text,
                      pos=[float(point.x()), float(point.y())],
                      color=self._pen_color.name(), opacity=self._pen_opacity,
                      size=size, points=[], width=self._pen_width,
                      pressure_size=False, pressure_opacity=False)
        strokes = list(self._drawings.get(self._current_frame, []))
        strokes.append(stroke)
        self._drawings[self._current_frame] = strokes
        self._rebuild_drawing_items()
        self.annotationFramesChanged.emit(self.annotation_frame_colors())
        self.drawingChanged.emit(len(self._drawing_items))
        self.edited.emit(self._current_frame)

    def _active_record(self):
        return dict(points=list(self._active_points), color=self._pen_color.name(),
                    width=self._pen_width, opacity=self._pen_opacity,
                    pressure_size=self._pressure_size,
                    pressure_opacity=self._pressure_opacity,
                    kind=self._draw_mode)

    def _refresh_active_stroke(self):
        if self._active_item is not None:
            self.scene().removeItem(self._active_item)
        self._active_item = self._make_stroke_item(self._active_record())

    def _finish_stroke(self):
        if not self._active_points:
            return
        strokes = self._pending_strokes if self._pending_strokes is not None else []
        strokes.append(self._active_record())
        self._drawings[self._current_frame] = strokes
        self._pending_strokes = None
        self._active_points = []
        self._shape_constrain = False
        if self._active_item is not None:
            self.scene().removeItem(self._active_item)
        self._active_item = None
        self.annotationFramesChanged.emit(self.annotation_frame_colors())
        self._rebuild_drawing_items()
        self.edited.emit(self._current_frame)

    def _eraser_radius(self):
        """Visible eraser radius converted from viewport pixels to scene units."""
        scale = max(0.0001, abs(self.transform().m11()))
        pressure = (0.1 + 0.9 * utils.clamp(self._eraser_pressure, 0.0, 1.0)
                    if self._pressure_size else 1.0)
        return max(0.5, self._eraser_size * pressure / (2.0 * scale))

    def _stroke_polylines(self, stroke):
        """Represent a shape as editable contours when an eraser hits it."""
        points = stroke.get('points', [])
        if len(points) < 2:
            return [points]
        kind = stroke.get('kind', 'brush')
        if kind == 'rectangle':
            x1, y1, p1 = points[0]
            x2, y2, p2 = points[-1]
            pressure = (p1 + p2) * 0.5
            return [[(x1, y1, pressure), (x2, y1, pressure),
                     (x2, y2, pressure), (x1, y2, pressure),
                     (x1, y1, pressure)]]
        if kind == 'ellipse':
            x1, y1, p1 = points[0]
            x2, y2, p2 = points[-1]
            cx, cy = (x1 + x2) * 0.5, (y1 + y2) * 0.5
            rx, ry = abs(x2 - x1) * 0.5, abs(y2 - y1) * 0.5
            count = max(48, min(256, int(math.pi * (rx + ry) / 4.0)))
            pressure = (p1 + p2) * 0.5
            return [[(cx + rx * math.cos(2.0 * math.pi * i / count),
                      cy + ry * math.sin(2.0 * math.pi * i / count), pressure)
                     for i in range(count + 1)]]
        if kind == 'arrow':
            x1, y1, p1 = points[0]
            x2, y2, p2 = points[-1]
            angle = math.atan2(y2 - y1, x2 - x1)
            head = min(18.0, math.hypot(x2 - x1, y2 - y1) * 0.22)
            pressure = (p1 + p2) * 0.5
            parts = [[(x1, y1, pressure), (x2, y2, pressure)]]
            for offset in (math.pi * 0.84, -math.pi * 0.84):
                parts.append([(x2, y2, pressure),
                              (x2 + math.cos(angle + offset) * head,
                               y2 + math.sin(angle + offset) * head, pressure)])
            return parts
        return [points]

    @staticmethod
    def _clip_points_outside_circle(points, cx, cy, radius):
        """Trim covered pieces of a polyline, preserving interpolated pressure."""
        if not points:
            return [], False
        if len(points) == 1:
            x, y, _pressure = points[0]
            covered = (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2
            return ([] if covered else [points]), covered

        fragments = []
        current = []
        changed = False

        def interpolate(a, b, t):
            return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))

        for a, b in zip(points, points[1:]):
            dx, dy = b[0] - a[0], b[1] - a[1]
            qa = dx * dx + dy * dy
            qb = 2.0 * ((a[0] - cx) * dx + (a[1] - cy) * dy)
            qc = (a[0] - cx) ** 2 + (a[1] - cy) ** 2 - radius ** 2
            cuts = [0.0, 1.0]
            if qa > 1e-12:
                discriminant = qb * qb - 4.0 * qa * qc
                if discriminant > 0.0:
                    root = math.sqrt(discriminant)
                    cuts.extend(t for t in ((-qb - root) / (2.0 * qa),
                                             (-qb + root) / (2.0 * qa))
                                if 0.0 < t < 1.0)
            cuts.sort()
            for start, end in zip(cuts, cuts[1:]):
                mid = (start + end) * 0.5
                inside = ((a[0] + dx * mid - cx) ** 2 +
                          (a[1] + dy * mid - cy) ** 2 < radius ** 2)
                if inside:
                    changed = True
                    if current:
                        fragments.append(current)
                        current = []
                    continue
                start_point = interpolate(a, b, start)
                end_point = interpolate(a, b, end)
                if (current and
                        math.hypot(current[-1][0] - start_point[0],
                                   current[-1][1] - start_point[1]) > 1e-5):
                    fragments.append(current)
                    current = []
                if not current:
                    current.append(start_point)
                if math.hypot(current[-1][0] - end_point[0],
                              current[-1][1] - end_point[1]) > 1e-6:
                    current.append(end_point)
        if current:
            fragments.append(current)
        return [part for part in fragments if len(part) >= 2], changed

    def _erase_stroke(self, stroke, cx, cy, radius):
        # Both the displayed circle and clipping live in scene units; brush
        # widths are cosmetic screen pixels. Expanding by the brush width here
        # made the removed area much larger than the cursor, especially when
        # the viewer was fitted down from a high-resolution source.
        fragments = []
        changed = False
        for points in self._stroke_polylines(stroke):
            pieces, hit = self._clip_points_outside_circle(
                points, cx, cy, radius)
            changed = changed or hit
            fragments.extend(dict(stroke, points=part, kind='brush')
                             for part in pieces)
        return (fragments, True) if changed else ([stroke], False)

    def _update_eraser_cursor(self, scene_point):
        if self._native.isEmpty() or scene_point is None:
            self._eraser_cursor.hide()
            return
        radius = self._eraser_radius()
        self._eraser_cursor.setRect(scene_point.x() - radius, scene_point.y() - radius,
                                    radius * 2.0, radius * 2.0)
        self._eraser_cursor.show()

    def _erase_at(self, point):
        self._erase_path([point])

    def _erase_path(self, points):
        """Erase a complete pointer move, rebuilding graphics only once."""
        if not points:
            return
        key = self.active_drawing_frame()
        if key < 0:
            return
        radius = self._eraser_radius()
        strokes = self._drawings.get(key, [])
        changed_any = False
        for point in points:
            surviving = []
            for stroke in strokes:
                fragments, changed = self._erase_stroke(
                    stroke, point.x(), point.y(), radius)
                surviving.extend(fragments)
                changed_any = changed_any or changed
            strokes = surviving
            if not strokes:
                break
        if not changed_any:
            return
        # A held note is read-only. Start a key at the current frame when it
        # is edited, so the earlier note retains its original geometry.
        key = self._current_frame
        self._drawings[key] = strokes
        # An empty key is intentional: it masks preceding held annotations.
        self.annotationFramesChanged.emit(self.annotation_frame_colors())
        self._rebuild_drawing_items()
        self._erase_changed = True
        if not self._erasing:
            self.edited.emit(key)
            self._erase_changed = False

    def _finish_erase(self):
        if self._erasing and self._erase_changed:
            self.edited.emit(self._current_frame)
        self._erasing = False
        self._erase_changed = False
        self._erase_last_point = None
        self._eraser_pressure = 1.0

    # Composition guide modes shown over the frame. 'safe' reproduces the old
    # safe-areas-and-center behaviour; the rest are standard framing guides.
    COMPOSITION_MODES = ('off', 'thirds', 'golden', 'diagonal', 'center', 'safe')

    def set_composition_mode(self, mode):
        mode = mode if mode in self.COMPOSITION_MODES else 'off'
        if mode == self._composition_mode:
            return
        self._composition_mode = mode
        self._rebuild_guides()

    def composition_mode(self):
        return self._composition_mode

    def _rebuild_guides(self):
        for item in self._guide_items:
            item.setParentItem(None)
            self.scene().removeItem(item)
        self._guide_items = []
        mode = self._composition_mode
        if mode == 'off' or self._native.isEmpty():
            return
        bounds = QtCore.QRectF(0, 0, self._native.width(), self._native.height())
        pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 130))
        pen.setCosmetic(True)
        pen.setStyle(QtCore.Qt.DashLine)

        def add_line(x1, y1, x2, y2):
            item = QtWidgets.QGraphicsLineItem(QtCore.QLineF(x1, y1, x2, y2), self._guides)
            item.setPen(pen)
            self._guide_items.append(item)

        def add_rect(scale):
            w, h = bounds.width() * scale, bounds.height() * scale
            rect = QtCore.QRectF((bounds.width() - w) / 2.0,
                                 (bounds.height() - h) / 2.0, w, h)
            item = QtWidgets.QGraphicsRectItem(rect, self._guides)
            item.setPen(pen)
            self._guide_items.append(item)

        width, height = bounds.width(), bounds.height()
        if mode in ('thirds', 'golden'):
            fractions = ((1 / 3.0, 2 / 3.0) if mode == 'thirds'
                         else (0.382, 0.618))
            for fx in fractions:
                add_line(width * fx, 0, width * fx, height)
            for fy in fractions:
                add_line(0, height * fy, width, height * fy)
        elif mode == 'diagonal':
            add_line(0, 0, width, height)
            add_line(width, 0, 0, height)
        elif mode == 'center':
            add_line(width / 2.0, 0, width / 2.0, height)
            add_line(0, height / 2.0, width, height / 2.0)
        elif mode == 'safe':
            for scale in (0.8, 0.9):
                add_rect(scale)
            add_line(width / 2.0, 0, width / 2.0, height)
            add_line(0, height / 2.0, width, height / 2.0)

    # -- view behaviour ---------------------------------------------------
    def _fit(self):
        if not self._native.isEmpty():
            self.fitInView(self.sceneRect(), QtCore.Qt.KeepAspectRatio)
            self._rebuild_drawing_items()
            if self._eraser_cursor.isVisible():
                self._update_eraser_cursor(self._eraser_cursor.rect().center())

    def fit_view(self):
        self._fit()

    def _fit_scale(self):
        """The view scale at which the whole frame just fits the viewport."""
        if self._native.isEmpty():
            return 1.0
        viewport = self.viewport()
        return min(viewport.width() / self._native.width(),
                   viewport.height() / self._native.height())

    def _is_zoomed(self):
        return self.transform().m11() > self._fit_scale() * 1.001

    def _zoom(self, factor, to_cursor=True):
        """Scale the view by ``factor``, clamped to [fit, 40x fit].

        The frame never zooms out past a full fit, so the viewer cannot get
        lost in empty space. ``to_cursor`` anchors the zoom under the pointer
        (wheel / pinch); the toolbar buttons anchor on the viewport centre.
        """
        if self._native.isEmpty():
            return
        fit = self._fit_scale()
        current = self.transform().m11()
        target = utils.clamp(current * float(factor), fit, fit * 40.0)
        if current <= 0 or abs(target - current) < 1e-6:
            return
        anchor = (QtWidgets.QGraphicsView.AnchorUnderMouse if to_cursor
                  else QtWidgets.QGraphicsView.AnchorViewCenter)
        self.setTransformationAnchor(anchor)
        self.scale(target / current, target / current)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorViewCenter)
        self._rebuild_drawing_items()
        if self._eraser_cursor.isVisible():
            self._update_eraser_cursor(self._eraser_cursor.rect().center())

    def _pan_by(self, dx, dy):
        hbar = self.horizontalScrollBar()
        vbar = self.verticalScrollBar()
        hbar.setValue(hbar.value() - int(dx))
        vbar.setValue(vbar.value() - int(dy))

    def zoom_by(self, factor):
        self._zoom(factor, to_cursor=False)

    def resizeEvent(self, event):
        QtWidgets.QGraphicsView.resizeEvent(self, event)
        self._fit()
        self._position_placeholder()

    def showEvent(self, event):
        QtWidgets.QGraphicsView.showEvent(self, event)
        self._position_placeholder()

    def delete_selected_strokes(self):
        """Delete selected committed strokes as one undoable edit."""
        key = self._selection_key
        if not self._selected_strokes or key not in self._drawings:
            return
        self._drawings[key] = [
            stroke for index, stroke in enumerate(self._drawings[key])
            if index not in self._selected_strokes]
        self._selected_strokes.clear()
        # Keep an empty key to prevent an earlier held note reappearing.
        self.annotationFramesChanged.emit(self.annotation_frame_colors())
        self._rebuild_drawing_items()
        self.edited.emit(key)

    def keyPressEvent(self, event):
        if self._tool == 'draw' and self._draw_mode == 'select':
            if event.key() in (QtCore.Qt.Key_Delete, QtCore.Qt.Key_Backspace):
                self.delete_selected_strokes()
                event.accept()
                return
            if event.key() == QtCore.Qt.Key_Escape:
                self._selected_strokes.clear()
                self._selection_drag = None
                self._selection_marquee.hide()
                self._update_selection_box()
                event.accept()
                return
        QtWidgets.QGraphicsView.keyPressEvent(self, event)

    def mousePressEvent(self, event):
        if self._tablet_active:
            event.accept()
            return
        if event.button() == QtCore.Qt.MiddleButton and not self._native.isEmpty():
            self._panning = True
            self._pan_last = event.pos()
            self.viewport().setCursor(QtCore.Qt.ClosedHandCursor)
            event.accept()
            return
        if event.button() == QtCore.Qt.RightButton and self._tool == 'crop':
            self.set_crop(None)
            event.accept()
            return
        if (event.button() == QtCore.Qt.LeftButton and self._tool == 'draw'
                and self._draw_mode == 'select' and not self._native.isEmpty()):
            self._selection_press(event)
            event.accept()
            return
        if (event.button() == QtCore.Qt.LeftButton and self._tool == 'draw'
                and self._draw_mode == 'text' and not self._native.isEmpty()):
            self._place_text(self.mapToScene(event.pos()))
            event.accept()
            return
        if (event.button() == QtCore.Qt.LeftButton and self._tool == 'draw'
                and not self._native.isEmpty()):
            point = self.mapToScene(event.pos())
            self._start_stroke(
                point, 1.0, constrain=bool(
                    event.modifiers() & QtCore.Qt.ShiftModifier))
            event.accept()
            return
        if (event.button() == QtCore.Qt.LeftButton and self._tool == 'crop'
                and not self._native.isEmpty()):
            point = self.mapToScene(event.pos())
            current = self._crop.rect() if self._has_crop else None
            if current is not None and current.contains(point):
                self._drag = ('move', point, QtCore.QRectF(current))
            else:
                self._drag = ('draw', point, None)
                self.set_crop(None)
            event.accept()
            return
        QtWidgets.QGraphicsView.mousePressEvent(self, event)

    def mouseMoveEvent(self, event):
        if self._tablet_active:
            event.accept()
            return
        if self._panning:
            delta = event.pos() - self._pan_last
            self._pan_last = event.pos()
            self._pan_by(delta.x(), delta.y())
            event.accept()
            return
        if self._tool == 'draw' and self._draw_mode == 'select' and self._selection_drag:
            self._selection_move(event)
            event.accept()
            return
        if self._tool == 'draw' and self._draw_mode == 'eraser':
            scene_point = self.mapToScene(event.pos())
            self._update_eraser_cursor(scene_point)   # follow the pointer
            if event.buttons() & QtCore.Qt.LeftButton:
                self._continue_stroke(scene_point, eraser=True)
            event.accept()
            return
        if self._active_points:
            point = self.mapToScene(event.pos())
            self._continue_stroke(
                point, 1.0, constrain=bool(
                    event.modifiers() & QtCore.Qt.ShiftModifier))
            event.accept()
            return
        if self._drag:
            kind, start, original = self._drag
            point = self.mapToScene(event.pos())
            if kind == 'move':
                bounds = self.sceneRect()
                moved = QtCore.QRectF(original)
                moved.translate(point - start)
                moved.moveLeft(utils.clamp(moved.left(), 0, bounds.width() - moved.width()))
                moved.moveTop(utils.clamp(moved.top(), 0, bounds.height() - moved.height()))
                self.set_crop(moved)
            else:
                self.set_crop(QtCore.QRectF(start, point))
            event.accept()
            return
        QtWidgets.QGraphicsView.mouseMoveEvent(self, event)

    def mouseReleaseEvent(self, event):
        if self._tablet_active:
            event.accept()
            return
        if self._panning and event.button() == QtCore.Qt.MiddleButton:
            self._panning = False
            self.viewport().unsetCursor()
            event.accept()
            return
        if (event.button() == QtCore.Qt.LeftButton and self._selection_drag
                and self._tool == 'draw' and self._draw_mode == 'select'):
            self._selection_move(event)
            self._selection_release(event)
            event.accept()
            return
        if self._active_points:
            self._finish_stroke()
            event.accept()
            return
        if self._erasing and event.button() == QtCore.Qt.LeftButton:
            self._continue_stroke(self.mapToScene(event.pos()), eraser=True)
            self._finish_erase()
            event.accept()
            return
        self._drag = None
        QtWidgets.QGraphicsView.mouseReleaseEvent(self, event)

    def leaveEvent(self, event):
        self._eraser_cursor.hide()
        QtWidgets.QGraphicsView.leaveEvent(self, event)

    def _tablet_position(self, event):
        position = event.position() if hasattr(event, 'position') else (
            event.posF() if hasattr(event, 'posF') else event.pos())
        return self.mapToScene(position.toPoint())

    def _tablet_is_eraser(self, event):
        try:
            return 'eraser' in str(event.pointerType()).lower()
        except Exception:
            return False

    def tabletEvent(self, event):
        """Consume native tablet events so pressure is not flattened to mouse input."""
        if self._tool != 'draw' or self._native.isEmpty():
            QtWidgets.QGraphicsView.tabletEvent(self, event)
            return
        event_type = event.type()
        press = getattr(QtCore.QEvent, 'TabletPress', None)
        move = getattr(QtCore.QEvent, 'TabletMove', None)
        release = getattr(QtCore.QEvent, 'TabletRelease', None)
        if press is None and hasattr(QtCore.QEvent, 'Type'):
            press = QtCore.QEvent.Type.TabletPress
            move = QtCore.QEvent.Type.TabletMove
            release = QtCore.QEvent.Type.TabletRelease
        pressure = utils.clamp(float(event.pressure()), 0.0, 1.0)
        eraser = self._tablet_is_eraser(event)
        point = self._tablet_position(event)
        self.tabletPressureChanged.emit(pressure)
        if self._draw_mode == 'select':
            if event_type == press:
                self._tablet_active = True
                self._selection_press_at(point, event.modifiers())
            elif event_type == move and self._tablet_active:
                self._selection_move_at(point, event.modifiers())
            elif event_type == release and self._tablet_active:
                self._selection_move_at(point, event.modifiers())
                self._selection_release(event)
                self._tablet_active = False
                self.tabletPressureChanged.emit(0.0)
            event.accept()
            return
        if event_type == press:
            self._tablet_active = True
            self._start_stroke(
                point, pressure, eraser,
                bool(event.modifiers() & QtCore.Qt.ShiftModifier))
        elif event_type == move and self._tablet_active:
            self._continue_stroke(
                point, pressure, eraser,
                bool(event.modifiers() & QtCore.Qt.ShiftModifier))
        elif event_type == release:
            if self._active_points:
                self._finish_stroke()
            elif self._erasing:
                self._continue_stroke(point, eraser=True)
                self._finish_erase()
            self._tablet_active = False
            self.tabletPressureChanged.emit(0.0)
        event.accept()

    def viewportEvent(self, event):
        event_type = event.type()
        tablet_types = [getattr(QtCore.QEvent, name, None)
                        for name in ('TabletPress', 'TabletMove', 'TabletRelease')]
        if hasattr(QtCore.QEvent, 'Type'):
            tablet_types += [getattr(QtCore.QEvent.Type, name, None)
                             for name in ('TabletPress', 'TabletMove', 'TabletRelease')]
        # viewportEvent can fire during construction, before __init__ finishes
        # setting these attributes; fall back rather than raise.
        if getattr(self, '_tool', 'select') == 'draw' and event_type in tablet_types:
            self.tabletEvent(event)
            return True
        native = getattr(QtCore.QEvent, 'NativeGesture', None)
        if native is None and hasattr(QtCore.QEvent, 'Type'):
            native = getattr(QtCore.QEvent.Type, 'NativeGesture', None)
        if native is not None and event_type == native:
            if self._native_gesture(event):
                return True
        return QtWidgets.QGraphicsView.viewportEvent(self, event)

    def _native_gesture(self, event):
        """Trackpad pinch-to-zoom (and two-finger double-tap smart zoom)."""
        try:
            gesture = event.gestureType()
        except AttributeError:
            return False
        if gesture == QtCore.Qt.ZoomNativeGesture:
            # value() is the incremental magnification for this event.
            self._zoom(1.0 + event.value(), to_cursor=True)
            event.accept()
            return True
        if gesture == QtCore.Qt.SmartZoomNativeGesture:
            self._fit() if self._is_zoomed() else self._zoom(3.0, to_cursor=True)
            event.accept()
            return True
        return False

    def wheelEvent(self, event):
        # Ctrl + wheel (mouse or trackpad): zoom toward the cursor.
        if event.modifiers() & QtCore.Qt.ControlModifier:
            delta = event.angleDelta().y() or event.pixelDelta().y()
            if delta:
                self._zoom(1.0015 ** delta, to_cursor=True)
            event.accept()
            return
        # Trackpad two-finger drag while zoomed in: pan the frame.
        pixel = event.pixelDelta()
        if self._is_zoomed() and not pixel.isNull():
            self._pan_by(pixel.x(), pixel.y())
            event.accept()
            return
        # Otherwise the wheel scrubs the clip, exactly as before.
        delta = event.angleDelta().y()
        if delta:
            step = 10 if event.modifiers() & QtCore.Qt.ShiftModifier else 1
            self.scrubRequested.emit(step if delta > 0 else -step)
        event.accept()
