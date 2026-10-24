# -*- coding: utf-8 -*-
"""Maya-safe GPU multi-source viewer.

A ``QOpenGLWidget`` that uploads each source frame to a texture once and applies
image placement, comparison, and compositing in shaders, with an explicit
software fallback (the NumPy :mod:`~framereviewer.viewer.compositor`) that keeps
working when no usable OpenGL context is available -- an OpenRV-style rule:
never silently drop fidelity, always say when the software path is in use.

Modes mirror :mod:`framereviewer.playback.composition`:

* ``SEQUENCE`` / single    - one texture, fit to the widget.
* ``GRID``                 - each texture drawn into its centered tile.
* ``DIFFERENCE`` / ``INVERTED_DIFFERENCE`` - two textures combined per channel
  (``|a-b|`` / ``1-|a-b|``; see the compositor note on OpenRV's signed form).
* ``OVER``                 - premultiplied ``a`` over ``b`` (OpenRV ``Over``).

The fragment functions are ported from OpenRV's ``Difference``/``Over`` GLSL.
Precision: native source/working EXR buffers are transformed using the tested
*CPU* color pipeline into display-referred float32 RGBA, then optionally
uploaded to a float texture. Shaders present these results; they do not perform
OCIO, HDR monitor output, or native precision pixel inspection. Inspection must
continue to use the original FramePayload, not a display framebuffer.

GPU drawing uses Qt's functions from this widget's already-current OpenGL
context, never creating a separate context or requiring PyOpenGL. It degrades
to the same CPU SDR presentation when a real context/float texture/shader is
unavailable. Untested multi-member shader modes use the CPU reference path.
"""
from __future__ import absolute_import

import ctypes
import ctypes.util
import os
import sys
from functools import lru_cache
from types import SimpleNamespace

from ..core.qtcompat import QtCore, QtGui, QtWidgets
from ..playback import composition as composition_mod

# Extra modes beyond the composition set that the viewer can also render.
OVER = 'over'

try:
    _QOpenGLWidget = QtWidgets.QOpenGLWidget
    _HAVE_QOPENGLWIDGET = True
except AttributeError:  # very old/edge Qt builds
    _QOpenGLWidget = QtWidgets.QWidget
    _HAVE_QOPENGLWIDGET = False


_VERTEX_SHADER = """
attribute vec2 aPos;      // -1..1 clip space
attribute vec2 aUV;       // 0..1 texture space
uniform vec4 uTransform;  // (sx, sy, tx, ty): place the quad in a tile
uniform bool uFlipY;
varying vec2 vUV;
void main() {
    vec2 p = aPos * uTransform.xy + uTransform.zw;
    gl_Position = vec4(p, 0.0, 1.0);
    vUV = uFlipY ? vec2(aUV.x, 1.0 - aUV.y) : aUV;
}
"""

_FRAG_SINGLE = """
uniform sampler2D tex0;
varying vec2 vUV;
void main() { gl_FragColor = texture2D(tex0, vUV); }
"""

_GLSL_PRECISION = "#ifdef GL_ES\nprecision highp float;\n#endif\n"

# Ported from OpenRV Difference2.glsl (made absolute for review) + inverted.
_FRAG_DIFFERENCE = """
uniform sampler2D tex0;
uniform sampler2D tex1;
uniform bool uInverted;
varying highp vec2 vUV;
void main() {
    vec4 a = texture2D(tex0, vUV);
    vec4 b = texture2D(tex1, vUV);
    vec3 d = abs(a.rgb - b.rgb);
    if (uInverted) d = vec3(1.0) - d;
    gl_FragColor = vec4(clamp(d, 0.0, 1.0), 1.0);
}
"""

# Ported from OpenRV Over2.glsl (premultiplied a over b).
_FRAG_OVER = """
uniform sampler2D tex0;
uniform sampler2D tex1;
varying highp vec2 vUV;
void main() {
    vec4 a = texture2D(tex0, vUV);
    vec4 b = texture2D(tex1, vUV);
    float ia = 1.0 - a.a;
    vec4 c = vec4(a.rgb + b.rgb * ia, a.a + b.a * ia);
    gl_FragColor = clamp(c, 0.0, 1.0);
}
"""

_FRAG_COMPARE = """
uniform sampler2D tex0;
uniform sampler2D tex1;
uniform int uCompare; // 0 blend, 1 wipe, 2 checker, 3 split
uniform float uBlend;
uniform float uWipe;
uniform bool uHorizontal;
uniform float uCheckerSize;
uniform highp vec2 uCanvasSize;
varying highp vec2 vUV;
void main() {
    vec4 a = texture2D(tex0, vUV);
    vec4 b = texture2D(tex1, vUV);
    if (uCompare == 0) {
        gl_FragColor = mix(a, b, uBlend);
    } else if (uCompare == 1) {
        float p = uHorizontal ? 1.0 - vUV.y : vUV.x;
        gl_FragColor = p < uWipe ? a : b;
    } else if (uCompare == 2) {
        vec2 pixel = vec2(vUV.x, 1.0 - vUV.y) * uCanvasSize;
        vec2 block = floor(pixel / max(uCheckerSize, 1.0));
        gl_FragColor = mod(block.x + block.y, 2.0) < 1.0 ? a : b;
    } else {
        if (uHorizontal) {
            gl_FragColor = vUV.y >= 0.5
                ? texture2D(tex0, vec2(vUV.x, (vUV.y - 0.5) * 2.0))
                : texture2D(tex1, vec2(vUV.x, vUV.y * 2.0));
        } else {
            gl_FragColor = vUV.x < 0.5
                ? texture2D(tex0, vec2(vUV.x * 2.0, vUV.y))
                : texture2D(tex1, vec2((vUV.x - 0.5) * 2.0, vUV.y));
        }
    }
}
"""


def gl_supported():
    """Whether a *current or shared* Qt GL context exists, without creating one.

    A true result is not proof that shader linking or float textures will work;
    the viewer must initialize successfully before reporting GPU presentation.
    """
    if not _HAVE_QOPENGLWIDGET:
        return False
    try:
        return (QtGui.QOpenGLContext.globalShareContext() is not None or
                QtGui.QOpenGLContext.currentContext() is not None)
    except Exception:
        return False


@lru_cache(maxsize=2)
def _native_gl_library(gles=False):
    """Load basic GL 1.1/GLES2 entry points, never creating a GL context.

    PySide2 on Maya 2024 omits glClear/glDrawArrays/glDisable from its wrapped
    QOpenGLFunctions, and does not expose QOpenGLContext.getProcAddress. These
    four functions are in the platform's baseline driver. Use only if the
    QOpenGLWidget context is *already current* (verified by _current_gl).
    """
    if sys.platform == 'darwin' and not gles:
        names = ('/System/Library/Frameworks/OpenGL.framework/OpenGL',)
    elif os.name == 'nt':
        names = (('libGLESv2.dll', 'GLESv2.dll') if gles else
                 ('opengl32.dll',))
    else:
        kind = 'GLESv2' if gles else 'GL'
        names = tuple(n for n in (
            ctypes.util.find_library(kind),
            'libGLESv2.so.2' if gles else 'libGL.so.1') if n)
    errors = []
    for name in names:
        try:
            return (ctypes.WinDLL(name) if os.name == 'nt' else ctypes.CDLL(name))
        except OSError as exc:
            errors.append(str(exc))
    raise RuntimeError('No compatible system {} driver: {}'.format(
        'OpenGL ES' if gles else 'OpenGL', '; '.join(errors)))


def _native_gl_functions(library):
    """Minimal typed baseline GL API, callable only in the current context."""
    signatures = {
        'glClearColor': (ctypes.c_float,) * 4,
        'glClear': (ctypes.c_uint,),
        'glDisable': (ctypes.c_uint,),
        'glDrawArrays': (ctypes.c_uint, ctypes.c_int, ctypes.c_int),
    }
    result = {}
    for name, args in signatures.items():
        function = getattr(library, name, None)
        if function is None:
            raise RuntimeError('GL driver lacks {}'.format(name))
        function.restype = None
        function.argtypes = list(args)
        result[name] = function
    return SimpleNamespace(**result)


class GPUMultiViewer(_QOpenGLWidget):
    """Composites one or more frames on the GPU, with a software fallback."""

    def __init__(self, parent=None):
        super(GPUMultiViewer, self).__init__(parent)
        self._mode = composition_mod.SEQUENCE
        self._frames = []          # list of QImage
        self._textures = []        # list of QOpenGLTexture
        self._programs = {}
        self._cells = None         # grid cells, or None for auto from mode
        self._gl_ok = False
        self._gl_initialized = False
        # Real framebuffer parity has not been certified in Maya hardware.
        # Never silently switch a production review viewport to experimental
        # GPU drawing solely because QOpenGLWidget happens to be installed.
        self._gpu_opt_in = False
        self._gl_attempted = False
        self._gl_dispatch = None
        self._gl_dispatch_context = None
        self._dirty = True
        self._quad = None
        self._parameters = {}
        self._color_state = None
        self._display_color_status = ''
        self._fallback_reason = ('Experimental GPU not enabled; CPU SDR preview.'
                                 if _HAVE_QOPENGLWIDGET else
                                 'QOpenGLWidget unavailable; CPU SDR preview.')
        self._frame_keys = ()

    # -- public API --------------------------------------------------------
    def set_mode(self, mode):
        self._mode = mode
        self._cells = None
        self.update()

    def set_frames(self, frames):
        """Accept QImages or immutable FramePayloads without quantizing float input."""
        frames = [f for f in (frames or []) if f is not None]
        keys = tuple((('payload', f.identity, id(f)) if hasattr(f, 'pixels') else
                      ('qimage', f.cacheKey())) for f in frames)
        if keys == self._frame_keys:
            return
        self._frames = frames
        self._frame_keys = keys
        self._dirty = True
        self.update()

    def set_comparison(self, mode=None, **parameters):
        """Set A/B, blend, wipe/split, checker and flicker review parameters."""
        if mode is not None:
            self._mode = mode
        self._parameters.update(parameters)
        self.update()

    def set_color_state(self, state):
        """Apply tested CPU color transforms before optional GL texture upload.

        Invalid or unavailable color configs use an explicitly reported
        unmanaged fallback. No shader runs directly on untransformed EXR values.
        """
        self._color_state = state
        self._dirty = True
        self.update()

    def fallback_reason(self):
        return self._fallback_reason

    def display_color_status(self):
        return self._display_color_status

    def set_experimental_gpu_enabled(self, enabled):
        """Opt in to *uncertified* native GL presentation on an existing widget.

        This never creates a context. Keep disabled for primary/studio review
        until hardware framebuffer/color parity is tested in Maya. Even when
        enabled it falls back to CPU for missing GL, upload failures, and
        multi-source comparisons without verified GPU equivalence.
        """
        enabled = bool(enabled)
        if enabled != self._gpu_opt_in:
            if enabled and not self._gl_ok:
                self._gl_attempted = False
            self._gpu_opt_in = enabled
            self._fallback_reason = (
                'Experimental GPU requested; waiting for Qt context/validation.'
                if enabled else 'Experimental GPU not enabled; CPU SDR preview.')
            self.update()

    def renderer_status(self):
        """Truthful renderer diagnostics for studio UI and safe integration.

        `gpu_active` concerns *this mode* (not mere GL installation). Color is
        always processed on the CPU; presentation is SDR even with RGBA32F.
        """
        active = self.is_gpu_active()
        reason = self._fallback_reason
        if not self._gpu_opt_in:
            reason = 'Experimental GPU not enabled; CPU SDR preview.'
        elif not active and self._gl_ok and self._gl_initialized and \
                not self._gpu_mode_supported():
            reason = ('Composition mode uses the CPU reference to avoid '
                      'unverified GPU comparison parity.')
        elif not active and self._gl_ok and self._gl_initialized and self._dirty:
            reason = 'Waiting for verified GPU texture upload; CPU SDR available.'
        return {'backend': 'qt-opengl' if active else 'cpu-sdr',
                'gpu_active': active,
                'experimental_gpu_requested': self._gpu_opt_in,
                'gl_initialized': self._gl_initialized,
                'color_processing': 'CPU source/working -> Display',
                'monitor_hdr': False, 'ocio_gpu_parity': False,
                'framebuffer_precision': 'unverified SDR',
                'reason': '' if active else reason,
                'color_status': self.display_color_status()}

    def _display_array(self, frame):
        if hasattr(frame, 'pixels'):
            from .color import precision_display_pixels
            pixels, status = precision_display_pixels(
                frame, self._color_state)
            self._display_color_status = status
            return pixels
        self._display_color_status = ''
        return self._qimage_to_array(frame)

    def set_grid_cells(self, cells):
        self._cells = list(cells) if cells else None
        self.update()

    def is_gpu_active(self):
        return bool(self._gpu_opt_in and self._gl_ok and self._gl_initialized and
                    _HAVE_QOPENGLWIDGET and self.isValid() and
                    not self._dirty and self._frames and
                    len(self._textures) == len(self._frames) and
                    self._gpu_mode_supported())

    def _gpu_mode_supported(self):
        """Only presentation modes with known CPU texture/color semantics."""
        return self._mode in (composition_mod.SEQUENCE, composition_mod.GRID,
                              'a_b', 'ab', 'a', 'b', 'flicker')

    # -- software fallback -------------------------------------------------
    def render_software(self, width=None, height=None):
        """Composite to a ``QImage`` on the CPU (the deterministic fallback).

        Used when no GL context is available, or by callers that need a pixmap
        (snapshots, export) regardless of the GPU path.
        """
        if not self._frames:
            return QtGui.QImage()
        first = self._frames[0]
        frame_w = first.geometry.width if hasattr(first, 'pixels') else first.width()
        frame_h = first.geometry.height if hasattr(first, 'pixels') else first.height()
        width = int(width or self.width() or frame_w or 1)
        height = int(height or self.height() or frame_h or 1)
        try:
            from . import compositor
            import numpy as np  # noqa: F401
        except Exception:
            if hasattr(first, 'pixels'):
                raise RuntimeError('NumPy is required to display precision frame payloads.')
            if self._mode == composition_mod.GRID:
                return self._qt_grid(width, height)
            return first.scaled(
                width, height, QtCore.Qt.KeepAspectRatio,
                QtCore.Qt.SmoothTransformation)
        arrays = [self._display_array(f) for f in self._frames]
        params = dict(self._parameters)
        if self._mode == composition_mod.GRID and self._cells is not None:
            params['cells'] = self._cells
        result = compositor.composite(self._mode, arrays, width=width,
                                      height=height, **params)
        return self._array_to_qimage(compositor.to_uint8(result))

    def _qt_grid(self, width, height):
        """Grid fallback for Maya installations where NumPy is unavailable."""
        canvas = QtGui.QImage(width, height, QtGui.QImage.Format_RGBA8888)
        canvas.fill(QtGui.QColor(0, 0, 0, 0))
        painter = QtGui.QPainter(canvas)
        try:
            painter.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
            for index, (cx, cy, cw, ch) in enumerate(self._grid_cells()):
                if index >= len(self._frames):
                    break
                x0, y0 = int(round(cx * width)), int(round(cy * height))
                x1 = int(round((cx + cw) * width))
                y1 = int(round((cy + ch) * height))
                if x1 > x0 and y1 > y0:
                    painter.drawImage(QtCore.QRect(x0, y0, x1 - x0, y1 - y0),
                                      self._frames[index])
        finally:
            painter.end()
        return canvas

    # -- GL lifecycle ------------------------------------------------------
    def initializeGL(self):
        self._gl_initialized = True
        self._gl_ok = False
        self._gl_attempted = False
        self._gl_dispatch = None
        self._gl_dispatch_context = None
        self._dirty = True
        # A reparented QOpenGLWidget may have received a new context. Never
        # reuse old-context GPU resources or assume retained float textures.
        self._textures = []
        self._quad = None
        self._programs = {}
        if self._gpu_opt_in:
            self._initialize_gpu_resources()

    def _initialize_gpu_resources(self):
        """Called only from Qt's current widget GL context, never a new one."""
        self._gl_attempted = True
        try:
            if self.context() is None or not self.context().isValid():
                raise RuntimeError('No valid QOpenGLWidget context')
            if QtGui.QOpenGLContext.currentContext() != self.context():
                raise RuntimeError('OpenGL initialization requires its current widget context')
            # Compile only the shader we actually expose as parity-safe.
            # Unverified two-source GPU comparisons explicitly use the CPU.
            self._programs = {'single': self._build_program(_FRAG_SINGLE)}
            self._build_quad()
            self._gl_ok = all(self._programs.values()) and self._quad is not None
            if not self._gl_ok:
                self._fallback_reason = 'OpenGL shader compile/link failed.'
            else:
                self._fallback_reason = ''
        except Exception as exc:
            self._gl_ok = False
            self._fallback_reason = 'OpenGL initialization: {}'.format(exc)

    def resizeGL(self, w, h):
        pass

    def paintGL(self):
        if not self._gpu_opt_in:
            self._paint_fallback()
            return
        if not self._gl_attempted:
            self._initialize_gpu_resources()
        try:
            gl = self._current_gl()
        except Exception as exc:
            self._gl_ok = False
            self._fallback_reason = 'OpenGL unavailable: {}'.format(exc)
            self._paint_fallback()
            return
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(0x00004000)  # GL_COLOR_BUFFER_BIT
        if not self._gl_ok:
            self._paint_fallback()
            return
        if not self._frames:
            return
        if not self._gpu_mode_supported():
            self._fallback_reason = ('Comparison mode requires tested CPU '
                                     'compositor parity; GPU disabled for this mode.')
            self._paint_fallback()
            return
        # Source pixels carry straight alpha. Blending them into the clear
        # buffer would premultiply RGB and lose the original alpha values.
        gl.glDisable(0x0BE2)  # GL_BLEND
        if self._dirty:
            try:
                self._upload_textures()
            except Exception as exc:
                self._gl_ok = False
                self._fallback_reason = 'Texture upload: {}'.format(exc)
                self._paint_fallback()
                return
            self._dirty = False
        try:
            mode = self._mode
            if mode in ('a_b', 'ab', 'a', 'b', 'flicker'):
                active = self._parameters.get('active', 1 if mode == 'b' else 0)
                self._draw_single(int(active) % min(2, len(self._frames)),
                                  (1.0, 1.0, 0.0, 0.0))
            elif mode == composition_mod.GRID:
                self._draw_grid()
            else:
                self._draw_single(0, (1.0, 1.0, 0.0, 0.0))
        except Exception as exc:
            self._gl_ok = False
            self._fallback_reason = 'OpenGL draw failed: {}'.format(exc)
            self._paint_fallback()

    def _paint_fallback(self):
        """Show deterministic software pixels when context/shaders fail."""
        if self._frames:
            painter = QtGui.QPainter(self)
            try:
                painter.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
                painter.drawImage(self.rect(), self.render_software(self.width(), self.height()))
            finally:
                painter.end()

    def paintEvent(self, event):
        # An old Qt distribution may not provide QOpenGLWidget at all. In that
        # case the QWidget subclass must still visibly paint its CPU preview.
        if _HAVE_QOPENGLWIDGET:
            super(GPUMultiViewer, self).paintEvent(event)
        else:
            self._paint_fallback()

    def _current_gl(self):
        """Use this widget's current Qt context; never create or share another."""
        context = self.context() if _HAVE_QOPENGLWIDGET else None
        if (context is None or not context.isValid() or
                QtGui.QOpenGLContext.currentContext() != context):
            raise RuntimeError('QOpenGLWidget has no current valid GL context')
        functions = context.functions()
        required = ('glClearColor', 'glClear', 'glDisable', 'glDrawArrays')
        if functions is not None and all(hasattr(functions, item)
                                         for item in required):
            return functions
        if self._gl_dispatch is None or self._gl_dispatch_context != context:
            library = _native_gl_library(bool(context.isOpenGLES()))
            self._gl_dispatch = _native_gl_functions(library)
            self._gl_dispatch_context = context
        return self._gl_dispatch

    # -- GL helpers (only called with a live context) ----------------------
    def _build_program(self, fragment):
        program = QtGui.QOpenGLShaderProgram(self)
        if not program.addShaderFromSourceCode(
                QtGui.QOpenGLShader.Vertex, _GLSL_PRECISION + _VERTEX_SHADER):
            raise RuntimeError('Vertex GLSL rejected: {}'.format(program.log()))
        if not program.addShaderFromSourceCode(
                QtGui.QOpenGLShader.Fragment, _GLSL_PRECISION + fragment):
            raise RuntimeError('Fragment GLSL rejected: {}'.format(program.log()))
        program.bindAttributeLocation('aPos', 0)
        program.bindAttributeLocation('aUV', 1)
        if not program.link():
            raise RuntimeError('GLSL link failed: {}'.format(program.log()))
        return program

    def _build_quad(self):
        # Interleaved pos.xy, uv.xy for a unit quad as a triangle strip.
        data = [-1.0, -1.0, 0.0, 0.0,
                 1.0, -1.0, 1.0, 0.0,
                -1.0,  1.0, 0.0, 1.0,
                 1.0,  1.0, 1.0, 1.0]
        buf = QtGui.QOpenGLBuffer(QtGui.QOpenGLBuffer.VertexBuffer)
        if not buf.create() or not buf.bind():
            raise RuntimeError('OpenGL vertex buffer creation failed')
        import struct
        blob = struct.pack('%df' % len(data), *data)
        buf.allocate(blob, len(blob))
        self._quad = buf

    def _upload_textures(self):
        new_textures = []
        try:
            for frame in self._frames:
                if hasattr(frame, 'pixels'):
                    # CPU transforms native source/working samples to display
                    # RGB first. RGBA32F avoids an extra pre-upload uint8 loss.
                    # The monitor framebuffer remains SDR/unverified precision.
                    import numpy as np
                    rgba = np.ascontiguousarray(self._display_array(frame),
                                                 dtype=np.float32)
                    if rgba.ndim != 3 or rgba.shape[2] != 4:
                        raise RuntimeError('CPU color output is not float RGBA')
                    tex = QtGui.QOpenGLTexture(QtGui.QOpenGLTexture.Target2D)
                    new_textures.append(tex)
                    tex.setFormat(QtGui.QOpenGLTexture.RGBA32F)
                    tex.setSize(rgba.shape[1], rgba.shape[0])
                    tex.allocateStorage(QtGui.QOpenGLTexture.RGBA,
                                        QtGui.QOpenGLTexture.Float32)
                    if not tex.isCreated() or not tex.isStorageAllocated():
                        raise RuntimeError('RGBA32F texture storage unsupported')
                    # Maya's PySide2 Qt5 binding expects a *native pointer*
                    # (int) to synchronously-read pixel storage, not Python
                    # bytes. Keep the contiguous array alive through setData.
                    uploaded = np.ascontiguousarray(rgba[::-1])
                    tex.setData(QtGui.QOpenGLTexture.RGBA,
                                QtGui.QOpenGLTexture.Float32,
                                int(uploaded.ctypes.data))
                else:
                    tex = QtGui.QOpenGLTexture(
                        frame.mirrored(False, True),
                        QtGui.QOpenGLTexture.DontGenerateMipMaps)
                    new_textures.append(tex)
                # CPU compositor uses nearest-neighbor stretch, not GL linear
                # interpolation; preserve edge pixels and transparent gaps.
                tex.setMinificationFilter(QtGui.QOpenGLTexture.Nearest)
                tex.setMagnificationFilter(QtGui.QOpenGLTexture.Nearest)
                tex.setWrapMode(QtGui.QOpenGLTexture.ClampToEdge)
        except Exception:
            for tex in new_textures:
                try:
                    tex.destroy()
                except Exception:
                    pass
            raise
        old_textures, self._textures = self._textures, new_textures
        for tex in old_textures:
            try:
                tex.destroy()
            except Exception:
                pass

    def _draw_compare(self, mode):
        gl = self._current_gl()
        if len(self._textures) < 2:
            self._draw_single(0, (1.0, 1.0, 0.0, 0.0))
            return
        program = self._programs['compare']
        program.bind()
        program.setUniformValue('uTransform', 1.0, 1.0, 0.0, 0.0)
        program.setUniformValue('uFlipY', False)
        program.setUniformValue('uCompare', {'blend': 0, 'wipe': 1,
                                             'checker': 2, 'split': 3}[mode])
        program.setUniformValue('uBlend', float(self._parameters.get('blend', 0.5)))
        program.setUniformValue('uWipe', float(self._parameters.get('wipe', 0.5)))
        program.setUniformValue('uHorizontal',
                                self._parameters.get('orientation') == 'horizontal')
        program.setUniformValue('uCheckerSize',
                                float(self._parameters.get('checker_size', 64)))
        first = self._frames[0]
        width = first.geometry.width if hasattr(first, 'pixels') else first.width()
        height = first.geometry.height if hasattr(first, 'pixels') else first.height()
        program.setUniformValue('uCanvasSize', float(width), float(height))
        self._textures[0].bind(0)
        self._textures[1].bind(1)
        program.setUniformValue('tex0', 0)
        program.setUniformValue('tex1', 1)
        self._bind_quad(program)
        gl.glDrawArrays(0x0005, 0, 4)  # GL_TRIANGLE_STRIP
        program.release()

    def _bind_quad(self, program):
        self._quad.bind()
        program.enableAttributeArray(0)
        program.setAttributeBuffer(0, 0x1406, 0, 2, 16)   # GL_FLOAT
        program.enableAttributeArray(1)
        program.setAttributeBuffer(1, 0x1406, 8, 2, 16)

    def _draw_single(self, index, transform):
        gl = self._current_gl()
        if index >= len(self._textures):
            return
        program = self._programs['single']
        program.bind()
        program.setUniformValue('uTransform', *transform)
        program.setUniformValue('uFlipY', False)
        self._textures[index].bind(0)
        program.setUniformValue('tex0', 0)
        self._bind_quad(program)
        gl.glDrawArrays(0x0005, 0, 4)
        program.release()

    def _draw_combined(self, key, inverted=False):
        gl = self._current_gl()
        if len(self._textures) < 2:
            self._draw_single(0, (1.0, 1.0, 0.0, 0.0))
            return
        program = self._programs[key]
        program.bind()
        program.setUniformValue('uTransform', 1.0, 1.0, 0.0, 0.0)
        program.setUniformValue('uFlipY', False)
        if key == 'difference':
            program.setUniformValue('uInverted', bool(inverted))
        self._textures[0].bind(0)
        self._textures[1].bind(1)
        program.setUniformValue('tex0', 0)
        program.setUniformValue('tex1', 1)
        self._bind_quad(program)
        gl.glDrawArrays(0x0005, 0, 4)
        program.release()

    def _grid_cells(self):
        cells = self._cells
        if cells is None:
            comp = composition_mod.Composition(
                composition_mod.GRID,
                [composition_mod.CompositionMember('m%d' % i, 1)
                 for i in range(len(self._frames))])
            cells = comp.grid_cells()
        return cells

    def _draw_grid(self):
        cells = self._grid_cells()
        for index, (cx, cy, cw, ch) in enumerate(cells):
            if index >= len(self._frames):
                break
            # Map a normalized top-left cell rect to clip-space transform.
            sx = cw
            sy = ch
            tx = (cx + cw / 2.0) * 2.0 - 1.0
            ty = 1.0 - (cy + ch / 2.0) * 2.0
            self._draw_single(index, (sx, sy, tx, ty))

    def shutdown(self):
        """Destroy GPU objects on the owning GUI thread before Maya reload."""
        context = self.context() if _HAVE_QOPENGLWIDGET else None
        can_destroy = (context is not None and context.isValid() and
                       self._gl_initialized)
        if can_destroy and (self._textures or self._programs or self._quad is not None):
            try:
                self.makeCurrent()
                for texture in self._textures:
                    texture.destroy()
                if self._quad is not None:
                    self._quad.destroy()
                for shader in self._programs.values():
                    if shader is not None:
                        shader.removeAllShaders()
            except Exception:
                pass  # destroyed Qt context: objects will be collected by Qt
            finally:
                try:
                    self.doneCurrent()
                except Exception:
                    pass
        self._textures = []
        self._programs = {}
        self._quad = None
        self._frames = []
        self._frame_keys = ()
        self._display_color_status = ''
        self._gl_ok = False
        self._gl_initialized = False
        self._gl_attempted = False
        self._gpu_opt_in = False
        self._gl_dispatch = None
        self._gl_dispatch_context = None
        self._fallback_reason = 'GPU viewer shut down; CPU SDR preview.'
        self._dirty = True

    def closeEvent(self, event):
        self.shutdown()
        super(GPUMultiViewer, self).closeEvent(event)

    def minimumSizeHint(self):
        return QtCore.QSize(64, 64)

    # -- QImage <-> array (software path) ----------------------------------
    @staticmethod
    def _qimage_to_array(image):
        import numpy as np
        img = image.convertToFormat(QtGui.QImage.Format_RGBA8888)
        w, h = img.width(), img.height()
        ptr = img.constBits()
        try:
            ptr.setsize(h * w * 4)
        except AttributeError:
            pass
        arr = np.frombuffer(bytes(ptr), dtype=np.uint8).reshape((h, w, 4)).copy()
        return arr

    @staticmethod
    def _array_to_qimage(arr):
        import numpy as np
        arr = np.ascontiguousarray(arr, dtype=np.uint8)
        h, w = arr.shape[:2]
        return QtGui.QImage(arr.data, w, h, w * 4,
                            QtGui.QImage.Format_RGBA8888).copy()


def create_multi_viewer(parent=None):
    """Return a :class:`GPUMultiViewer` (it self-selects GPU vs. software)."""
    return GPUMultiViewer(parent)
