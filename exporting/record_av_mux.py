# -*- coding: utf-8 -*-
"""Bounded, lossless RGB H.264 + original PCM MOV inside a v4 atomic folder.

The Display PNG16 and selected-source WAV remain primary reference assets.
MOV is a declared SDR 8-bit black-matted *derivative*, never an HDR export.
Movie video must decode byte-for-byte to the independently decoded reference
pixels; audio must decode byte-for-byte to the proven selected-source PCM.
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
import wave


MOV = 'record_av.mov'
MAX_FRAME_PIXELS = 1280 * 720
MAX_TOTAL_PIXELS = 1280 * 720 * 60
MAX_SOURCE_BYTES = 1024 * 1024 * 1024
MAX_SOURCE_FILES = 120
CONFIG = {'version': 1, 'file': MOV, 'video_codec': 'h264rgb_lossless',
          'audio_codec': 'pcm_s16le', 'matte': 'black',
          'color': 'display_sdr_8bit'}


def record_av_path(spec):
    return os.path.join(os.path.dirname(spec.path), MOV)


def freeze_record_av_sources(spec):
    """Freeze SHA256 of every original movie/EXR input, even unused frames.

    Called from the GUI before enqueue; the exact result is required as
    record_av['source_files']. Later changes fail closed at stage and recovery.
    """
    record = spec.to_context().get('composite_export') or {}
    if record.get('version') != 4 or not isinstance(record.get('members'), (list,tuple)):
        raise ValueError('Record A/V source proof needs a frozen v4 composition.')
    entries = {}
    for member in record['members']:
        paths = ([member['source_path']] if member.get('input_kind') == 'movie'
                 else member['seq_files'] if member.get('input_kind') == 'image_sequence'
                 else ())
        if not paths:
            raise ValueError('Record A/V source proof has unknown/empty media.')
        for path in paths:
            if (not isinstance(path,str) or not os.path.isabs(path) or
                    os.path.normpath(path) != path or os.path.islink(path)):
                raise ValueError('Record A/V source proof requires canonical regular input files.')
            info = os.stat(path)
            if not 0 < info.st_size <= 512 * 1024 * 1024:
                raise ValueError('Record A/V input exceeds bounded source file size.')
            entries[path] = info.st_size
    if len(entries)>MAX_SOURCE_FILES or sum(entries.values())>MAX_SOURCE_BYTES:
        raise ValueError('Record A/V source proof exceeds bounded input SHA budget.')
    proofs=[]
    for path,size in sorted(entries.items()):
        frozen = _hash(path)
        if os.stat(path).st_size != size:
            raise ValueError('Record A/V source changed during SHA capture.')
        proofs.append({'path':path,'size':size,'sha256':frozen})
    for member in record['members']:
        if member.get('input_kind') == 'movie':
            proof=member.get('movie_proof') or {}
            row=next(item for item in proofs if item['path']==member['source_path'])
            if (row['sha256']!=proof.get('source_sha256') or
                    row['size']!=proof.get('source_size_bytes')):
                raise ValueError('Record A/V movie source SHA disagrees with frozen CFR proof.')
    return proofs


def validate_record_av(spec, record):
    """Opt-in contract: entire v4 PNG/WAV folder remains atomic and auditable."""
    choice = record.get('record_av')
    if (not isinstance(choice,dict) or
            set(choice) != set(CONFIG) | {'source_files'} or
            any(choice.get(key)!=value for key,value in CONFIG.items()) or
            record.get('version') != 4 or
            record.get('audio_policy') != 'wav_pcm_v1' or
            spec.stage != 'display' or not spec.sequence or
            os.path.splitext(spec.path)[1].lower() != '.png' or
            Fraction(spec.fps) != Fraction(24000, 1001)):
        raise ValueError('Record MOV requires explicit v4 Display SDR PNG/WAV and lossless RGB H264 + PCM configuration.')
    width, height = spec.output_size
    if (width * height > MAX_FRAME_PIXELS or
            width * height * spec.count > MAX_TOTAL_PIXELS):
        raise ValueError('Record MOV exceeds bounded verified RGB decode budget.')
    if (spec.count < 1 or spec.count > 120 or not
            re.search(r'\#{4}', os.path.basename(spec.path))):
        raise ValueError('Record MOV needs 1..120 canonical numbered Display frames.')
    if choice['source_files'] != freeze_record_av_sources(spec):
        raise ValueError('Record MOV original source SHA256/size differs from frozen A/V proof.')
    return choice


def _hash(path):
    if not os.path.isfile(path) or os.path.islink(path):
        raise ValueError('Record A/V input must be a regular file, never a symlink.')
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _frames(spec):
    filename = os.path.basename(spec.path)
    if filename.count('####') != 1 or '#' in filename.replace('####', ''):
        raise ValueError('Record A/V reference PNG pattern is ambiguous.')
    folder = os.path.dirname(spec.path)
    return [os.path.join(folder, filename.replace('####', '{:04d}'.format(1001 + i)))
            for i in range(spec.count)]


def _run(args, timeout=60, limit=2*1024*1024, input_data=None,
         interrupted=None, set_process=None):
    from ..media.ffmpeg import _no_window_kwargs, media_environment
    if interrupted and interrupted():
        raise RuntimeError('Record MOV FFmpeg cancelled before starting.')
    kwargs = {'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE,
              'stdin': subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
              'env': media_environment()}
    kwargs.update(_no_window_kwargs())
    proc = subprocess.Popen(args, **kwargs)
    if set_process is not None:
        set_process(proc)
    end = time.monotonic() + timeout
    try:
        while True:
            if interrupted and interrupted():
                raise RuntimeError('Record MOV FFmpeg cancelled.')
            if time.monotonic() >= end:
                raise RuntimeError('Record MOV FFmpeg exceeded bounded runtime.')
            try:
                output, error = proc.communicate(input=input_data, timeout=.35)
                break
            except subprocess.TimeoutExpired:
                continue
        if proc.returncode or len(output) > limit:
            raise RuntimeError('Record A/V FFmpeg verification or mux failed: {}'.format(
                error.decode('utf-8', 'replace')[-350:]))
        if interrupted and interrupted():
            raise RuntimeError('Record MOV FFmpeg cancelled after completion.')
        return output
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if set_process is not None:
            set_process(None)


def _reference_rgb8(ffmpeg, frame, width, height,
                    interrupted=None, set_process=None):
    """Independent decoded reference: straight PNG RGBA16 black-matted to RGB8.

    The source is *the exact published PNG16*. A 16->8 normalization and alpha
    matte are always explicit; HDR scene-linear values are never passed here.
    """
    import numpy as np
    if os.path.islink(frame):
        raise ValueError('Record A/V cannot decode a symlinked reference PNG.')
    raw = _run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
                '-i', frame, '-map', '0:v:0', '-frames:v', '1',
                '-f', 'rawvideo', '-pix_fmt', 'rgba64be', 'pipe:1'],
               limit=width*height*8+1,
               interrupted=interrupted, set_process=set_process)
    if len(raw) != width*height*8:
        raise RuntimeError('Record A/V reference RGBA16 source is not exact expected size.')
    rgba = np.frombuffer(raw, dtype='>u2').reshape((height, width, 4))
    rgb = rgba[:, :, :3].astype(np.uint64)
    alpha = rgba[:, :, 3:4].astype(np.uint64)
    # Numerically defined 16-bit Display straight-alpha over opaque black,
    # then 8-bit quantization with nearest-half-up rounding.
    factor = 65535 * 65535
    output = ((rgb * alpha * 255 + factor//2) // factor).astype('uint8')
    return output.tobytes()


def _source_pixel_truth(spec, ffmpeg):
    """Rebuild each PNG16 from the actual source, not from editable receipts.

    This closes the forged movie + forged reference PNG + forged JSON receipt
    route during commit and crash recovery. Uses the existing production float
    compositor and verified movie decode-index reader; never cached UI pixels.
    Bounded to the same frozen 720p/total-pixel budget as MOV verification.
    """
    import numpy as np
    from .controller import ExportJob
    from .spec import managed_record_timeline_plan
    from ..viewer.color import ColorPipelineState
    members = spec.to_context()['composite_export']['members']
    state = ColorPipelineState.from_dict(spec.to_context()['color'])
    width, height = spec.output_size
    verifier = ExportJob(ffmpeg, spec.source_path, (), width, height,
                         spec.to_context())
    verifier._composite_movie_last = {}
    frames = _frames(spec)
    decoded_alpha = {}
    for row, frame in zip(managed_record_timeline_plan(spec), frames):
        if row['kind'] == 'record_gap':
            pixels = np.zeros((height, width, 4), np.float32)
        else:
            lineage = row['members'][0]
            member = members[lineage['member_index']]
            path = lineage['path']
            if path is not None and path not in decoded_alpha:
                decoded_alpha[path] = (False if member['input_kind'] == 'movie'
                                       else verifier._managed_probe_frame(
                                           path, size=(member['width'],member['height'])))
            pixels = verifier._managed_composite_pixels(
                {'members':[lineage]}, [member], {}, 'a_b', state,
                decoded_alpha, np)
        if pixels.shape != (height,width,4) or not np.all(np.isfinite(pixels)):
            raise RuntimeError('Record MOV source compositor produced invalid HDR/alpha pixels.')
        expected = (np.clip(pixels,0,1)*65535+0.5).astype('>u2').tobytes()
        observed = _run([ffmpeg,'-hide_banner','-loglevel','error','-nostdin',
                         '-i',frame,'-map','0:v:0','-frames:v','1',
                         '-f','rawvideo','-pix_fmt','rgba64be','pipe:1'],
                        timeout=45,limit=width*height*8+1)
        if expected != observed:
            raise RuntimeError('Record MOV Display PNG16 does not reproduce frozen native source/color pixels.')


def _selected_pcm(spec):
    from .record_audio import RATE, BYTES_PER_SAMPLE_FRAME, audio_sidecar_path
    path = audio_sidecar_path(spec)
    _hash(path)
    with wave.open(path, 'rb') as reader:
        if (reader.getnchannels(), reader.getframerate(),
                reader.getsampwidth(), reader.getcomptype()) != (2, RATE, 2, 'NONE'):
            raise RuntimeError('Record A/V source WAV is not 48k stereo PCM s16.')
        samples = reader.readframes(reader.getnframes())
        count = reader.getnframes()
    expected = (Fraction(spec.end+1)*RATE/Fraction(spec.fps)).__floor__() - (
        Fraction(spec.start)*RATE/Fraction(spec.fps)).__floor__()
    if count != expected or len(samples) != expected * BYTES_PER_SAMPLE_FRAME:
        raise RuntimeError('Record A/V WAV does not cover exact rational record sample clock.')
    return samples


def _metadata(spec, ffprobe):
    path = record_av_path(spec)
    info = json.loads(_run([ffprobe, '-v', 'error', '-count_frames',
        '-show_entries', 'stream=index,codec_type,codec_name,width,height,pix_fmt,avg_frame_rate,r_frame_rate,time_base,start_pts,duration_ts,nb_read_frames,sample_rate,channels,channel_layout,color_range,sample_aspect_ratio',
        '-of', 'json', path], timeout=30, limit=65536).decode('utf-8'))
    streams = info.get('streams', [])
    if len(streams) != 2:
        raise RuntimeError('Record MOV needs exactly one H264 video and one PCM audio stream.')
    video, audio = streams
    if (video.get('index') != 0 or video.get('codec_type') != 'video' or
            video.get('codec_name') != 'h264' or video.get('pix_fmt') != 'gbrp' or
            (video.get('width'), video.get('height')) != spec.output_size or
            video.get('avg_frame_rate') != '24000/1001' or
            video.get('r_frame_rate') != '24000/1001' or
            video.get('time_base') != '1/24000' or
            video.get('start_pts') != 0 or
            video.get('duration_ts') != spec.count * 1001 or
            video.get('nb_read_frames') != str(spec.count) or
            video.get('color_range') not in ('pc', None) or
            video.get('sample_aspect_ratio') not in ('1:1', None) or
            audio.get('index') != 1 or audio.get('codec_type') != 'audio' or
            audio.get('codec_name') != 'pcm_s16le' or
            audio.get('sample_rate') != '48000' or audio.get('channels') != 2 or
            audio.get('time_base') != '1/48000' or
            audio.get('start_pts') != 0):
        raise RuntimeError('Record MOV stream identity, RGB range, clock or codec failed proof.')
    expected_audio = len(_selected_pcm(spec)) // 4
    if audio.get('duration_ts') != expected_audio:
        raise RuntimeError('Record MOV selected PCM duration is truncated or extended.')
    pts = _run([ffprobe, '-v', 'error', '-select_streams', 'v:0',
        '-show_frames', '-show_entries', 'frame=best_effort_timestamp',
        '-of', 'csv=p=0', path], timeout=30, limit=65536)
    times = [int(line.strip().split(b',')[0]) for line in pts.splitlines() if line.strip()]
    if times != [i * 1001 for i in range(spec.count)]:
        raise RuntimeError('Record MOV presentation timestamps differ from exact CFR record clock.')
    # Audio packets must be contiguous at the original 48k sample clock.
    stamps = json.loads(_run([ffprobe, '-v', 'error', '-select_streams', 'a:0',
        '-show_frames', '-show_entries', 'frame=best_effort_timestamp,nb_samples',
        '-of', 'json', path], timeout=30, limit=262144).decode('utf-8'))
    running = 0
    for frame in stamps.get('frames', []):
        if (frame.get('best_effort_timestamp') != running or
                type(frame.get('nb_samples')) is not int or
                frame['nb_samples'] < 1):
            raise RuntimeError('Record MOV contains noncontiguous or offset PCM timestamps.')
        running += frame['nb_samples']
    if running != expected_audio:
        raise RuntimeError('Record MOV audio PTS samples do not match frozen record duration.')
    return {'video_stream':0, 'audio_stream':1,
            'video_frames':spec.count, 'video_time_base':'1/24000',
            'audio_samples':expected_audio, 'audio_time_base':'1/48000'}


def render_record_av(spec, ffmpeg, interrupted=None, set_process=None):
    """Stage-only encode and atomic-folder MOV mux after v4 PNG/WAV render."""
    record = spec.to_context()['composite_export']
    validate_record_av(spec, record)
    folder = os.path.dirname(spec.path)
    target = record_av_path(spec)
    if (not os.path.basename(folder).startswith('.frqueue-') or
            os.path.islink(folder) or os.path.lexists(target)):
        raise ValueError('Record MOV can only be created in a clean private durable stage.')
    from .record_audio import verify_record_audio, audio_sidecar_path
    # Independent audio proof both before and after video transcode.
    verify_record_audio(spec, ffmpeg=ffmpeg, complete=True)
    if interrupted and interrupted():
        raise RuntimeError('Record MOV cancelled.')
    width, height = spec.output_size
    fd, video_path = tempfile.mkstemp(prefix='.frav-video-',suffix='.mov',dir=folder)
    os.close(fd)
    os.unlink(video_path)
    from ..media.ffmpeg import _no_window_kwargs, media_environment
    args = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-video_size',
            '{}x{}'.format(width, height), '-framerate', spec.fps,
            '-i', 'pipe:0', '-frames:v', str(spec.count), '-an',
            '-c:v', 'libx264rgb', '-qp', '0', '-preset', 'veryfast',
            '-pix_fmt', 'rgb24', '-video_track_timescale', '24000',video_path]
    process = None
    try:
        kw = {'stdin': subprocess.PIPE, 'stdout': subprocess.DEVNULL,
              'stderr': subprocess.PIPE, 'env': media_environment()}
        kw.update(_no_window_kwargs())
        process = subprocess.Popen(args, **kw)
        if set_process is not None:
            set_process(process)
        try:
            for frame in _frames(spec):
                if interrupted and interrupted():
                    raise RuntimeError('Record MOV cancelled during RGB preparation.')
                process.stdin.write(_reference_rgb8(
                    ffmpeg, frame, width, height,
                    interrupted=interrupted, set_process=None))
            process.stdin.close()
            err = process.stderr.read()
            status = process.wait(timeout=120)
            if status or (interrupted and interrupted()):
                raise RuntimeError('Record MOV video encoding cancelled/failed: {}'.format(
                    err.decode('utf-8','replace')[-350:]))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            if set_process is not None:
                set_process(None)
            process.stderr.close()
        process = None
        # Audio must NOT be cut by -frames:v; perform a second, stream-copy
        # mux operation over the exact existing WAV, never AAC re-encoding.
        if interrupted and interrupted():
            raise RuntimeError('Record MOV cancelled before PCM mux.')
        _run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
              '-i', video_path, '-i', audio_sidecar_path(spec),
              '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'copy',
              '-video_track_timescale', '24000', '-movflags', '+faststart',
              target],timeout=90, interrupted=interrupted,
             set_process=set_process)
        if interrupted and interrupted():
            raise RuntimeError('Record MOV cancelled after PCM mux.')
        os.unlink(video_path)
        return verify_record_av(spec, ffmpeg=ffmpeg)
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait()
            if set_process is not None:
                set_process(None)
        if os.path.isfile(video_path):
            os.unlink(video_path)


def verify_record_av(spec, receipt=None, ffmpeg=None, ffprobe=None):
    """Independently recompute MOV video pixels and all PCM samples from assets."""
    record = spec.to_context()['composite_export']
    validate_record_av(spec, record)
    ffmpeg = ffmpeg or shutil.which('ffmpeg')
    ffprobe = ffprobe or shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        raise RuntimeError('Record MOV independent verification requires FFmpeg and ffprobe.')
    path = record_av_path(spec)
    if not os.path.isfile(path) or os.path.islink(path):
        raise RuntimeError('Record MOV output is absent or symlinked.')
    names = {os.path.basename(p) for p in _frames(spec)}
    names.update([MOV,'record_audio.wav',os.path.basename(spec.path)+'.frmanifest.json'])
    # A v4 record can deliberately deliver *both* independently verified
    # video derivatives in the same atomic queue directory. Accept the NUT
    # filename only when that exact second output was frozen in the spec;
    # queue_runner separately verifies its signed 16-bit pixels, audio and
    # receipt before publication/recovery. All unselected sidecars still fail.
    if record.get('record_rgba16') is not None:
        from .record_rgba16_mux import (
            record_rgba16_path, validate_record_rgba16)
        # Mere presence of an arbitrary JSON flag must not authorize an
        # otherwise unreceipted file. Check the frozen NUT configuration and
        # original-source hashes before widening the strict filename allowlist.
        validate_record_rgba16(spec, record)
        names.add(os.path.basename(record_rgba16_path(spec)))
    # Queue private stage has no manifest yet; final folder does.
    if set(os.listdir(os.path.dirname(path))) - names:
        raise RuntimeError('Record MOV folder contains unreceipted media or extra files.')
    from .record_audio import verify_record_audio
    verified_audio = verify_record_audio(spec, ffmpeg=ffmpeg, complete=True)
    metadata = _metadata(spec, ffprobe)
    size = spec.output_size[0] * spec.output_size[1] * 3
    # ffmpeg stdout is capped to the frozen record size (<= ~166 MiB RGB).
    decoded = _run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-i', path, '-map', '0:v:0', '-vsync', '0',
        '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
        timeout=120, limit=spec.count * size + 1)
    if len(decoded) != spec.count * size:
        raise RuntimeError('Record MOV decoded RGB frame count/shape differs from reference.')
    frame_hashes = []
    for i, frame in enumerate(_frames(spec)):
        reference = _reference_rgb8(ffmpeg, frame, *spec.output_size)
        if decoded[size*i:size*(i+1)] != reference:
            raise RuntimeError('Record MOV pixel comparison failed against black-matted Display PNG16 frame {}.'.format(i))
        frame_hashes.append(_hash(frame))
    # The PNG references themselves are re-derived from the immutable input
    # video/EXR + color snapshots. Merely forging them and the receipt cannot
    # change the real source truth during a crash recovery.
    _source_pixel_truth(spec, ffmpeg)
    selected = _run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-i', path, '-map', '0:a:0', '-vn', '-sn', '-dn',
        '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1'],
        timeout=45, limit=spec.count * 2002 * 4 + 1024)
    original = _selected_pcm(spec)
    if selected != original:
        raise RuntimeError('Record MOV decoded PCM differs from reconstructed selected-source WAV.')
    observed = {'version': 1, 'file': MOV, 'video_codec':'h264rgb_lossless',
                'audio_codec':'pcm_s16le','matte':'black','color':'display_sdr_8bit',
                'mov_sha256':_hash(path),'frame_sha256':frame_hashes,
                'pcm_sha256':hashlib.sha256(selected).hexdigest(),
                'source_audio_receipt':verified_audio,
                'streams':metadata,'pixel_truth':'exact_8bit_black_matte_png16'}
    if receipt is not None and receipt != observed:
        raise RuntimeError('Record MOV frozen receipt disagrees with independently checked movie, frames or PCM.')
    return observed


__all__ = ['CONFIG','freeze_record_av_sources','validate_record_av',
           'render_record_av','verify_record_av',
           'record_av_path']
