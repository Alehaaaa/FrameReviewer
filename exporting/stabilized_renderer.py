# -*- coding: utf-8 -*-
"""Verified, queue-agnostic Working EXR stabilization backend (v1).

Caller owns the ONE durable export queue, private staging, cancellation token,
and atomic publication. This module only freezes immutable source evidence,
validates it again at execution, and writes ONE staged full-raster RGBA float
EXR. It never modifies the review track, the source, or a published output.

Only canonical single-part, full-window, square-pixel root RGB(A) float/HALF
OpenEXR sequences are supported; no guessed colors, retimes, transforms,
data-window padding, holes, VFR, OCIO display conversion or native GPU/HDR
renderer claims. The pixels ARE true native scene-linear float working data.
"""
from __future__ import absolute_import

import copy
import hashlib
import json
import math
import os
import re
import subprocess

from ..viewer.stabilization import StabilizationPreview


MAX_FRAMES = 2400
MAX_RASTER_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BYTES = 256 * 1024 * 1024
_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_FIELDS = frozenset(('version', 'stage', 'source_id', 'width', 'height',
                      'fps_rational', 'pixel_aspect', 'rotation', 'flip_h',
                      'flip_v', 'alpha_mode', 'source_color', 'tracking_v1',
                      'tracking_sha256', 'source_timeline',
                      'existing_source_frames', 'anchor_source_frame',
                      'frames', 'frozen_sha256'))


def _cancel(cancel):
    if cancel is None:
        return
    if callable(cancel):
        requested = cancel()
    elif callable(getattr(cancel, 'isInterruptionRequested', None)):
        requested = cancel.isInterruptionRequested()
    else:
        raise TypeError('Cancellation must be callable or Qt interruption token.')
    if requested:
        raise RuntimeError('Stabilized EXR render cancelled.')


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False)


def _hash_json(obj):
    return hashlib.sha256(_canonical(obj).encode('ascii')).hexdigest()


def _hash_file(path, cancel):
    """SHA256 with before/after metadata race guard; no symlink traversal."""
    _cancel(cancel)
    if not os.path.isabs(path) or os.path.normpath(path) != path or \
            not path.lower().endswith('.exr') or os.path.islink(path):
        raise ValueError('Stabilization source must be an absolute non-symlink EXR.')
    source = os.stat(path, follow_symlinks=False)
    if not (0 < source.st_size <= MAX_SOURCE_BYTES):
        raise ValueError('Stabilized EXR source exceeds bounded file size.')
    hasher = hashlib.sha256()
    with open(path, 'rb') as stream:
        while True:
            _cancel(cancel)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    after = os.stat(path, follow_symlinks=False)
    if ((source.st_dev, source.st_ino, source.st_size, source.st_mtime_ns) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ValueError('Stabilized EXR source changed while being hashed.')
    return hasher.hexdigest(), source.st_size


def _header(path, width, height, alpha_mode):
    from ..media import exr
    try:
        header = exr._raw_header(path)
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise ValueError('EXR header proof failed: {}'.format(exc))
    if header is None:
        raise ValueError('Stabilization requires canonical single-part root RGB(A) HALF/FLOAT EXR.')
    full = (0, 0, width, height)
    if (header['data_window'] != full or header['display_window'] != full or
            header['part_count'] != 1 or header['layer'] or
            (header['width'], header['height']) != (width, height)):
        raise ValueError('Unsupported EXR data/display window, layer, or dimensions.')
    if abs(float(header['pixel_aspect_ratio']) - 1.0) > 1e-7:
        raise ValueError('Stabilization requires square-pixel EXR input.')
    names = tuple(header['channels'])
    if names not in (('R', 'G', 'B'), ('R', 'G', 'B', 'A')):
        raise ValueError('Unsupported EXR channels.')
    if (len(names) == 4 and alpha_mode not in ('straight', 'premultiplied')) or \
            (len(names) == 3 and alpha_mode != 'opaque'):
        raise ValueError('Frozen alpha association contradicts EXR channel layout.')
    return list(names)


def _color_state(value, alpha_mode):
    """An EXPLICIT scene-linear identity source->working state, no hidden grade."""
    from ..viewer.color import ColorPipelineState
    if not isinstance(value, dict) or \
            any(key not in ColorPipelineState.__dataclass_fields__ for key in value):
        raise ValueError('Stabilization requires explicitly frozen valid color settings.')
    if (value.get('input_space') != 'linear' or
            value.get('working_space') != 'linear' or
            value.get('alpha_mode') != alpha_mode):
        raise ValueError('Stabilization needs declared linear source/working colors and alpha.')
    state = ColorPipelineState.from_dict(value)
    if (state.config or state.display or state.view or state.look or
            state.ocio_context or state.lut_roles or
            state.exposure != 0 or state.gamma != 1 or
            state.contrast != 0 or state.saturation != 0 or
            state.temperature != 0 or state.cdl_slope != (1., 1., 1.) or
            state.cdl_offset != (0., 0., 0.) or
            state.cdl_power != (1., 1., 1.) or state.cdl_saturation != 1):
        raise ValueError('Working stabilization cannot silently apply grade/OCIO transforms.')
    return state.to_dict()


class _FrozenInfo(object):
    """Revalidate the exact original timeline, without changing live MediaInfo."""

    def __init__(self, frozen):
        from ..media.sequence import FrameMapping
        self.width, self.height = frozen['width'], frozen['height']
        self.pixel_aspect_ratio = 1.
        self.is_sequence = True
        self.sequence_source_frames = frozen['existing_source_frames']
        self._frame_mapping = FrameMapping(frozen['source_timeline'],
                                           fps_rational=frozen['fps_rational'])

    def frame_mapping(self):
        return self._frame_mapping


def _preview(frozen):
    return StabilizationPreview.from_tracking(
        frozen['tracking_v1'], _FrozenInfo(frozen), frozen['source_id'],
        [row['source_frame'] for row in frozen['frames']],
        enabled=True, flip_h=frozen['flip_h'], flip_v=frozen['flip_v'],
        rotation=0, anchor_source_frame=frozen['anchor_source_frame'])


def _track_quality_and_offsets(frozen):
    preview = _preview(frozen)
    for row in frozen['frames']:
        expected = preview.offset(row['source_frame'])
        if list(expected) != row['offset']:
            raise ValueError('Frozen stabilization offset disagrees with verified tracking.')
    return preview


def freeze_stabilized_export(info, source_id, tracking_v1, source_frames,
                             source_paths, *, source_color, alpha_mode,
                             flip_h=False, flip_v=False, rotation=0,
                             anchor_source_frame=None, cancel=None):
    """Freeze complete v1 Working EXR intent to embed in an existing queue spec.

    ``source_paths`` must be a {SIGNED SOURCE LABEL: absolute EXR path} mapping
    with exact existing MediaInfo sequence file paths. Do not use display
    indices or inferred filename interpolation. The returned dict is detached,
    JSON serializable and SHA256 protected; it has NO destination side effects.
    """
    _cancel(cancel)
    if (not isinstance(source_id, str) or not source_id or
            type(flip_h) is not bool or type(flip_v) is not bool or
            type(rotation) is not int or rotation != 0 or
            alpha_mode not in ('opaque', 'straight', 'premultiplied')):
        raise ValueError('Unsupported stabilization source identity, flips or rotation.')
    if not getattr(info, 'is_sequence', False) or \
            len(info.sequence_files) != len(info.sequence_source_frames):
        raise ValueError('Stabilization needs real EXR source sequence files.')
    if abs(float(info.pixel_aspect_ratio) - 1.) > 1e-7:
        raise ValueError('Stabilization EXR source PAR must be exactly square.')
    if type(info.width) is not int or type(info.height) is not int or \
            min(info.width, info.height) < 2 or \
            info.width * info.height * 16 > MAX_RASTER_BYTES:
        raise ValueError('Stabilized EXR raster exceeds 64 MiB float RGBA budget.')
    if not isinstance(source_paths, dict) or \
            any(type(x) is not int for x in source_paths):
        raise ValueError('Input paths must be keyed by exact signed source frames.')
    wanted = list(source_frames)
    if (not wanted or len(wanted) > MAX_FRAMES or
            set(wanted) != set(source_paths) or len(wanted) != len(source_paths)):
        raise ValueError('Frozen EXR source frames and paths must match one-to-one.')
    timeline = list(info.frame_mapping().source_frames)
    existing = list(info.sequence_source_frames)
    if (not 1 <= len(timeline) <= 100000 or len(set(existing)) != len(existing) or
            len(set(timeline)) != len(timeline)):
        raise ValueError('Ambiguous or oversized source-frame timeline.')
    actual = dict(zip(existing, info.sequence_files))
    if any(label not in actual or actual[label] != source_paths[label]
           for label in wanted):
        raise ValueError('Stabilized source path does not match exact original EXR frame.')
    color = _color_state(source_color, alpha_mode)
    preview = StabilizationPreview.from_tracking(
        tracking_v1, info, source_id, wanted, enabled=True,
        flip_h=flip_h, flip_v=flip_v, rotation=rotation,
        anchor_source_frame=anchor_source_frame)
    records = []
    for label in wanted:
        _cancel(cancel)
        path = source_paths[label]
        from ..media import exr
        if exr.filename_source_frame(path) != label:
            raise ValueError('EXR filename frame does not match exact signed source label.')
        digest, size = _hash_file(path, cancel)
        channels = _header(path, info.width, info.height, alpha_mode)
        second_digest, _ = _hash_file(path, cancel)
        if second_digest != digest:
            raise ValueError('EXR source changed between header proof and hashing.')
        records.append({'source_frame': label, 'source_path': path,
                        'source_sha256': digest, 'source_size_bytes': size,
                        'source_channels': channels,
                        'offset': list(preview.offset(label))})
    frozen = {'version': 1, 'stage': 'working', 'source_id': source_id,
              'width': info.width, 'height': info.height,
              'fps_rational': list(info.frame_mapping().fps_rational),
              'pixel_aspect': '1/1', 'rotation': 0,
              'flip_h': flip_h, 'flip_v': flip_v, 'alpha_mode': alpha_mode,
              'source_color': color,
              'tracking_v1': copy.deepcopy(tracking_v1),
              'tracking_sha256': _hash_json(tracking_v1),
              'source_timeline': timeline,
              'existing_source_frames': existing,
              'anchor_source_frame': preview.anchor_source_frame,
              'frames': records}
    frozen['frozen_sha256'] = _hash_json(frozen)
    validate_stabilized_export(frozen, cancel=cancel, verify_files=True)
    return frozen


def validate_stabilized_export(frozen, *, info=None, source_id=None,
                               tracking_v1=None, cancel=None,
                               verify_files=True):
    """Strict queue/recovery preflight; rejects changed intent/media/track.

    Unlike a cosmetic manifest SHA, this reconstructs the bounded preview from
    the ORIGINAL frozen tracking keyframes and checks every translation anew.
    The queue must still perform normal stage/receipt/atomic commit validation.
    """
    _cancel(cancel)
    if not isinstance(frozen, dict) or set(frozen) != _FIELDS or \
            type(frozen.get('version')) is not int or frozen['version'] != 1 or \
            frozen.get('stage') != 'working' or \
            not isinstance(frozen.get('source_id'), str) or not frozen['source_id']:
        raise ValueError('Invalid stabilization_export v1 schema.')
    for key in ('tracking_sha256', 'frozen_sha256'):
        if not isinstance(frozen[key], str) or not _DIGEST.fullmatch(frozen[key]):
            raise ValueError('Invalid frozen stabilization SHA256.')
    if _hash_json(frozen['tracking_v1']) != frozen['tracking_sha256'] or \
            _hash_json({k: v for k, v in frozen.items() if k != 'frozen_sha256'}) != frozen['frozen_sha256']:
        raise ValueError('Frozen stabilization evidence was modified.')
    if (type(frozen['width']) is not int or type(frozen['height']) is not int or
            min(frozen['width'], frozen['height']) < 2 or
            frozen['width'] * frozen['height'] * 16 > MAX_RASTER_BYTES or
            frozen['pixel_aspect'] != '1/1' or frozen['rotation'] != 0 or
            type(frozen['flip_h']) is not bool or type(frozen['flip_v']) is not bool):
        raise ValueError('Invalid frozen stabilization native geometry.')
    _color_state(frozen['source_color'], frozen['alpha_mode'])
    frames = frozen.get('frames')
    if (not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES or
            any(not isinstance(row, dict) or set(row) !=
                {'source_frame', 'source_path', 'source_sha256',
                 'source_size_bytes', 'source_channels', 'offset'} or
                type(row['source_frame']) is not int or
                type(row['source_size_bytes']) is not int or
                not isinstance(row['source_path'], str) or
                not isinstance(row['source_sha256'], str) or
                not _DIGEST.fullmatch(row['source_sha256']) or
                type(row['offset']) is not list or len(row['offset']) != 2 or
                any(type(v) not in (float, int) or not math.isfinite(v)
                    for v in row['offset'])
                for row in frames)):
        raise ValueError('Invalid frozen stabilization source frame proof.')
    if (not isinstance(frozen['source_timeline'], list) or
            not 1 <= len(frozen['source_timeline']) <= 100000 or
            any(type(v) is not int for v in frozen['source_timeline']) or
            sorted(set(frozen['source_timeline'])) != frozen['source_timeline'] or
            not isinstance(frozen['existing_source_frames'], list) or
            len(set(frozen['existing_source_frames'])) != len(frozen['existing_source_frames']) or
            any(type(v) is not int for v in frozen['existing_source_frames']) or
            any(v not in frozen['source_timeline'] for v in frozen['existing_source_frames'])):
        raise ValueError('Invalid frozen EXR source timeline.')
    if _track_quality_and_offsets(frozen) is None:
        raise ValueError('Stabilization preview unavailable.')
    if info is not None:
        if (info.width != frozen['width'] or info.height != frozen['height'] or
                info.frame_mapping().fps_rational != tuple(frozen['fps_rational']) or
                info.frame_mapping().source_frames != frozen['source_timeline'] or
                info.sequence_source_frames != frozen['existing_source_frames'] or
                abs(float(info.pixel_aspect_ratio) - 1.) > 1e-7):
            raise ValueError('Source media changed since stabilization was frozen.')
        existing_paths = dict(zip(info.sequence_source_frames, info.sequence_files))
        if any(existing_paths.get(r['source_frame']) != r['source_path']
               for r in frames):
            raise ValueError('Source sequence path changed since stabilization was frozen.')
    if source_id is not None and source_id != frozen['source_id']:
        raise ValueError('Stabilization queue intent belongs to a different Source.')
    if tracking_v1 is not None and _hash_json(tracking_v1) != frozen['tracking_sha256']:
        raise ValueError('Review track changed since stabilization was frozen.')
    if verify_files:
        from ..media import exr
        for row in frames:
            _cancel(cancel)
            if exr.filename_source_frame(row['source_path']) != row['source_frame']:
                raise ValueError('Frozen input original source frame identity changed.')
            if _header(row['source_path'], frozen['width'], frozen['height'],
                       frozen['alpha_mode']) != row['source_channels']:
                raise ValueError('Frozen EXR channel/window layout changed.')
            if _hash_file(row['source_path'], cancel) != (row['source_sha256'],
                                                         row['source_size_bytes']):
                raise ValueError('Frozen EXR source SHA256 changed.')
    return True


def _ffmpeg_command(args, ffmpeg, *, input_bytes=None, cancel=None,
                    timeout=40):
    """Synchronous bounded subprocess: cancellation interrupts stalled IO."""
    from ..media.ffmpeg import media_environment, _no_window_kwargs
    _cancel(cancel)
    kwargs = dict(stdin=(subprocess.PIPE if input_bytes is not None else
                         subprocess.DEVNULL), stdout=subprocess.PIPE,
                  stderr=subprocess.PIPE, env=media_environment())
    kwargs.update(_no_window_kwargs())
    proc = subprocess.Popen([ffmpeg, '-hide_banner', '-loglevel', 'error',
                             '-nostdin'] + list(args), **kwargs)
    data = input_bytes
    import time
    started = time.monotonic()
    try:
        while True:
            _cancel(cancel)
            if time.monotonic() - started > timeout:
                raise RuntimeError('Stabilized EXR FFmpeg operation timed out.')
            try:
                out, err = proc.communicate(input=data, timeout=.25)
                break
            except subprocess.TimeoutExpired:
                data = None
        if proc.returncode:
            raise RuntimeError('Stabilized EXR FFmpeg failed: {}'.format(
                err.decode('utf8', 'replace')[-500:]))
        return out
    except BaseException:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()
        raise


def _decode_float(path, width, height, channels, ffmpeg, cancel):
    import numpy as np
    fmt = 'gbrapf32le' if channels == 4 else 'gbrpf32le'
    raw = _ffmpeg_command(['-i', path, '-frames:v', '1', '-pix_fmt', fmt,
                           '-f', 'rawvideo', 'pipe:1'],
                          ffmpeg, cancel=cancel)
    if len(raw) != width * height * channels * 4:
        raise RuntimeError('EXR decoder did not return expected exact float raster.')
    planar = np.frombuffer(raw, '<f4').reshape(channels, height, width)
    indices = (2, 0, 1, 3) if channels == 4 else (2, 0, 1)
    return np.ascontiguousarray(np.stack([planar[i] for i in indices], -1))


def stabilized_linear_pixels(source_rgba, offset, *, flip_h=False,
                             flip_v=False, alpha_mode='straight'):
    """Bounded exact-coordinate bilinear float32 RGBA sampler, transparent 0 edges.

    Interpolates **associated** RGB so HDR/negative values are not clipped and
    straight-alpha sources cannot bleed hidden RGB into transparent borders.
    A pixel whose inverse CENTER lies outside the original raster is entirely
    (0,0,0,0). Fractionally covered in-bounds border pixels retain true alpha.
    """
    import numpy as np
    data = np.asarray(source_rgba)
    if (data.ndim != 3 or data.shape[2] not in (3, 4) or
            data.dtype != np.float32 or not np.all(np.isfinite(data))):
        raise ValueError('Stabilization needs finite linear float32 RGB(A) source pixels.')
    h, w = data.shape[:2]
    if w * h * 16 > MAX_RASTER_BYTES:
        raise ValueError('Stabilized frame is too large.')
    if alpha_mode not in ('straight', 'premultiplied', 'opaque') or \
            (data.shape[2] == 3) != (alpha_mode == 'opaque'):
        raise ValueError('Source alpha association is ambiguous.')
    if not isinstance(offset, (tuple, list)) or len(offset) != 2 or \
            any(type(v) not in (int, float) or not math.isfinite(v) for v in offset):
        raise ValueError('Stabilization needs a verified finite translation.')
    # Flip source pixels first: tracked centers/quads are post-flip native.
    if flip_h:
        data = data[:, ::-1]
    if flip_v:
        data = data[::-1, :]
    if data.shape[2] == 3:
        pixels = np.empty((h, w, 4), dtype=np.float32)
        pixels[:, :, :3] = data
        pixels[:, :, 3] = 1.
    else:
        pixels = np.array(data, dtype=np.float32, copy=True)
    if alpha_mode != 'premultiplied':
        pixels[:, :, :3] *= pixels[:, :, 3:4]
    # All output weights are calculated in float64, with no OpenCV 1/32-pixel
    # flow-coordinate quantization or implicit border clamp.
    dx, dy = offset
    xs = np.arange(w, dtype=np.float64) - float(dx)
    ys = np.arange(h, dtype=np.float64) - float(dy)
    x0, y0 = np.floor(xs).astype('int64'), np.floor(ys).astype('int64')
    wx, wy = xs - x0, ys - y0
    inside_x, inside_y = (xs >= 0) & (xs < w), (ys >= 0) & (ys < h)
    result = np.zeros((h, w, 4), dtype=np.float32)
    for jy, y_indices, wy_factor in ((0, y0, 1. - wy), (1, y0 + 1, wy)):
        valid_y = inside_y & (y_indices >= 0) & (y_indices < h)
        if not valid_y.any():
            continue
        row = pixels[np.clip(y_indices, 0, h - 1)]
        for jx, x_indices, wx_factor in ((0, x0, 1. - wx),
                                         (1, x0 + 1, wx)):
            valid_x = inside_x & (x_indices >= 0) & (x_indices < w)
            valid = valid_y[:, None] & valid_x[None, :]
            if not valid.any():
                continue
            weight = (wy_factor[:, None] * wx_factor[None, :]) * valid
            result += (row[:, np.clip(x_indices, 0, w - 1), :] *
                       weight[..., None]).astype(np.float32)
    if alpha_mode != 'premultiplied':
        alpha = result[:, :, 3:4]
        result[:, :, :3] = np.divide(
            result[:, :, :3], alpha,
            out=np.zeros_like(result[:, :, :3]), where=alpha > 1e-8)
    # Invalid regions always have EXACT ZERO RGBA; no edge cloning/fake motion.
    result[result[:, :, 3] == 0] = 0
    if not np.all(np.isfinite(result)):
        raise RuntimeError('Stabilized working pixels contain nonfinite values.')
    return result


def render_stabilized_frame(frozen, source_frame, stage_output_path, *,
                            ffmpeg, cancel=None, verify_pixels=True):
    """Render one frame into a caller-owned queue PRIVATE staging location.

    Existing files/symlinks are *never* overwritten. A returned JSON-safe
    receipt confirms the exact input SHA, signed source ID, translation, float
    RGBA output SHA, alpha support, and decoded pixel agreement. Only the
    managed queue can commit/recover/publish the staged file and this receipt.
    """
    import numpy as np
    _cancel(cancel)
    if verify_pixels is not True:
        raise ValueError('Stabilized EXR requires decoded output pixel verification.')
    validate_stabilized_export(frozen, cancel=cancel, verify_files=False)
    if type(source_frame) is not int:
        raise ValueError('Stabilization rendering needs an integer source label.')
    frame = next((r for r in frozen['frames']
                  if r['source_frame'] == source_frame), None)
    if frame is None:
        raise ValueError('No exact stabilized source-frame match.')
    if not isinstance(ffmpeg, str) or not ffmpeg:
        raise ValueError('A verified FFmpeg executable is required.')
    if (not isinstance(stage_output_path, str) or
            not os.path.isabs(stage_output_path) or
            os.path.normpath(stage_output_path) != stage_output_path or
            not stage_output_path.lower().endswith('.exr') or
            os.path.lexists(stage_output_path)):
        raise ValueError('Stabilized EXR destination must be a fresh private stage path.')
    parent = os.path.dirname(stage_output_path)
    if not os.path.isdir(parent) or os.path.islink(parent):
        raise ValueError('Stabilized EXR stage directory does not exist safely.')
    # Forbid delivery to anything except the queue's explicitly private stage
    # (or a controlled test stage with the same naming pattern).
    if not any(component.startswith(('.frqueue-', '.frstage-'))
               for component in os.path.realpath(parent).split(os.sep)):
        raise ValueError('Stabilization renderer may write only private queue staging files.')
    source = frame['source_path']
    if _header(source, frozen['width'], frozen['height'],
               frozen['alpha_mode']) != frame['source_channels'] or \
            _hash_file(source, cancel) != (frame['source_sha256'],
                                          frame['source_size_bytes']):
        raise ValueError('Frozen stabilization source SHA256/header changed before decode.')
    channels = len(frame['source_channels'])
    width, height = frozen['width'], frozen['height']
    decoded = _decode_float(source, width, height, channels, ffmpeg, cancel)
    _cancel(cancel)
    if _hash_file(source, cancel) != (frame['source_sha256'],
                                     frame['source_size_bytes']):
        raise ValueError('Stabilized source changed during decoding.')
    result = stabilized_linear_pixels(
        decoded, frame['offset'], flip_h=frozen['flip_h'],
        flip_v=frozen['flip_v'], alpha_mode=frozen['alpha_mode'])
    planar = np.ascontiguousarray(np.stack(
        (result[:, :, 1], result[:, :, 2], result[:, :, 0],
         result[:, :, 3]), axis=0), dtype='<f4')
    # FFmpeg EXR encoding never encodes preview RGB8, half quantization or PNG
    # sRGB here: true full-float working RGBA, transparent source-less pixels.
    command = ['-n', '-f', 'rawvideo', '-pix_fmt', 'gbrapf32le',
               '-video_size', '{}x{}'.format(width, height),
               '-framerate', '1', '-i', 'pipe:0', '-frames:v', '1',
               '-c:v', 'exr', '-pix_fmt', 'gbrapf32le', '-compression',
               'zip16', '-format', 'float', stage_output_path]
    try:
        _ffmpeg_command(command, ffmpeg, input_bytes=planar.tobytes(),
                        cancel=cancel)
        _cancel(cancel)
        if not os.path.isfile(stage_output_path):
            raise RuntimeError('Stabilized EXR encoder produced no output.')
        output_header = _header(stage_output_path, width, height, 'straight')
        if output_header != ['R', 'G', 'B', 'A']:
            raise RuntimeError('Stabilized EXR output lost float RGBA identity.')
        if verify_pixels:
            check = _decode_float(stage_output_path, width, height, 4, ffmpeg,
                                  cancel)
            if not np.all(np.isfinite(check)) or \
                    not np.allclose(check, result, atol=2e-5, rtol=3e-6):
                raise RuntimeError('Stabilized EXR output failed float pixel truth check.')
            edge = result[:, :, 3] == 0
            if not np.all(check[edge] == 0):
                raise RuntimeError('Stabilized EXR output has nonzero transparent edges.')
        digest, size = _hash_file(stage_output_path, cancel)
        return {'version': 1, 'stage': 'working',
                'source_id': frozen['source_id'],
                'source_frame': source_frame,
                'source_sha256': frame['source_sha256'],
                'tracking_sha256': frozen['tracking_sha256'],
                'frozen_sha256': frozen['frozen_sha256'],
                'source_color': copy.deepcopy(frozen['source_color']),
                'input_alpha_mode': frozen['alpha_mode'],
                'output_alpha_mode': ('premultiplied' if
                                      frozen['alpha_mode'] == 'premultiplied'
                                      else 'straight'),
                'offset': list(frame['offset']), 'width': width,
                'height': height, 'channels': 4, 'pixel_format': 'float32_rgba',
                'output_sha256': digest, 'output_size_bytes': size,
                'pixel_truth_verified': bool(verify_pixels),
                'alpha_edges': 'zero-transparent; no fill'}
    except BaseException:
        # Only remove our own *fresh* queue-stage frame; never prior output.
        if os.path.isfile(stage_output_path) and not os.path.islink(stage_output_path):
            os.unlink(stage_output_path)
        raise


__all__ = ['freeze_stabilized_export', 'validate_stabilized_export',
           'stabilized_linear_pixels', 'render_stabilized_frame']
