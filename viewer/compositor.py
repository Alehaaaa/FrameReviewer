# -*- coding: utf-8 -*-
"""Software (NumPy) compositor: the GPU viewer's deterministic fallback.

Pure NumPy, no Qt. It composites several source frames into one image for the
composition modes in :mod:`framereviewer.playback.composition`, and is both the
reference implementation and the explicit software fallback the GPU viewer
drops to when no usable OpenGL context is available (an OpenRV-style
requirement: never hide that fidelity changed).

Compositing semantics follow OpenRV's stack shaders, with one deliberate
review-oriented choice: ``difference`` is the **absolute** per-channel
difference ``|a - b|`` (so any change reads as brightness), and
``inverted_difference`` is ``1 - |a - b|`` (identical regions go white). OpenRV's
node is a signed ``clamp(a - b, 0, 1)``; the absolute form is what reviewers
expect from a "spot the difference" view. ``over`` matches OpenRV's premultiplied
``Over`` (``a`` over ``b``).

Buffers are processed as scene-linear float32 RGBA. HDR values and negative
values are retained until explicitly encoded for an 8-bit display.
"""
from __future__ import absolute_import

import math
import numpy as np

from ..playback import composition as composition_mod


def to_float_rgba(image):
    """Convert uint8/uint16/float or FramePayload to float32 RGBA."""
    if hasattr(image, 'pixels'):
        image = image.pixels
    arr = np.asarray(image)
    if arr.dtype.kind in 'ui':
        arr = arr.astype(np.float32) / np.iinfo(arr.dtype).max
    else:
        arr = arr.astype(np.float32)
    if arr.ndim == 2:
        arr = arr[:, :, None]
    channels = arr.shape[2]
    if channels == 1:
        arr = np.repeat(arr, 3, axis=2)
        channels = 3
    if channels == 2:
        arr = np.concatenate([np.repeat(arr[..., :1], 3, axis=2),
                              arr[..., 1:2]], axis=2)
        channels = 4
    if channels == 3:
        alpha = np.ones(arr.shape[:2] + (1,), np.float32)
        arr = np.concatenate([arr, alpha], axis=2)
    return arr[:, :, :4]


def _resize_nn(image, height, width):
    """Nearest *pixel-center* resize (not top/left-biased edge sampling).

    Maps destination center ``(i + 1/2) / N`` to an original image sample
    with ``floor(u * source_size)``. This is the same native UV indexing that
    ComparisonGeometry and the primary inspector use, including downsampling.
    """
    ih, iw = image.shape[:2]
    height, width = int(height), int(width)
    if (min(ih, iw, height, width) <= 0 or
            width * height > 16 * 1024 * 1024):
        raise ValueError('Comparison resize exceeds bounded positive CPU raster.')
    if ih == height and iw == width:
        return image
    ys = ((2 * np.arange(height, dtype=np.int64) + 1) * ih //
          (2 * height)).clip(0, ih - 1)
    xs = ((2 * np.arange(width, dtype=np.int64) + 1) * iw //
          (2 * width)).clip(0, iw - 1)
    return image[ys[:, None], xs[None, :]]


def _resize_nn_legacy(image, height, width):
    """Compatibility for direct historical ``composite('split')`` calls.

    Precision ``composite_working`` opts into physically aligned centers.
    Standalone legacy RGB pixmap helpers retain their preexisting indexing,
    since they do not carry native geometry or a precision-stage contract.
    """
    ih, iw = image.shape[:2]
    if min(ih, iw, height, width) < 1 or width * height > 16 * 1024 * 1024:
        raise ValueError('Comparison resize exceeds bounded positive CPU raster.')
    if (ih, iw) == (height, width):
        return image
    ys = (np.arange(height) * ih // height).clip(0, ih - 1)
    xs = (np.arange(width) * iw // width).clip(0, iw - 1)
    return image[ys[:, None], xs[None, :]]


def _match(image, shape):
    """Resize ``image`` (float RGBA) to ``shape`` (h, w, 4) if needed."""
    h, w = shape[0], shape[1]
    if image.shape[0] == h and image.shape[1] == w:
        return image
    return _resize_nn(image, h, w)


def rectify_comparison_member(image, reference, member, width, height):
    """Sample original pixels in the first member's physical image field.

    This is the CPU reference used by live precision comparison and native
    original-member inspection. The first image defines canvas UV; PAR is
    honoured *before* sampling either RGB or alpha. Out-of-coverage pixels
    have true transparent RGBA=0, never an edge smear or invented HDR sample.
    """
    from .geometry import ComparisonGeometry
    from .geometry import ViewGeometry
    ref = ViewGeometry(*reference)
    src = ViewGeometry(*member)
    ComparisonGeometry(ref, src)  # shared physical-field validation
    pixels = to_float_rgba(image)
    if (type(width) is not int or type(height) is not int or
            min(width, height) < 1 or width * height > 16 * 1024 * 1024):
        raise ValueError('Comparison output exceeds bounded positive CPU raster.')
    # Destination pixel centers are physical positions in the first image.
    u = (np.arange(width, dtype=np.float64) + .5) / width
    v = (np.arange(height, dtype=np.float64) + .5) / height
    x = .5 + (u - .5) * (ref.width * ref.pixel_aspect) / (src.width * src.pixel_aspect)
    y = .5 + (v - .5) * ref.height / src.height
    inside_x = (x >= 0) & (x < 1)
    inside_y = (y >= 0) & (y < 1)
    # Native geometry and the in-memory raster can differ legitimately: a
    # 4K member may arrive as a 1K display proxy. Its pixels still cover the
    # *whole native physical field*. Sample its own raster at normalized UV;
    # never multiply by frozen native width or infer an extra view stretch.
    # Match ComparisonGeometry.canvas_to_member_uv and managed-v2 native
    # sampling at the exact same binary floating point pixel center. Do not
    # snap borderline values independently of their current inspector mapping.
    sx = np.clip(np.floor(x * pixels.shape[1]).astype(np.int64),
                 0, pixels.shape[1] - 1)
    sy = np.clip(np.floor(y * pixels.shape[0]).astype(np.int64),
                 0, pixels.shape[0] - 1)
    out = pixels[sy[:, None], sx[None, :]].copy()
    out[~inside_y, :, :] = 0
    out[:, ~inside_x, :] = 0
    return out


def difference(a, b, inverted=False):
    """Absolute per-channel difference of two frames (optionally inverted)."""
    a = to_float_rgba(a)
    b = _match(to_float_rgba(b), a.shape)
    diff = np.abs(a[:, :, :3] - b[:, :, :3])
    if inverted:
        diff = np.maximum(1.0 - diff, 0.0)
    alpha = np.ones(a.shape[:2] + (1,), np.float32)
    return np.concatenate([diff, alpha], axis=2)


def over(a, b):
    """Premultiplied ``a`` over ``b`` (OpenRV ``Over``)."""
    a = to_float_rgba(a)
    b = _match(to_float_rgba(b), a.shape)
    inv = 1.0 - a[:, :, 3:4]
    rgb = a[:, :, :3] + b[:, :, :3] * inv
    alpha = a[:, :, 3:4] + b[:, :, 3:4] * inv
    return np.concatenate([rgb, alpha], axis=2)


def blend(a, b, mix=0.5):
    """Interpolate scene-linear pixels (mix 0 shows A, 1 shows B)."""
    a = to_float_rgba(a)
    b = _match(to_float_rgba(b), a.shape)
    mix = float(np.clip(mix, 0.0, 1.0))
    return a * (1.0 - mix) + b * mix


def wipe(a, b, position=0.5, orientation='vertical', split=False):
    """Show A before a boundary, B after; split and wipe share pixel math."""
    a = to_float_rgba(a)
    b = _match(to_float_rgba(b), a.shape)
    h, w = a.shape[:2]
    position = float(np.clip(position, 0.0, 1.0))
    if orientation == 'vertical':
        mask = np.arange(w)[None, :, None] < int(round(position * w))
    elif orientation == 'horizontal':
        mask = np.arange(h)[:, None, None] < int(round(position * h))
    else:
        raise ValueError('Wipe orientation must be vertical or horizontal.')
    return np.where(mask, a, b)


def split(a, b, orientation='vertical', pixel_centers=False):
    """Display each image in half; precision consumers use center sampling."""
    a = to_float_rgba(a)
    b = _match(to_float_rgba(b), a.shape)
    height, width = a.shape[:2]
    result = np.empty_like(a)
    resize = _resize_nn if pixel_centers else _resize_nn_legacy
    if orientation == 'vertical':
        first = width // 2
        if first:
            result[:, :first] = resize(a, height, first)
        if width - first:
            result[:, first:] = resize(b, height, width - first)
    elif orientation == 'horizontal':
        first = height // 2
        if first:
            result[:first] = resize(a, first, width)
        if height - first:
            result[first:] = resize(b, height - first, width)
    else:
        raise ValueError('Split orientation must be vertical or horizontal.')
    return result


def checker(a, b, size=64):
    a = to_float_rgba(a)
    b = _match(to_float_rgba(b), a.shape)
    size = max(1, int(size))
    y, x = np.indices(a.shape[:2])
    mask = ((x // size + y // size) % 2 == 0)[..., None]
    return np.where(mask, a, b)


def grid(images, width, height, background=(0.0, 0.0, 0.0, 0.0),
         fit=False, cells=None):
    """Stretch each image across its complete grid cell, preserving its alpha.

    Cell placement follows :meth:`Composition.grid_cells` (the short last row
    is centered). Unoccupied pixels stay transparent. Pixel boundaries are
    rounded from the *edges* of each rectangle to avoid seams and overdraw.
    The legacy ``fit`` argument is accepted but ignored: grids always stretch.
    """
    canvas = np.empty((height, width, 4), np.float32)
    canvas[:] = np.asarray(background, np.float32)
    n = len(images)
    if n == 0:
        return canvas
    if cells is None:
        dummy = composition_mod.Composition(
            composition_mod.GRID,
            [composition_mod.CompositionMember('m%d' % i, 1) for i in range(n)])
        cells = dummy.grid_cells()
    for image, (cx, cy, cw, ch) in zip(images, cells):
        img = to_float_rgba(image)
        x0 = max(0, min(width, int(round(cx * width))))
        y0 = max(0, min(height, int(round(cy * height))))
        x1 = max(0, min(width, int(round((cx + cw) * width))))
        y1 = max(0, min(height, int(round((cy + ch) * height))))
        if x1 > x0 and y1 > y0:
            canvas[y0:y1, x0:x1] = _resize_nn(img, y1 - y0, x1 - x0)
    return canvas


def composite(mode, images, width=None, height=None, **parameters):
    """Composite ``images`` per a composition mode. Returns float32 RGBA.

    DIFFERENCE / INVERTED_DIFFERENCE use the first two images; GRID tiles all of
    them into a ``width`` x ``height`` canvas (defaults to the first image's
    size); any other mode returns the first image.
    """
    if not images:
        raise ValueError('composite needs at least one image.')
    if mode == composition_mod.DIFFERENCE and len(images) >= 2:
        return difference(images[0], images[1])
    if mode == composition_mod.INVERTED_DIFFERENCE and len(images) >= 2:
        return difference(images[0], images[1], inverted=True)
    if mode == composition_mod.GRID:
        first = to_float_rgba(images[0])
        w = width or first.shape[1]
        h = height or first.shape[0]
        return grid(images, w, h, cells=parameters.get('cells'))
    if len(images) >= 2:
        if mode in ('a_b', 'ab', 'a', 'b', 'flicker'):
            active = parameters.get('active', 1 if mode == 'b' else 0)
            return to_float_rgba(images[int(active) % 2])
        if mode == 'blend':
            return blend(images[0], images[1], parameters.get('blend', 0.5))
        if mode == 'split':
            return split(images[0], images[1],
                         parameters.get('orientation', 'vertical'),
                         parameters.get('pixel_centers', False))
        if mode == 'wipe':
            return wipe(images[0], images[1], parameters.get('wipe', 0.5),
                        parameters.get('orientation', 'vertical'))
        if mode == 'checker':
            return checker(images[0], images[1],
                           parameters.get('checker_size', 64))
        if mode == 'over':
            return over(images[0], images[1])
    return to_float_rgba(images[0])


def composite_working(mode, frames, color_state=None, width=None,
                      height=None, source_color_states=None,
                      source_geometries=None, reference_geometry=None,
                      **parameters):
    """Compose native frames as working-linear float32 straight-alpha RGBA.

    Source HDR/negative values survive; uint8 members are interpreted as
    sRGB. Transforms happen before comparison and the caller performs the
    working-to-display transform once *after* compositing. OpenRV likewise
    composes in linear light with explicit premultiplied Over handling.
    """
    if not frames:
        raise ValueError('composite_working needs at least one frame.')
    from dataclasses import replace
    from .color import ColorPipelineState, process_pixels, _grade
    supplied = (dict(color_state) if isinstance(color_state, dict) else
                color_state.to_dict() if isinstance(color_state, ColorPipelineState)
                else {})
    state = ColorPipelineState.from_dict(supplied)
    prepared = []
    source_color_states = list(source_color_states or ())
    source_geometries = list(source_geometries or ())
    def geometry(index, frame):
        if index < len(source_geometries) and source_geometries[index] is not None:
            value = source_geometries[index]
            candidate = (float(value[0]), float(value[1]), float(value[2]))
        else:
            shape = frame.pixels.shape if hasattr(frame, 'pixels') else np.asarray(frame).shape
            geom = getattr(frame, 'geometry', None)
            candidate = (float(shape[1]), float(shape[0]),
                         float(getattr(geom, 'pixel_aspect', 1.0)))
        if (not all(math.isfinite(n) for n in candidate) or
                min(candidate[:2]) <= 0 or
                not 1.0 / 64.0 <= candidate[2] <= 64.0):
            raise ValueError('Comparison native member geometry is invalid.')
        return candidate
    member_geometry = [geometry(index, frame) for index, frame in enumerate(frames)]
    # For A/B and Flicker the only displayed source is the selected member,
    # but its physical field still belongs to source A. An explicit reference
    # therefore removes the need to decode/transform an invisible second
    # member just to establish physical coordinates. All other modes retain
    # their established first-member contract.
    if reference_geometry is not None:
        if mode not in (composition_mod.A_B, composition_mod.FLICKER) or len(frames) != 1:
            raise ValueError('Explicit comparison reference requires one selected A/B or Flicker member.')
        reference = tuple(float(n) for n in reference_geometry)
        from .geometry import ViewGeometry
        ViewGeometry(*reference)
        if min(reference[:2]) <= 0:
            raise ValueError('Comparison reference dimensions must be positive.')
    else:
        reference = member_geometry[0]
    shape = frames[0].pixels.shape if hasattr(frames[0], 'pixels') else np.asarray(frames[0]).shape
    if width is None:
        width = int(shape[1])
    if height is None:
        height = int(shape[0])
    if (type(width) is not int or type(height) is not int or
            min(width, height) <= 0 or width * height > 16 * 1024 * 1024):
        raise ValueError('Comparison output exceeds bounded positive CPU raster.')
    needs_rectification = (mode != composition_mod.GRID and
                           any(abs(g[2] - reference[2]) > 1e-8 or
                               abs(g[0] * g[2] - reference[0] * reference[2]) > 1e-6 or
                               abs(g[1] - reference[1]) > 1e-6
                               for g in member_geometry))
    for index, frame in enumerate(frames):
        stage = getattr(frame, 'stage', 'source')
        if stage == 'display':
            raise ValueError('Display-referred pixels cannot enter working composition.')
        metadata = getattr(frame, 'color_metadata', {})
        input_space = (metadata.get('input_space', 'linear')
                       if hasattr(frame, 'pixels') else 'sRGB')
        local = (source_color_states[index]
                 if index < len(source_color_states) else None)
        if isinstance(local, ColorPipelineState):
            local = local.to_dict()
        local = dict(local or {})
        # Per-member configs may assign distinct camera/input color spaces;
        # never silently take one source's state for the other. The shared
        # comparison working space is mandatory for apples-to-apples pixels.
        source_settings = dict(supplied)
        source_settings.update({key: value for key, value in local.items()
                                if key in ('input_space', 'config', 'ocio_context',
                                           'bypass_input')})
        # The common grade's ColorPipelineState has a default sRGB input.
        # Never let that default override an EXR's own scene-linear metadata.
        # Source-specific input overrides are explicit and take precedence.
        source_settings['input_space'] = local.get('input_space', input_space)
        source_state = replace(ColorPipelineState.from_dict(source_settings),
                               bypass_grade=True)
        pixels = frame.pixels if hasattr(frame, 'pixels') else frame
        converted = process_pixels(
            pixels, source_state, stage='working', source_stage=stage,
            alpha_mode=getattr(frame, 'alpha_mode', 'straight'))
        rgba = to_float_rgba(converted).copy()
        if getattr(frame, 'alpha_mode', 'straight') == 'premultiplied':
            alpha = rgba[..., 3:4]
            rgba[..., :3] = np.divide(rgba[..., :3], alpha,
                                      out=np.zeros_like(rgba[..., :3]),
                                      where=alpha > 1e-8)
        # Fully transparent pixels must never contribute hidden RGB to split,
        # wipe, difference or a subsequently graded comparison canvas.
        rgba[..., :3] = np.where(rgba[..., 3:4] > 1e-8,
                                     rgba[..., :3], 0.0)
        if needs_rectification:
            rgba = rectify_comparison_member(
                rgba, reference, member_geometry[index], width, height)
        elif mode != composition_mod.GRID:
            # Convert exactly once before pixel-center resampling. For GRID,
            # let each source enter its final cell at native sample density;
            # pre-downscaling to the *whole* canvas double-samples/aliases it.
            rgba = _resize_nn(rgba, height, width)
        prepared.append(rgba)
    if mode == composition_mod.OVER and len(prepared) >= 2:
        premult = []
        for rgba in prepared:
            item = rgba.copy()
            item[..., :3] *= item[..., 3:4]
            premult.append(item)
        result = composite(mode, premult, width, height, **parameters)
        alpha = result[..., 3:4]
        result[..., :3] = np.divide(result[..., :3], alpha,
                                    out=np.zeros_like(result[..., :3]),
                                    where=alpha > 1e-8)
    else:
        if mode == composition_mod.SPLIT:
            parameters = dict(parameters)
            parameters['pixel_centers'] = True
        result = composite(mode, prepared, width, height, **parameters)
    if not state.bypass_grade:
        # Grade the composite once, not each member independently.
        result[..., :3] = _grade(result[..., :3], state)
    # A grade may add an offset even to an explicitly empty physical region.
    # Keep those regions true transparent black; retain signed/HDR RGB where
    # there are real contributing samples and fractional alpha.
    result[..., :3] = np.where(result[..., 3:4] > 1e-8,
                              result[..., :3], 0.0)
    return np.ascontiguousarray(result, dtype=np.float32)


def rectify_selected_working(frame, reference_geometry, member_geometry, width,
                             height, color_state=None, source_color_state=None,
                             mode='a_b'):
    """One selected *native* Source/Working plate -> reference-field Working.

    The caller retains the untouched original for pixel inspection. This
    pure CPU path owns exactly one Source->Working conversion, one native UV
    pixel-center physical rectification, and one optional common grade. Its
    float32 straight-alpha result is not display-referred or a new Source.
    """
    if mode not in (composition_mod.A_B, composition_mod.FLICKER):
        raise ValueError('Selected physical rectification is A/B or Flicker only.')
    return composite_working(
        mode, [frame], color_state=color_state, width=width, height=height,
        source_color_states=[source_color_state],
        source_geometries=[member_geometry],
        reference_geometry=reference_geometry)


def to_uint8(image):
    """Convert a float RGBA buffer back to 8-bit for display."""
    return np.clip(np.asarray(image) * 255.0 + 0.5, 0, 255).astype(np.uint8)
