# -*- coding: utf-8 -*-
"""ffmpeg / ffprobe discovery, a blocking runner, and media probing.

Cross-platform: binaries are located on PATH plus the usual install dirs for
macOS, Linux and Windows. Nothing is bundled.
"""
from __future__ import absolute_import

import os
import shutil
import subprocess
import sys
import re

from ..core import utils

# Common install locations per platform, searched after PATH.
_EXTRA_DIRS = (
    '/usr/bin', '/usr/local/bin', '/opt/homebrew/bin', '/opt/local/bin',
    r'C:\ffmpeg\bin', r'C:\Program Files\ffmpeg\bin',
)


# DCCs prepend incompatible OpenEXR/GCC/Qt libraries to these paths.
# FFmpeg/FFprobe are separate programs and should load their own libraries.
_MEDIA_ENV_KEYS = ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'LD_AUDIT',
                   'DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH',
                   'DYLD_INSERT_LIBRARIES', 'QT_PLUGIN_PATH',
                   'QT_QPA_PLATFORM_PLUGIN_PATH', 'PYTHONHOME', 'PYTHONPATH')


def media_environment():
    """A copy of the environment suitable for standalone media utilities."""
    env = os.environ.copy()
    for key in _MEDIA_ENV_KEYS:
        env.pop(key, None)
    return env


def configure_process(proc):
    """Apply the same isolation to Qt QProcess as to Python subprocess."""
    from ..core.qtcompat import QtCore
    env = QtCore.QProcessEnvironment.systemEnvironment()
    for key in _MEDIA_ENV_KEYS:
        env.remove(key)
    proc.setProcessEnvironment(env)
    return proc


def _no_window_kwargs():
    """Keep ffmpeg from flashing a console window on Windows."""
    if sys.platform == 'win32':
        return {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}
    return {}


def find_binary(name):
    """Prefer a working binary over broken studio PATH entries."""
    exe = name + ('.exe' if sys.platform == 'win32' else '')
    # Preserve old per-machine settings after the FrameReviewer rename, while
    # letting the new package-specific override take precedence.
    override = (os.environ.get('FRAMEREVIEWER_' + name.upper(), '').strip() or
                os.environ.get('VIDEOCROPPER_' + name.upper(), '').strip())
    candidates = []
    if override:
        candidates.append(os.path.join(override, exe) if os.path.isdir(override) else override)
    else:
        found = shutil.which(name) or shutil.which(exe)
        if found:
            candidates.append(found)
        for directory in _EXTRA_DIRS:
            candidates.append(os.path.join(directory, exe))
    tried = []
    for candidate in dict.fromkeys(candidates):
        if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
            continue
        try:
            kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          env=media_environment())
            kwargs.update(_no_window_kwargs())
            proc = subprocess.Popen([candidate, '-version'], **kwargs)
            try:
                _, err = proc.communicate(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise RuntimeError('startup timed out')
            if proc.returncode == 0:
                return candidate
            tried.append('{}: {}'.format(candidate, err.decode('utf-8', 'replace')[-500:]))
        except (OSError, RuntimeError) as exc:
            tried.append('{}: {}'.format(candidate, exc))
    hint = 'Set FRAMEREVIEWER_{} to a compatible executable.'.format(name.upper())
    raise RuntimeError('No working {} found. {}\n{}'.format(name, hint, '\n'.join(tried)))


def run(args):
    """Run to completion, returning decoded stdout; raise on failure."""
    kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=media_environment())
    kwargs.update(_no_window_kwargs())
    proc = subprocess.Popen(args, **kwargs)
    out, err = proc.communicate()
    if proc.returncode:
        tail = err.decode('utf-8', 'replace')[-2000:]
        raise RuntimeError('{} failed ({}):\n{}\nCheck runtime libraries or configure FRAMEREVIEWER_FFMPEG / FRAMEREVIEWER_FFPROBE.'.format(os.path.basename(args[0]), proc.returncode, tail))
    return out.decode('utf-8', 'replace')


class MediaInfo(object):
    """Immutable description of imported media."""

    def __init__(self, path, width, height, fps, frame_count, sequence_files=None,
                 audio=None, frame_numbers=None, fps_rational=None,
                 metadata=None, missing_policy='skip', sequence_source_frames=None,
                 declared_frames=None):
        self.path = path
        self.width = int(width)
        self.height = int(height)
        self.fps = max(0.001, float(fps))
        self.frame_count = max(1, int(frame_count))
        self.sequence_files = list(sequence_files or [])
        self.audio = dict(audio or {})
        self.metadata = dict(metadata or {})
        self.pixel_aspect_ratio = float(self.metadata.get('pixel_aspect_ratio', 1.0))
        self.bit_depth = self.metadata.get('bit_depth')
        self.pixel_format = self.metadata.get('pixel_format')
        self.codec = self.metadata.get('codec')
        self.alpha_mode = self.metadata.get('alpha_mode', 'unknown')
        self.timecode = self.metadata.get('timecode')
        self.color_tags = dict(self.metadata.get('color_tags') or {})
        # Real source frame numbers in display order. For a sequence these are
        # the numbers on disk (non-zero, negative, and holey ranges survive);
        # for a movie they default to a contiguous 0-based range. Kept distinct
        # from the dense display index the playback cache addresses.
        self.fps_rational = tuple(fps_rational) if fps_rational else None
        if frame_numbers is not None:
            self.frame_numbers = [int(f) for f in frame_numbers]
        elif self.sequence_files:
            self.frame_numbers = list(range(len(self.sequence_files)))
        else:
            self.frame_numbers = list(range(self.frame_count))
        # FrameMapping describes the *playback* timeline. Source filenames are
        # separate: a hole has no path, but owns its real signed source number.
        # Skip retains preexisting dense behavior until an artist opts into a
        # missing-frame policy for a specific logical Source.
        from .sequence import MISSING_POLICIES
        if missing_policy not in ('skip',) + MISSING_POLICIES:
            raise ValueError('Unknown missing-frame policy: {}'.format(missing_policy))
        self.missing_policy = missing_policy
        self.sequence_source_frames = (list(sequence_source_frames)
                                       if sequence_source_frames is not None else
                                       (list(self.frame_numbers[:len(self.sequence_files)])
                                        if self.sequence_files else []))
        if self.sequence_files and len(self.sequence_source_frames) != len(self.sequence_files):
            if sequence_source_frames is not None or missing_policy != 'skip':
                raise ValueError('Source frame numbers must match existing files.')
            # Legacy callers may supply synthetic frame mappings that differ
            # from the preview file list (e.g. EDL test sources). Keep their
            # original mapping; only the opt-in gap policy requires 1:1 paths.
            self.sequence_source_frames = list(range(len(self.sequence_files)))
        self.declared_frames = (list(declared_frames) if declared_frames is not None
                                else None)
        if missing_policy != 'skip' and self.sequence_files:
            if len(self.frame_numbers) != self.frame_count:
                raise ValueError('Expanded frame count must match its source timeline.')

    @property
    def sequence_holes(self):
        """Bounded preview of missing source frames (at most 10,000 IDs)."""
        if not self.is_sequence:
            return ()
        if self.missing_policy == 'skip':
            real = self.sequence_source_frames
            if not real:
                return ()
            present = set(real)
            from itertools import islice
            return tuple(islice((n for n in range(real[0], real[-1] + 1)
                                 if n not in present), 10000))
        real = set(self.sequence_source_frames)
        from itertools import islice
        return tuple(islice((n for n in self.frame_numbers if n not in real), 10000))

    @property
    def missing_frame_count(self):
        """Exact missing count without enumerating a potentially huge span."""
        if not self.is_sequence or not self.sequence_source_frames:
            return 0
        present = set(self.sequence_source_frames)
        if self.missing_policy == 'skip':
            return max(0, self.sequence_source_frames[-1] -
                       self.sequence_source_frames[0] + 1 - len(present))
        return sum(1 for frame in self.frame_numbers if frame not in present)

    def with_missing_policy(self, policy):
        """Opt into a source-number timeline without changing original files.

        Deliberately never edits a session or the source probe in place. One
        accepted policy can be restored from Source.review after session open.
        """
        from .sequence import MISSING_POLICIES
        if policy not in ('skip',) + MISSING_POLICIES:
            raise ValueError('Unknown missing-frame policy: {}'.format(policy))
        if not self.is_sequence or policy == self.missing_policy:
            return self
        real = list(self.sequence_source_frames)
        if policy == 'skip':
            frames = real
        else:
            if self.declared_frames is None and real[-1] - real[0] + 1 > 1000000:
                raise ValueError('Sequence span exceeds 1,000,000 frames.')
            frames = (list(self.declared_frames) if self.declared_frames is not None
                      else list(range(real[0], real[-1] + 1)))
            if len(frames) > 1000000:
                raise ValueError('Sequence span exceeds 1,000,000 frames.')
            if not set(real).issubset(frames):
                raise ValueError('Declared sequence frames omit an existing file.')
        return type(self)(
            self.path, self.width, self.height, self.fps, len(frames),
            sequence_files=self.sequence_files, audio=self.audio,
            frame_numbers=frames, fps_rational=self.fps_rational,
            metadata=self.metadata, missing_policy=policy,
            sequence_source_frames=real, declared_frames=self.declared_frames)

    def with_authored_timeline(self, declared_frames):
        """Restrict a fresh disk probe to an authored range saved in a session.

        The directory may contain other frames from the same numbered shot.
        Never adopt them into a range that the artist explicitly selected.
        Do include newly arrived frames *inside* the declaration.
        """
        if not self.is_sequence:
            return self
        declared = [int(frame) for frame in declared_frames]
        if (not declared or len(declared) > 1000000 or
                declared != sorted(set(declared))):
            raise ValueError('Invalid authored sequence timeline.')
        present = set(declared)
        actual = [(frame, path) for frame, path in zip(
            self.sequence_source_frames, self.sequence_files) if frame in present]
        if not actual:
            raise ValueError('No existing image lies in the authored sequence timeline.')
        frames, paths = zip(*actual)
        return type(self)(self.path, self.width, self.height, self.fps,
                          len(frames), sequence_files=list(paths),
                          audio=self.audio, frame_numbers=list(frames),
                          fps_rational=self.fps_rational, metadata=self.metadata,
                          sequence_source_frames=list(frames),
                          declared_frames=declared)

    @property
    def is_sequence(self):
        return bool(self.sequence_files)

    @property
    def start_frame(self):
        """First real source frame number (may be negative or non-zero)."""
        return self.frame_numbers[0] if self.frame_numbers else 0

    def frame_mapping(self):
        """Display-index / source-frame / rational-FPS / timecode mapping."""
        from .sequence import FrameMapping
        return FrameMapping(self.frame_numbers or [0], fps=self.fps,
                            fps_rational=self.fps_rational)

    @property
    def duration(self):
        return self.frame_count / self.fps

    @property
    def has_audio(self):
        return bool(self.audio.get('channels'))

    @property
    def audio_description(self):
        if not self.has_audio:
            return 'No audio'
        channels = int(self.audio.get('channels', 0))
        layout = self.audio.get('channel_layout') or ''
        if not layout:
            layout = {1: 'mono', 2: 'stereo'}.get(channels,
                                                   '{} channels'.format(channels))
        rate = self.audio.get('sample_rate')
        return '{} · {} Hz'.format(layout, rate) if rate else layout


def probe(ffprobe, qimage_reader, path, sequence_pattern=None,
          exr_layer=None, exr_part=None):
    """Return MediaInfo for a video or image sequence.

    qimage_reader is QtGui.QImageReader, passed in so this module stays Qt-free.
    """
    if sequence_pattern is None:
        from .sequence import has_frame_cookie
        if has_frame_cookie(os.path.basename(path)):
            sequence_pattern = path
    if not os.path.isfile(path):
        if sequence_pattern is None:
            raise RuntimeError('Missing input: {}'.format(path))
        from .sequence import expand_pattern
        sequence = expand_pattern(sequence_pattern)
        # A declared range can include leading holes but must still contain a
        # real image for size/decode. Do not open an absent first frame.
        files = sequence.ordered_paths()
        if not files:
            raise RuntimeError('No decodable sequence images: {}'.format(path))
        path = files[0]

    if utils.is_image(path):
        if sequence_pattern is not None:
            from .sequence import expand_pattern, split_sequence_name
            seq = expand_pattern(sequence_pattern)
            authored_range, _ = split_sequence_name(os.path.basename(sequence_pattern))
            # An explicit authored time range is authoritative. Do not pull in
            # neighboring sequence files outside it merely because they share
            # the basename/cookie.
            included = (set(seq.declared_frames) if authored_range else None)
            frame_numbers = [frame for frame in seq.frames
                             if included is None or frame in included]
            files = [seq.path_for_frame(frame) for frame in frame_numbers]
            declared = list(seq.declared_frames) if authored_range else None
            if not files:
                raise RuntimeError('Declared sequence range contains no images.')
            path = files[0]
        else:
            seq = utils.sequence_info(path)
            files = seq.ordered_paths() or [path]
            frame_numbers = seq.frames if seq.frames else None
            declared = (seq.declared_frames if seq.declared_frames != seq.frames
                        else None)
        # Keep ordinary image/PNG probes Qt-free on host Python. The EXR
        # decoder imports Qt for pixmap conversion and is optional here.
        if os.path.splitext(path)[1].lower() == '.exr':
            from . import exr
            header = exr.read_header(path, layer=exr_layer, part=exr_part)
            if header is not None:
                native_offset_verified = False
                if not exr.available():
                    window = header['data_window']
                    if (window != (0, 0, header['width'], header['height']) or
                            window != header['display_window']):
                        # This signed-window path has NO FFmpeg inferred pixel
                        # placement. A bounded native CPU scanline reader must
                        # actually decode every original source sample, including
                        # absolute Y chunks, before granting canvas ownership.
                        # The header alone is insufficient evidence, and later
                        # sequence frames must pass SequenceSource's independently
                        # frozen displayWindow/PAR contract on each decode.
                        native = exr.read_frame_payload(path)
                        native_offset_verified = bool(
                            native is not None and
                            native.color_metadata.get(
                                'exr_native_windows_verified') is True and
                            tuple(native.geometry.data_window) ==
                            tuple(header['data_window']) and
                            tuple(native.geometry.display_window) ==
                            tuple(header['display_window']) and
                            (native.geometry.width, native.geometry.height) ==
                            (header['width'], header['height']) and
                            abs(float(native.geometry.pixel_aspect) -
                                float(header['pixel_aspect_ratio'])) <= 1e-6)
                        if not native_offset_verified:
                            raise RuntimeError('EXR signed/offset native windows '
                                               'require OpenEXR Python decoding '
                                               'or independently verified bounded '
                                               'CPU scanline placement; neither '
                                               'proved this file: {}'.format(path))
                metadata = {
                    'file_size': os.path.getsize(path),
                    'codec': 'exr', 'pixel_format': None,
                    'pixel_aspect_ratio': header['pixel_aspect_ratio'],
                    'data_window': header['data_window'],
                    'display_window': header['display_window'],
                    'exr_layer': header['layer'],
                    'exr_part': header['part'],
                    'exr_part_count': header['part_count'],
                    'alpha_mode': header['alpha_mode'],
                    'geometry_accuracy': header['geometry_accuracy'],
                    'exr_header_verified': bool(exr.available() or
                                                native_offset_verified),
                    'exr_native_first_frame_verified': native_offset_verified,
                }
                return MediaInfo(path, header['width'], header['height'],
                                 24.0, len(files), sequence_files=files,
                                 frame_numbers=frame_numbers, metadata=metadata,
                                 declared_frames=declared)
            if exr.available():
                raise RuntimeError('Unreadable, unsupported or ambiguous EXR '
                                   '(select an explicit part/layer for decoding): '
                                   '{}'.format(path))
            # Without OpenEXR, an invalid/ambiguous EXR header is never
            # evidence of a safely decodable first layer via FFmpeg.
            raise RuntimeError('Unreadable, unsupported or ambiguous EXR '
                               'header: {}'.format(path))
        reader = qimage_reader(path)
        size = reader.size()
        if size.isValid():
            metadata = {'file_size': os.path.getsize(path),
                        'pixel_aspect_ratio': 1.0,
                        'codec': os.path.splitext(path)[1].lstrip('.').lower(),
                        'pixel_format': None}
            return MediaInfo(path, size.width(), size.height(), 24.0,
                             len(files), sequence_files=files,
                             frame_numbers=frame_numbers, metadata=metadata,
                             declared_frames=declared)
        # Qt cannot size some formats (notably EXR). Keep treating it as an
        # image sequence, but read one frame's dimensions from ffprobe.
        try:
            dims = run([ffprobe, '-v', 'error', '-select_streams', 'v:0',
                        '-show_entries', 'stream=width,height',
                        '-of', 'default=noprint_wrappers=1', path])
            meta = dict(line.split('=', 1) for line in dims.splitlines()
                        if '=' in line)
            return MediaInfo(path, int(meta['width']), int(meta['height']),
                             24.0, len(files), sequence_files=files,
                             frame_numbers=frame_numbers,
                             declared_frames=declared,
                             metadata={'file_size': os.path.getsize(path),
                                       'codec': os.path.splitext(path)[1][1:].lower()})
        except (RuntimeError, KeyError, ValueError):
            pass

    out = run([ffprobe, '-v', 'error', '-select_streams', 'v:0',
               '-show_entries', ('stream=width,height,avg_frame_rate,nb_frames,'
                                'duration,codec_name,pix_fmt,sample_aspect_ratio,'
                                'display_aspect_ratio,bits_per_raw_sample,'
                                'bits_per_sample,color_space,color_range,color_primaries,'
                                'color_transfer,field_order:stream_tags=timecode'),
               '-show_entries', 'format=duration,size,format_name',
               '-of', 'default=noprint_wrappers=1', path])
    meta = {}
    for line in out.splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            meta[key] = value
    par = 1.0
    par_text = meta.get('sample_aspect_ratio', '1:1')
    if ':' in par_text:
        try:
            par_numerator, par_denominator = map(float, par_text.split(':', 1))
            if par_denominator > 0 and par_numerator > 0:
                par = par_numerator / par_denominator
        except (TypeError, ValueError):
            pass
    pixel_format = meta.get('pix_fmt')
    bit_depth = None
    for key in ('bits_per_raw_sample', 'bits_per_sample'):
        try:
            value = int(meta.get(key, ''))
            if value > 0:
                bit_depth = value
                break
        except (ValueError, TypeError):
            pass
    if bit_depth is None and pixel_format:
        match = re.search(r'(?:p|f)(10|12|14|16|32)(?:le|be|$)', pixel_format)
        if match:
            bit_depth = int(match.group(1))
    tags = {key: meta[key] for key in ('color_space', 'color_range',
                                      'color_primaries', 'color_transfer')
            if key in meta}
    metadata = {
        'file_size': int(meta.get('size') or os.path.getsize(path)),
        'codec': meta.get('codec_name'),
        'container': meta.get('format_name'),
        'pixel_format': pixel_format,
        'pixel_aspect_ratio': par,
        'display_aspect_ratio': meta.get('display_aspect_ratio'),
        'bit_depth': bit_depth,
        'alpha_mode': ('unknown' if not pixel_format else 'straight'
                       if any(part in pixel_format for part in ('rgba', 'argb', 'bgra', 'yuva'))
                       else 'opaque'),
        'timecode': meta.get('TAG:timecode'),
        'color_tags': tags,
    }
    fps = utils.fps_from_text(meta.get('avg_frame_rate'))
    from .sequence import parse_rational_fps
    fps_rational = parse_rational_fps(meta.get('avg_frame_rate'), default=None) \
        if meta.get('avg_frame_rate') else None
    try:
        count = int(meta['nb_frames'])
    except (ValueError, TypeError, KeyError):
        count = max(1, int(round(float(meta.get('duration', 0) or 0) * fps)))
    audio = {}
    try:
        audio_out = run([
            ffprobe, '-v', 'error', '-select_streams', 'a:0',
            '-show_entries',
            'stream=codec_name,channels,channel_layout,sample_rate,bit_rate',
            '-of', 'default=noprint_wrappers=1', path])
        audio = dict(line.split('=', 1) for line in audio_out.splitlines()
                     if '=' in line)
        if audio.get('channels'):
            audio['channels'] = int(audio['channels'])
    except (RuntimeError, TypeError, ValueError):
        audio = {}
    return MediaInfo(path, int(meta['width']), int(meta['height']), fps, count,
                     audio=audio, fps_rational=fps_rational, metadata=metadata)
