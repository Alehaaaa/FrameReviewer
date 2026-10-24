"""Pure CPU geometry for positioning EXR data pixels on their display canvas.

EXR windows are inclusive integer rectangles on disk. FrameGeometry carries
them as signed ``(x, y, width, height)`` rectangles. A source payload owns the
dataWindow-sized array; a canvas payload owns the displayWindow-sized array.
Both retain the *original absolute* data/display windows for inspection.

All coordinates in this module use the EXR file's pixel-axis orientation. No
color transform, resampling, pixel-aspect scaling, flip or GPU path is applied.
"""
from __future__ import absolute_import

from dataclasses import dataclass
import math

MAX_CANVAS_BYTES = 256 * 1024 * 1024
MAX_WINDOW_EDGE = 16384
_INT32_MIN = -(2 ** 31)
_INT32_MAX = 2 ** 31 - 1


def _rect(value, name):
    try:
        x, y, w, h = tuple(value)
        values = tuple(int(item) for item in (x, y, w, h))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('Invalid {}: expected (x, y, width, height).'.format(
            name)) from exc
    if any(raw != fixed for raw, fixed in zip((x, y, w, h), values)):
        raise ValueError('{} must use exact integer coordinates.'.format(name))
    x, y, w, h = values
    if not (1 <= w <= MAX_WINDOW_EDGE and 1 <= h <= MAX_WINDOW_EDGE and
            _INT32_MIN <= x <= _INT32_MAX and _INT32_MIN <= y <= _INT32_MAX and
            x + w - 1 <= _INT32_MAX and y + h - 1 <= _INT32_MAX):
        raise ValueError('{} exceeds the supported signed EXR bounds.'.format(name))
    return values


@dataclass(frozen=True)
class DisplayCanvasPlacement(object):
    """Mapping from source data-local samples to destination display-local pixels.

    `intersection` is the visible rectangle in *absolute EXR* coordinates.
    `source_slices` and `canvas_slices` are NumPy [row, column] slice pairs,
    and are safe after validating the source array matches data_window.
    """

    data_window: tuple
    display_window: tuple
    max_bytes: int = MAX_CANVAS_BYTES

    def __post_init__(self):
        data = _rect(self.data_window, 'dataWindow')
        display = _rect(self.display_window, 'displayWindow')
        limit = int(self.max_bytes)
        if not 1 <= limit <= MAX_CANVAS_BYTES:
            raise ValueError('Unsupported EXR canvas allocation limit.')
        x0 = max(data[0], display[0])
        y0 = max(data[1], display[1])
        x1 = min(data[0] + data[2], display[0] + display[2])
        y1 = min(data[1] + data[3], display[1] + display[3])
        if x1 <= x0 or y1 <= y0:
            raise ValueError('EXR dataWindow and displayWindow do not overlap.')
        object.__setattr__(self, 'data_window', data)
        object.__setattr__(self, 'display_window', display)
        object.__setattr__(self, 'max_bytes', limit)

    @classmethod
    def from_geometry(cls, geometry, max_bytes=MAX_CANVAS_BYTES):
        return cls(geometry.data_window, geometry.display_window, max_bytes)

    @property
    def size(self):
        """Output canvas (width, height), regardless of source pixel aspect."""
        return self.display_window[2:]

    @property
    def intersection(self):
        data, display = self.data_window, self.display_window
        x0 = max(data[0], display[0])
        y0 = max(data[1], display[1])
        x1 = min(data[0] + data[2], display[0] + display[2])
        y1 = min(data[1] + data[3], display[1] + display[3])
        return (x0, y0, x1 - x0, y1 - y0)

    @property
    def source_slices(self):
        """Image data crop expressed in NumPy (y, x) slices."""
        x, y, w, h = self.intersection
        dx, dy = self.data_window[:2]
        return (slice(y - dy, y - dy + h), slice(x - dx, x - dx + w))

    @property
    def canvas_slices(self):
        """Destination position of the overlapping data pixels (y, x)."""
        x, y, w, h = self.intersection
        dx, dy = self.display_window[:2]
        return (slice(y - dy, y - dy + h), slice(x - dx, x - dx + w))

    def exr_to_canvas(self, x, y):
        """Absolute EXR coordinates -> display-local coordinates (subpixels)."""
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError('EXR coordinates must be finite.')
        return (x - self.display_window[0], y - self.display_window[1])

    def canvas_to_exr(self, x, y):
        """Display-local coordinates -> absolute EXR coordinates."""
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError('Display coordinates must be finite.')
        return (x + self.display_window[0], y + self.display_window[1])

    def data_to_canvas(self, x, y):
        """Data-buffer local coordinates -> display-local coordinates."""
        return self.exr_to_canvas(x + self.data_window[0],
                                  y + self.data_window[1])

    def canvas_to_data(self, x, y):
        """Display-local coordinates -> data-buffer local coordinates."""
        exr_x, exr_y = self.canvas_to_exr(x, y)
        return (exr_x - self.data_window[0], exr_y - self.data_window[1])

    def canvas_sample_to_data(self, x, y):
        """(x, y) data-buffer sample index, or None for a padded canvas pixel."""
        if not isinstance(x, int) or not isinstance(y, int):
            raise TypeError('Canvas sample coordinates must be integers.')
        if not (0 <= x < self.size[0] and 0 <= y < self.size[1]):
            return None
        source_x, source_y = self.canvas_to_data(x, y)
        if not (0 <= source_x < self.data_window[2] and
                0 <= source_y < self.data_window[3]):
            return None
        return (source_x, source_y)


def rectify_to_display_canvas(payload, max_bytes=MAX_CANVAS_BYTES):
    """Create a source-precision, immutable display-sized copy of EXR pixels.

    Source data is cropped to displayWindow and placed at its original offset.
    Empty margins contain typed zero (RGB black, alpha zero when present).
    The same EXR identity, dtype, alpha association and absolute windows are
    preserved. The payload's *raster width/height* become displayWindow size.

    Explicit FFmpeg-fallback geometry is accepted only for verified full-frame
    zero-origin EXRs; cropped/offset files require native OpenEXR decoding.
    This function never synthesizes precision from a display pixmap.
    """
    import numpy as np
    from ..core.frame_payload import FrameGeometry, FramePayload

    if not isinstance(payload, FramePayload):
        raise TypeError('Expected a FramePayload with native source pixels.')
    if payload.stage != 'source':
        raise ValueError('Canvas rectification requires source-stage pixels.')
    if payload.color_metadata.get('exr_native_windows_verified') is False:
        raise ValueError('Canvas rectification requires verified EXR windows.')
    geometry = payload.geometry
    placement = DisplayCanvasPlacement.from_geometry(geometry, max_bytes)
    rectified = bool(payload.color_metadata.get('exr_canvas_rectified'))
    if rectified:
        if (geometry.width, geometry.height) != placement.size:
            raise ValueError('Previously rectified EXR has inconsistent geometry.')
        return payload
    if (geometry.width, geometry.height) != geometry.data_window[2:]:
        raise ValueError('EXR source pixels must cover exactly the dataWindow.')
    if (payload.color_metadata.get('decoder', '').startswith('FFmpeg') and
            (geometry.data_window != geometry.display_window or
             geometry.data_window != (0, 0, geometry.width, geometry.height))):
        raise ValueError('FFmpeg EXR native cropped/window origin is unverified.')
    pixels = np.asarray(payload.pixels)
    channels = pixels.shape[2] if pixels.ndim == 3 else 1
    output_bytes = placement.size[0] * placement.size[1] * channels * pixels.dtype.itemsize
    if output_bytes > placement.max_bytes or pixels.nbytes > placement.max_bytes:
        raise ValueError('EXR display canvas exceeds the CPU allocation limit.')
    output_shape = ((placement.size[1], placement.size[0], channels)
                    if pixels.ndim == 3 else (placement.size[1], placement.size[0]))
    canvas = np.zeros(output_shape, dtype=pixels.dtype)
    canvas[placement.canvas_slices] = pixels[placement.source_slices]
    metadata = dict(payload.color_metadata)
    metadata.update(exr_canvas_rectified=True,
                    exr_canvas_coverage=placement.intersection,
                    exr_canvas_origin=placement.display_window[:2])
    display_geometry = FrameGeometry(
        placement.size[0], placement.size[1], geometry.pixel_aspect,
        geometry.data_window, geometry.display_window)
    return FramePayload(canvas, source_id=payload.source_id,
                        source_frame=payload.source_frame,
                        generation=payload.generation, tier=payload.tier,
                        geometry=display_geometry, color_metadata=metadata,
                        alpha_mode=payload.alpha_mode, stage=payload.stage)


__all__ = ['DisplayCanvasPlacement', 'rectify_to_display_canvas',
           'MAX_CANVAS_BYTES', 'MAX_WINDOW_EDGE']
