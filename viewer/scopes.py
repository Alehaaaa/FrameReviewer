# -*- coding: utf-8 -*-
"""Cancellable, bounded, Qt-optional image scopes for Maya review.

``compute_scopes`` is pure NumPy, suitable for an ordinary Python worker.
``ScopesDialog`` snapshots QImages on the GUI thread, coalesces frame changes
into a single pending task, and builds all Qt images/widgets on the GUI thread.

Scopes describe the incoming pixel stage.  QImages are display-referred;
FramePayload stages are explicit.  To inspect display values from a source or
working FramePayload, supply ``color_state`` (managed OCIO if applicable).
"""
from __future__ import absolute_import

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import math
import threading

try:
    import numpy as np
except ImportError:
    np = None

try:
    from ..core.qtcompat import QtCore, QtGui, QtWidgets, Signal
except ImportError:
    QtCore = QtGui = QtWidgets = Signal = None


SCOPE_BINS = 256
DEFAULT_MAX_PIXELS = 131072
_RGB_WEIGHTS = (0.2126, 0.7152, 0.0722)


class ScopeCancelled(Exception):
    """A superseded frame was cancelled between NumPy processing stages."""


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise ScopeCancelled()


def _frozen(array):
    """Return an immutable, buffer-owned result safe for queued Qt signals."""
    arr = np.ascontiguousarray(array)
    return np.frombuffer(arr.tobytes(), dtype=arr.dtype).reshape(arr.shape)


def _downsample(pixels, max_pixels):
    if max_pixels < 1:
        raise ValueError('max_pixels must be positive.')
    height, width = pixels.shape[:2]
    stride = max(1, int(math.ceil(math.sqrt(height * width / float(max_pixels)))))
    # Row/column stride retains deterministic spatial alignment for waveform.
    # A prime/non-square dimension may yield slightly more than max_pixels;
    # truncate the flattened sample only after preserving its column indices.
    return pixels[::stride, ::stride], stride


@dataclass(frozen=True)
class ScopeData:
    """Pure numeric analysis, ordered histogram channels R/G/B/Y."""
    histogram: object
    waveform: object
    vectorscope: object
    sampled_pixels: int
    original_pixels: int
    under_range: float
    over_range: float
    stage: str = 'display'
    source_id: str = ''
    source_frame: int = 0
    frame_generation: int = 0
    request_generation: int = 0


def compute_scopes(pixels, max_pixels=DEFAULT_MAX_PIXELS, cancel=None,
                   stage='display', source_id='', source_frame=0,
                   frame_generation=0, request_generation=0,
                   color_state=None, source_stage=None, alpha_mode='straight'):
    """Compute RGB/Y histogram, luma waveform and Rec.709 Cb/Cr vectorscope.

    Arrays are 256 bins / 256x256 integer density maps. Input integer pixels
    are normalized by dtype max; floating point HDR/negative samples remain
    unmodified for under/over reporting, then clamp for plotted axes.

    Cancellations are checked around all expensive vectorized passes. Source
    arrays never change. All output arrays are immutable, owned CPU memory.
    """
    if np is None:
        raise RuntimeError('NumPy is required for image scopes.')
    if stage not in ('source', 'working', 'display'):
        raise ValueError('Unknown scope stage {}'.format(stage))
    _check_cancel(cancel)
    src = np.asarray(pixels)
    if src.ndim not in (2, 3) or not src.size or (src.ndim == 3 and src.shape[2] not in (1, 2, 3, 4)):
        raise ValueError('Expected non-empty HxW image with 1 to 4 channels.')
    if src.dtype.kind not in 'uif':
        raise TypeError('Expected unsigned/signed integer or floating-point pixels.')
    original_pixels = int(src.shape[0] * src.shape[1])
    view, stride = _downsample(src, max_pixels)
    # Bound all work after tiling the frame. Strictly bounded even for skinny
    # or non-divisible dimensions, unlike a stride-only sqrt approximation.
    if view.shape[0] * view.shape[1] > max_pixels:
        # Subsample a deterministic 1D projection while retaining x positions.
        _, xx = np.indices(view.shape[:2])
        pick = np.linspace(0, view.size // (view.shape[2] if view.ndim == 3 else 1) - 1,
                           max_pixels, dtype=np.int64)
        xs = xx.reshape(-1)[pick]
        view = view.reshape(-1, view.shape[2])[pick] if view.ndim == 3 else view.reshape(-1)[pick]
    else:
        xs = np.broadcast_to(np.arange(view.shape[1])[None, :],
                             view.shape[:2]).reshape(-1)
        view = view.reshape(-1, view.shape[2]) if view.ndim == 3 else view.reshape(-1)
    columns = max(1, int(math.ceil(src.shape[1] / float(stride))))
    _check_cancel(cancel)
    if view.dtype.kind in 'ui':
        arr = view.astype(np.float32) / float(np.iinfo(view.dtype).max)
    else:
        arr = view.astype(np.float32)
    if arr.ndim == 1:
        rgb = np.repeat(arr[:, None], 3, axis=1)
    elif arr.shape[1] in (1, 2):
        rgb = np.repeat(arr[:, :1], 3, axis=1)
    else:
        rgb = np.ascontiguousarray(arr[:, :3])
    # Scope calculations require straight RGB. Transparent pixels keep their
    # color when represented as straight and are unpremultiplied otherwise.
    if alpha_mode == 'premultiplied' and arr.ndim == 2 and arr.shape[1] in (2, 4):
        alpha = arr[:, -1:]
        rgb = np.divide(rgb, alpha, out=np.zeros_like(rgb), where=alpha > 1e-8)
    if color_state is not None:
        from .color import process_pixels
        # Process already downsampled RGB, so a LUT/OCIO never blocks playback.
        rgb = process_pixels(rgb.reshape(1, -1, 3), color_state, stage=stage,
                             source_stage=source_stage or 'source')[0]
    _check_cancel(cancel)
    finite = np.all(np.isfinite(rgb), axis=1)
    total = int(np.count_nonzero(finite))
    if not total:
        raise ValueError('Image contains no finite RGB pixels.')
    rgb = rgb[finite]
    xs = xs[finite]
    over = float(np.count_nonzero(np.any(rgb > 1.0, axis=1))) / total
    under = float(np.count_nonzero(np.any(rgb < 0.0, axis=1))) / total
    rgb = np.clip(rgb, 0.0, 1.0)
    luma = rgb.dot(np.asarray(_RGB_WEIGHTS, np.float32))
    color_indices = np.minimum((rgb * 255.0).astype(np.int32), 255)
    lum_bins = np.minimum((luma * 255.0).astype(np.int32), 255)
    hist = np.zeros((4, SCOPE_BINS), np.int32)
    for i in range(3):
        hist[i] = np.bincount(color_indices[:, i], minlength=SCOPE_BINS)
    hist[3] = np.bincount(lum_bins, minlength=SCOPE_BINS)
    _check_cancel(cancel)
    wave_x = np.minimum((xs * SCOPE_BINS // columns).astype(np.int32), 255)
    wave_y = 255 - lum_bins
    waveform = np.bincount(wave_y * 256 + wave_x,
                           minlength=256 * 256).reshape((256, 256)).astype(np.int32)
    _check_cancel(cancel)
    # Rec.709 chroma differences, centered neutral at (128, 128).
    cb = (rgb[:, 2] - luma) / 1.8556
    cr = (rgb[:, 0] - luma) / 1.5748
    vec_x = np.clip(((0.5 + cb) * 255.0).astype(np.int32), 0, 255)
    vec_y = np.clip(((0.5 - cr) * 255.0).astype(np.int32), 0, 255)
    vectorscope = np.bincount(vec_y * 256 + vec_x,
                              minlength=256 * 256).reshape((256, 256)).astype(np.int32)
    _check_cancel(cancel)
    return ScopeData(_frozen(hist), _frozen(waveform), _frozen(vectorscope),
                     int(total), original_pixels, under, over, stage,
                     str(source_id or ''), int(source_frame),
                     int(frame_generation), int(request_generation))


if QtCore is not None:
    class _ResultBridge(QtCore.QObject):
        finished = Signal(int, object, str)


class ScopesDialog(QtWidgets.QDialog if QtWidgets else object):
    """Non-modal RGB histogram, waveform, vectorscope panel.

    Main-thread API: ``submit_frame(QImage | QPixmap | FramePayload, *,
    color_state=None, source_id=None, source_frame=None, stage=None)``.
    No work is queued while hidden. ``visibilityChanged(True)`` requests a
    current-frame refresh from the host, whose frame update callback should
    call ``submit_frame`` only while this dialog is visible.

    Close calls ``shutdown``. It is safe and idempotent on Maya reload.
    """

    if Signal is not None:
        visibilityChanged = Signal(bool)
        scopesReady = Signal(object)
        stageChanged = Signal(str)
        memberChanged = Signal(int)
        domainChanged = Signal(str)

    def __init__(self, parent=None, max_pixels=DEFAULT_MAX_PIXELS):
        if QtWidgets is None:
            raise RuntimeError('ScopesDialog requires PySide2 or PySide6.')
        if np is None:
            raise RuntimeError('ScopesDialog requires NumPy.')
        if not 1 <= int(max_pixels) <= 1048576:
            raise ValueError('max_pixels must be between 1 and 1048576.')
        super(ScopesDialog, self).__init__(parent)
        self.setWindowTitle('Image Scopes')
        self.setObjectName('FrameReviewerScopesDialog')
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, False)
        self.setMinimumSize(690, 300)
        self.resize(990, 380)
        self._max_pixels = int(max_pixels)
        self._generation = 0
        self._cancel = None
        self._busy = False
        self._pending = None
        self._closed = False
        self._last_result = None
        self._scope_domain_note = ''
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='FrameReviewerScopes')
        self._bridge = _ResultBridge(self)
        self._bridge.finished.connect(self._receive, QtCore.Qt.QueuedConnection)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(9)
        header = QtWidgets.QHBoxLayout()
        self.status_label = QtWidgets.QLabel('Open a frame to inspect its scopes')
        self.status_label.setObjectName('scopesStatus')
        header.addWidget(self.status_label, 1)
        header.addWidget(QtWidgets.QLabel('Pixel stage:'))
        self.stage_combo = QtWidgets.QComboBox()
        self.stage_combo.setObjectName('frameReviewerScopesStage')
        for title, stage in (('Display (actual SDR)', 'display'),
                             ('Working (HDR)', 'working'),
                             ('Source (original)', 'source')):
            self.stage_combo.addItem(title, stage)
        self.stage_combo.setToolTip(
            'Display samples the actual SDR viewer; Working and Source use '
            'native FramePayload samples when available. No HDR hardware readback.')
        self.stage_combo.currentIndexChanged.connect(
            lambda *_: self.stageChanged.emit(str(self.stage_combo.currentData())))
        header.addWidget(self.stage_combo)
        header.addWidget(QtWidgets.QLabel('Member:'))
        self.member_combo = QtWidgets.QComboBox()
        self.member_combo.setObjectName('frameReviewerScopesMember')
        self.member_combo.addItem('Composited output', -1)
        self.member_combo.setToolTip(
            'Inspect each source before compositing. Source values retain '
            'their original color interpretation; Working values are '
            'converted to shared working light before the common grade.')
        self.member_combo.currentIndexChanged.connect(
            lambda *_: self.memberChanged.emit(int(self.member_combo.currentData())))
        header.addWidget(self.member_combo)
        header.addWidget(QtWidgets.QLabel('EXR coverage:'))
        self.domain_combo = QtWidgets.QComboBox()
        self.domain_combo.setObjectName('frameReviewerScopesExrCoverage')
        self.domain_combo.addItem('Display canvas', 'canvas')
        self.domain_combo.addItem('Source data only', 'data')
        self.domain_combo.setToolTip(
            'For rectified native EXR, exclude intentionally blank '
            'displayWindow padding from Source/Working histograms and waveform. '
            'Display always scopes the actual presented SDR canvas.')
        self.domain_combo.currentIndexChanged.connect(
            lambda *_: self.domainChanged.emit(str(self.domain_combo.currentData())))
        header.addWidget(self.domain_combo)
        root.addLayout(header)

        self._scope_labels = {}
        group = QtWidgets.QHBoxLayout()
        group.setSpacing(10)
        for title, key in (('Histogram · RGB / Y', 'histogram'),
                           ('Waveform · Luminance', 'waveform'),
                           ('Vectorscope · Rec.709', 'vectorscope')):
            col = QtWidgets.QVBoxLayout()
            head = QtWidgets.QLabel(title)
            head.setAlignment(QtCore.Qt.AlignCenter)
            plot = QtWidgets.QLabel()
            plot.setMinimumSize(192, 192)
            plot.setAlignment(QtCore.Qt.AlignCenter)
            plot.setScaledContents(True)
            plot.setStyleSheet('background:#14181f;border:1px solid #38414d;')
            plot.setObjectName('scopes_' + key)
            col.addWidget(head)
            col.addWidget(plot, 1)
            group.addLayout(col, 1)
            self._scope_labels[key] = plot
        root.addLayout(group, 1)
        self.metrics_label = QtWidgets.QLabel('Range: —   Samples: —')
        root.addWidget(self.metrics_label)

    @property
    def selected_stage(self):
        return str(self.stage_combo.currentData())

    @property
    def selected_member(self):
        return int(self.member_combo.currentData())

    @property
    def selected_domain(self):
        return str(self.domain_combo.currentData())

    def set_members(self, members):
        """Refresh available sources only when their stable identity changes."""
        self._assert_gui_thread()
        items = [('Composited output', -1)]
        items += [(str(name), i) for i, name in enumerate(members or ())]
        current = [(self.member_combo.itemText(i),
                    self.member_combo.itemData(i))
                   for i in range(self.member_combo.count())]
        if items == current:
            return False
        previous = self.selected_member
        self.member_combo.blockSignals(True)
        try:
            self.member_combo.clear()
            for label, index in items:
                self.member_combo.addItem(label, index)
            next_index = self.member_combo.findData(previous)
            self.member_combo.setCurrentIndex(max(0, next_index))
        finally:
            self.member_combo.blockSignals(False)
        return True

    @property
    def latest_result(self):
        return self._last_result

    @property
    def request_generation(self):
        return self._generation

    def _assert_gui_thread(self):
        if QtCore.QThread.currentThread() != self.thread():
            raise RuntimeError('ScopesDialog must be called on the GUI thread.')

    @staticmethod
    def _snapshot_qimage(frame, max_pixels):
        """Qt conversion and pointer lifetime are confined to the GUI thread."""
        if frame.isNull():
            raise ValueError('Cannot scope a null QImage.')
        image = frame.convertToFormat(QtGui.QImage.Format_RGBA8888)
        width, height = image.width(), image.height()
        stride = max(1, int(math.ceil(math.sqrt(width * height / float(max_pixels)))))
        ptr = image.constBits()
        size = image.bytesPerLine() * height
        try:
            ptr.setsize(size)  # PySide2
        except AttributeError:
            pass  # PySide6 memoryview
        rows = np.frombuffer(ptr, dtype=np.uint8, count=size).reshape(height, image.bytesPerLine())
        sampled = rows[::stride, :width * 4].reshape(
            (int(math.ceil(height / float(stride))), width, 4))[:, ::stride, :].copy()
        # Source dimensions/stride are retained for correct waveform X bins.
        return sampled, width * height

    def submit_frame(self, frame, color_state=None, source_id=None,
                     source_frame=None, stage=None):
        """Queue the newest visible frame; cancels old source/frame generations.

        ``QImage`` and ``QPixmap`` are display-referred and snapshotted before
        dispatch. ``FramePayload`` keeps source IDs and frame generation;
        provide ``color_state`` and ``stage='display'`` to view transformed
        scopes. Work is coalesced: at most one active task and one latest pending.
        """
        self._assert_gui_thread()
        if self._closed or not self.isVisible():
            return False
        if isinstance(frame, QtGui.QPixmap):
            frame = frame.toImage()
        payload = hasattr(frame, 'pixels') and hasattr(frame, 'stage')
        if not payload and not isinstance(frame, QtGui.QImage):
            raise TypeError('Expected QImage, QPixmap or FramePayload.')
        if payload:
            source_id = frame.source_id if source_id is None else source_id
            source_frame = frame.source_frame if source_frame is None else source_frame
            stage = frame.stage if stage is None else stage
            frame_generation = frame.generation
            if stage != frame.stage and color_state is None:
                raise ValueError('ColorPipelineState is required to change pixel stage.')
            pixels = frame.pixels
            source_stage = frame.stage
            alpha_mode = frame.alpha_mode
            original_pixels = 0
            if frame.color_metadata.get('exr_canvas_rectified'):
                from ..media.window_geometry import DisplayCanvasPlacement
                placement = DisplayCanvasPlacement.from_geometry(frame.geometry)
                if (self.selected_domain == 'data' and
                        stage in ('source', 'working')):
                    pixels = pixels[placement.canvas_slices]
                    self._scope_domain_note = ' · EXR native data coverage'
                else:
                    self._scope_domain_note = ' · EXR display canvas incl. padding'
            else:
                self._scope_domain_note = ''
        else:
            stage = stage or 'display'
            if stage != 'display':
                raise ValueError('Qt images are display-referred.')
            pixels, original_pixels = self._snapshot_qimage(frame, self._max_pixels)
            source_id = source_id or ''
            source_frame = source_frame or 0
            frame_generation = 0
            source_stage = 'display'
            alpha_mode = 'straight'
            self._scope_domain_note = ''
        if color_state is not None:
            from .color import ColorPipelineState
            color_state = ColorPipelineState.from_dict(color_state)
        self._generation += 1
        if self._cancel is not None:
            self._cancel.set()
        cancel = threading.Event()
        self._cancel = cancel
        req = (pixels, self._max_pixels, cancel, stage, source_id,
               source_frame, frame_generation, self._generation,
               color_state if stage != source_stage else None,
               source_stage, alpha_mode, original_pixels)
        self._pending = req
        self.status_label.setText('Analyzing {} · frame {} · {} …'.format(
            source_id or 'current view', source_frame, stage.title()))
        self._launch_pending()
        return True

    def _launch_pending(self):
        self._assert_gui_thread()
        if self._busy or self._closed or not self.isVisible() or self._pending is None:
            return
        req, self._pending = self._pending, None
        self._busy = True
        bridge = self._bridge

        def work():
            pixels, limit, cancel, stage, srcid, frame, framegen, generation, color, srcstage, alpha, total = req
            try:
                result = compute_scopes(
                    pixels, max_pixels=limit, cancel=cancel,
                    stage=stage, source_id=srcid, source_frame=frame,
                    frame_generation=framegen, request_generation=generation,
                    color_state=color, source_stage=srcstage, alpha_mode=alpha)
                if total:
                    # QImage snapshots already decimated on the main thread.
                    result = ScopeData(result.histogram, result.waveform,
                                       result.vectorscope, result.sampled_pixels,
                                       total, result.under_range, result.over_range,
                                       result.stage, result.source_id, result.source_frame,
                                       result.frame_generation, result.request_generation)
                reason = ''
            except ScopeCancelled:
                result, reason = None, ''
            except Exception as exc:
                result, reason = None, '{}: {}'.format(type(exc).__name__, exc)
            # Worker never calls QWidget/QLabel/QImage or constructs a QPixmap.
            try:
                bridge.finished.emit(generation, result, reason)
            except RuntimeError:
                pass  # QObject disposed during Maya shutdown.

        try:
            self._executor.submit(work)
        except RuntimeError:
            self._busy = False

    def _receive(self, generation, result, error):
        self._assert_gui_thread()
        self._busy = False
        if self._closed:
            return
        if generation == self._generation and self.isVisible():
            if error:
                self.status_label.setText('Scopes unavailable: ' + error)
            elif result is not None:
                self._last_result = result
                self._draw(result)
                self.scopesReady.emit(result)
        self._launch_pending()

    @staticmethod
    def _base_plot():
        pixmap = QtGui.QPixmap(256, 256)
        pixmap.fill(QtGui.QColor('#14181f'))
        return pixmap

    @staticmethod
    def _grid(painter, cross=False):
        painter.setPen(QtGui.QPen(QtGui.QColor('#38414d'), 1))
        for point in (64, 128, 192):
            painter.drawLine(point, 0, point, 255)
            painter.drawLine(0, point, 255, point)
        if cross:
            painter.setPen(QtGui.QPen(QtGui.QColor('#677480'), 1))
            painter.drawEllipse(QtCore.QPoint(128, 128), 64, 64)
            painter.drawEllipse(QtCore.QPoint(128, 128), 120, 120)

    @classmethod
    def _histogram_pixmap(cls, result):
        pixmap = cls._base_plot()
        painter = QtGui.QPainter(pixmap)
        try:
            cls._grid(painter)
            colors = ('#e86b70', '#62c68e', '#75aaf8', '#d4dce5')
            maximum = max(1, int(result.histogram.max()))
            for channel, color in enumerate(colors):
                values = result.histogram[channel]
                pen = QtGui.QPen(QtGui.QColor(color), 1.1)
                painter.setPen(pen)
                # Log height keeps sparse detail visible against big spikes.
                heights = np.log1p(values) / math.log1p(maximum) * 244.0
                points = [QtCore.QPointF(float(x), float(255 - heights[x]))
                          for x in range(256)]
                painter.drawPolyline(QtGui.QPolygonF(points))
        finally:
            painter.end()
        return pixmap

    @classmethod
    def _density_pixmap(cls, density, color, cross=False):
        maximum = max(1, int(density.max()))
        intensity = np.log1p(density) / math.log1p(maximum)
        # Array is always 256*256 and is converted into QImage on GUI thread.
        rgb = np.empty((256, 256, 4), np.uint8)
        rgb[..., :3] = (int(color[0]) * intensity[..., None]).astype(np.uint8)
        rgb[..., 3] = 255
        image = QtGui.QImage(rgb.data, 256, 256, rgb.strides[0],
                             QtGui.QImage.Format_RGBA8888).copy()
        pixmap = cls._base_plot()
        painter = QtGui.QPainter(pixmap)
        try:
            cls._grid(painter, cross=cross)
            painter.setCompositionMode(QtGui.QPainter.CompositionMode_Plus)
            painter.drawImage(0, 0, image)
        finally:
            painter.end()
        return pixmap

    def _draw(self, result):
        self._assert_gui_thread()
        self._scope_labels['histogram'].setPixmap(self._histogram_pixmap(result))
        self._scope_labels['waveform'].setPixmap(
            self._density_pixmap(result.waveform, (112, 232, 170)))
        self._scope_labels['vectorscope'].setPixmap(
            self._density_pixmap(result.vectorscope, (143, 175, 250), cross=True))
        self.status_label.setText('{} · frame {} · {} · generation {}'.format(
            result.source_id or 'Current view', result.source_frame,
            result.stage.title(), result.frame_generation) +
            self._scope_domain_note)
        self.metrics_label.setText(
            'Samples: {:,} / {:,} pixels     Below 0: {:.2f}%     Above 1: {:.2f}%'.format(
                result.sampled_pixels, result.original_pixels,
                100.0 * result.under_range, 100.0 * result.over_range))

    def cancel_pending(self):
        """Invalidate queued computations without closing the panel."""
        self._assert_gui_thread()
        self._generation += 1
        if self._cancel is not None:
            self._cancel.set()
        self._pending = None
        # Never leave old HDR histograms on screen under an unavailable
        # member/source stage; their values could belong to another Version.
        self._last_result = None
        for label in self._scope_labels.values():
            label.clear()
        self.metrics_label.setText('Range: —   Samples: —')

    def showEvent(self, event):
        super(ScopesDialog, self).showEvent(event)
        if not self._closed:
            self.visibilityChanged.emit(True)

    def hideEvent(self, event):
        if not self._closed:
            self.cancel_pending()
            self.visibilityChanged.emit(False)
        super(ScopesDialog, self).hideEvent(event)

    def shutdown(self):
        """Invalidate work and detach the worker before closing Maya/UI."""
        if self._closed:
            return
        self._assert_gui_thread()
        self._closed = True
        self.cancel_pending()
        try:
            self._bridge.finished.disconnect(self._receive)
        except (RuntimeError, TypeError):
            pass
        self._executor.shutdown(wait=False, cancel_futures=True)

    def closeEvent(self, event):
        was_visible = self.isVisible()
        self.shutdown()
        if was_visible:
            self.visibilityChanged.emit(False)
        super(ScopesDialog, self).closeEvent(event)


__all__ = ['ScopeCancelled', 'ScopeData', 'compute_scopes', 'ScopesDialog',
           'DEFAULT_MAX_PIXELS']
