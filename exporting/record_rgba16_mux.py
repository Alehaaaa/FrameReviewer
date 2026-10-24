# -*- coding: utf-8 -*-
"""Opt-in v4 Display PNG16/WAV -> verified straight-RGBA16 FFV1 + PCM NUT.

This is a complementary queue-stage helper. It does not publish any public
output on its own. Full 16-bit integer channels, including alpha, must decode
bit-for-bit equal to independently read Display PNG16 references, which are
also checked against the frozen original media and color pipeline.

NUT retains exact 1/48000 video+audio stream time bases: every video frame
starts at an exact 2002-sample boundary at 24000/1001. Matroska on the
supported FFmpeg rounds both streams to 1 ms, losing this property. NUT is a
professional/interchange archival derivative, NOT a universal-player MOV/MP4.
"""
from __future__ import absolute_import

from fractions import Fraction
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from .record_av_mux import freeze_record_av_sources, _source_pixel_truth


FILENAME = 'record_rgba16.nut'
MAX_FRAME_PIXELS = 1280 * 720
MAX_TOTAL_RGBA_BYTES = 64 * 1024 * 1024
MAX_SOURCE_FRAMES = 120
CONFIG = {
    'version': 1,
    'file': FILENAME,
    'video_codec': 'ffv1_gbrap16le_lossless',
    'audio_codec': 'pcm_s16le',
    'pixel_format': 'rgba64be',
    'alpha': 'straight',
    'color': 'display_sdr_16bit',
}


def record_rgba16_path(spec):
    return os.path.join(os.path.dirname(spec.path), FILENAME)


def freeze_record_rgba16_sources(spec):
    """Original-source SHA256/size snapshot, identical to existing v4 MOV proof."""
    return freeze_record_av_sources(spec)


def validate_record_rgba16(spec, record):
    """Fail closed on unselected, HDR, oversized or non-frozen requests."""
    choice = record.get('record_rgba16')
    if (not isinstance(choice, dict) or
            set(choice) != set(CONFIG) | {'source_files'} or
            any(choice.get(k) != value for k, value in CONFIG.items()) or
            record.get('version') != 4 or
            record.get('audio_policy') != 'wav_pcm_v1' or
            record.get('record_audio') is None or
            spec.stage != 'display' or not spec.sequence or
            os.path.splitext(spec.path)[1].lower() != '.png' or
            Fraction(spec.fps) != Fraction(24000, 1001)):
        raise ValueError('RGBA16 NUT requires frozen v4 Display SDR PNG16 and selected 48k PCM audio.')
    w, h = spec.output_size
    if (w < 1 or h < 1 or w * h > MAX_FRAME_PIXELS or
            not 1 <= spec.count <= MAX_SOURCE_FRAMES or
            w * h * spec.count * 8 > MAX_TOTAL_RGBA_BYTES or
            spec.start < 0 or spec.end >= 120):
        raise ValueError('RGBA16 NUT requested frames/pixels exceed bounded verification budget.')
    filename = os.path.basename(spec.path)
    if (filename.count('####') != 1 or '#' in filename.replace('####', '') or
            not os.path.isabs(spec.path) or
            os.path.normpath(spec.path) != spec.path or
            filename == FILENAME):
        raise ValueError('RGBA16 NUT requires canonical absolute four-digit PNG pattern.')
    frozen = freeze_record_rgba16_sources(spec)
    if choice['source_files'] != frozen:
        raise ValueError('RGBA16 NUT original-source SHA256/size changed since frozen proof.')
    from .record_audio import validate_record_audio_plan
    validate_record_audio_plan(spec, record)
    return choice


def _frames(spec):
    basename = os.path.basename(spec.path)
    return [os.path.join(os.path.dirname(spec.path),
            basename.replace('####', '{:04d}'.format(1001 + i)))
            for i in range(spec.count)]


def _hash(path):
    if not os.path.isfile(path) or os.path.islink(path):
        raise RuntimeError('RGBA16 proof input/output is missing or symlinked.')
    sha = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            sha.update(block)
    return sha.hexdigest()


def _run(args, timeout=50, byte_cap=262144, interrupted=None,
         set_process=None):
    """Cancellable bounded subprocess; never shell-execute frozen filenames."""
    from ..media.ffmpeg import _no_window_kwargs, media_environment
    if interrupted and interrupted():
        raise RuntimeError('RGBA16 FFmpeg cancelled.')
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                  stderr=subprocess.PIPE, env=media_environment())
    kwargs.update(_no_window_kwargs())
    proc = subprocess.Popen(args, **kwargs)
    if set_process:
        set_process(proc)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if interrupted and interrupted():
                raise RuntimeError('RGBA16 FFmpeg cancelled.')
            if time.monotonic() >= deadline:
                raise RuntimeError('RGBA16 FFmpeg exceeded bounded timeout.')
            try:
                out, err = proc.communicate(timeout=.3)
                break
            except subprocess.TimeoutExpired:
                continue
        if proc.returncode or len(out) > byte_cap:
            raise RuntimeError('RGBA16 FFmpeg failed or exceeded output budget: {}'.format(
                err.decode('utf-8', 'replace')[-400:]))
        return out
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if set_process:
            set_process(None)


def _reference_rgba16(spec, ffmpeg, frame, interrupted=None):
    w, h = spec.output_size
    if os.path.islink(frame):
        raise RuntimeError('RGBA16 reference PNG is a symlink.')
    data = _run([ffmpeg, '-hide_banner', '-loglevel', 'error',
                 '-nostdin', '-i', frame, '-map', '0:v:0', '-frames:v', '1',
                 '-pix_fmt', 'rgba64be', '-f', 'rawvideo', 'pipe:1'],
                timeout=40, byte_cap=w*h*8+1, interrupted=interrupted)
    if len(data) != w*h*8:
        raise RuntimeError('RGBA16 reference PNG has wrong dimensions or bit depth.')
    return data


def _audio_truth(spec, ffmpeg):
    from .record_audio import verify_record_audio
    from .record_av_mux import _selected_pcm
    receipt = verify_record_audio(spec, ffmpeg=ffmpeg, complete=True)
    pcm = _selected_pcm(spec)
    if hashlib.sha256(pcm).hexdigest() != receipt['pcm_sha256']:
        raise RuntimeError('Selected PCM does not match original source reconstruction.')
    return receipt, pcm


def _probe_container(spec, ffprobe, pcm_samples):
    target = record_rgba16_path(spec)
    info = json.loads(_run([ffprobe, '-v', 'error', '-count_frames',
        '-show_entries',
        'stream=index,codec_type,codec_name,width,height,pix_fmt,avg_frame_rate,r_frame_rate,time_base,start_pts,nb_read_frames,sample_rate,channels,channel_layout,sample_aspect_ratio',
        '-of', 'json', target], timeout=30).decode('utf-8'))
    streams = info.get('streams', [])
    if len(streams) != 2:
        raise RuntimeError('RGBA16 NUT must contain exactly FFV1 video and PCM audio.')
    v, a = streams
    if (v.get('index') != 0 or v.get('codec_type') != 'video' or
            v.get('codec_name') != 'ffv1' or v.get('pix_fmt') != 'gbrap16le' or
            (v.get('width'), v.get('height')) != spec.output_size or
            v.get('avg_frame_rate') != '24000/1001' or
            v.get('r_frame_rate') != '24000/1001' or
            v.get('time_base') != '1/48000' or
            v.get('start_pts') != 0 or
            v.get('sample_aspect_ratio') not in ('1:1', None) or
            a.get('index') != 1 or a.get('codec_type') != 'audio' or
            a.get('codec_name') != 'pcm_s16le' or
            a.get('channels') != 2 or a.get('sample_rate') != '48000' or
            a.get('time_base') != '1/48000' or a.get('start_pts') != 0):
        raise RuntimeError('RGBA16 NUT codec, pixel format or exact stream clocks mismatch.')

    frames = json.loads(_run([ffprobe, '-v', 'error',
        '-select_streams', 'v:0', '-show_frames', '-show_entries',
        'frame=best_effort_timestamp,pix_fmt,width,height', '-of', 'json',
        target], timeout=45, byte_cap=131072).decode('utf-8'))['frames']
    if len(frames) != spec.count:
        raise RuntimeError('RGBA16 NUT video frame count mismatch.')
    observed = [f.get('best_effort_timestamp') for f in frames]
    expected = [i * 2002 for i in range(spec.count)]
    if (observed != expected or any(
            (f.get('pix_fmt') != 'gbrap16le' or
             (f.get('width'), f.get('height')) != spec.output_size)
            for f in frames)):
        raise RuntimeError('RGBA16 NUT PTS are not exact 24000/1001 CFR frames.')

    audio = json.loads(_run([ffprobe, '-v', 'error',
        '-select_streams', 'a:0', '-show_frames', '-show_entries',
        'frame=best_effort_timestamp,nb_samples', '-of', 'json', target],
        timeout=45, byte_cap=262144).decode('utf-8'))['frames']
    tally = 0
    for f in audio:
        if (f.get('best_effort_timestamp') != tally
                or type(f.get('nb_samples')) is not int or
                f['nb_samples'] < 1):
            raise RuntimeError('RGBA16 NUT audio PTS has a delay or gap.')
        tally += f['nb_samples']
    if not audio or tally != pcm_samples:
        raise RuntimeError('RGBA16 NUT audio PCM frames truncated, padded or missing.')
    return {'video_stream': 0, 'audio_stream': 1,
            'video_frames': spec.count, 'video_fps': '24000/1001',
            'container_time_base': '1/48000',
            'pts_quantization': 'none',
            'audio_samples': pcm_samples,
            'audio_sample_rate': 48000}


def render_record_rgba16(spec, ffmpeg, interrupted=None, set_process=None):
    """Create a new NUT *only* in the existing private .frqueue- stage folder."""
    record = spec.to_context()['composite_export']
    validate_record_rgba16(spec, record)
    folder = os.path.dirname(spec.path)
    target = record_rgba16_path(spec)
    from .queue_runner import _unsafe_output_parent
    if (not os.path.basename(folder).startswith('.frqueue-') or
            not os.path.isdir(folder) or os.path.islink(folder) or
            _unsafe_output_parent(spec.path) or
            os.path.lexists(target)):
        raise ValueError('RGBA16 NUT may render only into a clean canonical private queue stage.')
    from .record_audio import audio_sidecar_path
    _audio_truth(spec, ffmpeg)
    video_fd, video_path = tempfile.mkstemp(
        prefix='.frrgba16-video-', suffix='.nut', dir=folder)
    os.close(video_fd)
    os.unlink(video_path)
    w, h = spec.output_size
    from ..media.ffmpeg import _no_window_kwargs, media_environment
    args = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-f', 'rawvideo', '-pixel_format', 'rgba64be',
            '-video_size', '{}x{}'.format(w,h), '-framerate', spec.fps,
            '-i', 'pipe:0', '-frames:v', str(spec.count), '-an',
            '-c:v', 'ffv1', '-level', '3', '-slicecrc', '1',
            '-pix_fmt', 'gbrap16le', '-fps_mode', 'passthrough', video_path]
    encode = None
    try:
        kw = {'stdin':subprocess.PIPE, 'stdout':subprocess.DEVNULL,
              'stderr':subprocess.PIPE, 'env':media_environment()}
        kw.update(_no_window_kwargs())
        encode = subprocess.Popen(args, **kw)
        if set_process:
            set_process(encode)
        try:
            for path in _frames(spec):
                if interrupted and interrupted():
                    raise RuntimeError('RGBA16 NUT encoder cancelled.')
                encode.stdin.write(_reference_rgba16(spec, ffmpeg, path, interrupted))
            encode.stdin.close()
            err = encode.stderr.read()
            status = encode.wait(timeout=120)
            if status or (interrupted and interrupted()):
                raise RuntimeError('RGBA16 FFV1 encoder cancelled or failed: {}'.format(
                    err.decode('utf-8', 'replace')[-400:]))
        finally:
            if encode.poll() is None:
                encode.kill()
                encode.wait()
            for pipe in (encode.stdin, encode.stderr):
                if pipe and not pipe.closed:
                    pipe.close()
            if set_process:
                set_process(None)
        encode = None
        if interrupted and interrupted():
                raise RuntimeError('RGBA16 NUT cancelled before PCM remux.')
        # Audio is *not* bounded by -frames:v. Remux the complete WAV, copy
        # both streams; otherwise audio may be truncated at the final video PTS.
        _run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
              '-i', video_path, '-i', audio_sidecar_path(spec),
              '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy',
              '-c:a', 'copy', target], timeout=90, byte_cap=1024,
             interrupted=interrupted, set_process=set_process)
        if interrupted and interrupted():
            raise RuntimeError('RGBA16 NUT cancelled after mux.')
        os.unlink(video_path)
        return verify_record_rgba16(spec, ffmpeg=ffmpeg)
    finally:
        if encode is not None:
            if encode.poll() is None:
                encode.kill()
                encode.wait()
            if set_process:
                set_process(None)
        if os.path.lexists(video_path):
            os.unlink(video_path)


def verify_record_rgba16(spec, receipt=None, ffmpeg=None, ffprobe=None):
    """Proof from actual NUT bytes, original sources, PNG16 and selected PCM."""
    record = spec.to_context()['composite_export']
    validate_record_rgba16(spec, record)
    ffmpeg = ffmpeg or shutil.which('ffmpeg')
    ffprobe = ffprobe or shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        raise RuntimeError('RGBA16 NUT requires independent FFmpeg and FFprobe.')
    target = record_rgba16_path(spec)
    if not os.path.isfile(target) or os.path.islink(target):
        raise RuntimeError('RGBA16 NUT is missing or symlinked.')
    allowed = {os.path.basename(p) for p in _frames(spec)}
    allowed.update([FILENAME, 'record_audio.wav',
                    os.path.basename(spec.path)+'.frmanifest.json'])
    if record.get('record_av') is not None:
        allowed.add('record_av.mov')
    if set(os.listdir(os.path.dirname(target))) - allowed:
        raise RuntimeError('RGBA16 stage contains unreceipted extra media.')
    audio_receipt, expected_pcm = _audio_truth(spec, ffmpeg)
    streams = _probe_container(spec, ffprobe, len(expected_pcm)//4)
    frame_bytes = spec.output_size[0] * spec.output_size[1] * 8
    raw = _run([ffmpeg, '-hide_banner', '-loglevel', 'error',
                '-nostdin', '-i', target, '-map', '0:v:0',
                '-fps_mode', 'passthrough', '-pix_fmt', 'rgba64be',
                '-f', 'rawvideo', 'pipe:1'], timeout=100,
               byte_cap=frame_bytes*spec.count+1)
    if len(raw) != frame_bytes*spec.count:
        raise RuntimeError('RGBA16 NUT output has missing or extra RGBA16 pixels.')
    hashes=[]
    alpha_nonopaque=False
    for i, png in enumerate(_frames(spec)):
        expected = _reference_rgba16(spec, ffmpeg, png)
        actual = raw[i*frame_bytes:(i+1)*frame_bytes]
        if expected != actual:
            raise RuntimeError('FFV1 16-bit RGB/alpha bytes differ from Display PNG16 frame {}.'.format(i))
        # Alpha MUST be preserved even for transparent/nonopaque pixels.
        if not alpha_nonopaque:
            import numpy as np
            alpha_nonopaque=bool(np.any(np.frombuffer(expected, dtype='>u2')[3::4] != 65535))
        hashes.append(_hash(png))
    _source_pixel_truth(spec, ffmpeg)
    decoded_pcm = _run([ffmpeg, '-hide_banner', '-loglevel', 'error',
        '-nostdin', '-i', target, '-map', '0:a:0', '-vn', '-sn', '-dn',
        '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1'],
        timeout=45, byte_cap=len(expected_pcm)+1)
    if decoded_pcm != expected_pcm:
        raise RuntimeError('RGBA16 NUT decoded PCM differs from full selected-source WAV samples.')
    observed = {'version':1, 'file':FILENAME,
        'video_codec':'ffv1_gbrap16le_lossless', 'audio_codec':'pcm_s16le',
        'pixel_format':'rgba64be', 'alpha':'straight',
        'color':'display_sdr_16bit', 'nut_sha256':_hash(target),
        'frame_sha256':hashes, 'pcm_sha256':hashlib.sha256(decoded_pcm).hexdigest(),
        'audio_receipt':audio_receipt, 'streams':streams,
        'alpha_nonopaque':alpha_nonopaque,
        'pixel_truth':'exact_straight_rgba16_display_png16'}
    if receipt is not None and receipt != observed:
        raise RuntimeError('RGBA16 NUT or frozen recovery receipt failed independent validation.')
    return observed


__all__ = ['CONFIG', 'freeze_record_rgba16_sources',
           'validate_record_rgba16', 'render_record_rgba16',
           'verify_record_rgba16', 'record_rgba16_path']
