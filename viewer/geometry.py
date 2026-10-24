"""Qt-independent primary-view presentation geometry.

Scene storage stays in *native, post-flip pixel coordinates* for crop, tracking,
annotations and selection. Pixel aspect (horizontal source-pixel width) is
applied before a clockwise quarter-turn. Geometry affects presentation only:
no image re-sampling, exported crop rewriting or video decoder rotation occurs.
Qt's QGraphicsView applies this one affine transform to every scene item so
overlay and image coordinates cannot diverge under PAR/rotation/zoom.
"""
from __future__ import absolute_import

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class ViewGeometry(object):
    width: float
    height: float
    pixel_aspect: float = 1.0
    rotation: int = 0

    def __post_init__(self):
        width, height, par = (float(self.width), float(self.height),
                              float(self.pixel_aspect))
        if not all(math.isfinite(value) for value in (width, height, par)):
            raise ValueError('Image geometry must be finite.')
        if width < 0 or height < 0:
            raise ValueError('Native image dimensions must be nonnegative.')
        if not 1.0 / 64.0 <= par <= 64.0:
            raise ValueError('Pixel aspect must be in [1/64, 64].')
        angle = float(self.rotation)
        if not math.isfinite(angle) or angle % 90 != 0:
            raise ValueError('Only multiples of 90 degrees are supported.')
        object.__setattr__(self, 'width', width)
        object.__setattr__(self, 'height', height)
        object.__setattr__(self, 'pixel_aspect', par)
        object.__setattr__(self, 'rotation', int(angle) % 360)

    def display_size(self):
        """Physical oriented size in square *display* pixels (not Qt viewport)."""
        w, h = self.width * self.pixel_aspect, self.height
        return (h, w) if self.rotation in (90, 270) else (w, h)

    def scene_to_display(self, x, y):
        """Scene native post-flip coordinates -> oriented physical coordinates."""
        x, y = float(x), float(y)
        u = self.pixel_aspect * x
        w, h = self.width * self.pixel_aspect, self.height
        if self.rotation == 90:
            return h - y, u
        if self.rotation == 180:
            return w - u, h - y
        if self.rotation == 270:
            return y, w - u
        return u, y

    def display_to_scene(self, x, y):
        """Exact inverse; boundary coordinates are not rounded to pixel ints."""
        x, y = float(x), float(y)
        w, h = self.width * self.pixel_aspect, self.height
        if self.rotation == 90:
            return y / self.pixel_aspect, h - x
        if self.rotation == 180:
            return (w - x) / self.pixel_aspect, h - y
        if self.rotation == 270:
            return (w - y) / self.pixel_aspect, x
        return x / self.pixel_aspect, y

    def source_to_display(self, x, y, flip_h=False, flip_v=False):
        """Source sample/continuous-pixel coordinates before the viewer flip."""
        sx = self.width - float(x) if flip_h else float(x)
        sy = self.height - float(y) if flip_v else float(y)
        return self.scene_to_display(sx, sy)

    def display_to_source(self, x, y, flip_h=False, flip_v=False):
        sx, sy = self.display_to_scene(x, y)
        return (self.width - sx if flip_h else sx,
                self.height - sy if flip_v else sy)

    def qt_transform(self, QtGui):
        """Create exact native scene -> display QTransform; accepts either Qt5/6."""
        p, w, h = self.pixel_aspect, self.width, self.height
        if self.rotation == 90:
            return QtGui.QTransform(0, p, -1, 0, h, 0)
        if self.rotation == 180:
            return QtGui.QTransform(-p, 0, 0, -1, p * w, h)
        if self.rotation == 270:
            return QtGui.QTransform(0, -p, 1, 0, 0, p * w)
        return QtGui.QTransform(p, 0, 0, 1, 0, 0)


@dataclass(frozen=True)
class ComparisonGeometry:
    """A member mapped into the reference image's *physical* pixel field.

    The first plate defines canvas coverage, not an invented square-pixel
    source. A narrower plate occupies less of the same physical area, centred
    with transparent outside samples. This is deliberately independent of
    orientation/flip (owned by the shared ViewGeometry at the primary viewer).
    GRID cells explicitly stretch to fit, so callers should not rectify GRID.
    """
    reference: ViewGeometry
    member: ViewGeometry

    def __post_init__(self):
        if self.reference.rotation or self.member.rotation:
            raise ValueError('Comparison member rotations require explicit rectification.')
        if not min(self.reference.width, self.reference.height,
                   self.member.width, self.member.height) > 0:
            raise ValueError('Comparison geometry requires positive image dimensions.')

    def canvas_to_member_uv(self, u, v):
        """Return original native normalized sample coordinates, or None.

        Canvas UV is referenced to the first source's physical width/height.
        No interpolation, image resampling or color transform occurs here.
        """
        u, v = float(u), float(v)
        if not all(math.isfinite(x) and 0 <= x < 1 for x in (u, v)):
            return None
        physical_x = (u - .5) * self.reference.width * self.reference.pixel_aspect
        physical_y = (v - .5) * self.reference.height
        member_u = .5 + physical_x / (self.member.width * self.member.pixel_aspect)
        member_v = .5 + physical_y / self.member.height
        if not 0 <= member_u < 1 or not 0 <= member_v < 1:
            return None
        return (member_u, member_v)


def comparison_pixel_owners(mode, uv, width, height, count=2, parameters=None,
                            cells=None):
    """Actual composite pixel ownership on the integer output raster.

    Returns (pixel_x, pixel_y, [(member_index, member_canvas_uv), ...]).
    UV is snapped to the displayed output pixel center before deciding
    ownership. Wipe uses Python round(position*size), split uses floor(size/2),
    checker uses integer tile parity, grid rounds every cell edge separately.
    These are the CPU compositor's exact positional decisions, NOT continuous
    half-screen approximations. GRID samples native pixels directly at a tile
    pixel centre (no whole-canvas intermediate resize). SPLIT uses the full
    intermediate canvas via legacy top-left or precision pixel-centre resize
    chosen by the parameters['pixel_centers'] switch. Rectification follows.
    """
    import math
    if (type(width) is not int or type(height) is not int or
            not 1 <= width <= 32768 or not 1 <= height <= 32768 or
            type(count) is not int or not 1 <= count <= 32 or
            not isinstance(uv, (tuple, list)) or len(uv) != 2):
        raise ValueError('Comparison pixel ownership needs bounded output dimensions and UV.')
    u, v = float(uv[0]), float(uv[1])
    if not math.isfinite(u) or not math.isfinite(v) or not (
            0 <= u < 1 and 0 <= v < 1):
        return None
    x, y = min(width - 1, int(u * width)), min(height - 1, int(v * height))
    params = parameters or {}

    def all_owners():
        return [(i, ((x + .5) / width, (y + .5) / height))
                for i in range(count)]

    if mode == 'grid':
        if cells is None:
            raise ValueError('Grid ownership requires compositor grid cells.')
        # Draws in authored order; last member owns any overlapping rounded
        # edge pixel; empty background has NO original source.
        owner = None
        for i, (cx, cy, cw, ch) in enumerate(cells[:count]):
            x0 = max(0, min(width, int(round(cx * width))))
            y0 = max(0, min(height, int(round(cy * height))))
            x1 = max(0, min(width, int(round((cx + cw) * width))))
            y1 = max(0, min(height, int(round((cy + ch) * height))))
            if x0 <= x < x1 and y0 <= y < y1:
                owner = (i, (((x - x0 + .5) / (x1 - x0)),
                             ((y - y0 + .5) / (y1 - y0))))
        return x, y, [] if owner is None else [owner]
    if mode == 'split':
        orientation = params.get('orientation', 'vertical')
        if orientation not in ('vertical', 'horizontal'):
            raise ValueError('Split orientation must be vertical or horizontal.')
        axis, length = (x, width) if orientation == 'vertical' else (y, height)
        boundary = length // 2
        selected = 0 if axis < boundary else 1
        if selected >= count:
            return x, y, []
        first = 0 if selected == 0 else boundary
        extent = boundary if selected == 0 else length - boundary
        if not extent:
            return x, y, []
        if params.get('pixel_centers', False):
            native_axis = ((2 * (axis - first) + 1) * length //
                           (2 * extent))
        else:
            native_axis = (axis - first) * length // extent
        sx, sy = ((native_axis, y) if orientation == 'vertical'
                  else (x, native_axis))
        return x, y, [(selected, ((sx + .5) / width, (sy + .5) / height))]
    if mode in ('wipe', 'checker'):
        if mode == 'wipe':
            orientation = params.get('orientation', 'vertical')
            if orientation not in ('vertical', 'horizontal'):
                raise ValueError('Wipe orientation must be vertical or horizontal.')
            position = max(0.0, min(1.0, float(params.get('wipe', .5))))
            axis = x if orientation == 'vertical' else y
            length = width if orientation == 'vertical' else height
            selected = 0 if axis < int(round(position * length)) else 1
        else:
            size = max(1, int(params.get('checker_size', 64)))
            selected = (x // size + y // size) % 2
        return x, y, [all_owners()[selected]] if selected < count else []
    if mode in ('a_b', 'flicker'):
        # Caller resolves selected member at its actual session frame.
        selected = int(params.get('active', 0))
        return x, y, [all_owners()[selected]] if 0 <= selected < count else []
    return x, y, all_owners()


__all__ = ['ViewGeometry', 'ComparisonGeometry', 'comparison_pixel_owners']
