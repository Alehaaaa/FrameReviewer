# -*- coding: utf-8 -*-
"""Strict, independently reproducible proof for opt-in v4 external PCM audio.

An external route is a canonical, zero-origin WAV with no hidden BWF/metadata
offset or edit list. The file and every decoded sample are frozen by SHA256;
FFprobe independently checks the audio stream and every decoded frame PTS.
"""
from __future__ import absolute_import

import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
import wave


MAX_WAV_BYTES = 6 * 1024 * 1024
RATE = 48000


def _safe_wav_path(path):
    if (not isinstance(path, str) or not os.path.isabs(path) or
            not path.lower().endswith('.wav') or
            os.path.normpath(path) != path or
            any(ord(c) < 32 or ord(c) == 127 for c in path) or
            '..' in path.split(os.sep)):
        raise ValueError('External record audio needs a canonical absolute local .wav path.')
    # Guard all path components, including parents, against symlink redirection.
    probe = path
    while probe != os.path.dirname(probe):
        if os.path.islink(probe):
            # macOS has two fixed, system-managed aliases for temporary
            # folders. Accept these exact roots, mirroring queue_store's
            # output-parent guard; every other symlink component is unsafe.
            system_alias = ((probe == '/var' and
                             os.path.realpath(probe) == '/private/var') or
                            (probe == '/tmp' and
                             os.path.realpath(probe) == '/private/tmp'))
            if not system_alias:
                raise ValueError('External PCM path contains a symlink.')
        probe = os.path.dirname(probe)
    info = os.stat(path)
    if (not stat.S_ISREG(info.st_mode) or
            not 44 <= info.st_size <= MAX_WAV_BYTES):
        raise ValueError('External WAV must be a bounded regular local file.')
    return info


def _canonical_pcm(path):
    """Accept only 44-byte PCM RIFF, so no implicit BWF origin or metadata.

    Prohibiting extra RIFF chunks also excludes channel masks, time-reference
    chunks, RF64 and ambiguous padded/trailing payloads.
    """
    with open(path, 'rb') as stream:
        header = stream.read(44)
        if len(header) != 44 or header[:4] != b'RIFF' or header[8:16] != b'WAVEfmt ':
            raise ValueError('External audio must be canonical RIFF/WAVE PCM.')
        riff_size, fmt_len = struct.unpack_from('<II', header, 4)[0], struct.unpack_from('<I', header, 16)[0]
        details = struct.unpack_from('<HHIIHH', header, 20)
        if (fmt_len != 16 or details != (1, 2, RATE, RATE * 4, 4, 16) or
                header[36:40] != b'data'):
            raise ValueError('External WAV must have a single 48k stereo s16le PCM data chunk.')
        data_bytes = struct.unpack_from('<I', header, 40)[0]
        if (not data_bytes or data_bytes % 4 or riff_size != data_bytes + 36 or
                os.fstat(stream.fileno()).st_size != data_bytes + 44):
            raise ValueError('External WAV has trailing chunks, padding or ambiguous sample length.')
        digest = hashlib.sha256()
        count = 0
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            count += len(chunk)
        if count != data_bytes:
            raise ValueError('External WAV is truncated.')
    with wave.open(path, 'rb') as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(),
                wav.getcomptype(), wav.getnframes()) != (
                    2, 2, RATE, 'NONE', data_bytes // 4):
            raise ValueError('External WAV PCM header disagrees with independent parser.')
    return data_bytes // 4, digest.hexdigest()


def freeze_external_record_audio_proof(path, ffprobe=None):
    """Return a frozen canonical WAV identity for an explicit EDL occurrence.

    FFprobe frame PTS and sample counts must be contiguous without resampling.
    The frozen proof is rebuilt at render and post-export/recovery validation.
    """
    before = _safe_wav_path(path)
    count, pcm_digest = _canonical_pcm(path)
    ffprobe = ffprobe or shutil.which('ffprobe')
    if not ffprobe:
        raise RuntimeError('External PCM requires ffprobe to prove sample timestamps.')

    def probe(params, max_bytes):
        result = subprocess.run(
            [ffprobe, '-v', 'error'] + params + [path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35,
            check=False)
        if result.returncode or len(result.stdout) > max_bytes:
            raise ValueError('External audio ffprobe failed or exceeded bounded metadata.')
        return result.stdout

    data = json.loads(probe([
        '-show_entries',
        'stream=index,codec_type,codec_name,sample_fmt,sample_rate,channels,channel_layout,time_base,start_pts,start_time,duration_ts',
        '-of', 'json'], 16384).decode('utf-8'))
    streams = data.get('streams', [])
    if len(streams) != 1:
        raise ValueError('External WAV must have exactly one audio stream.')
    audio = streams[0]
    if (audio.get('index') != 0 or audio.get('codec_type') != 'audio' or
            audio.get('codec_name') != 'pcm_s16le' or
            audio.get('sample_fmt') != 's16' or
            audio.get('sample_rate') != str(RATE) or
            audio.get('channels') != 2 or
            audio.get('time_base') != '1/48000' or
            audio.get('start_pts') not in (None, 0) or
            audio.get('start_time') not in (None, '0.000000', '0') or
            audio.get('duration_ts') != count):
        raise ValueError('External audio stream has an unsupported codec, offset or duration.')
    frames = json.loads(probe([
        '-select_streams', 'a:0', '-show_frames',
        '-show_entries', 'frame=pts,best_effort_timestamp,nb_samples',
        '-of', 'json'], 192 * 1024).decode('utf-8')).get('frames', [])
    sample_position = 0
    for frame in frames:
        pts = frame.get('pts', frame.get('best_effort_timestamp'))
        n = frame.get('nb_samples')
        if type(pts) is not int or type(n) is not int or \
                pts != sample_position or n < 1:
            raise ValueError('External WAV has ambiguous or discontinuous decoded sample timestamps.')
        sample_position += n
    if not frames or sample_position != count:
        raise ValueError('External WAV decoded samples disagree with PCM header.')

    # Immutable full-file identity: protects against header/metadata manipulation.
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    after = _safe_wav_path(path)
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ValueError('External WAV changed during source proof capture.')
    return {'version': 1, 'source_sha256': digest.hexdigest(),
            'source_size_bytes': before.st_size,
            'pcm_sha256': pcm_digest, 'sample_frames': count,
            'audio_ordinal': 0, 'stream_index': 0,
            'codec': 'pcm_s16le', 'sample_rate': RATE, 'channels': 2,
            'time_base': '1/48000', 'start_pts': 0}
