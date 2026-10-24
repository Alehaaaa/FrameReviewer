# -*- coding: utf-8 -*-
"""Image windows and crop geometry for precision review.

Pure Python and Qt-free, so the geometry can be reasoned about and unit tested
independently of the viewer. The module keeps the distinct concepts a review
tool must not conflate (per OpenRV):

* **source aperture** - the full pixel extent and pixel aspect of the media,
* **data window** / **display window** - the EXR-style windows that may be
  smaller or larger than the aperture,
* **viewer crop** - a non-destructive framing used for inspection,
* **export crop** - the crop baked into an output.

The viewer crop and export crop are the same :class:`CropRect` type but are held
separately so inspecting a frame never silently changes what an export writes. A
:class:`CropRect` is stored in normalized ``[0, 1]`` display coordinates so it
survives a representation relink to a different resolution.
"""
from __future__ import absolute_import


def _clamp(value, low, high):
    return max(low, min(high, value))


# Common framing aspect ratios (width / height), by label.
ASPECT_PRESETS = {
    '2.39:1': 2.39,
    '1.85:1': 1.85,
    '16:9': 16.0 / 9.0,
    '4:3': 4.0 / 3.0,
    '1:1': 1.0,
    '9:16': 9.0 / 16.0,
}


class Window(object):
    """An integer pixel window (x, y, width, height), EXR-style."""

    __slots__ = ('x', 'y', 'width', 'height')

    def __init__(self, x, y, width, height):
        self.x = int(x)
        self.y = int(y)
        self.width = max(0, int(width))
        self.height = max(0, int(height))

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y + self.height

    def to_tuple(self):
        return (self.x, self.y, self.width, self.height)

    def __eq__(self, other):
        return isinstance(other, Window) and self.to_tuple() == other.to_tuple()

    def __repr__(self):
        return 'Window({}, {}, {}, {})'.format(*self.to_tuple())


class ImageGeometry(object):
    """Source aperture plus optional data/display windows and pixel aspect."""

    def __init__(self, width, height, pixel_aspect=1.0,
                 data_window=None, display_window=None):
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.pixel_aspect = float(pixel_aspect) if pixel_aspect else 1.0
        self.display_window = display_window or Window(0, 0, self.width, self.height)
        self.data_window = data_window or Window(0, 0, self.width, self.height)

    def display_size(self):
        """Displayed size after applying pixel aspect (anamorphic unsqueeze).

        Returns floats; width is scaled by the pixel aspect so e.g. a 2x
        anamorphic plate presents at twice its stored pixel width.
        """
        return (self.width * self.pixel_aspect, float(self.height))

    def display_aspect(self):
        w, h = self.display_size()
        return w / h if h else 1.0

    def storage_aspect(self):
        return self.width / float(self.height)


class CropRect(object):
    """A normalized crop in ``[0, 1]`` display coordinates.

    ``(x, y)`` is the top-left corner, ``(w, h)`` the size, all as fractions of
    the display window. Operations return new instances; the rectangle is kept
    inside the unit square and non-empty.
    """

    __slots__ = ('x', 'y', 'w', 'h')

    def __init__(self, x=0.0, y=0.0, w=1.0, h=1.0):
        self.x = float(x)
        self.y = float(y)
        self.w = float(w)
        self.h = float(h)

    # -- constructors ------------------------------------------------------
    @classmethod
    def full(cls):
        return cls(0.0, 0.0, 1.0, 1.0)

    @classmethod
    def from_pixels(cls, px, py, pw, ph, width, height):
        width = max(1, int(width))
        height = max(1, int(height))
        return cls(px / float(width), py / float(height),
                   pw / float(width), ph / float(height))

    # -- queries -----------------------------------------------------------
    def to_tuple(self):
        return (self.x, self.y, self.w, self.h)

    def to_pixels(self, width, height):
        """Integer pixel box ``(x, y, w, h)`` in a ``width`` x ``height`` image."""
        width = max(1, int(width))
        height = max(1, int(height))
        x = int(round(self.x * width))
        y = int(round(self.y * height))
        w = int(round(self.w * width))
        h = int(round(self.h * height))
        # Keep the box inside the image and at least one pixel.
        x = _clamp(x, 0, width - 1)
        y = _clamp(y, 0, height - 1)
        w = _clamp(w, 1, width - x)
        h = _clamp(h, 1, height - y)
        return (x, y, w, h)

    def aspect(self, frame_aspect=1.0):
        """Display aspect of the crop, given the frame's display aspect."""
        if self.h <= 0:
            return 1.0
        return (self.w * frame_aspect) / self.h

    def is_full(self, tol=1e-6):
        return (abs(self.x) < tol and abs(self.y) < tol and
                abs(self.w - 1.0) < tol and abs(self.h - 1.0) < tol)

    # -- operations (return new rects) -------------------------------------
    def clamped(self):
        w = _clamp(self.w, 1e-4, 1.0)
        h = _clamp(self.h, 1e-4, 1.0)
        x = _clamp(self.x, 0.0, 1.0 - w)
        y = _clamp(self.y, 0.0, 1.0 - h)
        return CropRect(x, y, w, h)

    def centered(self):
        """Center the box within the frame, keeping its size."""
        return CropRect((1.0 - self.w) / 2.0, (1.0 - self.h) / 2.0,
                        self.w, self.h).clamped()

    def scaled(self, factor):
        """Grow (``factor`` > 1, overscan) or shrink about the box center."""
        factor = max(1e-4, float(factor))
        cx = self.x + self.w / 2.0
        cy = self.y + self.h / 2.0
        w = self.w * factor
        h = self.h * factor
        return CropRect(cx - w / 2.0, cy - h / 2.0, w, h).clamped()

    def moved(self, dx, dy):
        return CropRect(self.x + dx, self.y + dy, self.w, self.h).clamped()

    def with_aspect(self, target_aspect, frame_aspect=1.0):
        """Return the largest centered crop of ``target_aspect`` that fits.

        ``target_aspect`` and ``frame_aspect`` are display (width/height) ratios.
        The crop's *display* aspect equals ``target_aspect`` regardless of the
        frame's own aspect.
        """
        target_aspect = max(1e-4, float(target_aspect))
        frame_aspect = max(1e-4, float(frame_aspect))
        # Normalized crop aspect that yields target display aspect:
        #   (w * frame_aspect) / h == target_aspect
        # Fit within the unit square: try full width first.
        w = 1.0
        h = w * frame_aspect / target_aspect
        if h > 1.0:
            h = 1.0
            w = h * target_aspect / frame_aspect
        return CropRect((1.0 - w) / 2.0, (1.0 - h) / 2.0, w, h).clamped()

    def copy(self):
        return CropRect(self.x, self.y, self.w, self.h)

    def __eq__(self, other):
        return (isinstance(other, CropRect) and
                all(abs(a - b) < 1e-9
                    for a, b in zip(self.to_tuple(), other.to_tuple())))

    def __repr__(self):
        return 'CropRect({:.4f}, {:.4f}, {:.4f}, {:.4f})'.format(*self.to_tuple())


def aspect_crop(label_or_ratio, frame_aspect=1.0):
    """Centered :class:`CropRect` for a named preset or numeric ratio."""
    if isinstance(label_or_ratio, str):
        if label_or_ratio not in ASPECT_PRESETS:
            raise ValueError('Unknown aspect preset {!r}.'.format(label_or_ratio))
        ratio = ASPECT_PRESETS[label_or_ratio]
    else:
        ratio = float(label_or_ratio)
    return CropRect.full().with_aspect(ratio, frame_aspect)
