# -*- coding: utf-8 -*-
"""Declared source-to-working-to-display color transforms and legacy UI.

A small grade (exposure, contrast, saturation, temperature) applied to the
*displayed* pixmap only -- it never touches decoded cache frames, exported
media or annotation data. Grading runs on the already-downscaled display
pixmap (<= the cache's decode width), so a few milliseconds of NumPy per frame
keeps playback smooth. When NumPy is unavailable the grade is disabled and the
image passes through untouched.
"""
from __future__ import absolute_import

from dataclasses import asdict, dataclass

try:
    from ..core.qtcompat import QtCore, QtGui, QtWidgets, Signal
except ImportError:  # pure NumPy color transform is usable without Maya/Qt
    QtCore = QtGui = QtWidgets = Signal = None

try:
    import numpy as np
    HAVE_NUMPY = True
except Exception:
    np = None
    HAVE_NUMPY = False

# Keys and their neutral (no-op) values.
NEUTRAL = {'exposure': 0.0, 'contrast': 0.0, 'saturation': 0.0, 'temperature': 0.0}


@dataclass(frozen=True)
class ColorPipelineState:
    """Immutable settings for reproducible CPU color and export manifests.

    Without an OCIO configuration, recognized sRGB/linear/raw spaces use the
    documented built-in math. Unrecognized spaces and unavailable OCIO raise.
    """
    config: str = ''
    input_space: str = 'sRGB'
    working_space: str = 'linear'
    display_space: str = 'sRGB'
    display: str = ''
    view: str = ''
    look: str = ''
    ocio_context: tuple = ()
    lut_roles: tuple = ()
    exposure: float = 0.0
    gamma: float = 1.0
    contrast: float = 0.0
    saturation: float = 0.0
    temperature: float = 0.0
    cdl_slope: tuple = (1.0, 1.0, 1.0)
    cdl_offset: tuple = (0.0, 0.0, 0.0)
    cdl_power: tuple = (1.0, 1.0, 1.0)
    cdl_saturation: float = 1.0
    alpha_mode: str = 'straight'
    bypass_input: bool = False
    bypass_grade: bool = False
    bypass_display: bool = False

    def __post_init__(self):
        for name in ('cdl_slope', 'cdl_offset', 'cdl_power'):
            values = tuple(float(v) for v in getattr(self, name))
            if len(values) != 3:
                raise ValueError('{} must contain RGB values'.format(name))
            object.__setattr__(self, name, values)
        if self.gamma <= 0 or any(x <= 0 for x in self.cdl_power):
            raise ValueError('Gamma and CDL powers must be positive.')
        if self.alpha_mode not in ('straight', 'premultiplied', 'opaque'):
            raise ValueError('Unsupported alpha mode.')
        context = self.ocio_context
        if isinstance(context, dict):
            context = tuple(sorted((str(k), str(v)) for k, v in context.items()))
        object.__setattr__(self, 'ocio_context', tuple(context))
        object.__setattr__(self, 'lut_roles', tuple(self.lut_roles))

    def to_dict(self):
        data = asdict(self)
        data['ocio_context'] = dict(self.ocio_context)
        return data

    @classmethod
    def from_dict(cls, data):
        if isinstance(data, cls):
            return data
        if not data:
            return cls()
        return cls(**{k: v for k, v in dict(data).items()
                     if k in cls.__dataclass_fields__})


def _srgb_to_linear(rgb):
    return np.where(rgb <= 0.04045, rgb / 12.92,
                    ((np.maximum(rgb, 0.0) + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(rgb):
    return np.where(rgb <= 0.0031308, rgb * 12.92,
                    1.055 * np.maximum(rgb, 0.0) ** (1.0 / 2.4) - 0.055)


def _ocio_convert(rgb, state, source, destination, display=False):
    try:
        import PyOpenColorIO as ocio
    except ImportError:
        raise RuntimeError('The requested OCIO config requires PyOpenColorIO.')
    config = ocio.Config.CreateFromFile(state.config)
    context = None
    if state.ocio_context:
        context = config.getCurrentContext()
        if hasattr(context, 'createEditableCopy'):
            context = context.createEditableCopy()
        for key, value in state.ocio_context:
            context.setStringVar(str(key), str(value))
    if display and state.display and state.view:
        if hasattr(ocio, 'DisplayViewTransform'):
            transform = ocio.DisplayViewTransform()
            transform.setSrc(source)
        elif hasattr(ocio, 'DisplayTransform'):
            transform = ocio.DisplayTransform()
            transform.setInputColorSpaceName(source)
        else:
            raise RuntimeError('This OCIO build does not support display/view transforms.')
        transform.setDisplay(state.display)
        transform.setView(state.view)
        if state.look:
            if not hasattr(transform, 'setLooksOverride'):
                raise RuntimeError('This OCIO version cannot apply the selected look.')
            transform.setLooksOverride(state.look)
            transform.setLooksOverrideEnabled(True)
        if context is None:
            processor = config.getProcessor(transform)
        else:
            try:
                processor = config.getProcessor(context, transform)
            except TypeError:
                processor = config.getProcessor(transform, context)
    else:
        if display and (state.display or state.view or state.look):
            raise ValueError('OCIO display and view must both be selected.')
        if context is None:
            processor = config.getProcessor(source, destination)
        else:
            try:
                processor = config.getProcessor(context, source, destination)
            except TypeError:
                processor = config.getProcessor(source, destination, context)
    cpu = processor.getDefaultCPUProcessor() if hasattr(processor, 'getDefaultCPUProcessor') else processor
    flat = np.ascontiguousarray(rgb.reshape(-1, 3), dtype=np.float32)
    if hasattr(ocio, 'PackedImageDesc'):
        desc = ocio.PackedImageDesc(flat, flat.shape[0], 1, 3)
        cpu.apply(desc)
    else:  # OCIO v1 processor
        for pixel in flat:
            pixel[:] = cpu.applyRGB([float(v) for v in pixel])
    return flat.reshape(rgb.shape)


def _convert(rgb, state, source, destination, display=False):
    if display and (state.display or state.view or state.look) and not state.config:
        raise ValueError('OCIO display/view/look requires an OCIO config.')
    if state.ocio_context and not state.config:
        raise ValueError('OCIO context variables require an OCIO config.')
    if state.lut_roles:
        raise RuntimeError('External LUT roles require a configured LUT processor.')
    if display and state.config and (state.display or state.view):
        return _ocio_convert(rgb, state, source, destination, display=True)
    if source.casefold() == destination.casefold() or source.casefold() == 'raw':
        return rgb
    if state.config:
        return _ocio_convert(rgb, state, source, destination)
    src, dst = source.casefold(), destination.casefold()
    if (src, dst) == ('srgb', 'linear'):
        return _srgb_to_linear(rgb)
    if (src, dst) == ('linear', 'srgb'):
        return _linear_to_srgb(rgb)
    raise ValueError('Unmanaged color conversion: {} -> {}'.format(source, destination))


def _grade(rgb, state):
    rgb = rgb * (2.0 ** state.exposure)
    if state.contrast:
        rgb = (rgb - 0.18) * (1.0 + state.contrast / 100.0) + 0.18
    if state.temperature:
        temp = state.temperature / 100.0
        rgb = rgb * np.array((1.0 + 0.2 * temp, 1.0, 1.0 - 0.2 * temp), np.float32)
    # Avoid changing negative scene-linear values when the CDL is neutral.
    # Non-neutral CDL power uses its defined non-negative domain.
    if (state.cdl_slope != (1.0, 1.0, 1.0) or
            state.cdl_offset != (0.0, 0.0, 0.0) or
            state.cdl_power != (1.0, 1.0, 1.0)):
        rgb = np.maximum(rgb * np.array(state.cdl_slope) +
                         np.array(state.cdl_offset), 0.0) ** (1.0 / np.array(state.cdl_power))
    if state.gamma != 1.0:
        rgb = np.sign(rgb) * np.abs(rgb) ** (1.0 / state.gamma)
    sat = (1.0 + state.saturation / 100.0) * state.cdl_saturation
    if sat != 1.0:
        lum = np.sum(rgb * [0.2126, 0.7152, 0.0722], axis=-1, keepdims=True)
        rgb = lum + (rgb - lum) * sat
    return rgb.astype(np.float32)


def process_pixels(pixels, state=None, stage='display', source_stage='source',
                   alpha_mode=None):
    """Return owned float32 pixels in the requested stage, without clipping HDR.

    Source integer samples normalize to [0, 1], float samples retain negatives
    and highlights. RGB is unpremultiplied for transforms then restored to the
    original alpha association. Non-RGB channels are passed through.
    """
    if not HAVE_NUMPY:
        raise RuntimeError('NumPy is required for precision color processing.')
    state = ColorPipelineState.from_dict(state)
    stages = ('source', 'working', 'display')
    if stage not in stages or source_stage not in stages or stages.index(stage) < stages.index(source_stage):
        raise ValueError('Color stages must proceed source -> working -> display.')
    arr = np.asarray(pixels)
    if arr.dtype.kind in 'ui':
        out = arr.astype(np.float32) / float(np.iinfo(arr.dtype).max)
    else:
        out = arr.astype(np.float32, copy=True)
    if out.ndim == 2:
        out = out[..., None]
    if stage == source_stage or out.shape[2] < 3:
        return out.copy()
    rgb = out[..., :3].copy()
    mode = alpha_mode or state.alpha_mode
    if mode == 'premultiplied' and out.shape[2] == 4:
        alpha = out[..., 3:4]
        rgb = np.divide(rgb, alpha, out=np.zeros_like(rgb), where=alpha > 1e-8)
    if source_stage == 'source':
        if not state.bypass_input:
            rgb = _convert(rgb, state, state.input_space, state.working_space)
        if not state.bypass_grade:
            rgb = _grade(rgb, state)
    if stage == 'display' and not state.bypass_display:
        rgb = _convert(rgb, state, state.working_space, state.display_space,
                       display=True)
    if mode == 'premultiplied' and out.shape[2] == 4:
        rgb *= out[..., 3:4]
    out[..., :3] = rgb
    return out


def process_payload(payload, state=None, stage='display'):
    """Transform a :class:`FramePayload`, retaining immutable identity/metadata."""
    from ..core.frame_payload import FramePayload
    if not isinstance(payload, FramePayload):
        raise TypeError('A FramePayload is required.')
    return payload.with_pixels(process_pixels(payload.pixels, state, stage,
                                               payload.stage, payload.alpha_mode), stage)


UNMANAGED_HDR_LABEL = 'Unmanaged preview: assumed scene-linear -> sRGB (no OCIO config)'
MAX_PREVIEW_PIXELS = 1920 * 1080


def _precision_state(payload, state=None, legacy_grade=None):
    """Resolve an explicit color pipeline for a native payload.

    EXR input color is scene-linear by default, including when just the legacy
    grade sliders are supplied. An explicit input_space always takes priority.
    Legacy sliders override their matching pipeline fields instead of grading a
    second time after 8-bit presentation.
    """
    from dataclasses import replace
    metadata = payload.color_metadata
    input_space = metadata.get('input_space', 'linear')
    if isinstance(state, dict):
        data = dict(state)
        data.setdefault('input_space', input_space)
        state = ColorPipelineState.from_dict(data)
    elif state is None:
        state = ColorPipelineState(input_space=input_space)
    else:
        state = ColorPipelineState.from_dict(state)
    if legacy_grade and payload.stage == 'source':
        fields = ('exposure', 'contrast', 'saturation', 'temperature')
        update = {key: float(legacy_grade[key]) for key in fields
                  if key in legacy_grade}
        if update:
            state = replace(state, **update)
    return state


def _preview_shape(width, height, target_size=None, max_pixels=MAX_PREVIEW_PIXELS):
    """Bound temporary CPU pixel allocations while preserving view geometry."""
    width, height = int(width), int(height)
    if target_size is not None:
        width, height = min(width, max(1, int(target_size[0]))), \
                        min(height, max(1, int(target_size[1])))
    if max_pixels is not None and width * height > max_pixels:
        scale = (float(max_pixels) / (width * height)) ** 0.5
        width, height = max(1, int(width * scale)), max(1, int(height * scale))
    return width, height


def precision_display_pixels(payload, color_state=None, legacy_grade=None,
                             target_size=None, max_pixels=MAX_PREVIEW_PIXELS):
    """Return display-referred float RGBA and honest transform status.

    Every transform executes on source/working float samples before any 8-bit
    quantization. Nearest-neighbor downsampling is performed on *native* samples
    to cap CPU memory use, never on an already quantized pixmap. The FramePayload
    itself is never modified. This is an SDR presentation transform; high-precision
    pixel inspection must continue using ``payload.pixels``.
    """
    if not HAVE_NUMPY:
        raise RuntimeError('NumPy is required for HDR precision presentation.')
    from ..core.frame_payload import FramePayload
    if not isinstance(payload, FramePayload):
        raise TypeError('Expected a FramePayload.')
    width, height = _preview_shape(payload.geometry.width,
                                   payload.geometry.height,
                                   target_size, max_pixels)
    native = payload.pixels
    if native.shape[0] != height or native.shape[1] != width:
        ys = np.minimum((np.arange(height) * native.shape[0] // height),
                        native.shape[0] - 1)
        xs = np.minimum((np.arange(width) * native.shape[1] // width),
                        native.shape[1] - 1)
        # One indexed copy at the *destination* size. Chained slicing first
        # allocates all original columns, which can spike RAM for large EXRs.
        native = native[ys[:, None], xs[None, :]]
    try:
        state = _precision_state(payload, color_state, legacy_grade)
        output = process_pixels(native, state, stage='display',
                                source_stage=payload.stage,
                                alpha_mode=payload.alpha_mode)
        if state.config:
            used_ocio = (
                (payload.stage == 'source' and not state.bypass_input and
                 state.input_space.casefold() != state.working_space.casefold()) or
                (payload.stage != 'display' and not state.bypass_display and
                 (state.display or state.view or
                  state.working_space.casefold() != state.display_space.casefold())))
            status = ('OCIO managed: {}'.format(state.config) if used_ocio else
                      'OCIO configured, color-space transforms bypassed: {}'.format(
                          state.config))
        elif state.input_space.casefold() == 'linear':
            status = UNMANAGED_HDR_LABEL
        else:
            status = ('Unmanaged preview: {} -> {} -> {} (no OCIO config)'.format(
                state.input_space, state.working_space, state.display_space))
        if payload.stage == 'display':
            status = 'Display-referred source pixels (no additional transform)'
        elif payload.stage == 'working':
            status += ' (working-stage input; upstream grade retained)'
    except (ValueError, RuntimeError, TypeError, ImportError, OSError) as exc:
        # An invalid/unavailable OCIO configuration must never silently appear
        # managed. Fall back to a deterministic linear -> sRGB monitor preview.
        fallback = ColorPipelineState(input_space='linear',
                                      working_space='linear', display_space='sRGB')
        output = process_pixels(native, fallback, stage='display',
                                source_stage=payload.stage,
                                alpha_mode=payload.alpha_mode)
        status = 'Unmanaged fallback: color transform failed ({}); linear -> sRGB'.format(exc)
    if output.ndim == 2:
        output = output[..., None]
    channels = output.shape[2]
    if channels == 1:
        output = np.repeat(output, 3, axis=-1)
        channels = 3
    elif channels == 2:
        output = np.concatenate((np.repeat(output[..., :1], 3, axis=-1),
                                 output[..., 1:2]), axis=-1)
        channels = 4
    if channels == 3:
        output = np.concatenate((output,
                                 np.ones(output.shape[:2] + (1,), np.float32)), axis=-1)
    if payload.alpha_mode == 'premultiplied' and channels == 4:
        # The QImage RGBA8888 and GL software paths expect straight alpha.
        alpha = output[..., 3:4]
        rgb = np.divide(output[..., :3], alpha,
                        out=np.zeros_like(output[..., :3]), where=alpha > 1e-8)
        output = np.concatenate((rgb, alpha), axis=-1)
    return np.asarray(output, np.float32), status


def precision_display_image(payload, color_state=None, legacy_grade=None,
                            target_size=None, max_pixels=MAX_PREVIEW_PIXELS):
    """Owned, bounded QImage in display sRGB/OCIO output, plus status label."""
    if QtGui is None:
        raise RuntimeError('Qt is required for an HDR display QImage.')
    pixels, status = precision_display_pixels(
        payload, color_state, legacy_grade, target_size, max_pixels)
    data = np.ascontiguousarray(np.clip(
        np.nan_to_num(pixels, nan=0.0, posinf=1.0, neginf=0.0) * 255.0 + 0.5,
        0, 255).astype(np.uint8))
    height, width = data.shape[:2]
    image = QtGui.QImage(data.data, width, height, width * 4,
                         QtGui.QImage.Format_RGBA8888)
    return image.copy(), status


def is_neutral(params):
    if not params:
        return True
    return all(abs(float(params.get(key, 0.0))) < 1e-6 for key in NEUTRAL)


def _qimage_to_array(image):
    """RGB888 QImage -> contiguous HxWx3 uint8 array (handles row padding)."""
    image = image.convertToFormat(QtGui.QImage.Format_RGB888)
    width, height, stride = image.width(), image.height(), image.bytesPerLine()
    ptr = image.constBits()
    try:
        ptr.setsize(height * stride)        # PySide2 returns a sip.voidptr
    except AttributeError:
        pass                                # PySide6 returns a memoryview
    flat = np.frombuffer(ptr, np.uint8, count=height * stride)
    rows = flat.reshape(height, stride)
    return rows[:, :width * 3].reshape(height, width, 3)


def grade_pixmap(pixmap, params):
    """Return a graded copy of ``pixmap``; the original when neutral/unavailable."""
    if pixmap is None or pixmap.isNull() or not HAVE_NUMPY or is_neutral(params):
        return pixmap
    try:
        source = _qimage_to_array(pixmap.toImage()).astype(np.float32) / 255.0
    except Exception:
        return pixmap
    exposure = float(params.get('exposure', 0.0))        # stops
    contrast = float(params.get('contrast', 0.0)) / 100.0
    saturation = 1.0 + float(params.get('saturation', 0.0)) / 100.0
    temperature = float(params.get('temperature', 0.0)) / 100.0

    out = source * (2.0 ** exposure)
    if abs(contrast) > 1e-6:
        out = (out - 0.5) * (1.0 + contrast) + 0.5
    if abs(temperature) > 1e-6:
        # Warm shifts red up and blue down; cool does the reverse.
        out[..., 0] *= (1.0 + 0.2 * temperature)
        out[..., 2] *= (1.0 - 0.2 * temperature)
    if abs(saturation - 1.0) > 1e-6:
        luma = (0.2126 * out[..., 0] + 0.7152 * out[..., 1] +
                0.0722 * out[..., 2])[..., None]
        out = luma + (out - luma) * saturation
    out = np.clip(out * 255.0, 0, 255).astype(np.uint8)
    out = np.ascontiguousarray(out)
    height, width = out.shape[:2]
    image = QtGui.QImage(out.data, width, height, width * 3,
                         QtGui.QImage.Format_RGB888)
    return QtGui.QPixmap.fromImage(image.copy())


class ColorCorrectionDialog(QtWidgets.QDialog if QtWidgets else object):
    """Live exposure/contrast/saturation/temperature sliders."""

    valuesChanged = Signal(object, bool) if Signal else None  # params dict, enabled

    SLIDERS = (
        ('exposure', 'Exposure', -300, 300, 0, 100.0),      # stops *100
        ('contrast', 'Contrast', -100, 100, 0, 1.0),
        ('saturation', 'Saturation', -100, 100, 0, 1.0),
        ('temperature', 'Temperature', -100, 100, 0, 1.0),
    )

    def __init__(self, parent=None):
        super(ColorCorrectionDialog, self).__init__(parent)
        self.setWindowTitle('Color Correction')
        self._sliders = {}
        self._scales = {}
        self._value_labels = {}
        layout = QtWidgets.QVBoxLayout(self)

        self.enable_check = QtWidgets.QCheckBox('Enable color correction')
        layout.addWidget(self.enable_check)

        grid = QtWidgets.QGridLayout()
        for row, (key, label, lo, hi, default, scale) in enumerate(self.SLIDERS):
            slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            slider.setRange(lo, hi)
            slider.setValue(default)
            value_label = QtWidgets.QLabel('0')
            value_label.setMinimumWidth(40)
            value_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            grid.addWidget(QtWidgets.QLabel(label), row, 0)
            grid.addWidget(slider, row, 1)
            grid.addWidget(value_label, row, 2)
            self._sliders[key] = slider
            self._scales[key] = scale
            self._value_labels[key] = value_label
            slider.valueChanged.connect(self._emit)
        layout.addLayout(grid)

        buttons = QtWidgets.QHBoxLayout()
        reset = QtWidgets.QPushButton('Reset')
        reset.clicked.connect(self.reset_values)
        buttons.addStretch(1)
        buttons.addWidget(reset)
        layout.addLayout(buttons)

        if not HAVE_NUMPY:
            self.enable_check.setChecked(False)
            self.enable_check.setEnabled(False)
            note = QtWidgets.QLabel(
                'NumPy is required in Maya Python for color correction.')
            note.setWordWrap(True)
            layout.addWidget(note)

        self.enable_check.toggled.connect(self._emit)

    def values(self):
        return {key: self._sliders[key].value() / self._scales[key]
                for key in self._sliders}

    def reset_values(self):
        for key, slider in self._sliders.items():
            slider.blockSignals(True)
            slider.setValue(0)
            slider.blockSignals(False)
        self._emit()

    def _emit(self, *args):
        params = self.values()
        for key, label in self._value_labels.items():
            value = params[key]
            label.setText('{:+.2f}'.format(value) if key == 'exposure'
                          else '{:+d}'.format(int(round(value))))
        enabled = bool(self.enable_check.isChecked()) and HAVE_NUMPY
        self.valuesChanged.emit(params, enabled)
