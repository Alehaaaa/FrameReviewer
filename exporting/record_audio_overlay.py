# -*- coding: utf-8 -*-
"""Frozen external PCM overlays for v4 record WAV.

The primary edit soundtrack is assembled by ``record_audio._record_pcm``.
This module validates the legacy ONE-overlay contract or an ordered set of
up to THREE external 48 kHz stereo PCM tracks, preserving original cuts. Overlays are
addressed in the SAME absolute 24000/1001 record clock, never source labels.

Gain is EXACT integer rational: for signed source s16 sample ``s``,
``gain(s) = sign(s) * floor(abs(s) * gain_num / gain_den)``. Add that value to
the underlying s16 sample and SATURATE to [-32768,32767]. There are no fades,
retimes, guessed codecs, floating quantization, automatic downmix or output
normalization. A zero gain is an explicitly silent yet still verified overlay.
"""
from __future__ import absolute_import

import array
from fractions import Fraction
import hashlib
import re
import sys

from .external_audio_proof import (
    MAX_WAV_BYTES, _canonical_pcm, _safe_wav_path,
    freeze_external_record_audio_proof)

RATE = 48000
SAMPLE_BYTES = 4  # one interleaved stereo signed PCM16 sample frame
FPS = Fraction(24000, 1001)
OVERLAY_KEYS = frozenset((
    'occurrence_id', 'kind', 'external_path', 'external_proof',
    'audio_ordinal', 'stream_index',
    'record_start_frame', 'source_in_frame', 'duration_frames',
    'gain_num', 'gain_den'))
PROOF_KEYS = frozenset((
    'version', 'source_sha256', 'source_size_bytes', 'pcm_sha256',
    'sample_frames', 'audio_ordinal', 'stream_index', 'codec',
    'sample_rate', 'channels', 'time_base', 'start_pts'))


def _samples(frame):
    """Floor of absolute record time in 48kHz stereo sample frames."""
    return (Fraction(int(frame) * RATE, 1) / FPS).__floor__()


def validate_record_audio_overlay(spec, record):
    """Validate a frozen optional singleton overlay; None if not requested.

    Probe/hash/decode are intentionally performed later on the background
    render thread (and repeated during receipt verification), not here on UI.
    """
    audio = record.get('record_audio')
    if not isinstance(audio, dict) or 'overlay' not in audio:
        return None
    if 'overlays' in audio:
        raise ValueError('Legacy overlay and ordered overlays cannot coexist.')
    overlay = audio['overlay']
    return _validate_overlay_entry(spec, record, overlay, set())


def validate_record_audio_overlays(spec, record):
    """Validate 1..3 authored-order, separately identified canonical WAV beds."""
    audio = record.get('record_audio')
    if not isinstance(audio, dict) or 'overlays' not in audio:
        return ()
    if 'overlay' in audio:
        raise ValueError('Legacy overlay and ordered overlays cannot coexist.')
    entries = audio['overlays']
    if not isinstance(entries, list) or not 1 <= len(entries) <= 3:
        raise ValueError('Ordered record overlays must be a LIST of 1..3 frozen entries.')
    seen = set()
    for entry in entries:
        _validate_overlay_entry(spec, record, entry, seen)
        seen.add(entry['occurrence_id'])
    return tuple(entries)


def _validate_overlay_entry(spec, record, overlay, used_ids):
    """Exact legacy 11-key overlay schema and independent occurrence identity."""
    if not isinstance(overlay, dict) or set(overlay) != OVERLAY_KEYS:
        raise ValueError('Record audio overlay requires exactly 11 frozen fields.')
    occurrence = overlay['occurrence_id']
    members = record.get('members')
    if (not isinstance(occurrence, str) or not occurrence or
            len(occurrence) > 128 or occurrence.strip() != occurrence or
            any(ord(c) < 32 or ord(c) == 127 for c in occurrence) or
            not isinstance(members, (tuple, list)) or
            occurrence in used_ids or
            occurrence in {item.get('occurrence_id') for item in members
                           if isinstance(item, dict)}):
        raise ValueError('Overlay needs its own unique frozen occurrence_id.')
    if overlay['kind'] != 'external':
        raise ValueError('Record audio overlay only supports independently proven external PCM.')
    if (type(overlay['audio_ordinal']) is not int or
            overlay['audio_ordinal'] != 0 or
            type(overlay['stream_index']) is not int or
            overlay['stream_index'] != 0):
        raise ValueError('Overlay requires explicitly selected zero-origin WAV audio stream a:0.')
    start, source_in, duration = (
        overlay['record_start_frame'], overlay['source_in_frame'],
        overlay['duration_frames'])
    if (any(type(value) is not int for value in (start, source_in, duration)) or
            start < 0 or source_in < 0 or duration < 1 or
            start + duration > 120 or
            Fraction(spec.fps) != FPS or
            not (start < spec.end + 1 and start + duration > spec.start)):
        raise ValueError('Overlay must occupy a bounded, intersecting signed record-frame interval without retime.')
    # Edit plan can include trimmed/exported subsections. Never place audio on
    # an imaginary record beyond the end of ALL immutable edit occurrences.
    try:
        record_end = max(m['session_offset'] + m['record_length'] for m in members)
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError('Overlay requires complete frozen edit positions: {}'.format(exc))
    if start + duration > record_end:
        raise ValueError('Overlay extends past the frozen full record timeline.')
    numerator, denominator = overlay['gain_num'], overlay['gain_den']
    if (type(numerator) is not int or type(denominator) is not int or
            denominator != 2 or numerator not in (0, 1, 2)):
        raise ValueError('Overlay gain must be exact 0/2, 1/2 or 2/2; no float or boost.')
    proof = overlay['external_proof']
    if (not isinstance(proof, dict) or set(proof) != PROOF_KEYS or
            type(proof['version']) is not int or proof['version'] != 1 or
            type(proof['source_size_bytes']) is not int or
            not 44 <= proof['source_size_bytes'] <= MAX_WAV_BYTES or
            type(proof['sample_frames']) is not int or
            proof['sample_frames'] < 1 or
            any(type(proof[key]) is not int for key in (
                'audio_ordinal', 'stream_index', 'sample_rate', 'channels',
                'start_pts')) or
            any(not isinstance(proof[key], str) or not re.fullmatch(
                r'[0-9a-f]{64}', proof[key])
                for key in ('source_sha256', 'pcm_sha256')) or
            (proof['audio_ordinal'], proof['stream_index'], proof['codec'],
             proof['sample_rate'], proof['channels'], proof['time_base'],
             proof['start_pts']) !=
            (0, 0, 'pcm_s16le', RATE, 2, '1/48000', 0) or
            _samples(source_in + duration) > proof['sample_frames']):
        raise ValueError('Overlay external PCM proof, source trim or stereo 48k zero origin is unsupported.')
    _safe_wav_path(overlay['external_path'])
    return overlay


def _s16_samples(raw):
    if len(raw) % 2:
        raise ValueError('Stereo PCM must contain whole signed 16-bit samples.')
    samples = array.array('h')
    samples.frombytes(raw)
    if samples.itemsize != 2:
        raise RuntimeError('Platform signed short is not 16 bits.')
    if sys.byteorder != 'little':
        samples.byteswap()
    return samples


def _s16_bytes(samples):
    if sys.byteorder != 'little':
        samples.byteswap()
    return samples.tobytes()


def saturated_pcm_overlay(primary, source, gain_num, gain_den=2):
    """Mix byte-aligned stereo PCM sections with exact signed saturating gain."""
    if (type(gain_num) is not int or gain_num not in (0, 1, 2) or
            type(gain_den) is not int or gain_den != 2 or
            len(primary) != len(source) or len(primary) % SAMPLE_BYTES):
        raise ValueError('Overlay mix requires equal-length stereo PCM16 and canonical gain.')
    underneath = _s16_samples(primary)
    additional = _s16_samples(source)
    for i, sample in enumerate(additional):
        # Defined and symmetric round-toward-zero, including -32768/2.
        contribution = ((abs(sample) * gain_num) // gain_den) * (
            -1 if sample < 0 else 1)
        total = underneath[i] + contribution
        underneath[i] = max(-32768, min(32767, total))
    return _s16_bytes(underneath)


def apply_record_audio_overlay(spec, record, primary, ffmpeg,
                               interrupted=None, set_process=None):
    """Return (mixed_pcm, independent_overlay_source_receipt).

    Called once by the existing `_record_pcm` stage with already-assembled
    primary bytes. Rechecks the canonical WAV proof via ffprobe/RIFF/full SHA,
    FFmpeg-decodes only the named a:0 stream, and independently compares each
    decoded sample to the WAV bytes BEFORE and AFTER the mix. Interrupted
    builds never publish a partial destination or another sidecar.
    """
    overlay = validate_record_audio_overlay(spec, record)
    if overlay is None:
        return primary, None
    return _apply_overlay_entry(spec, overlay, primary, ffmpeg,
                                interrupted, set_process)


def apply_record_audio_overlays(spec, record, primary, ffmpeg,
                                interrupted=None, set_process=None):
    """Mix frozen tracks IN LIST ORDER; saturate after every signed addition.

    Every independent external WAV/PCM/FFprobe PTS proof is rechecked before
    and after that entry's exact source cut. The original edit receipt keys
    remain unchanged, and each overlay keeps its own mixed-output SHA as a
    durable receipt of the ordered arithmetic state immediately after it.
    """
    entries = validate_record_audio_overlays(spec, record)
    result = primary
    receipts = {}
    for overlay in entries:
        if interrupted and interrupted():
            raise RuntimeError('Record overlays cancelled before next source.')
        result, receipt = _apply_overlay_entry(
            spec, overlay, result, ffmpeg, interrupted, set_process)
        receipts[overlay['occurrence_id']] = receipt
    return result, receipts


def _apply_overlay_entry(spec, overlay, primary, ffmpeg,
                         interrupted=None, set_process=None):
    """Shared legacy/list member render: proves source before and after mix."""
    if interrupted and interrupted():
        raise RuntimeError('Record overlay cancelled before source proof.')
    expected = (_samples(spec.end + 1) - _samples(spec.start)) * SAMPLE_BYTES
    if len(primary) != expected:
        raise ValueError('Overlay base PCM differs from record clock or sample duration.')
    path, proof = overlay['external_path'], overlay['external_proof']
    if freeze_external_record_audio_proof(path) != proof:
        raise ValueError('Overlay WAV ffprobe/PTS/SHA256 differs from its frozen source proof.')
    from .record_audio import _pcm, _source_sha, _sha
    if _source_sha(path) != proof['source_sha256']:
        raise ValueError('Overlay WAV source changed after independently frozen proof.')
    source = _pcm(ffmpeg, path, 0, interrupted, set_process)
    sample_frames, canonical_hash = _canonical_pcm(path)
    if (sample_frames != proof['sample_frames'] or
            canonical_hash != proof['pcm_sha256'] or
            _sha(source) != canonical_hash or
            len(source) != sample_frames * SAMPLE_BYTES):
        raise ValueError('Overlay FFmpeg selected PCM samples disagree with canonical WAV.')
    if interrupted and interrupted():
        raise RuntimeError('Record overlay cancelled before signed PCM mix.')

    first = max(spec.start, overlay['record_start_frame'])
    last = min(spec.end + 1, overlay['record_start_frame'] + overlay['duration_frames'])
    source_start = overlay['source_in_frame'] + first - overlay['record_start_frame']
    source_end = source_start + last - first
    lo, hi = _samples(first), _samples(last)
    source_lo, source_hi = _samples(source_start), _samples(source_end)
    if (source_hi - source_lo != hi - lo or
            source_hi > sample_frames or source_lo < 0):
        raise ValueError('Overlay cannot implicitly stretch or shift audio samples.')
    primary_start, primary_stop = (
        (lo - _samples(spec.start)) * SAMPLE_BYTES,
        (hi - _samples(spec.start)) * SAMPLE_BYTES)
    segment = source[source_lo * SAMPLE_BYTES:source_hi * SAMPLE_BYTES]
    if (len(segment) != primary_stop - primary_start or
            not 0 <= primary_start < primary_stop <= len(primary)):
        raise RuntimeError('Overlay source/record intersections exceed frozen PCM bounds.')
    mixed = (primary[:primary_start] + saturated_pcm_overlay(
        primary[primary_start:primary_stop], segment,
        overlay['gain_num'], overlay['gain_den']) + primary[primary_stop:])
    if interrupted and interrupted():
        raise RuntimeError('Record overlay cancelled before final source recheck.')
    after_proof = freeze_external_record_audio_proof(path)
    after_count, after_pcm_sha = _canonical_pcm(path)
    if (after_proof != proof or
            after_count != proof['sample_frames'] or
            after_pcm_sha != proof['pcm_sha256'] or
            _source_sha(path) != proof['source_sha256']):
        raise ValueError('Overlay WAV changed during signed PCM mix.')
    return mixed, {
        'kind': 'overlay_external',
        'source_sha256': proof['source_sha256'],
        'audio_ordinal': 0, 'stream_index': 0,
        'decoded_pcm_sha256': _sha(source),
        'external_path': path, 'external_proof': proof,
        'record_start_frame': overlay['record_start_frame'],
        'source_in_frame': overlay['source_in_frame'],
        'duration_frames': overlay['duration_frames'],
        'gain_num': overlay['gain_num'], 'gain_den': overlay['gain_den'],
        'overlay_segment_pcm_sha256': hashlib.sha256(segment).hexdigest(),
        'mixed_pcm_sha256': hashlib.sha256(mixed).hexdigest(),
    }


__all__ = ['validate_record_audio_overlay', 'validate_record_audio_overlays',
           'apply_record_audio_overlay', 'apply_record_audio_overlays',
           'saturated_pcm_overlay']
