# -*- coding: utf-8 -*-
"""Qt-free inspection of source/working/display pixels with explicit stage."""
from __future__ import absolute_import

import colorsys
from dataclasses import dataclass

import numpy as np

from ..core.frame_payload import FramePayload


@dataclass(frozen=True)
class PixelSample:
    source_id: str
    source_frame: int
    generation: int
    x: int
    y: int
    rgba: tuple
    hsv: tuple
    luminance: float
    dtype: str
    alpha_mode: str
    stage: str
    tile: int = 0


def _rgba(pixel):
    values = [float(c) for c in np.atleast_1d(pixel)]
    if len(values) == 1:
        return (values[0], values[0], values[0], 1.0)
    if len(values) == 2:
        return (values[0], values[0], values[0], values[1])
    if len(values) == 3:
        return tuple(values) + (1.0,)
    return tuple(values[:4])


class SampleService(object):
    """Stage-aware sampler; use the shared geometry mapping before calling."""

    def sample(self, payload, x, y, coordinate='pixel', stage=None,
               color_state=None, tile=0):
        if not isinstance(payload, FramePayload):
            raise TypeError('SampleService requires a FramePayload.')
        if stage is not None and stage != payload.stage:
            from .color import process_payload
            payload = process_payload(payload, color_state, stage=stage)
        h, w = payload.pixels.shape[:2]
        if coordinate == 'normalized':
            x, y = float(x) * w, float(y) * h
        elif coordinate != 'pixel':
            raise ValueError('Expected pixel or normalized coordinates.')
        ix, iy = int(np.floor(x)), int(np.floor(y))
        if ix < 0 or iy < 0 or ix >= w or iy >= h:
            raise ValueError('Sample coordinate is outside the image.')
        pixel = np.atleast_1d(payload.pixels[iy, ix])
        if payload.pixels.dtype.kind in 'ui':
            pixel = pixel.astype(np.float64) / np.iinfo(payload.pixels.dtype).max
        rgba = _rgba(pixel)
        rgb = rgba[:3]
        hsv = colorsys.rgb_to_hsv(*(max(0.0, min(1.0, c)) for c in rgb))
        luminance = sum(c * weight for c, weight in zip(rgb, (0.2126, 0.7152, 0.0722)))
        return PixelSample(payload.source_id, payload.source_frame,
                           payload.generation, ix, iy, rgba, hsv,
                           float(luminance), str(payload.dtype),
                           payload.alpha_mode, payload.stage, int(tile))


def histogram(payload, bins=256, channel='luminance', maximum_pixels=262144):
    """Bounded diagnostic histogram, returning edges and counts (no Qt/GL)."""
    arr = np.asarray(payload.pixels)
    stride = max(1, int(np.ceil(np.sqrt(arr.shape[0] * arr.shape[1] /
                                        float(maximum_pixels)))))
    view = arr[::stride, ::stride]
    if view.ndim == 2:
        values = view
    elif channel == 'luminance' and view.shape[2] >= 3:
        values = np.sum(view[..., :3] * [0.2126, 0.7152, 0.0722], axis=-1)
    else:
        index = {'r': 0, 'g': 1, 'b': 2, 'a': 3}.get(channel)
        if index is None or index >= view.shape[2]:
            raise ValueError('Invalid histogram channel {}'.format(channel))
        values = view[..., index]
    if values.dtype.kind in 'ui':
        values = values.astype(np.float32) / np.iinfo(values.dtype).max
    return np.histogram(values[np.isfinite(values)], bins=bins, range=(0.0, 1.0))
