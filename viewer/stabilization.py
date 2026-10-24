# -*- coding: utf-8 -*-
"""Bounded, opt-in translation stabilization from an existing tracking_v1.

This module only plans a geometric preview. It never resamples pixels, edits a
track/session, adds nonexistent image edges, or changes color/HDR precision.
All coordinates are *native, post-flip scene pixels*, shared by the primary
viewer image, annotations, crop, and pixel inspector. Apply the same scene
translation to image AND overlays, or apply the returned display translation
after ViewGeometry's PAR/quarter-turn; invert for sampling/hit testing.

No missing/held/ambiguous source frame may be inferred or interpolated.
"""
from __future__ import absolute_import

import math
from types import MappingProxyType

from .geometry import ViewGeometry


_MAX_KEYS = 2400
_EPS = 1e-4


def _finite(value, label):
    if type(value) not in (float, int) or not math.isfinite(value):
        raise ValueError('Stabilization {} must be a finite number.'.format(label))
    return float(value)


def _pair(value, label):
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError('Stabilization {} must be a 2D pair.'.format(label))
    return (_finite(value[0], label), _finite(value[1], label))


def _int_label(value, label):
    if type(value) is not int:
        raise ValueError('Stabilization {} must be an integer source label.'.format(label))
    return value


def _keys(record_map, name):
    if not isinstance(record_map, dict) or not record_map or len(record_map) > _MAX_KEYS:
        raise ValueError('Stabilization {} is absent or oversized.'.format(name))
    for key in record_map:
        if not isinstance(key, str):
            raise ValueError('Stabilization {} keys must be signed frame strings.'.format(name))
        try:
            value = int(key)
        except ValueError:
            raise ValueError('Invalid source-frame key in {}.'.format(name))
        if key != str(value):
            raise ValueError('Ambiguous source-frame key in {}.'.format(name))
    return set(record_map)


def _matrix(dx, dy):
    return ((1.0, 0.0, float(dx)),
            (0.0, 1.0, float(dy)),
            (0.0, 0.0, 1.0))


def _check_quad(quad, center, base_size, width, height):
    """A 1-point track must encode the *same* axis-aligned rectangle each frame."""
    if not isinstance(quad, (list, tuple)) or len(quad) != 4:
        raise ValueError('Stabilization requires complete tracking quadrilaterals.')
    points = [_pair(p, 'quad corner') for p in quad]
    for px, py in points:
        if px < -_EPS or py < -_EPS or px > width + _EPS or py > height + _EPS:
            raise ValueError('Tracked crop moved outside native source raster.')
    expected = ((-base_size[0] / 2.0, -base_size[1] / 2.0),
                (base_size[0] / 2.0, -base_size[1] / 2.0),
                (base_size[0] / 2.0, base_size[1] / 2.0),
                (-base_size[0] / 2.0, base_size[1] / 2.0))
    for (px, py), (ex, ey) in zip(points, expected):
        if abs((px - center[0]) - ex) > .15 or abs((py - center[1]) - ey) > .15:
            raise ValueError('Unsupported affine/perspective or inconsistent tracking quad.')


def _check_quality(metric, source_label, first, min_confidence):
    if not isinstance(metric, dict) or type(metric.get('lost')) is not bool:
        raise ValueError('Missing or ambiguous tracking quality at {}.'.format(source_label))
    status = metric.get('status')
    if metric['lost'] or (isinstance(status, str) and status.startswith('lost_')):
        raise ValueError('Lost/held tracking motion at source frame {}.'.format(source_label))
    if status == 'reference':
        if not first or metric.get('confidence') is not None:
            raise ValueError('Ambiguous track reference at {}.'.format(source_label))
        detected = metric.get('seeded_count')
        if type(detected) is not int or detected < 4:
            raise ValueError('Track reference has insufficient real features.')
        return
    if status != 'tracked':
        raise ValueError('Stabilization needs measured tracking at {}, got {}.'
                         .format(source_label, status))
    confidence = _finite(metric.get('confidence'), 'confidence')
    fb_error = _finite(metric.get('fb_error_px'), 'forward/back error')
    reproj_error = _finite(metric.get('reprojection_error_px'), 'reprojection error')
    for field in ('feature_count', 'tracked_count', 'inlier_count'):
        value = metric.get(field)
        if type(value) is not int or value < 4:
            raise ValueError('Tracking {} insufficient at {}.'.format(field, source_label))
    if (metric['inlier_count'] > metric['tracked_count'] or
            metric['tracked_count'] > metric['feature_count']):
        raise ValueError('Impossible inlier counts at {}.'.format(source_label))
    ratio = _finite(metric.get('inlier_ratio'), 'inlier ratio')
    measured_ratio = metric['inlier_count'] / float(metric['tracked_count'])
    if (abs(ratio - measured_ratio) > .002 or ratio < .35 or
            confidence < min_confidence or confidence > 1 or
            fb_error >= 1.75 or reproj_error > 2.25):
        raise ValueError('Tracking evidence is insufficient at {}.'.format(source_label))


class StabilizationPreview(object):
    """Immutable scene-space translation consumer for one exact reviewed range.

    ``matrix(label)`` maps original native post-flip scene -> stabilized scene.
    ``inverse_matrix(label)`` maps stabilized scene -> actual source coordinates.
    Source labels are ALWAYS original signed frame numbers, never dense indices.
    """

    __slots__ = ('enabled', 'source_id', 'source_frames', 'anchor_source_frame',
                 'geometry', 'fps_rational', '_offsets', '_source_set', '_frozen')

    def __init__(self, enabled, source_id, source_frames, anchor_source_frame,
                 geometry, fps_rational, offsets):
        object.__setattr__(self, 'enabled', bool(enabled))
        object.__setattr__(self, 'source_id', str(source_id))
        object.__setattr__(self, 'source_frames', tuple(source_frames))
        object.__setattr__(self, 'anchor_source_frame', anchor_source_frame)
        object.__setattr__(self, 'geometry', geometry)
        object.__setattr__(self, 'fps_rational', tuple(fps_rational))
        object.__setattr__(self, '_offsets', MappingProxyType(dict(offsets)))
        object.__setattr__(self, '_source_set', frozenset(source_frames))
        object.__setattr__(self, '_frozen', True)

    def __setattr__(self, name, value):
        if getattr(self, '_frozen', False):
            raise AttributeError('StabilizationPreview is immutable.')
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise AttributeError('StabilizationPreview is immutable.')

    @classmethod
    def from_tracking(cls, record, info, source_id, requested_source_frames,
                      *, enabled=False, flip_h=False, flip_v=False,
                      pixel_aspect=None, rotation=0, anchor_source_frame=None,
                      max_shift_px=64.0, max_step_px=24.0,
                      min_confidence=.20):
        """Build a verified, non-destructive preview for exact source labels.

        ``info`` is the current MediaInfo, not a stale file probe. Requested
        labels must form one contiguous slice of its DENSE DISPLAY order; source
        numbers themselves may jump, start below zero, or skip missing labels.
        Disabled preview is identity even with a missing/unsupported track.
        """
        if not source_id or not isinstance(source_id, str):
            raise ValueError('Stabilization needs a stable source ID.')
        width, height = info.width, info.height
        if type(width) is not int or type(height) is not int or min(width, height) < 2:
            raise ValueError('Invalid source raster for stabilization.')
        geometry = ViewGeometry(width, height,
                                info.pixel_aspect_ratio if pixel_aspect is None
                                else pixel_aspect, rotation)
        if (pixel_aspect is not None and
                abs(geometry.pixel_aspect - float(info.pixel_aspect_ratio)) > 1e-6):
            raise ValueError('Stabilization pixel aspect differs from probed media.')
        fps = tuple(info.frame_mapping().fps_rational)
        if (len(fps) != 2 or any(type(x) is not int or x <= 0 for x in fps)
                or fps[0] / float(fps[1]) > 240):
            raise ValueError('Invalid rational media rate for stabilization.')
        timeline = tuple(info.frame_mapping().source_frames)
        if (not timeline or any(type(x) is not int for x in timeline) or
                sorted(set(timeline)) != list(timeline)):
            raise ValueError('Stabilization source timeline is ambiguous.')
        selected = tuple(requested_source_frames)
        if (not selected or len(selected) > _MAX_KEYS or
                any(type(x) is not int for x in selected)):
            raise ValueError('Stabilization requires 1–2,400 exact source labels.')
        indices = {label: index for index, label in enumerate(timeline)}
        if any(label not in indices for label in selected):
            raise ValueError('Stabilization requested a missing source frame.')
        position = indices[selected[0]]
        if timeline[position:position + len(selected)] != selected:
            raise ValueError('Stabilization cannot skip intervening display frames.')
        if info.is_sequence:
            # Includes expanded Hold/Black/Checker/Error holes: no image at a
            # declared label means no safe correction for that plate.
            existing = set(info.sequence_source_frames)
            if any(label not in existing for label in selected):
                raise ValueError('Stabilization range contains an absent source plate.')
        anchor = selected[0] if anchor_source_frame is None else _int_label(
            anchor_source_frame, 'anchor')
        if anchor not in selected:
            raise ValueError('Stabilization anchor must be inside selected source frames.')
        if not enabled:
            return cls(False, source_id, selected, anchor, geometry, fps,
                       {label: (0.0, 0.0) for label in selected})

        if not isinstance(record, dict) or record.get('version') != 1 or \
                record.get('algorithm') != 'opencv_lk_quad_v1':
            raise ValueError('Stabilization requires a supported tracking_v1 record.')
        if record.get('source_id') != source_id:
            raise ValueError('Stabilization source identity changed.')
        if (type(record.get('width')) is not int or
                type(record.get('height')) is not int or
                (record['width'], record['height']) != (width, height)):
            raise ValueError('Stabilization source raster changed.')
        if record.get('fps_rational') != list(fps):
            raise ValueError('Stabilization source rational FPS changed.')
        if record.get('flip_h') is not bool(flip_h) or \
                record.get('flip_v') is not bool(flip_v):
            raise ValueError('Stabilization tracking flip orientation changed.')
        if type(record.get('mode')) is not int or record['mode'] != 1:
            raise ValueError('Stabilization supports verified translation-only tracking.')
        size = _pair(record.get('crop_size'), 'crop size')
        base_size = _pair(record.get('base_size'), 'base crop size')
        if (size != base_size or min(size) < 2 or size[0] > width or
                size[1] > height or _pair(record.get('offset'), 'crop offset')
                != (0.0, 0.0)):
            raise ValueError('Stabilization cannot infer offsets or resized tracking crops.')
        centers, quads, qualities = (record.get('centers'), record.get('quads'),
                                    record.get('quality'))
        center_keys = _keys(centers, 'centers')
        if center_keys != _keys(quads, 'quads') or \
                center_keys != _keys(qualities, 'quality'):
            raise ValueError('Stabilization requires complete matched center/quad/quality keys.')
        if not center_keys.issubset({str(x) for x in timeline}):
            raise ValueError('Stabilization contains unrecognized source-frame keys.')
        if not all(str(frame) in center_keys for frame in selected):
            raise ValueError('Stabilization has an unmeasured source frame.')
        shift_limit = _finite(max_shift_px, 'maximum translation')
        step_limit = _finite(max_step_px, 'maximum step')
        confidence_floor = _finite(min_confidence, 'minimum confidence')
        if (not 0 < shift_limit <= 2048 or not 0 < step_limit <= 256 or
                not 0 <= confidence_floor <= 1):
            raise ValueError('Invalid stabilization safety limits.')
        shift_limit = min(shift_limit, .20 * min(width, height))
        center_map = {}
        for index, label in enumerate(selected):
            key = str(label)
            center = _pair(centers[key], 'tracked center')
            _check_quad(quads[key], center, base_size, width, height)
            _check_quality(qualities[key], label, index == 0, confidence_floor)
            center_map[label] = center
        reference = center_map[anchor]
        offsets = {}
        previous = None
        for label in selected:
            center = center_map[label]
            dx, dy = reference[0] - center[0], reference[1] - center[1]
            if math.hypot(dx, dy) > shift_limit + _EPS:
                raise ValueError('Stabilization correction exceeds bounded translation.')
            if previous is not None and math.hypot(dx - previous[0],
                                                   dy - previous[1]) > step_limit + _EPS:
                raise ValueError('Stabilization correction exceeds bounded per-frame step.')
            offsets[label] = (dx, dy)
            previous = (dx, dy)
        return cls(True, source_id, selected, anchor, geometry, fps, offsets)

    def offset(self, source_frame):
        label = _int_label(source_frame, 'lookup')
        try:
            return self._offsets[label]
        except KeyError:
            raise ValueError('No exact stabilization sample for source frame {}.'.format(label))

    def matrix(self, source_frame):
        dx, dy = self.offset(source_frame)
        return _matrix(dx, dy)

    def inverse_matrix(self, source_frame):
        dx, dy = self.offset(source_frame)
        return _matrix(-dx, -dy)

    def source_to_stabilized(self, source_frame, x, y):
        dx, dy = self.offset(source_frame)
        return _finite(x, 'x') + dx, _finite(y, 'y') + dy

    def stabilized_to_source(self, source_frame, x, y):
        dx, dy = self.offset(source_frame)
        return _finite(x, 'x') - dx, _finite(y, 'y') - dy

    def display_to_stabilized(self, source_frame, x, y):
        """Current oriented physical display -> stabilized oriented display."""
        sx, sy = self.geometry.display_to_scene(x, y)
        tx, ty = self.source_to_stabilized(source_frame, sx, sy)
        return self.geometry.scene_to_display(tx, ty)

    def stabilized_display_to_display(self, source_frame, x, y):
        """Inverse for native annotations, hit testing, scopes and sampling."""
        sx, sy = self.geometry.display_to_scene(x, y)
        tx, ty = self.stabilized_to_source(source_frame, sx, sy)
        return self.geometry.scene_to_display(tx, ty)

    def qt_scene_transform(self, source_frame, QtGui):
        """QTransform for *all* native scene items (plate + overlays)."""
        dx, dy = self.offset(source_frame)
        return QtGui.QTransform.fromTranslate(dx, dy)

    def qt_display_transform(self, source_frame, QtGui):
        """QTransform AFTER the existing PAR/rotation presentation transform."""
        dx, dy = self.offset(source_frame)
        x0, y0 = self.geometry.scene_to_display(0, 0)
        x1, y1 = self.geometry.scene_to_display(dx, dy)
        return QtGui.QTransform.fromTranslate(x1 - x0, y1 - y0)

    def source_at_output(self, source_frame, x, y):
        """Inverse source sample for an on-canvas output pixel, or None (no fill)."""
        x, y = _finite(x, 'output x'), _finite(y, 'output y')
        width, height = self.geometry.width, self.geometry.height
        if not (0 <= x < width and 0 <= y < height):
            return None
        source_x, source_y = self.stabilized_to_source(source_frame, x, y)
        if not (0 <= source_x < width and 0 <= source_y < height):
            return None
        return (source_x, source_y)

    def valid_output_rect(self, source_frame):
        """Half-open native output rectangle backed by real source pixels."""
        dx, dy = self.offset(source_frame)
        width, height = self.geometry.width, self.geometry.height
        return (max(0.0, dx), max(0.0, dy),
                min(width, width + dx), min(height, height + dy))

    def common_valid_output_rect(self):
        """Safe shared output intersection. No invented edge pixels."""
        rectangles = [self.valid_output_rect(frame) for frame in self.source_frames]
        return (max(r[0] for r in rectangles),
                max(r[1] for r in rectangles),
                min(r[2] for r in rectangles),
                min(r[3] for r in rectangles))


__all__ = ['StabilizationPreview']
