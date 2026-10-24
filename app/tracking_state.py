# -*- coding: utf-8 -*-
"""Strict, portable source-frame tracking records (no Qt or OpenCV).

Coordinates are post-flip native pixels, not viewport/PAR pixels. A missing
source key is never snapped to FrameMapping's nearest-frame fallback.
"""
from __future__ import absolute_import

import math

_MAX_FRAMES = 2400


def _point(raw):
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValueError('Tracking point must have two coordinates.')
    point = tuple(float(n) for n in raw)
    if not all(math.isfinite(n) and abs(n) <= 1000000 for n in point):
        raise ValueError('Tracking coordinates must be finite and bounded.')
    return point


def _size(raw, width, height):
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValueError('Tracking size must have two dimensions.')
    size = tuple(int(n) for n in raw)
    if (size[0] < 2 or size[1] < 2 or
            size[0] > width or size[1] > height):
        raise ValueError('Tracking size is outside source geometry.')
    return size


def _labels(info):
    frames = info.frame_mapping().source_frames
    if not frames or len(set(frames)) != len(frames):
        raise ValueError('Source frame labels are ambiguous.')
    return frames


def _clean_quality(item, index, source_label):
    if not isinstance(item, dict) or \
            item.get('frame') != index or item.get('source_frame') != source_label:
        raise ValueError('Tracking quality source identity mismatch.')
    allowed = {'reference', 'tracked', 'manual', 'lost_no_features', 'lost_flow',
               'lost_insufficient_inliers', 'lost_implausible_motion'}
    status = item.get('status')
    if status not in allowed or type(item.get('lost')) is not bool or \
            item['lost'] != status.startswith('lost_'):
        raise ValueError('Invalid tracking quality status.')
    if status == 'manual' and item.get('confidence') is not None:
        raise ValueError('Manually corrected track cannot claim measured confidence.')
    observed = {'status': status, 'lost': item['lost']}
    for field in ('feature_count', 'tracked_count', 'inlier_count',
                  'detected_count', 'seeded_count'):
        number = item.get(field)
        if type(number) is not int or number < 0 or number > 10000:
            raise ValueError('Invalid tracking quality {}.'.format(field))
        observed[field] = number
    for field in ('inlier_ratio', 'fb_error_px',
                  'reprojection_error_px', 'confidence'):
        value = item.get(field)
        if value is not None:
            if type(value) not in (float, int) or \
                    not math.isfinite(value) or value < 0 or \
                    (field in ('inlier_ratio', 'confidence') and value > 1):
                raise ValueError('Invalid tracking metric {}.'.format(field))
        observed[field] = value
    return observed


def freeze_tracking(info, source_id, centers, quads, crop_size, base_size,
                    offset, mode, flip_h=False, flip_v=False, quality=None):
    """Freeze dense playhead indices into distinct signed SOURCE frame labels."""
    labels = _labels(info)
    if not isinstance(centers, dict) or not centers or len(centers) > _MAX_FRAMES:
        raise ValueError('Tracking needs 1–2,400 frame keys.')
    quad_map = dict(quads or {})
    if quad_map and set(quad_map) != set(centers):
        raise ValueError('Every tracked center must have one matching quad.')
    source_centers, source_quads, source_quality = {}, {}, {}
    quality = dict(quality or {})
    if quality and set(quality) != set(centers):
        raise ValueError('Tracking quality keys must match measured centers.')
    for index, center in centers.items():
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(labels):
            raise ValueError('Tracking display index is outside current media.')
        key = str(labels[index])
        if key in source_centers:
            raise ValueError('Duplicate source frame in tracking record.')
        source_centers[key] = list(_point(center))
        if quad_map:
            quad = quad_map[index]
            if not isinstance(quad, (list, tuple)) or len(quad) != 4:
                raise ValueError('Tracking quad must have four corners.')
            source_quads[key] = [list(_point(p)) for p in quad]
        if quality:
            source_quality[key] = _clean_quality(quality[index], index, labels[index])
    record = {
        'version': 1, 'algorithm': 'opencv_lk_quad_v1',
        'source_id': str(source_id),
        'width': int(info.width), 'height': int(info.height),
        'fps_rational': list(info.frame_mapping().fps_rational),
        'mode': int(mode), 'flip_h': bool(flip_h), 'flip_v': bool(flip_v),
        'crop_size': list(_size(crop_size, info.width, info.height)),
        'base_size': list(_size(base_size, info.width, info.height)),
        'offset': list(_point(offset)),
        'centers': source_centers, 'quads': source_quads,
    }
    if source_quality:
        record['quality'] = source_quality
    restore_tracking(info, source_id, record, flip_h=flip_h, flip_v=flip_v)
    return record


def restore_tracking(info, source_id, record, flip_h=False, flip_v=False):
    """Validate identity/timing/geometry; restore only EXACT source labels.

    Raises for relinked media with missing track keys, rather than attaching
    annotations or stabilization to adjacent source plates by accident.
    """
    if not isinstance(record, dict) or record.get('version') != 1:
        raise ValueError('Unsupported tracking record version.')
    if record.get('algorithm') != 'opencv_lk_quad_v1':
        raise ValueError('Unsupported optical-flow track algorithm.')
    if record.get('source_id') != str(source_id):
        raise ValueError('Tracking belongs to a different logical Source.')
    if (record.get('width'), record.get('height')) != (info.width, info.height):
        raise ValueError('Tracking source raster changed; retrack at native resolution.')
    if (record.get('fps_rational') != list(info.frame_mapping().fps_rational)):
        raise ValueError('Tracking source rational FPS changed.')
    if (record.get('flip_h') is not bool(flip_h) or
            record.get('flip_v') is not bool(flip_v)):
        raise ValueError('Tracking flip orientation differs from the source view.')
    mode = record.get('mode')
    if type(mode) is not int or mode not in (1, 2, 3, 4):
        raise ValueError('Unsupported tracking transform mode.')
    width, height = info.width, info.height
    crop_size = _size(record.get('crop_size'), width, height)
    base_size = _size(record.get('base_size'), width, height)
    offset = _point(record.get('offset'))
    raw_centers = record.get('centers')
    raw_quads = record.get('quads')
    raw_quality = record.get('quality') or {}
    if (not isinstance(raw_centers, dict) or
            not 0 < len(raw_centers) <= _MAX_FRAMES or
            not isinstance(raw_quads, dict) or
            (raw_quads and set(raw_centers) != set(raw_quads)) or
            not isinstance(raw_quality, dict) or
            (raw_quality and set(raw_quality) != set(raw_centers))):
        raise ValueError('Tracking keys/quads are incomplete or oversized.')
    labels = _labels(info)
    by_label = {str(number): index for index, number in enumerate(labels)}
    centers, quads, quality = {}, {}, {}
    for key, point in raw_centers.items():
        if not isinstance(key, str) or key not in by_label:
            raise ValueError('Tracking source frame {} missing after relink.'.format(key))
        index = by_label[key]
        centers[index] = _point(point)
        if raw_quads:
            quad = raw_quads[key]
            if not isinstance(quad, (list, tuple)) or len(quad) != 4:
                raise ValueError('Invalid source tracking quad {}.'.format(key))
            quads[index] = [_point(p) for p in quad]
        if raw_quality:
            item = raw_quality[key]
            # Reapply the same strict metric/type validation used on authoring,
            # never trust arbitrary session JSON for viewer/export decisions.
            if not isinstance(item, dict):
                raise ValueError('Invalid tracking quality payload.')
            quality[index] = dict(_clean_quality(
                dict(item, frame=index, source_frame=labels[index]),
                index, labels[index]), frame=index, source_frame=labels[index])
    return {'centers': centers, 'quads': quads, 'crop_size': crop_size,
            'base_size': base_size, 'offset': offset, 'mode': mode,
            'quality': quality}
