# -*- coding: utf-8 -*-
"""Bounded v4 record audio: one PCM WAV INSIDE the atomic image deliverable.

Only proven zero-origin stereo PCM movie or canonical external WAV audio, and
explicit image-edit silence are supported. Optional one legacy overlay or
an ordered list of up to three independently proven external WAV overlays
is added with exact signed saturated PCM16 arithmetic.
Samples follow exact source-frame indices; no Qt latency compensation,
arbitrary retiming or codec guessing.
"""
from __future__ import absolute_import

from fractions import Fraction
import hashlib
import json
import os
import re
import shutil
import subprocess
import wave


FILENAME = 'record_audio.wav'
RATE = 48000
CHANNELS = 2
BYTES_PER_SAMPLE_FRAME = 4


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _source_sha(path):
    if not os.path.isfile(path) or os.path.islink(path):
        raise ValueError('Record PCM input needs an immutable regular source, not a symlink.')
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def audio_sidecar_path(spec):
    return os.path.join(os.path.dirname(spec.path), FILENAME)


def validate_record_audio_plan(spec, record):
    """Validate the complete frozen user intent before any render or queue IO."""
    frozen = record.get('record_audio')
    base_keys = {'version', 'file', 'sample_rate', 'channels',
                 'sample_format', 'routes'}
    if (not isinstance(frozen, dict) or
            set(frozen) not in (base_keys, base_keys | {'overlay'},
                                base_keys | {'overlays'}) or
            type(frozen['version']) is not int or frozen['version'] != 1 or
            frozen['file'] != FILENAME or
            frozen['sample_rate'] != RATE or frozen['channels'] != CHANNELS or
            frozen['sample_format'] != 's16le' or
            Fraction(spec.fps) != Fraction(24000, 1001) or
            spec.start < 0 or spec.end >= 120 or
            not spec.sequence or spec.stage not in ('working', 'display')):
        raise ValueError('Record PCM requires bounded 24000/1001 EXR/PNG sequence with frozen 48k stereo s16 WAV sidecar.')
    members, routes = record.get('members'), frozen['routes']
    if not isinstance(members, (list, tuple)) or not isinstance(routes, list) or \
            len(routes) != len(members) or not 2 <= len(routes) <= 8:
        raise ValueError('Record WAV requires exactly one audio route for every edit occurrence.')
    seen = set()
    for member, route in zip(members, routes):
        occurrence = member.get('occurrence_id')
        if (not isinstance(route, dict) or
                route.get('occurrence_id') != occurrence or
                occurrence in seen):
            raise ValueError('Audio routes must follow unique frozen record occurrence IDs.')
        seen.add(occurrence)
        if route.get('kind') == 'silence':
            if set(route) != {'occurrence_id', 'kind'} or \
                    member.get('input_kind') != 'image_sequence':
                raise ValueError('Silence may only be explicitly selected for image-sequence edits.')
            continue
        external = route.get('kind') == 'external'
        if external:
            from .external_audio_proof import MAX_WAV_BYTES, _safe_wav_path
            if (set(route) != {'occurrence_id', 'kind', 'external_path',
                              'audio_ordinal', 'stream_index', 'external_proof'} or
                    member.get('input_kind') not in ('movie', 'image_sequence') or
                    route.get('audio_ordinal') != 0 or
                    type(route.get('audio_ordinal')) is not int or
                    route.get('stream_index') != 0 or
                    type(route.get('stream_index')) is not int or
                    type(route.get('external_proof')) is not dict):
                raise ValueError('External PCM requires exactly one explicit zero-origin WAV stream per occurrence.')
            external_proof = route['external_proof']
            if (set(external_proof) != {
                    'version', 'source_sha256', 'source_size_bytes',
                    'pcm_sha256', 'sample_frames', 'audio_ordinal',
                    'stream_index', 'codec', 'sample_rate', 'channels',
                    'time_base', 'start_pts'} or
                    external_proof['version'] != 1 or
                    type(external_proof['version']) is not int or
                    type(external_proof['source_size_bytes']) is not int or
                    not 44 <= external_proof['source_size_bytes'] <= MAX_WAV_BYTES or
                    type(external_proof['sample_frames']) is not int or
                    external_proof['sample_frames'] < 1 or
                    any(not isinstance(external_proof[key], str) or not
                        re.fullmatch(r'[0-9a-f]{64}', external_proof[key])
                        for key in ('source_sha256', 'pcm_sha256')) or
                    (external_proof['audio_ordinal'],
                     external_proof['stream_index'], external_proof['codec'],
                     external_proof['sample_rate'], external_proof['channels'],
                     external_proof['time_base'], external_proof['start_pts']) !=
                    (0, 0, 'pcm_s16le', RATE, CHANNELS, '1/48000', 0)):
                raise ValueError('External PCM source proof is incomplete or inconsistent.')
            _safe_wav_path(route.get('external_path'))
            if (Fraction(member['source_fps']) != Fraction(spec.fps) or
                    Fraction(member['rate']) != 1 or
                    (member['source_in'] + member['record_length']) * RATE /
                    Fraction(spec.fps) > external_proof['sample_frames']):
                raise ValueError('External PCM does not cover exact source cuts without retime or delay.')
            continue
        if route.get('kind') != 'embedded' or \
                set(route) != {'occurrence_id', 'kind', 'audio_ordinal', 'stream_index'} or \
                member.get('input_kind') != 'movie':
            raise ValueError('Record WAV requires explicit embedded PCM, proven external PCM or image-cut silence.')
        proof = member.get('movie_proof') or {}
        tracks = proof.get('audio_tracks') or []
        ordinal = route.get('audio_ordinal')
        if (proof.get('version') != 2 or
                type(ordinal) is not int or ordinal < 0 or ordinal >= len(tracks) or
                type(route.get('stream_index')) is not int or
                route['stream_index'] != tracks[ordinal]['stream_index'] or
                Fraction(member['source_fps']) != Fraction(spec.fps) or
                Fraction(member['rate']) != 1):
            raise ValueError('Selected embedded track needs verified CFR movie/audio proof and exact 1:1 record/source clock.')
        if (not re.fullmatch(r'[0-9a-f]{64}', proof.get('source_sha256', '')) or
                tracks[ordinal]['sample_rate'] != RATE or
                tracks[ordinal]['channels'] != CHANNELS or
                tracks[ordinal]['codec'] != 'pcm_s16le'):
            raise ValueError('Record PCM audio selection lacks frozen source/track provenance.')
        first = member['source_in']
        duration = member['record_length']
        if (first < member['trim_in'] or first + duration - 1 > member['trim_out'] or
                first + duration > proof['frame_count'] or
                Fraction(str(tracks[ordinal]['duration'])) <
                Fraction(first + duration, 1) / Fraction(spec.fps)):
            raise ValueError('Record PCM cannot run past selected audio trim/duration; implicit hold or stretch is unsupported.')
    if 'overlay' in frozen:
        from .record_audio_overlay import validate_record_audio_overlay
        validate_record_audio_overlay(spec, record)
    if 'overlays' in frozen:
        from .record_audio_overlay import validate_record_audio_overlays
        validate_record_audio_overlays(spec, record)
    return frozen


def _pcm(ffmpeg, path, ordinal, interrupted=None, set_process=None):
    """Decode a small, selected embedded track; never infer Qt audio ordinals."""
    if interrupted and interrupted():
        raise RuntimeError('Record PCM cancelled.')
    args = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-i', path, '-map', '0:a:{}'.format(ordinal), '-vn', '-sn', '-dn',
            '-af', 'aresample=48000:async=0:first_pts=0',
            '-ac', '2', '-ar', '48000', '-c:a', 'pcm_s16le',
            '-f', 's16le', 'pipe:1']
    from ..media.ffmpeg import _no_window_kwargs, media_environment
    kwargs = {'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE,
              'env': media_environment()}
    kwargs.update(_no_window_kwargs())
    process = subprocess.Popen(args, **kwargs)
    if set_process is not None:
        set_process(process)
    try:
        data, error = process.communicate(timeout=45)
        if (process.returncode or (interrupted and interrupted()) or
                not data or len(data) % BYTES_PER_SAMPLE_FRAME or
                len(data) > 6 * 1024 * 1024):
            raise RuntimeError('Record PCM decoder failed, was cancelled or exceeded bounded raw samples: {}'.format(
                error.decode('utf-8', 'replace')[-300:]))
        return data
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if set_process is not None:
            set_process(None)


def _record_pcm(spec, ffmpeg, interrupted=None, set_process=None):
    from .spec import managed_record_timeline_plan
    frozen = spec.to_context()['composite_export']
    validate_record_audio_plan(spec, frozen)
    rows = managed_record_timeline_plan(spec)
    members = frozen['members']
    routes = frozen['record_audio']['routes']
    expected_count = (Fraction(spec.end + 1) * RATE / Fraction(spec.fps)).__floor__() - \
                     (Fraction(spec.start) * RATE / Fraction(spec.fps)).__floor__()
    output = bytearray()
    cache = {}
    source_receipts = {}
    for row in rows:
        if interrupted and interrupted():
            raise RuntimeError('Record PCM cancelled.')
        index = row['display_index']
        begin = (Fraction(index) * RATE / Fraction(spec.fps)).__floor__()
        finish = (Fraction(index + 1) * RATE / Fraction(spec.fps)).__floor__()
        count = finish - begin
        if row['kind'] == 'record_gap':
            output.extend(b'\x00' * (count * BYTES_PER_SAMPLE_FRAME))
            continue
        member_lineage = row['members'][0]
        member_index = member_lineage['member_index']
        member = members[member_index]
        route = routes[member_index]
        if route['kind'] == 'silence' or member_lineage['kind'] == 'outside_trim':
            output.extend(b'\x00' * (count * BYTES_PER_SAMPLE_FRAME))
            continue
        external = route['kind'] == 'external'
        proof = route['external_proof'] if external else member['movie_proof']
        path = route['external_path'] if external else member['source_path']
        key = (path, route['audio_ordinal'])
        if key not in cache:
            if external:
                from .external_audio_proof import (
                    freeze_external_record_audio_proof, _canonical_pcm)
                actual = freeze_external_record_audio_proof(path)
                if actual != proof:
                    raise ValueError('External PCM ffprobe/PTS/SHA256 differs from frozen proof.')
            if _source_sha(path) != proof['source_sha256']:
                raise ValueError('Record PCM source SHA256 differs from frozen movie proof.')
            cache[key] = _pcm(ffmpeg, key[0], key[1], interrupted, set_process)
            if external:
                sample_frames, wav_pcm_sha = _canonical_pcm(path)
                if (sample_frames != proof['sample_frames'] or
                        wav_pcm_sha != proof['pcm_sha256'] or
                        _sha(cache[key]) != wav_pcm_sha):
                    raise ValueError('External selected FFmpeg stream differs from the independent WAV PCM bytes.')
        source_receipts[member['occurrence_id']] = {
            'kind': route['kind'],
            'source_sha256': proof['source_sha256'],
            'audio_ordinal': route['audio_ordinal'],
            'stream_index': route['stream_index'],
            'decoded_pcm_sha256': _sha(cache[key])}
        if external:
            source_receipts[member['occurrence_id']].update({
                'external_path': path, 'external_proof': proof})
        source_index = (member_lineage.get('input_frame_index') if not external
                        else member_lineage['member_frame_index'])
        sample_start = (Fraction(source_index) * RATE /
                        Fraction(member['source_fps'])).__floor__()
        sample_end = (Fraction(source_index + 1) * RATE /
                      Fraction(member['source_fps'])).__floor__()
        if sample_end - sample_start != count:
            raise ValueError('Record PCM fractional frame-to-sample map differs at cut; no hidden resampling/stretch.')
        section = cache[key][sample_start * BYTES_PER_SAMPLE_FRAME:
                             sample_end * BYTES_PER_SAMPLE_FRAME]
        if len(section) != count * BYTES_PER_SAMPLE_FRAME:
            raise ValueError('Record PCM selected source audio ends before the source-frame cut.')
        output.extend(section)
    if len(output) != expected_count * BYTES_PER_SAMPLE_FRAME:
        raise RuntimeError('Record PCM assembled wrong absolute record sample count.')
    if 'overlay' in frozen['record_audio']:
        from .record_audio_overlay import apply_record_audio_overlay
        mixed, overlay_receipt = apply_record_audio_overlay(
            spec, frozen, bytes(output), ffmpeg, interrupted, set_process)
        output = bytearray(mixed)
        if overlay_receipt is not None:
            source_receipts[frozen['record_audio']['overlay']['occurrence_id']] = overlay_receipt
    elif 'overlays' in frozen['record_audio']:
        from .record_audio_overlay import apply_record_audio_overlays
        mixed, overlay_receipts = apply_record_audio_overlays(
            spec, frozen, bytes(output), ffmpeg, interrupted, set_process)
        output = bytearray(mixed)
        source_receipts.update(overlay_receipts)
    for member, route in zip(members, routes):
        if route['kind'] == 'embedded' and _source_sha(member['source_path']) != \
                member['movie_proof']['source_sha256']:
            raise ValueError('Record PCM source changed while rendering audio.')
        if route['kind'] == 'external':
            from .external_audio_proof import freeze_external_record_audio_proof
            if (interrupted and interrupted() or
                    freeze_external_record_audio_proof(route['external_path']) !=
                    route['external_proof']):
                raise ValueError('Record PCM external WAV changed during render.')
    return bytes(output), source_receipts


def render_record_audio(spec, ffmpeg, interrupted=None, set_process=None):
    """Write a new bounded lossless WAV in the EXISTING private queue stage."""
    target = audio_sidecar_path(spec)
    if (not os.path.basename(os.path.dirname(target)).startswith('.frqueue-') or
            os.path.lexists(target)):
        raise ValueError('Record WAV must be created only once inside a private queue stage.')
    data, originals = _record_pcm(spec, ffmpeg, interrupted, set_process)
    if interrupted and interrupted():
        raise RuntimeError('Record PCM cancelled before stage write.')
    with wave.open(target, 'wb') as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(data)
    return {'version': 1, 'file': FILENAME, 'sample_rate': RATE,
            'channels': CHANNELS, 'sample_format': 's16le',
            'sample_frames': len(data) // BYTES_PER_SAMPLE_FRAME,
            'pcm_sha256': _sha(data), 'wav_sha256': _source_sha(target),
            'selected_sources': originals}


def verify_record_audio(spec, receipt=None, ffmpeg=None, complete=True):
    """At stage, promotion and crash recovery: verify actual WAV AND source PCM.

    The receipt alone is NOT proof: it is compared with independently decoded
    selected source tracks and with every actual output PCM byte. This catches
    a replaced WAV even if the attacker also edits the JSON receipt.
    """
    record = spec.to_context()['composite_export']
    validate_record_audio_plan(spec, record)
    path = audio_sidecar_path(spec)
    if os.path.islink(path) or not os.path.isfile(path):
        raise RuntimeError('Record audio WAV is missing or symlinked.')
    with wave.open(path, 'rb') as wav:
        if (wav.getnchannels() != CHANNELS or wav.getframerate() != RATE or
                wav.getsampwidth() != 2 or wav.getcomptype() != 'NONE'):
            raise RuntimeError('Record audio WAV must be stereo 48k PCM s16le.')
        actual_count = wav.getnframes()
        raw = wav.readframes(actual_count)
    expected_count = (Fraction(spec.end + 1) * RATE / Fraction(spec.fps)).__floor__() - \
                     (Fraction(spec.start) * RATE / Fraction(spec.fps)).__floor__()
    if actual_count != expected_count or len(raw) != actual_count * 4:
        raise RuntimeError('Record audio sample count differs from exact rational record duration.')
    observed = {'version': 1, 'file': FILENAME, 'sample_rate': RATE,
                'channels': CHANNELS, 'sample_format': 's16le',
                'sample_frames': actual_count, 'pcm_sha256': _sha(raw),
                'wav_sha256': _source_sha(path)}
    if receipt is not None and any(receipt.get(key) != value for key, value in observed.items()):
        raise RuntimeError('Record audio receipt was tampered or does not match actual WAV.')
    if complete:
        ffmpeg = ffmpeg or shutil.which('ffmpeg')
        if not ffmpeg:
            raise RuntimeError('Record PCM cannot verify actual source-selected samples without FFmpeg.')
        expected, selected = _record_pcm(spec, ffmpeg)
        if raw != expected or (receipt is not None and receipt.get('selected_sources') != selected):
            raise RuntimeError('Record WAV selected-source samples or frozen audio receipt do not match.')
        observed['selected_sources'] = selected
    return observed
