# -*- coding: utf-8 -*-
"""Frozen export intent, reproducible sidecars, and post-export verification.

Qt-free. Jobs snapshot their inputs before running, and may only report success
after checking the produced media. A manifest is written atomically afterward.
"""
from __future__ import absolute_import

from dataclasses import dataclass, field
from fractions import Fraction
import hashlib
import math
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import stat


MANAGED_INPUT_EXTENSIONS = frozenset(('.exr', '.png', '.tif', '.tiff'))
MANAGED_MOVIE_EXTENSIONS = frozenset(('.mp4', '.mov', '.mkv', '.avi', '.mxf'))
MANAGED_OUTPUT_EXTENSIONS = frozenset(('.exr', '.png', '.jpg', '.jpeg', '.mp4'))


def freeze_managed_composite_movie_proof(path, ffprobe=None, allow_audio=False):
    """Read-only, bounded UI preflight for a v3 CFR movie member.

    The resulting dictionary is *not* a substitute for user-confirmed input
    color: the UI must supply movie_color_provenance separately. The worker
    rechecks bytes, stream tags and every frame timestamp at execution.
    """
    if (not isinstance(path, str) or not os.path.isabs(path) or
            os.path.islink(path) or
            os.path.splitext(path)[1].lower() not in MANAGED_MOVIE_EXTENSIONS):
        raise ValueError('Composite movie proof needs a regular absolute local movie (not a symlink).')
    info = os.stat(path)
    if (not stat.S_ISREG(info.st_mode) or
            not 0 < info.st_size <= 512 * 1024 * 1024):
        raise ValueError('Composite movie proof needs a regular source at most 512 MiB.')
    ffprobe = ffprobe or shutil.which('ffprobe')
    if not ffprobe:
        raise RuntimeError('Composite v3 movie proof requires ffprobe.')
    def run(args, limit):
        proc = subprocess.run([ffprobe, '-v', 'error'] + args + [path],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=60, check=False)
        if proc.returncode or len(proc.stdout) > limit:
            raise ValueError('Movie ffprobe failed or emitted unbounded metadata: {}'.format(
                proc.stderr.decode('utf-8', 'replace')[-350:]))
        return proc.stdout
    metadata = json.loads(run([
        '-count_frames', '-show_entries',
        'stream=index,codec_type,codec_name,width,height,pix_fmt,avg_frame_rate,r_frame_rate,time_base,nb_read_frames,sample_aspect_ratio,color_space,color_range,start_time,duration,sample_rate,channels,channel_layout,side_data_list:stream_tags=rotate',
        '-of', 'json'], 256 * 1024).decode('utf-8'))
    streams = metadata.get('streams', [])
    if not streams or streams[0].get('codec_type') != 'video' or (
            len(streams) != 1 and not allow_audio):
        raise ValueError('Composite movie requires a single proven video stream; opt-in audio needs an explicit v4 proof.')
    value = streams[0]
    if (any(float(item.get('rotation', 0) or 0)
            for item in value.get('side_data_list', [])) or
            value.get('tags', {}).get('rotate') not in (None, '0')):
        raise ValueError('Composite movie rotation metadata is unsupported.')
    try:
        proof = {'version': 1, 'stream_index': int(value['index']),
                 'codec': value['codec_name'], 'pix_fmt': value['pix_fmt'],
                 'avg_frame_rate': value['avg_frame_rate'],
                 'r_frame_rate': value['r_frame_rate'],
                 'time_base': value['time_base'],
                 'frame_count': int(value['nb_read_frames']),
                 'width': int(value['width']), 'height': int(value['height']),
                 'sample_aspect_ratio': value['sample_aspect_ratio'],
                 'rotation': 0, 'color_space': value['color_space'],
                 'color_range': value['color_range'],
                 'start_time': value['start_time']}
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError('Movie needs unambiguous ffprobe CFR/color/SAR metadata: {}'.format(exc))
    if allow_audio:
        proof['version'] = 2
        proof['audio_tracks'] = _record_audio_track_proofs(streams)
    fps = Fraction(proof['avg_frame_rate'])
    count = proof['frame_count']
    if (not 1 <= count <= 240 or fps <= 0 or
            Fraction(proof['r_frame_rate']) != fps or
            Fraction(proof['start_time']) != 0 or
            proof['sample_aspect_ratio'] != '1:1' or
            proof['color_space'] not in ('bt709', 'bt470bg', 'smpte170m', 'bt2020nc') or
            proof['color_range'] not in ('tv', 'pc') or
            proof['stream_index'] != 0 or
            proof['codec'] not in ('h264', 'hevc', 'prores', 'ffv1') or
            proof['pix_fmt'] not in ('yuv420p', 'yuv422p', 'yuv444p',
                                    'yuv420p10le', 'yuv422p10le', 'yuv444p10le')):
        raise ValueError('Movie cannot prove supported square-pixel, CFR opaque YUV decode.')
    raw = run(['-select_streams', 'v:0', '-show_frames', '-show_entries',
               'frame=best_effort_timestamp_time', '-of', 'csv=p=0'], 256 * 1024)
    try:
        stamps = [Fraction(line.strip().split(',')[0]) for line in
                  raw.decode('utf-8').splitlines() if line.strip()]
    except (ValueError, TypeError, ZeroDivisionError) as exc:
        raise ValueError('Movie cannot prove exact decoded presentation timestamps: {}'.format(exc))
    if len(stamps) != count or any(
            abs(t - Fraction(i, 1) / fps) > Fraction(1, 2000)
            for i, t in enumerate(stamps)):
        raise ValueError('Movie is VFR, timestamp-offset or has inconsistent frame count.')
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    after = os.stat(path)
    if ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ValueError('Composite movie changed during proof capture.')
    proof['source_sha256'] = digest.hexdigest()
    proof['source_size_bytes'] = info.st_size
    return proof


def _record_audio_track_proofs(streams):
    """Exact selected-track ordinals, no guessing container stream IDs."""
    if not 2 <= len(streams) <= 5 or any(
            item.get('codec_type') != 'audio' for item in streams[1:]):
        raise ValueError('Record audio needs one video and 1..4 audio streams (no data/subtitles).')
    tracks = []
    for ordinal, item in enumerate(streams[1:]):
        try:
            track = {'audio_ordinal': ordinal, 'stream_index': int(item['index']),
                     'codec': item['codec_name'],
                     'sample_rate': int(item['sample_rate']),
                     'channels': int(item['channels']),
                     'channel_layout': item['channel_layout'],
                     'start_time': item['start_time'],
                     'duration': item['duration'],
                     'time_base': item['time_base']}
            if (track['stream_index'] != ordinal + 1 or
                    track['codec'] != 'pcm_s16le' or
                    track['sample_rate'] != 48000 or track['channels'] != 2 or
                    track['channel_layout'] not in ('stereo', '2.0') or
                    Fraction(str(track['start_time'])) != 0 or
                    Fraction(str(track['duration'])) <= 0 or
                    Fraction(track['time_base']) <= 0):
                raise ValueError('unsupported PCM channel/timing')
        except (TypeError, ValueError, ZeroDivisionError, KeyError) as exc:
            raise ValueError('Record audio requires zero-origin 48k stereo PCM s16le: {}'.format(exc))
        tracks.append(track)
    return tracks


def validate_managed_export(spec, seq_files, input_size):
    """Fail closed unless this intent can receive the actual precision renderer.

    ``start``/``end`` index the complete ordered ``seq_files`` array (display
    indices), NOT the filename's potentially negative/sparse source frame.
    No inferred movie/EXR transfer function is silently applied: callers must
    snapshot an explicit input/working/display color pipeline in the context.
    """
    c = spec.to_context()
    movie = bool(c.get('managed_movie'))
    composite = c.get('composite_export') is not None
    geometry = c.get('managed_geometry_export')
    if c.get('stabilization_export') is not None:
        frozen = c['stabilization_export']
        from .stabilized_renderer import validate_stabilized_export
        if (not isinstance(frozen, dict) or
                not isinstance(frozen.get('frames'), list) or
                not isinstance(frozen.get('source_timeline'), list) or
                movie or composite or geometry is not None or
                spec.stage != 'working' or not spec.sequence or
                not spec.path.endswith('.exr') or '####' not in spec.path or
                len(frozen.get('frames', ())) != spec.count or
                spec.start < 0 or spec.end >= len(frozen['source_timeline']) or
                any(row.get('source_frame') != frozen['source_timeline'][spec.start + i]
                    for i, row in enumerate(frozen['frames'])) or
                (spec.output_size != (frozen.get('width'), frozen.get('height'))) or
                Fraction(spec.fps) != Fraction(*frozen['fps_rational']) or
                Fraction(spec.source_fps) != Fraction(spec.fps) or
                spec.source_id != frozen['source_id'] or
                spec.source_path != frozen['frames'][0]['source_path'] or
                (seq_files and list(seq_files) !=
                 [f['source_path'] for f in frozen['frames']]) or
                c.get('color') != frozen.get('source_color') or
                c.get('audio') or c.get('burnin') or
                c.get('crop') or float(c.get('speed', 1)) != 1 or
                c.get('rotation') not in (None, 0)):
            raise ValueError('Stabilization requires exact frozen Working float EXR, source identity, colors, geometry and frame lineage.')
        validate_stabilized_export(frozen, source_id=spec.source_id,
                                   verify_files=True)
        return
    if geometry is not None and (movie or composite or spec.stage == 'source'):
        raise ValueError('Frozen managed image geometry requires Working/Display image-sequence input, not Source, movie or composite.')
    if geometry is None and not movie and not composite:
        # Avoid accepting a declared non-square/rotated source while quietly
        # emitting a square-pixel unrotated file through the legacy path.
        for key in ('pixel_aspect', 'pixel_aspect_ratio'):
            if c.get(key) is not None:
                try:
                    if Fraction(str(c[key])) != 1:
                        raise ValueError('Non-square source PAR needs a frozen managed_geometry_export plan.')
                except (ValueError, ZeroDivisionError, TypeError):
                    raise ValueError('Source pixel aspect is invalid or needs a frozen managed_geometry_export plan.')
        for key in ('rotation', 'view_rotation'):
            if c.get(key) not in (None, 0):
                raise ValueError('Rotated source needs a frozen managed_geometry_export plan.')
    if composite and movie:
        raise ValueError('Managed composite and movie are mutually exclusive.')
    if composite:
        managed_composite_plan(spec, seq_files=seq_files)
        return
    if spec.stage == 'source' and not movie:
        return
    if not isinstance(c.get('color'), dict) or not all(
            isinstance(c['color'].get(key), str) and c['color'][key].strip()
            for key in ('input_space', 'working_space', 'display_space')):
        raise ValueError('Managed export needs explicit input, working and display color spaces.')
    color = c['color']
    source_space, working_space, display_space = (
        str(color[key]).casefold() for key in
        ('input_space', 'working_space', 'display_space'))
    if ((source_space == 'raw' and working_space != 'raw') or
            (color.get('bypass_input') and source_space != working_space) or
            (spec.stage == 'display' and color.get('bypass_display') and
             working_space != display_space)):
        raise ValueError('Managed color stage cannot silently bypass a required color-space conversion.')
    if movie:
        if seq_files:
            raise ValueError('Managed movie input must not carry image-sequence files.')
        audio_policy = c.get('movie_audio_policy')
        if audio_policy == 'preserve':
            validate_managed_movie_audio(spec)
        elif audio_policy != 'drop' or c.get('movie_audio_export') is not None:
            raise ValueError('Managed movie audio requires explicit drop or a valid frozen preserve plan.')
        if (not os.path.isabs(spec.source_path) or
                os.path.splitext(spec.source_path)[1].lower() not in
                MANAGED_MOVIE_EXTENSIONS):
            raise ValueError('Managed movie requires an absolute supported movie source.')
        confirmed = c.get('movie_color_provenance')
        if (not isinstance(confirmed, dict) or
                confirmed.get('confirmed') is not True or
                confirmed.get('origin') != 'user' or
                not isinstance(confirmed.get('input_space'), str) or
                confirmed['input_space'].casefold() != source_space):
            raise ValueError('Managed movie needs explicitly user-confirmed frozen movie_color_provenance.input_space matching the input color space.')
        if c.get('movie_frame_map') is not None:
            raise ValueError('Managed movie does not support arbitrary remapping; frames must remain sequential.')
        managed_movie_plan(spec)
    else:
        if not seq_files or spec.start < 0:
            raise ValueError('Managed export requires ordered image inputs and a nonnegative display range.')
        if os.path.splitext(spec.source_path)[1].lower() not in MANAGED_INPUT_EXTENSIONS:
            raise ValueError('Managed export Source must identify image-sequence media, not a movie or unknown container.')
        if not all(isinstance(path, str) and os.path.isabs(path) and
                   os.path.splitext(path)[1].lower() in MANAGED_INPUT_EXTENSIONS
                   for path in seq_files):
            raise ValueError('Managed export only supports EXR/PNG/TIFF image sequence inputs, not movies.')
    if (len(input_size) != 2 or min(int(n) for n in input_size) <= 0):
        raise ValueError('Managed export requires known positive source dimensions.')
    if geometry is not None:
        for key in ('pixel_aspect', 'pixel_aspect_ratio'):
            if c.get(key) is not None:
                try:
                    if Fraction(str(c[key])) != Fraction(geometry['pixel_aspect']):
                        raise ValueError('Frozen source pixel aspect contradicts geometry plan.')
                except (KeyError, TypeError, ZeroDivisionError):
                    raise ValueError('Frozen source pixel aspect is invalid.')
        for key in ('rotation', 'view_rotation'):
            if c.get(key) is not None and c[key] != geometry['rotation']:
                raise ValueError('Frozen source rotation contradicts geometry plan.')
        managed_geometry_plan(spec, input_size)
    if (float(c.get('speed', 1.0)) != 1.0 or c.get('tracking_enabled') or
            c.get('tracking_quads') or c.get('burnin')):
        raise ValueError('Managed export does not yet support speed changes, tracking, or burn-in; refusing a false color-managed result.')
    ext = os.path.splitext(spec.path)[1].lower()
    if ext not in MANAGED_OUTPUT_EXTENSIONS:
        raise ValueError('Unsupported managed export format.')
    if spec.stage == 'working' and (not spec.sequence or ext != '.exr'):
        raise ValueError('Working-stage precision/HDR output requires an EXR image sequence.')
    if movie and spec.stage == 'source' and spec.sequence and ext not in ('.exr', '.png'):
        raise ValueError('Managed movie Source-stage image output requires EXR or PNG.')
    if spec.sequence and ext == '.mp4' or not spec.sequence and ext != '.mp4':
        raise ValueError('Managed sequence output must be an image, movie output must be MP4.')
    if ext == '.mp4' and any(n % 2 for n in spec.output_size):
        raise ValueError('Managed MP4 yuv420p output needs even dimensions.')
    if movie:
        return
    plan = managed_input_plan(spec, seq_files)
    if ext in ('.mp4', '.jpg', '.jpeg'):
        # Keep the main-thread queue preflight bounded for very long
        # sequences. The worker probes *every* real input before encoding, so
        # later/mixed RGBA frames still fail explicitly before any media write.
        ffprobe = shutil.which('ffprobe')
        if ffprobe:
            real_paths = list(dict.fromkeys(
                item['path'] for item in plan if item['path']))
            sampled = (real_paths[0:1] +
                       real_paths[len(real_paths) // 2:len(real_paths) // 2 + 1] +
                       real_paths[-1:])
            for path in dict.fromkeys(sampled):
                if not os.path.isfile(path):
                    continue
                try:
                    details = json.loads(subprocess.check_output([
                        ffprobe, '-v', 'error', '-select_streams', 'v:0',
                        '-show_entries', 'stream=pix_fmt', '-of', 'json',
                        path], stderr=subprocess.STDOUT, timeout=30))
                    fmt = details['streams'][0]['pix_fmt'].casefold()
                except (IndexError, KeyError, ValueError, OSError,
                        subprocess.SubprocessError):
                    continue  # Corrupt inputs must fail as decode errors later.
                if _has_alpha_format(fmt):
                    raise ValueError('Alpha image sequences cannot be exported to {}: the codec discards transparency; use EXR or PNG.'.format(ext))


def _has_alpha_format(fmt):
    """FFprobe formats that contain or may contain an alpha channel."""
    return str(fmt).startswith(('rgba', 'bgra', 'argb', 'abgr', 'gbrap',
                                'yuva', 'ya8', 'ya16', 'pal8'))


def managed_geometry_plan(spec, input_size):
    """Deterministic frozen sample-domain rectification (never viewport DPI).

    Flip -> crop in *post-flip native source coordinates* -> square-pixel PAR
    correction of source X (linear float interpolation) -> clockwise rotation.
    Exact output raster is determined by PAR/crop/orientation; arbitrary
    additional rescaling would corrupt aspect and is rejected.
    """
    c = spec.to_context()
    frozen = c.get('managed_geometry_export')
    if (not isinstance(frozen, dict) or
            set(frozen) != {'version', 'input_size', 'pixel_aspect',
                            'rotation', 'resample'} or
            type(frozen.get('version')) is not int or
            frozen['version'] != 1 or frozen.get('resample') != 'linear' or
            type(frozen.get('rotation')) is not int or
            frozen['rotation'] not in (0, 90, 180, 270) or
            not isinstance(frozen.get('pixel_aspect'), str)):
        raise ValueError('Frozen geometry needs version 1, exact PAR, quarter-turn rotation and linear filtering.')
    size = frozen.get('input_size')
    if (not isinstance(size, (list, tuple)) or len(size) != 2 or
            any(type(n) is not int or n < 1 or n > 16384 for n in size) or
            tuple(size) != tuple(input_size)):
        raise ValueError('Frozen geometry input_size disagrees with true source dimensions.')
    try:
        par = Fraction(frozen['pixel_aspect'])
    except (ValueError, ZeroDivisionError, TypeError) as exc:
        raise ValueError('Frozen geometry PAR must be an exact positive rational: {}'.format(exc))
    if not Fraction(1, 64) <= par <= 64 or par.denominator > 1000000:
        raise ValueError('Frozen geometry PAR exceeds bounded pixel aspect support.')
    if (float(c.get('speed', 1)) != 1 or c.get('tracking_enabled') or
            c.get('tracking_quads') or c.get('burnin')):
        raise ValueError('Managed rectification does not support moving geometry, speed or burn-ins.')
    if not all(type(c.get(k, False)) is bool for k in ('flip_h', 'flip_v')):
        raise ValueError('Managed flip fields must be frozen booleans.')
    crop = c.get('crop')
    if crop is None:
        crop = [0, 0, size[0], size[1]]
    if (not isinstance(crop, (tuple, list)) or len(crop) != 4 or
            any(type(n) is not int for n in crop)):
        raise ValueError('Managed source crop requires integer native coordinates.')
    x, y, w, h = crop
    if (min(x, y) < 0 or min(w, h) < 1 or
            x + w > size[0] or y + h > size[1]):
        raise ValueError('Managed source crop extends beyond frozen dimensions.')
    square_width = (w * par.numerator * 2 + par.denominator) // (2 * par.denominator)
    if square_width < 1:
        raise ValueError('Pixel aspect correction collapses source width.')
    output = ((h, square_width) if frozen['rotation'] in (90, 270)
              else (square_width, h))
    if (tuple(spec.output_size) != output or
            max(output) > 16384 or output[0] * output[1] * 16 > 256 * 1024 * 1024):
        raise ValueError('Managed geometry output dimensions must match the square-pixel oriented raster {}.'.format(output))
    if spec.stage not in ('working', 'display') or c.get('managed_movie') or c.get('composite_export') is not None:
        raise ValueError('Managed geometry only supports single image-sequence Working/Display.')
    if not spec.sequence or os.path.splitext(spec.path)[1].lower() not in ('.exr', '.png'):
        raise ValueError('Managed rectification requires image-sequence EXR or PNG output; movies and JPEG unsupported.')
    return {'version': 1, 'input_size': list(size),
            'pixel_aspect': frozen['pixel_aspect'], 'rotation': frozen['rotation'],
            'resample': frozen['resample'], 'flip_h': c.get('flip_h', False),
            'flip_v': c.get('flip_v', False), 'crop': list(crop),
            'square_pixel_size': [square_width, h],
            'output_size': list(output), 'output_pixel_aspect': '1/1',
            'order': 'flip-crop-par-rotate', 'working_linear_filter': True}


def _composite_member_geometry(member):
    """Version-2 exact, bounded native image geometry; no viewport guessing.

    EXR native window/PAR proof is subsequently checked against *every actual
    image* by the export worker. PNG/TIFF cannot prove a non-square source PAR
    and are accepted only as square pixels. No rotation/crop/offset windows.
    """
    if not isinstance(member, dict):
        raise ValueError('Composite member geometry must be a mapping.')
    frozen = member.get('source_geometry')
    if (not isinstance(frozen, dict) or
            set(frozen) != {'version', 'size', 'pixel_aspect', 'data_window',
                            'display_window', 'rotation'} or
            type(frozen.get('version')) is not int or frozen['version'] != 1 or
            type(frozen.get('rotation')) is not int or frozen['rotation'] != 0 or
            not isinstance(frozen.get('size'), (list, tuple)) or
            len(frozen['size']) != 2 or
            any(type(n) is not int or n < 1 or n > 4096
                for n in frozen['size']) or
            tuple(frozen['size']) != (member.get('width'), member.get('height')) or
            not isinstance(frozen.get('pixel_aspect'), str) or
            not isinstance(member.get('pixel_aspect'), str)):
        raise ValueError('Composite v2 requires exact source_geometry size, rational PAR, zero rotation.')
    width, height = frozen['size']
    full = (0, 0, width, height)
    if (frozen['data_window'] != list(full) or
            frozen['display_window'] != list(full)):
        raise ValueError('Composite PAR rejects cropped, offset or oversized native data/display windows.')
    try:
        par = Fraction(frozen['pixel_aspect'])
        member_par = Fraction(member['pixel_aspect'])
    except (TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        raise ValueError('Composite member PAR must be an exact rational: {}'.format(exc))
    if (not Fraction(1, 64) <= par <= 64 or par.denominator > 1000000 or
            par != member_par or
            frozen['pixel_aspect'] != '{}/{}'.format(par.numerator, par.denominator) or
            member['pixel_aspect'] != frozen['pixel_aspect']):
        raise ValueError('Composite source PAR is invalid, noncanonical or disagrees with frozen geometry.')
    files = member.get('seq_files', ())
    if (not files or
            os.path.splitext(member.get('source_path', ''))[1].lower() != '.exr' or
            any(os.path.splitext(path)[1].lower() != '.exr' for path in files)):
        raise ValueError('Composite v2 requires native EXR member sequences for verified PAR, windows and channel selection; PNG/TIFF cannot prove EXR geometry.')
    return ((width, height), par)


def _composite_v3_movie_proof(member):
    """Opt-in movie identity, CFR clock and explicitly confirmed decode color.

    These are the exact stream fields captured by ffprobe -count_frames; the
    worker independently probes every field AND all frame PTS before rendering.
    Unsupported alpha/rotation/PAR/VFR/codecs fail closed instead of guessing.
    """
    proof = member.get('movie_proof')
    required = {'version', 'stream_index', 'codec', 'pix_fmt',
                'avg_frame_rate', 'r_frame_rate', 'time_base', 'frame_count',
                'width', 'height', 'sample_aspect_ratio', 'rotation',
                'color_space', 'color_range', 'start_time',
                'source_sha256', 'source_size_bytes'}
    if not isinstance(proof, dict) or (set(proof) != required and
            set(proof) != required | {'audio_tracks'}) or \
            type(proof.get('version')) is not int or proof['version'] not in (1, 2) or \
            (proof['version'] == 1 and 'audio_tracks' in proof) or \
            (proof['version'] == 2 and not isinstance(proof.get('audio_tracks'), list)) or \
            type(proof.get('stream_index')) is not int or proof['stream_index'] != 0 or \
            type(proof.get('rotation')) is not int or proof['rotation'] != 0 or \
            type(proof.get('frame_count')) is not int or \
            not 1 <= proof['frame_count'] <= 240 or \
            type(proof.get('width')) is not int or \
            type(proof.get('height')) is not int or \
            not 1 <= proof['width'] <= 1024 or \
            not 1 <= proof['height'] <= 1024 or \
            (proof['width'], proof['height']) != (member['width'], member['height']) or \
            proof.get('sample_aspect_ratio') != '1:1' or \
            proof.get('codec') not in ('h264', 'hevc', 'prores', 'ffv1') or \
            proof.get('pix_fmt') not in ('yuv420p', 'yuv422p', 'yuv444p',
                                        'yuv420p10le', 'yuv422p10le', 'yuv444p10le') or \
            proof.get('color_space') not in ('bt709', 'bt470bg', 'smpte170m',
                                             'bt2020nc') or \
            proof.get('color_range') not in ('tv', 'pc'):
        raise ValueError('Composite v3 movie needs exact bounded CFR ffprobe stream proof (known opaque YUV, square pixels, no rotation).')
    if (not isinstance(proof.get('source_sha256'), str) or
            not re.fullmatch('[0-9a-f]{64}', proof['source_sha256']) or
            type(proof.get('source_size_bytes')) is not int or
            not 0 < proof['source_size_bytes'] <= 512 * 1024 * 1024):
        raise ValueError('Composite movie requires a bounded frozen SHA256 source identity.')
    if proof['version'] == 2:
        tracks = proof['audio_tracks']
        if not 1 <= len(tracks) <= 4 or any(
                t.get('audio_ordinal') != i or t.get('stream_index') != i + 1 or
                t.get('sample_rate') != 48000 or t.get('channels') != 2 or
                t.get('codec') != 'pcm_s16le' or
                t.get('channel_layout') not in ('stereo', '2.0') or
                Fraction(str(t.get('start_time'))) != 0 or
                Fraction(str(t.get('duration'))) <= 0
                for i, t in enumerate(tracks)):
            raise ValueError('Composite movie audio source has unsupported or ambiguous PCM stream proof.')
    try:
        frame_rate = Fraction(proof['avg_frame_rate'])
        nominal = Fraction(proof['r_frame_rate'])
        time_base = Fraction(proof['time_base'])
        first = Fraction(str(proof['start_time']))
    except (TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        raise ValueError('Composite movie clock proof is invalid: {}'.format(exc))
    if (frame_rate <= 0 or nominal != frame_rate or
            frame_rate != Fraction(member['source_fps']) or
            not Fraction(1, 100000000) <= time_base <= Fraction(1, 1) or
            first != 0):
        raise ValueError('Composite movie frozen source FPS/timebase must be CFR with zero time origin.')
    if len(member['frame_numbers']) != proof['frame_count'] or \
            any(right != left + 1 for left, right in zip(
                member['frame_numbers'], member['frame_numbers'][1:])):
        raise ValueError('Composite movie signed source labels must correspond to every consecutive zero-based decoded frame.')
    provenance = member.get('movie_color_provenance')
    if (not isinstance(provenance, dict) or
            provenance.get('confirmed') is not True or provenance.get('origin') != 'user' or
            provenance.get('input_space') != member['color']['input_space'] or
            provenance.get('alpha_mode') != 'opaque' or
            member.get('alpha_mode') != 'opaque' or
            provenance.get('yuv_range') != ('full' if proof['color_range'] == 'pc' else 'limited') or
            provenance.get('yuv_matrix') != {
                'bt709': 'bt709', 'bt470bg': 'bt601',
                'smpte170m': 'bt601', 'bt2020nc': 'bt2020'
            }[proof['color_space']]):
        raise ValueError('Composite movie source YUV matrix/range/alpha/input color requires matching user-confirmed provenance.')
    if (member['color']['input_space'].casefold() in ('raw', 'unknown', 'auto', '') or
            member.get('audio') or
            not os.path.isfile(member['source_path'])):
        raise ValueError('Composite movie must have an existing locally readable source and known input color; audio is drop only.')
    return proof


def managed_composite_plan(spec, seq_files=()):
    """Frozen session -> member source frame lineage, with intentional holes.

    This validates only serializable metadata; media itself is probed and
    decoded by the worker. Each member has its own ordered, signed sparse
    source IDs and exact session-rate mapping.
    """
    c = spec.to_context()
    frozen = c.get('composite_export')
    if isinstance(frozen, dict) and frozen.get('version') == 4:
        return managed_record_timeline_plan(spec, seq_files=seq_files)
    if not isinstance(frozen, dict) or type(frozen.get('version')) is not int or \
            frozen['version'] not in (1, 2, 3):
        raise ValueError('Composite export needs a version-1, version-2 or opt-in version-3 frozen composite_export.')
    physical = frozen['version'] == 2
    mixed = frozen['version'] == 3
    if c.get('managed_movie') or seq_files:
        raise ValueError('Composite exports require per-member image files, not top-level sequence/movie inputs.')
    if spec.stage not in ('working', 'display') or spec.start < 0:
        raise ValueError('Composite outputs must be Working or Display and start at a nonnegative session index.')
    if frozen.get('audio_policy') != 'drop':
        raise ValueError('Composite export requires explicit audio_policy="drop".')
    if _rational(frozen.get('session_fps')) != spec.fps or spec.source_fps != spec.fps:
        raise ValueError('Frozen composite session_fps must match both export timebases.')
    if (float(c.get('speed', 1)) != 1 or c.get('tracking_enabled') or
            c.get('tracking_quads') or c.get('burnin') or c.get('crop') or
            c.get('flip_h') or c.get('flip_v') or c.get('pixel_aspect', 1) != 1 or
            c.get('audio') or c.get('resample') or
            c.get('rotation', 0) not in (None, 0) or
            c.get('view_rotation', 0) not in (None, 0)):
        raise ValueError('Composite precision export does not support crop/flip/tracking/speed/audio/geometry effects.')
    if c.get('frame_numbers') or c.get('existing_frame_numbers'):
        raise ValueError('Composite session must not reuse individual source frame lists.')
    if not isinstance(c.get('color'), dict):
        raise ValueError('Composite needs a frozen shared working/display color pipeline.')
    common = c['color']
    if not all(isinstance(common.get(k), str) and common[k].strip()
               for k in ('input_space', 'working_space', 'display_space')):
        raise ValueError('Composite needs explicit working/display color spaces.')
    if (common['working_space'].casefold() != 'linear' and not common.get('config')):
        raise ValueError('Composite Working space requires linear or a verified OCIO config.')
    if (common.get('lut_roles') or
            common.get('ocio_context') and not common.get('config') or
            spec.stage == 'display' and any(common.get(key) for key in
                                            ('display', 'view', 'look')) and
            not common.get('config')):
        raise ValueError('Composite display/working LUT and OCIO settings require a supported configured processor.')
    if common.get('bypass_display') and spec.stage == 'display' and (
            common['working_space'].casefold() != common['display_space'].casefold()):
        raise ValueError('Cannot bypass required composite Display conversion.')
    ext = os.path.splitext(spec.path)[1].lower()
    if (spec.stage == 'working' and (not spec.sequence or ext != '.exr') or
            spec.stage == 'display' and not (
                spec.sequence and ext == '.png' or
                not spec.sequence and ext == '.mp4')):
        raise ValueError('Composite Working must be float EXR; Display must be PNG sequence or MP4.')
    width, height = spec.output_size
    members = frozen.get('members')
    mode = frozen.get('mode')
    two = frozenset(('a_b', 'flicker', 'blend', 'wipe', 'split', 'checker', 'over',
                     'difference', 'inverted_difference'))
    if (not isinstance(members, (list, tuple)) or
            not 2 <= len(members) <= 8 or
            mode not in two | frozenset(('grid',)) or
            (mode in two and len(members) != 2)):
        raise ValueError('Composite mode needs exactly two members, or 2-8 members for grid.')
    if mode == 'flicker' and not physical:
        # A selected frame's reference-field pixel mapping is verified only
        # for native EXR v2. Explicitly reject v1/v3 rather than let unrelated
        # v3 geometry constraints mask an unsupported Flicker clock.
        raise ValueError('Managed Flicker requires verified native EXR physical-field version 2.')
    if mixed:
        if (frozen.get('geometry_model') != 'square_pixel_v1' or
                not spec.sequence or ext not in ('.exr', '.png')):
            raise ValueError('Composite v3 needs square_pixel_v1 and only Working EXR / Display PNG image sequence output.')
        if spec.count > 120:
            raise ValueError('Composite movie decode-index selection is bounded to 120 output frames.')
    if physical:
        if (frozen.get('geometry_model') != (
                'grid_stretch_v1' if mode == 'grid' else 'physical_field_v1') or
                not spec.sequence or
                os.path.splitext(spec.path)[1].lower() not in ('.exr', '.png')):
            raise ValueError('Composite PAR requires explicit physical-field or grid-stretch geometry_model and EXR/PNG image-sequence output.')
        source_geometries = tuple(_composite_member_geometry(m) for m in members)
        native_working_bytes = sum(size[0] * size[1] * 16
                                   for size, _par in source_geometries)
        if (native_working_bytes +
                width * height * 16 * (len(members) + 3) > 384 * 1024 * 1024):
            raise ValueError('Composite PAR exceeds bounded aggregate source/canvas CPU precision memory.')
        if mode != 'grid':
            ref_size, ref_par = source_geometries[0]
            square_width = (ref_size[0] * ref_par.numerator * 2 +
                            ref_par.denominator) // (2 * ref_par.denominator)
            if (square_width < 1 or (square_width, ref_size[1]) !=
                    tuple(spec.output_size)):
                raise ValueError('Composite output must be square-pixel physical reference field {}x{}.'.format(
                    square_width, ref_size[1]))
    elif not mixed and frozen.get('geometry_model') is not None:
        raise ValueError('Composite physical geometry requires opt-in version 2.')
    if spec.count > 2000:
        raise ValueError('Composite exports are limited to 2,000 session frames.')
    if not isinstance(frozen.get('parameters'), dict):
        raise ValueError('Composite parameters must be frozen as a mapping.')
    params = frozen['parameters']
    allowed = {'active': (0, 1), 'orientation': ('vertical', 'horizontal')}
    key_modes = {'active': ('a_b',), 'flicker_frames': ('flicker',),
                 'orientation': ('wipe', 'split'),
                 'blend': ('blend',), 'wipe': ('wipe',),
                 'checker_size': ('checker',)}
    if any(key not in key_modes or mode not in key_modes[key] for key in params):
        raise ValueError('Composite has unknown or unsupported mode parameters.')
    for key, choices in allowed.items():
        if key in params and params[key] not in choices:
            raise ValueError('Invalid composite {} parameter.'.format(key))
    for key in ('blend', 'wipe'):
        if key in params and (isinstance(params[key], bool) or
                not isinstance(params[key], (int, float)) or
                not 0 <= params[key] <= 1):
            raise ValueError('Composite {} must lie within [0, 1].'.format(key))
    if 'checker_size' in params and (type(params['checker_size']) is not int or
                                     not 1 <= params['checker_size'] <= 1024):
        raise ValueError('Composite checker_size must be 1..1024 pixels.')
    if mode == 'flicker' and (type(params.get('flicker_frames', 1)) is not int or
                              not 1 <= params.get('flicker_frames', 1) <= 120):
        raise ValueError('Managed Flicker period must be 1..120 integer session frames.')
    if width * height * 16 * (len(members) + 3) > 384 * 1024 * 1024 or (
            width > 4096 or height > 4096):
        raise ValueError('Composite image exceeds bounded CPU precision memory.')
    if ext == '.mp4' and (width % 2 or height % 2):
        raise ValueError('Composite MP4 requires even output dimensions.')
    from ..playback.composition import CompositionMember
    plans = []
    unique_ids = set()
    total_files = 0
    for pos, member in enumerate(members):
        if not isinstance(member, dict) or not isinstance(member.get('source_id'), str):
            raise ValueError('Composite member must have a frozen source_id.')
        source_id = member['source_id']
        if not source_id or source_id in unique_ids:
            raise ValueError('Composite member source IDs must be nonempty and unique.')
        unique_ids.add(source_id)
        is_movie = mixed and member.get('input_kind') == 'movie'
        if mixed and member.get('input_kind') not in ('image_sequence', 'movie'):
            raise ValueError('Every v3 member must explicitly declare image_sequence or movie input_kind.')
        if mixed and (member.get('pixel_aspect') != '1/1' or
                      member.get('source_geometry') is not None):
            raise ValueError('Composite v3 only supports native square-pixel members with no extra transforms.')
        files = member.get('seq_files')
        labels = member.get('frame_numbers')
        existing = member.get('existing_frame_numbers')
        source_path = member.get('source_path')
        if (not isinstance(files, (list, tuple)) or (not files and not is_movie) or
                not isinstance(labels, (list, tuple)) or not labels or
                not isinstance(existing, (list, tuple)) or
                len(files) != len(existing) or
                not isinstance(source_path, str) or not os.path.isabs(source_path) or
                not isinstance(member.get('width'), int) or
                not isinstance(member.get('height'), int) or
                min(member['width'], member['height']) < 1 or
                max(member['width'], member['height']) > 4096 or
                member['width'] * member['height'] * 16 > 256 * 1024 * 1024 or
                not all(isinstance(path, str) and os.path.isabs(path) and
                        os.path.splitext(path)[1].lower() in MANAGED_INPUT_EXTENSIONS
                        for path in files) or
                os.path.splitext(source_path)[1].lower() not in (
                    MANAGED_MOVIE_EXTENSIONS if is_movie else MANAGED_INPUT_EXTENSIONS)):
            raise ValueError('Composite member needs bounded absolute image sequence or explicitly proven CFR movie.')
        if is_movie:
            if existing or files:
                raise ValueError('Composite movie must not carry image files or sparse existing-frame mappings.')
            _composite_v3_movie_proof(member)
        elif mixed and (any(os.path.splitext(path)[1].lower() != '.exr'
                            for path in files) or
                        os.path.splitext(source_path)[1].lower() != '.exr'):
            raise ValueError('Composite v3 image members require native full-window square-PAR EXR proof, not ambiguous PNG/TIFF PAR.')
        elif mixed and member.get('movie_proof') is not None:
            raise ValueError('Image sequence must not carry movie stream proof.')
        if ((not physical and not mixed and member.get('pixel_aspect', 1) != 1) or
                member.get('crop') or member.get('flip_h') or
                member.get('flip_v') or member.get('tracking_enabled') or
                member.get('speed', 1) != 1 or member.get('audio') or
                member.get('transform') or
                member.get('rotation') not in (None, 0) or
                member.get('view_rotation') not in (None, 0) or
                (not physical and member.get('source_geometry') is not None) or
                any(member.get(name) not in (
                    None, (0, 0, member['width'], member['height']),
                    [0, 0, member['width'], member['height']])
                    for name in ('data_window', 'display_window'))):
            raise ValueError('Composite member geometry, PAR, transforms and audio are unsupported.')
        total_files += len(files)
        if total_files > 2000:
            raise ValueError('Composite member decode inputs exceed the 2,000-frame budget.')
        if (any(type(n) is not int for n in labels) or
                any(type(n) is not int for n in existing) or
                list(labels) != sorted(set(labels)) or
                list(existing) != sorted(set(existing)) or
                not set(existing).issubset(labels)):
            raise ValueError('Composite member signed source frames must be sorted and uniquely mapped.')
        if member.get('missing_frame_policy') not in (
                'skip', 'hold', 'black', 'checker', 'error'):
            raise ValueError('Composite member missing-frame policy must be explicit.')
        if (member['missing_frame_policy'] == 'skip' and
                set(labels) != set(existing)):
            raise ValueError('Composite member cannot skip sparse holes: use an explicit placeholder policy.')
        color = member.get('color')
        if not isinstance(color, dict) or not all(
                isinstance(color.get(k), str) and color[k].strip()
                for k in ('input_space', 'working_space', 'display_space')):
            raise ValueError('Every composite member needs a frozen input color state.')
        if (color['working_space'] != common['working_space'] or
                color.get('alpha_mode') != member.get('alpha_mode') or
                member.get('alpha_mode') not in ('opaque', 'straight', 'premultiplied') or
                color.get('bypass_input') and color['input_space'] != color['working_space']):
            raise ValueError('Composite member working space or alpha association differs from frozen pipeline.')
        # Viewer compositor intentionally grades after combining. Reject any
        # member-specific grading that this reference would otherwise ignore.
        neutral = {'exposure': 0, 'gamma': 1, 'contrast': 0, 'saturation': 0,
                   'temperature': 0, 'cdl_slope': (1, 1, 1),
                   'cdl_offset': (0, 0, 0), 'cdl_power': (1, 1, 1),
                   'cdl_saturation': 1}
        if any(tuple(color.get(k, v)) != tuple(v) if isinstance(v, tuple)
               else color.get(k, v) != v for k, v in neutral.items()):
            raise ValueError('Per-member grading is unsupported; grade the composite once.')
        if color.get('lut_roles') or color.get('display') or color.get('view') or color.get('look'):
            raise ValueError('Composite member LUT/display/view transforms are unsupported before working composition.')
        try:
            from fractions import Fraction
            m = CompositionMember(source_id, len(labels),
                                  source_in=member['source_in'],
                                  session_offset=member['session_offset'],
                                  rate=member.get('rate'),
                                  trim_in=member['trim_in'],
                                  trim_out=member['trim_out'],
                                  hold=member['hold'])
            source_fps = _rational(member['source_fps'])
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            raise ValueError('Invalid frozen composite member time mapping: {}'.format(exc))
        if (m.source_in < 0 or m.trim_out >= len(labels) or
                m.trim_in > m.trim_out or
                (member.get('rate') is not None and
                 m.rate != Fraction(source_fps) / Fraction(spec.fps))):
            raise ValueError('Composite source trim/rate incompatible with bounded member frames or session FPS.')
        by_id = dict(zip(existing, files))
        timeline = []
        for index in range(spec.start, spec.end + 1):
            member_index = m.map_frame(index, spec.fps, source_fps)
            if member_index is None:
                timeline.append({
                    'member_index': pos, 'source_id': source_id,
                    'source_frame': None, 'member_frame_index': None,
                    'path': None, 'hole': True, 'held_from': None,
                    'missing_policy': 'transparent', 'kind': 'outside_trim'})
                continue
            frame_id = labels[member_index]
            path = source_path if is_movie else by_id.get(frame_id)
            previous = None if is_movie else next((v for v in reversed(labels[:member_index + 1])
                                                  if v in by_id), None)
            policy = member['missing_frame_policy'] if path is None else None
            held_from = previous if policy == 'hold' and previous is not None else None
            if held_from is not None:
                path = by_id[held_from]
            timeline.append({
                'member_index': pos, 'source_id': source_id,
                'source_frame': frame_id, 'member_frame_index': member_index,
                'path': path, 'hole': (not is_movie and frame_id not in by_id),
                'held_from': held_from, 'missing_policy': policy,
                'kind': ('decoded_movie_frame' if is_movie else
                         'intentional_hole' if frame_id not in by_id else
                         'decoded_source'),
                **({'input_frame_index': member_index} if is_movie else {})})
        plans.append(timeline)
    if spec.source_path != members[0]['source_path']:
        raise ValueError('Composite job source identity must match its first member.')
    return tuple({
        'display_index': frame, 'source_frame': frame,
        'hole': False, 'held_from': None, 'kind': 'composite_frame',
        'members': [plan[frame - spec.start] for plan in plans]}
        for frame in range(spec.start, spec.end + 1))


def managed_record_timeline_plan(spec, seq_files=()):
    """Opt-in v4 EDL record clock, one independently frozen occurrence per cut.

    Reuses v3 member validation/proven movie SHA/CFR/EXR checks by building a
    private, non-persistent synthetic composition with unique occurrence IDs.
    The original logical Source IDs and color proofs are never rewritten in
    the returned record-frame lineage. Gaps carry *transparent black* RGBA.
    """
    ctx = spec.to_context()
    record = ctx.get('composite_export')
    if (not isinstance(record, dict) or record.get('version') != 4 or
            record.get('mode') != 'sequence' or
            record.get('geometry_model') != 'square_pixel_v1' or
            record.get('parameters') != {} or
            record.get('audio_policy') not in ('drop', 'wav_pcm_v1') or
            spec.start < 0 or spec.end >= 120 or seq_files or
            not spec.sequence or spec.stage not in ('working', 'display') or
            os.path.splitext(spec.path)[1].lower() != (
                '.exr' if spec.stage == 'working' else '.png')):
        raise ValueError('Record v4 requires sequence mode, <=120 record frames, Working EXR/Display PNG and frozen square-pixel geometry.')
    if record['audio_policy'] == 'wav_pcm_v1':
        from .record_audio import validate_record_audio_plan
        validate_record_audio_plan(spec, record)
    elif record.get('record_audio') is not None:
        raise ValueError('Record audio metadata cannot be present with audio_policy drop.')
    if record.get('record_av') is not None:
        from .record_av_mux import validate_record_av
        validate_record_av(spec, record)
    if record.get('record_rgba16') is not None:
        from .record_rgba16_mux import validate_record_rgba16
        validate_record_rgba16(spec, record)
    members = record.get('members')
    if not isinstance(members, (list, tuple)) or not 2 <= len(members) <= 8:
        raise ValueError('Record v4 requires 2..8 ordered edit occurrences.')
    seen_occurrences, source_contracts = set(), {}
    previous_end = -1
    synthetic = []
    for member in members:
        if not isinstance(member, dict):
            raise ValueError('Every record occurrence must be a frozen mapping.')
        occurrence = member.get('occurrence_id')
        start, duration = member.get('session_offset'), member.get('record_length')
        if (not isinstance(occurrence, str) or not occurrence or
                len(occurrence) > 128 or occurrence in seen_occurrences or
                type(start) is not int or type(duration) is not int or
                start < 0 or not 1 <= duration <= 120 or
                start <= previous_end or start + duration > 120):
            raise ValueError('Record occurrences must have unique IDs, ordered nonoverlapping bounded record ranges.')
        seen_occurrences.add(occurrence)
        previous_end = start + duration - 1
        # Source identity belongs to the actual logical media, not its
        # occurrence. For repeated Source IDs, every frozen non-edit field
        # must be identical so decoders and color provenance cannot diverge.
        logical = member.get('source_id')
        if not isinstance(logical, str) or not logical:
            raise ValueError('Record occurrence needs its real logical Source ID.')
        if (type(member.get('source_in')) is not int or
                type(member.get('trim_in')) is not int or
                type(member.get('trim_out')) is not int or
                not member['trim_in'] <= member['source_in'] <= member['trim_out']):
            raise ValueError('Record source_in must be within this occurrence’s frozen trim.')
        if (member.get('width'), member.get('height')) != spec.output_size:
            raise ValueError('Record v4 requires each square-pixel cut to match the frozen output resolution; implicit rescaling is unsupported.')
        try:
            exact = Fraction(member['rate'])
            expected = Fraction(member['source_fps']) / Fraction(spec.fps)
        except (KeyError, ValueError, TypeError, ZeroDivisionError,
                OverflowError) as exc:
            raise ValueError('Record occurrence needs exact rational source/session rate: {}'.format(exc))
        if (type(member.get('rate')) is not str or exact <= 0 or
                exact.denominator > 1000000 or
                member['rate'] != '{}/{}'.format(exact.numerator, exact.denominator) or
                exact != expected):
            raise ValueError('Record occurrence rate must be canonical exact source_fps/session_fps rational.')
        excluded = {'occurrence_id', 'record_length', 'source_in', 'session_offset',
                    'rate', 'trim_in', 'trim_out', 'hold'}
        contract = {key: value for key, value in member.items()
                    if key not in excluded}
        if logical in source_contracts and contract != source_contracts[logical]:
            raise ValueError('Repeated record Source ID has inconsistent frozen media/color provenance.')
        source_contracts[logical] = contract
        from copy import deepcopy
        copy = deepcopy(member)
        copy.pop('occurrence_id', None)
        copy.pop('record_length', None)
        copy['source_id'] = occurrence
        # v3 validates exact rational rates, signed sparse frame identity,
        # native EXR geometry and CFR movie SHA/color provenance. No relaxed
        # duplicate-ID exception can bypass this independent validation.
        synthetic.append(copy)
    if spec.source_path != members[0].get('source_path'):
        raise ValueError('Record source path must identify the first frozen occurrence.')
    frozen_v3 = dict(record, version=3, mode='grid', parameters={},
                     audio_policy='drop',
                     members=synthetic)
    temporary = dict(ctx, composite_export=frozen_v3)
    surrogate = ExportSpec.from_context(
        temporary, source_path=spec.source_path, source_id=spec.source_id)
    mapped = managed_composite_plan(surrogate)
    rows = []
    for item in mapped:
        frame = item['display_index']
        active = next((pos for pos, m in enumerate(members)
                       if m['session_offset'] <= frame <
                       m['session_offset'] + m['record_length']), None)
        if active is None:
            rows.append({'display_index': frame, 'source_frame': frame,
                         'hole': True, 'held_from': None,
                         'kind': 'record_gap', 'gap_policy': 'transparent_black',
                         'members': []})
            continue
        source = members[active]
        member = dict(item['members'][active])
        member.update(source_id=source['source_id'],
                      occurrence_id=source['occurrence_id'])
        rows.append({'display_index': frame, 'source_frame': frame,
                     'hole': bool(member['hole'] or member['kind'] == 'outside_trim'),
                     'held_from': member['held_from'],
                     'kind': 'record_frame', 'occurrence_id': source['occurrence_id'],
                     'logical_source_id': source['source_id'],
                     'members': [member]})
    return tuple(rows)


def managed_composite_geometry_plan(spec):
    """Derived immutable-v2 proof for durable composite output receipts.

    File-header evidence is confirmed anew by the worker before rendering;
    this plan is the exact expectation for queue commit/crash recovery.
    """
    frozen = spec.to_context().get('composite_export') or {}
    if frozen.get('version') == 4:
        managed_record_timeline_plan(spec)
        return {'version': 4, 'mapping': 'record_timeline_square_pixel_v1',
                'output_size': list(spec.output_size),
                'output_pixel_aspect': '1/1',
                'source_geometries': [{
                    'occurrence_id': m['occurrence_id'],
                    'source_id': m['source_id'],
                    'size': [m['width'], m['height']],
                    'pixel_aspect': '1/1', 'input_kind': m['input_kind']}
                    for m in frozen['members']],
                'movie_proofs': {m['occurrence_id']: m['movie_proof']
                                 for m in frozen['members'] if
                                 m['input_kind'] == 'movie'},
                'gap_semantics': 'transparent black RGBA(0,0,0,0)',
                'filter': 'CPU per-cut source→working→display, no overlapping cuts'}
    if frozen.get('version') == 3:
        managed_composite_plan(spec)
        return {'version': 2, 'mapping': 'square_pixel_v1',
                'output_size': list(spec.output_size),
                'output_pixel_aspect': '1/1',
                'source_geometries': [{
                    'source_id': m['source_id'], 'size': [m['width'], m['height']],
                    'pixel_aspect': '1/1', 'input_kind': m['input_kind']}
                    for m in frozen['members']],
                'movie_proofs': {m['source_id']: m['movie_proof']
                                 for m in frozen['members'] if
                                 m['input_kind'] == 'movie'},
                'filter': 'CPU working-linear compositor, native-square sample'}
    if frozen.get('version') != 2:
        return None
    # Validates output physical field and *all* members, not only first-frame.
    managed_composite_plan(spec)
    members = frozen['members']
    return {'version': 1,
            'mapping': frozen['geometry_model'],
            'output_size': list(spec.output_size),
            'output_pixel_aspect': '1/1',
            'reference_member': members[0]['source_id'],
            'reference_source_size': [members[0]['width'], members[0]['height']],
            'reference_pixel_aspect': members[0]['pixel_aspect'],
            'source_geometries': [dict(member['source_geometry'])
                                  for member in members],
            'native_exr_header_check': 'all_decoded_frames',
            'filter': 'CPU physical-field nearest sample' if
            frozen['geometry_model'] == 'physical_field_v1' else
            'CPU grid cell stretch nearest sample'}


def validate_managed_movie_audio(spec):
    """Frozen, opt-in embedded audio for a *single* CFR Display MP4.

    Audio follows the verified video decode-frame clock. Unverified offsets
    and retimes fail closed, rather than silently adopting playback routes.
    """
    c = spec.to_context()
    p = c.get('movie_audio_export')
    if (not c.get('managed_movie') or
            c.get('movie_audio_policy') != 'preserve' or
            not isinstance(p, dict) or p.get('version') != 1 or
            p.get('confirmed') is not True or
            p.get('source') != 'embedded'):
        raise ValueError('Managed movie audio requires an explicitly confirmed embedded audio plan.')
    if (spec.stage != 'display' or spec.sequence or
            os.path.splitext(spec.path)[1].lower() != '.mp4'):
        raise ValueError('Managed movie audio can only be preserved in final Display MP4.')
    if spec.fps != spec.source_fps or c.get('speed', 1) != 1:
        raise ValueError('Managed movie audio requires unchanged exact CFR source FPS and speed.')
    if (c.get('audio') or c.get('external_audio') or c.get('audio_route') or
            c.get('audio_delay_ms') or c.get('audio_offset_ms')):
        raise ValueError('External audio routes, source delays and audio offsets are unsupported.')
    required = {'version', 'confirmed', 'source', 'stream_index', 'codec',
                'sample_rate', 'channels', 'bit_rate', 'offset_ms',
                'alignment', 'source_time_base', 'source_sample_rate'}
    if (set(p) != required or
            type(p.get('stream_index')) is not int or p['stream_index'] < 0 or
            p.get('codec') != 'aac' or p.get('sample_rate') != 48000 or
            p.get('channels') != 2 or p.get('bit_rate') != 192000 or
            p.get('offset_ms') != 0 or
            p.get('alignment') != 'cfr_frame_count' or
            type(p.get('source_sample_rate')) is not int or
            p['source_sample_rate'] not in (44100, 48000)):
        raise ValueError('Unsupported frozen movie audio stream, codec, resample, offset or channel layout.')
    try:
        base = Fraction(str(p['source_time_base']))
    except (ValueError, ZeroDivisionError, TypeError):
        raise ValueError('Managed audio needs a frozen exact source time_base.')
    if base <= 0 or base > Fraction(1, 1000):
        raise ValueError('Managed audio source time_base is implausible.')
    if Fraction(spec.count, 1) / Fraction(spec.fps) > 300:
        raise ValueError('Managed embedded audio is limited to five-minute selected ranges.')
    return p


def managed_movie_plan(spec):
    """CFR display indices retain their absolute zero-based decode indices.

    Source-frame labels may be signed, but are consecutive; an encoded movie
    does not declare missing images. Exact index->label mapping is snapshotted
    and checked before any decoder starts.
    """
    c = spec.to_context()
    if spec.start < 0:
        raise ValueError('Managed movie display-index range must be nonnegative.')
    labels = c.get('frame_numbers')
    if labels is not None:
        if (not isinstance(labels, (list, tuple)) or len(labels) <= spec.end or
                any(isinstance(x, bool) or not isinstance(x, int)
                    for x in labels) or
                any(b != a + 1 for a, b in zip(labels, labels[1:]))):
            raise ValueError('Managed movie frame_numbers must be consecutive signed integers.')
    else:
        labels = tuple(range(spec.end + 1))
    if c.get('existing_frame_numbers'):
        raise ValueError('Managed movie must not carry image existing_frame_numbers.')
    endpoints = c.get('source_frames')
    if isinstance(endpoints, dict) and endpoints:
        if (endpoints.get('start') != labels[spec.start] or
                endpoints.get('end') != labels[spec.end]):
            raise ValueError('Managed movie source_frames disagree with signed frame labels.')
    return tuple({'display_index': index, 'source_frame': labels[index],
                  'input_frame_index': index, 'hole': False, 'held_from': None,
                  'kind': 'decoded_movie_frame'}
                 for index in range(spec.start, spec.end + 1))


def managed_input_plan(spec, seq_files):
    """Immutable display-index -> signed source ID / file-or-placeholder plan.

    Full `frame_numbers` includes intentional holes; `existing_frame_numbers`
    maps 1:1 to `seq_files` (which excludes holes). Without explicit frames the
    filename's final numeric field is mandatory: guessing an index would shift
    all source identities on sparse sequences. Decode errors are NEVER holes.
    """
    c = spec.to_context()
    declared = c.get('frame_numbers')
    existing = c.get('existing_frame_numbers')
    if existing is None:
        existing = []
        for path in seq_files:
            match = re.search(r'(?:^|[._])(-?\d+)\.[^.]+$', os.path.basename(path))
            if match is None:
                raise ValueError('Managed export requires explicit existing_frame_numbers or numbered image filenames.')
            existing.append(int(match.group(1)))
    if (not isinstance(existing, (tuple, list)) or len(existing) != len(seq_files) or
            any(isinstance(f, bool) or not isinstance(f, int) for f in existing) or
            list(existing) != sorted(set(existing))):
        raise ValueError('existing_frame_numbers must identify each ordered image exactly once.')
    if declared is None:
        declared = existing
    if (not isinstance(declared, (tuple, list)) or not declared or
            any(isinstance(f, bool) or not isinstance(f, int) for f in declared) or
            list(declared) != sorted(set(declared)) or
            not set(existing).issubset(declared)):
        raise ValueError('frame_numbers must be sorted, unique and include all existing frames.')
    if spec.end >= len(declared):
        raise ValueError('Managed export display range exceeds the mapped image timeline.')
    by_id = dict(zip(existing, seq_files))
    policy = c.get('missing_frame_policy', 'error')
    if policy not in ('skip', 'hold', 'black', 'checker', 'error'):
        raise ValueError('Unknown managed sequence missing-frame policy.')
    out = []
    previous = None
    for index, source_frame in enumerate(declared):
        path = by_id.get(source_frame)
        if path is not None:
            previous = source_frame
        if not spec.start <= index <= spec.end:
            continue
        if path is None and policy == 'skip':
            raise ValueError('Managed export cannot render a missing source frame using skip; select Hold/Black/Checker/Error or remove the hole.')
        # Hold before any prior existing frame behaves as black, like viewer.
        actual = (path if path is not None else
                  by_id.get(previous) if policy == 'hold' else None)
        out.append({'display_index': index, 'source_frame': source_frame,
                    'path': actual, 'hole': path is None,
                    'held_from': previous if path is None and actual else None,
                    'hole_policy': policy if path is None else None})
    if len(out) != spec.count:
        raise ValueError('Managed frame plan does not match output count.')
    declared_endpoints = c.get('source_frames')
    if isinstance(declared_endpoints, dict) and all(
            key in declared_endpoints for key in ('start', 'end')):
        if (int(declared_endpoints['start']) != out[0]['source_frame'] or
                int(declared_endpoints['end']) != out[-1]['source_frame']):
            raise ValueError('Managed export Source frame endpoints disagree with mapped display range.')
    return tuple(out)


def _freeze(value):
    if isinstance(value, dict):
        return ('__map__', tuple(sorted(((k, _freeze(v)) for k, v in value.items()),
                                        key=lambda item: str(item[0]))))
    if isinstance(value, (tuple, list)):
        return ('__sequence__', tuple(_freeze(item) for item in value),
                isinstance(value, tuple))
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    raise TypeError('Export context contains an unsupported value: {}'.format(type(value)))


def _thaw(value):
    if isinstance(value, tuple) and value and value[0] == '__map__':
        return {k: _thaw(v) for k, v in value[1]}
    if isinstance(value, tuple) and value and value[0] == '__sequence__':
        values = [_thaw(v) for v in value[1]]
        return tuple(values) if value[2] else values
    return value


def _rational(value):
    fraction = Fraction(str(value)).limit_denominator(1001000)
    if fraction <= 0:
        raise ValueError('FPS must be positive.')
    # Decimal media metadata commonly rounds NTSC rates to 23.976/29.97/59.94.
    # Recover the canonical rational when that rounding is unambiguous.
    for ntsc in (Fraction(24000, 1001), Fraction(30000, 1001),
                 Fraction(60000, 1001), Fraction(120000, 1001)):
        if abs(float(ntsc - fraction)) < 0.0001:
            fraction = ntsc
            break
    return '{}/{}'.format(fraction.numerator, fraction.denominator)


@dataclass(frozen=True)
class ExportSpec:
    path: str
    source_path: str
    source_id: str
    start: int
    end: int
    count: int
    output_size: tuple
    fps: str
    source_fps: str
    stage: str = 'source'
    sequence: bool = False
    context_items: tuple = field(default_factory=tuple, repr=False)
    schema_version: int = 1

    def __post_init__(self):
        if self.end < self.start or self.count <= 0 or self.count != self.end - self.start + 1:
            raise ValueError('Export range must be inclusive and agree with frame count.')
        if len(self.output_size) != 2 or min(self.output_size) <= 0:
            raise ValueError('Export dimensions must be positive.')
        if self.stage not in ('source', 'working', 'display'):
            raise ValueError('Unknown export color stage.')
        if not self.path:
            raise ValueError('Output path is required.')

    @classmethod
    def from_context(cls, context, source_path='', source_id=''):
        c = dict(context)
        start = int(c['start'])
        end = int(c.get('end', start + int(c['count']) - 1))
        size = (int(c['out_w']), int(c['out_h']))
        return cls(str(c['path']), str(source_path or c.get('source_path', '')),
                   str(source_id or c.get('source_id', '')), start, end,
                   int(c['count']), size, _rational(c['fps']),
                   _rational(c['source_fps']), str(c.get('color_stage', 'source')),
                   bool(c.get('sequence', False)), _freeze(c))

    def to_context(self):
        return _thaw(self.context_items)

    def manifest(self, verification=None):
        context = self.to_context()
        return {
            'schema_version': self.schema_version,
            'source': {'id': self.source_id, 'path': self.source_path},
            'output': {'path': self.path, 'sequence': self.sequence,
                       'width': self.output_size[0], 'height': self.output_size[1],
                       'stage': self.stage},
            'frames': {'start': self.start, 'end': self.end,
                       'count': self.count, 'fps': self.fps,
                       'source_fps': self.source_fps},
            'transforms': {'flip_h': bool(context.get('flip_h')),
                           'flip_v': bool(context.get('flip_v')),
                           'crop': context.get('crop'),
                           'tracking_enabled': bool(context.get('tracking_enabled')),
                           'tracking_mode': context.get('tracking_mode'),
                           'tracking_centers': context.get('centers', {}),
                           'tracking_quads': context.get('tracking_quads', {}),
                           'speed': context.get('speed', 1.0),
                           'color': context.get('color', {}),
                           'notes': context.get('note_ids', []),
                           'burnin': context.get('burnin', {})},
            'verification': verification or {},
        }

    def write_manifest(self, verification):
        path = self.path + '.frmanifest.json'
        folder = os.path.dirname(os.path.abspath(path))
        fd, temp_path = tempfile.mkstemp(prefix='.frmanifest-', suffix='.tmp', dir=folder)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(self.manifest(verification), out, indent=2,
                          sort_keys=True, ensure_ascii=False)
                out.write('\n')
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        return path


def _files_for_sequence(pattern):
    match = re.search(r'(#+|%0\d+d)', pattern)
    if match is None:
        raise ValueError('Sequence output must have a numbered frame pattern.')
    matches = sorted(glob.glob(pattern[:match.start()] + '*' + pattern[match.end():]))
    return [path for path in matches if os.path.isfile(path)]


def verify_output(spec, ffprobe=None):
    """Check output before success; use ffprobe for stream/time metadata.

    Missing ffprobe permits basic output checks and records the reduced
    verification level; an installed but failing ffprobe is a hard failure.
    """
    if spec.sequence:
        files = _files_for_sequence(spec.path)
        if len(files) != spec.count:
            raise RuntimeError('Image sequence: expected {} frames, found {}.'.format(
                spec.count, len(files)))
        if any(os.path.getsize(path) <= 0 for path in files):
            raise RuntimeError('Image sequence contains empty files.')
        result = {'status': 'verified', 'kind': 'sequence',
                  'frame_count': len(files), 'first': os.path.basename(files[0]),
                  'last': os.path.basename(files[-1])}
        target = files[0]
        record = spec.to_context().get('composite_export') or {}
        if record.get('version') == 4:
            from .record_audio import audio_sidecar_path, verify_record_audio
            if record.get('audio_policy') == 'wav_pcm_v1':
                result['record_audio_verification'] = verify_record_audio(spec)
            elif os.path.lexists(audio_sidecar_path(spec)):
                raise RuntimeError('Audio-drop record output unexpectedly contains a WAV sidecar.')
            if record.get('record_av') is not None:
                from .record_av_mux import verify_record_av
                result['record_av_verification'] = verify_record_av(spec)
            else:
                from .record_av_mux import record_av_path
                if os.path.lexists(record_av_path(spec)):
                    raise RuntimeError('Unselected record MOV appeared in a v4 WAV/PNG output.')
            if record.get('record_rgba16') is not None:
                from .record_rgba16_mux import verify_record_rgba16
                result['record_rgba16_verification'] = verify_record_rgba16(spec)
            else:
                from .record_rgba16_mux import record_rgba16_path
                if os.path.lexists(record_rgba16_path(spec)):
                    raise RuntimeError('Unselected RGBA16 NUT appeared in v4 WAV/PNG output.')
    else:
        target = spec.path
        if not os.path.isfile(target) or os.path.getsize(target) <= 16:
            raise RuntimeError('Export output is absent or empty: {}'.format(target))
        result = {'status': 'basic', 'kind': 'movie',
                  'file_size': os.path.getsize(target)}
    if ffprobe is None:
        ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        context = spec.to_context()
        if context.get('managed_movie') and context.get('movie_audio_policy') == 'preserve':
            raise RuntimeError('Managed embedded audio cannot be verified without ffprobe.')
        return result
    cmd = [ffprobe, '-v', 'error', '-count_frames', '-select_streams', 'v:0',
           '-show_entries', 'stream=width,height,codec_name,pix_fmt,avg_frame_rate,nb_read_frames:format=duration',
           '-of', 'json', target]
    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=30)
        data = json.loads(output.decode('utf-8'))
        streams = data['streams']
        stream = streams[0]
    except (IndexError, ValueError, KeyError, subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError('ffprobe could not verify export: {}'.format(exc))
    size = (int(stream['width']), int(stream['height']))
    if size != spec.output_size:
        raise RuntimeError('Output dimensions {} differ from expected {}.'.format(
            size, spec.output_size))
    counted = stream.get('nb_read_frames')
    if not spec.sequence and counted not in (None, 'N/A'):
        actual_count = int(counted)
        context = spec.to_context()
        if spec.stage != 'source' or context.get('managed_movie'):
            # The managed frame plan enumerates one output per display-index
            # entry, even when output FPS differs from media/source FPS.
            expected = spec.count
        else:
            speed = abs(float(context.get('speed', 1.0)))
            if speed <= 0:
                raise RuntimeError('Output specification has invalid playback speed.')
            expected = max(1, int(round(spec.count * float(Fraction(spec.fps)) /
                                         float(Fraction(spec.source_fps)) / speed)))
            if not (context.get('tracking_enabled') or context.get('burnin')):
                expected = min(expected, spec.count)  # FFmpeg -frames:v cap
        if abs(actual_count - expected) > max(1, int(round(expected * 0.02))):
            raise RuntimeError('Output has {} frames, expected about {}.'.format(
                actual_count, expected))
    stream_rate = stream.get('avg_frame_rate')
    if not spec.sequence and stream_rate not in (None, '0/0', 'N/A'):
        actual_rate = float(Fraction(stream_rate))
        desired_rate = float(Fraction(spec.fps))
        if abs(actual_rate - desired_rate) > 0.05:
            raise RuntimeError('Output FPS {} differs from requested {}.'.format(
                stream_rate, spec.fps))
    result.update({'status': 'verified', 'width': size[0], 'height': size[1],
                   'codec': stream.get('codec_name'),
                   'pixel_format': stream.get('pix_fmt'),
                   'fps': stream_rate,
                   'duration': data.get('format', {}).get('duration'),
                   'frames_probed': counted})
    context = spec.to_context()
    if context.get('managed_movie') and context.get('movie_audio_policy') == 'preserve':
        validate_managed_movie_audio(spec)
        try:
            raw_audio = subprocess.check_output(
                [ffprobe, '-v', 'error', '-count_frames',
                 '-select_streams', 'a',
                 '-show_entries',
                 'stream=index,codec_type,codec_name,channels,sample_rate,start_time,duration,nb_read_frames',
                 '-of', 'json', target],
                stderr=subprocess.STDOUT, timeout=30)
            if len(raw_audio) > 256 * 1024:
                raise RuntimeError('Muxed audio metadata exceeds bounded probe size.')
            audio_streams = json.loads(raw_audio.decode('utf-8'))['streams']
            if len(audio_streams) != 1:
                raise RuntimeError('Managed output needs exactly one selected audio track.')
            audio = audio_streams[0]
            duration = Fraction(spec.count, 1) / Fraction(spec.fps)
            actual_duration = Fraction(str(audio['duration']))
            start = Fraction(str(audio['start_time']))
            frames = int(audio['nb_read_frames'])
            if (audio.get('codec_name') != 'aac' or
                    int(audio['sample_rate']) != 48000 or
                    int(audio['channels']) != 2 or
                    abs(float(start)) > .025 or
                    abs(float(actual_duration - duration)) > .05 or
                    frames <= 0):
                raise RuntimeError('Audio stream codec, rate, channel count, timing or duration mismatch.')
            result['audio_verification'] = {
                'status': 'verified', 'codec': 'aac',
                'sample_rate': 48000, 'channels': 2,
                'start_time': str(start),
                'duration': str(actual_duration),
                'expected_duration': str(duration),
                'decoded_audio_frames': frames}
        except (IndexError, KeyError, TypeError, ValueError,
                subprocess.SubprocessError, OSError) as exc:
            raise RuntimeError('Managed audio stream verification failed: {}'.format(exc))
    return result
