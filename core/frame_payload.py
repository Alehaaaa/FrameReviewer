# -*- coding: utf-8 -*-
"""Immutable, owned decoded frame pixels and their source/time identity.

The backing memory is an immutable ``bytes`` allocation.  Consumers can share
the array across threads without a decoder recycling it or a caller modifying
it behind the cache's back.  Color processing creates a new payload.
"""
from __future__ import absolute_import

from dataclasses import dataclass, field
from types import MappingProxyType
import math

import numpy as np


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(v) for v in value)
    return value


def _window(value, width, height):
    if value is None:
        return (0, 0, width, height)
    if hasattr(value, 'to_tuple'):
        value = value.to_tuple()
    result = tuple(int(x) for x in value)
    if len(result) != 4 or result[2] < 0 or result[3] < 0:
        raise ValueError('A pixel window must be (x, y, width, height).')
    return result


@dataclass(frozen=True)
class FrameGeometry:
    width: int
    height: int
    pixel_aspect: float = 1.0
    data_window: tuple = None
    display_window: tuple = None

    def __post_init__(self):
        if (self.width <= 0 or self.height <= 0 or
                not math.isfinite(self.pixel_aspect) or self.pixel_aspect <= 0):
            raise ValueError('Frame dimensions and pixel aspect must be positive.')
        object.__setattr__(self, 'data_window',
                           _window(self.data_window, self.width, self.height))
        object.__setattr__(self, 'display_window',
                           _window(self.display_window, self.width, self.height))

    @classmethod
    def from_geometry(cls, geometry, width, height):
        if geometry is None:
            return cls(width, height)
        if isinstance(geometry, cls):
            return geometry
        return cls(int(geometry.width), int(geometry.height),
                   float(getattr(geometry, 'pixel_aspect', 1.0)),
                   getattr(geometry, 'data_window', None),
                   getattr(geometry, 'display_window', None))

    def to_dict(self):
        return {'width': self.width, 'height': self.height,
                'pixel_aspect': self.pixel_aspect,
                'data_window': list(self.data_window),
                'display_window': list(self.display_window)}

    @property
    def display_size(self):
        """Native display canvas width/height, before PAR and viewer rotation."""
        return self.display_window[2:]


@dataclass(frozen=True)
class FramePayload:
    pixels: object = field(repr=False, compare=False)
    source_id: str = ''
    source_frame: int = 0
    generation: int = 0
    tier: str = 'source'
    geometry: object = None
    color_metadata: object = field(default_factory=dict, compare=False)
    alpha_mode: str = 'straight'
    stage: str = 'source'

    def __post_init__(self):
        arr = np.asarray(self.pixels)
        if arr.ndim not in (2, 3) or arr.size == 0:
            raise ValueError('Frame pixels must have shape (height, width[, channels]).')
        if arr.ndim == 3 and arr.shape[2] not in (1, 2, 3, 4):
            raise ValueError('Frame pixels must have between one and four channels.')
        if arr.dtype.kind not in 'uif' or arr.dtype.itemsize not in (1, 2, 4, 8):
            raise TypeError('Unsupported pixel dtype: {}'.format(arr.dtype))
        arr = np.ascontiguousarray(arr)
        immutable = np.frombuffer(arr.tobytes(order='C'), dtype=arr.dtype).reshape(arr.shape)
        height, width = arr.shape[:2]
        geom = FrameGeometry.from_geometry(self.geometry, width, height)
        if (geom.width, geom.height) != (width, height):
            raise ValueError('Frame geometry must match decoded pixel dimensions.')
        if self.alpha_mode not in ('straight', 'premultiplied', 'opaque'):
            raise ValueError('Unknown alpha mode {}'.format(self.alpha_mode))
        if self.stage not in ('source', 'working', 'display'):
            raise ValueError('Unknown pixel stage {}'.format(self.stage))
        object.__setattr__(self, 'pixels', immutable)
        object.__setattr__(self, 'geometry', geom)
        object.__setattr__(self, 'color_metadata', _freeze(dict(self.color_metadata or {})))
        object.__setattr__(self, 'source_id', str(self.source_id))
        object.__setattr__(self, 'source_frame', int(self.source_frame))
        object.__setattr__(self, 'generation', int(self.generation))

    @property
    def dtype(self):
        return self.pixels.dtype

    @property
    def channels(self):
        return self.pixels.shape[2] if self.pixels.ndim == 3 else 1

    @property
    def identity(self):
        return (self.source_id, self.source_frame, self.generation,
                self.tier, self.stage)

    def with_pixels(self, pixels, stage=None):
        return FramePayload(pixels, self.source_id, self.source_frame,
                            self.generation, self.tier, self.geometry,
                            self.color_metadata, self.alpha_mode,
                            self.stage if stage is None else stage)

    def to_display_canvas(self, max_bytes=None):
        """Position native EXR samples on an immutable, display-window canvas."""
        from ..media.window_geometry import (MAX_CANVAS_BYTES,
                                             rectify_to_display_canvas)
        return rectify_to_display_canvas(
            self, max_bytes=MAX_CANVAS_BYTES if max_bytes is None else max_bytes)
