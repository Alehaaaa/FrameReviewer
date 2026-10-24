# -*- coding: utf-8 -*-
"""Qt-independent audio routing metadata, probing, channel maps and timing.

stream_index is the 0-based AUDIO track ordinal used by Qt6, rather than the
container-wide stream ID reported by FFprobe. Only Player owns playback.
Matrices describe DSP routing; QMediaPlayer cannot apply a channel matrix.
"""
from __future__ import absolute_import

from fractions import Fraction
import json
import math


_CHANNELS = {'mono': 1, '1.0': 1, 'stereo': 2, '2.0': 2, '5.1': 6,
             '5.1(side)': 6, '7.1': 8, '7.1(wide)': 8}
_SURROUND_GAIN = math.sqrt(0.5)


def _fraction(value):
    if isinstance(value, Fraction):
        return value
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return Fraction(int(value[0]), int(value[1]))
    return Fraction(str(value))


class AudioRoute(object):
    """The chosen source, audio clock origin and output interpretation."""

    def __init__(self, external_path='', stream_index=0, delay_ms=0,
                 sample_rate=None, channels=None, channel_layout='',
                 output_layout='source'):
        self.external_path = str(external_path or '')
        self.stream_index = int(stream_index)
        self.delay_ms = int(delay_ms)
        self.sample_rate = int(sample_rate) if sample_rate is not None else None
        self.channels = int(channels) if channels is not None else None
        self.channel_layout = str(channel_layout or '')
        self.output_layout = str(output_layout or 'source').strip().lower()
        if self.stream_index < 0:
            raise ValueError('Audio stream index must be nonnegative.')
        if self.sample_rate is not None and self.sample_rate <= 0:
            raise ValueError('Sample rate must be positive.')
        if self.channels is not None and self.channels <= 0:
            raise ValueError('Channel count must be positive.')
        if self.output_layout not in ('source', 'mono', 'stereo'):
            raise ValueError('Unsupported audio output layout.')
        count = layout_channels(self.channel_layout)
        if count is not None and self.channels is not None and count != self.channels:
            raise ValueError('Channel count disagrees with channel layout.')

    def to_dict(self):
        return {'external_path': self.external_path,
                'stream_index': self.stream_index, 'delay_ms': self.delay_ms,
                'sample_rate': self.sample_rate, 'channels': self.channels,
                'channel_layout': self.channel_layout,
                'output_layout': self.output_layout}

    @classmethod
    def from_dict(cls, data):
        fields = ('external_path', 'stream_index', 'delay_ms', 'sample_rate',
                  'channels', 'channel_layout', 'output_layout')
        return cls(**{key: value for key, value in dict(data or {}).items()
                      if key in fields})

    def audio_ms_for_video_ms(self, video_ms):
        """Positive delay means sound starts later than picture."""
        return max(0, int(video_ms) - self.delay_ms)

    def audio_ms_for_video_frame(self, frame, fps):
        """Exact rational frame clock, floored once at the Qt ms boundary.

        This is a seek *request*, not a sample-accurate sound-device clock.
        A positive delay holds sound at zero; a negative delay seeks forward.
        """
        rate = _fraction(fps)
        if rate <= 0:
            raise ValueError('Frame rate must be positive.')
        when = _fraction(frame) * 1000 / rate
        if when < 0:
            raise ValueError('Video frame must be nonnegative.')
        milliseconds = when.numerator // when.denominator
        return max(0, milliseconds - self.delay_ms)

    def video_ms_for_audio_ms(self, audio_ms):
        return max(0, int(audio_ms) + self.delay_ms)

    def audio_sample_for_video_frame(self, frame, fps, sample_rate=None):
        """Map fractional frame rate to an exact audio sample index.

        Floors to the sample containing the requested frame, preserving
        monotonicity through 24000/1001 and 30000/1001 timebases.
        """
        rate = int(sample_rate or self.sample_rate or 0)
        if rate <= 0:
            raise ValueError('An audio sample rate is required.')
        frame_rate = _fraction(fps)
        if frame_rate <= 0:
            raise ValueError('Frame rate must be positive.')
        samples = (_fraction(frame) / frame_rate -
                   Fraction(self.delay_ms, 1000)) * rate
        return max(0, samples.numerator // samples.denominator)

    def file(self, embedded_movie):
        return self.external_path or embedded_movie


def layout_channels(layout, fallback=None):
    return _CHANNELS.get(str(layout or '').lower(), fallback)


def downmix_matrix(input_layout, output_layout='stereo', normalize=False):
    """Pure coefficients for DSP-capable output paths; Qt cannot apply them.

    5.1: FL FR FC LFE BL BR; 5.1(side): FL FR FC LFE SL SR. Center and
    surround use -3 dB; LFE is excluded without a bass-management stage.
    Unknown channel order fails safely. Optional peak normalization reserves
    headroom for simultaneous full-scale channels, reducing output level.
    """
    src = str(input_layout or '').strip().lower()
    dst = str(output_layout or '').strip().lower()
    if src not in ('mono', '1.0', 'stereo', '2.0', '5.1', '5.1(side)'):
        raise ValueError('No verified {} -> {} downmix matrix.'.format(src, dst))
    if dst == 'source':
        dst = ('mono' if src in ('mono', '1.0') else
               'stereo' if src in ('stereo', '2.0') else '5.1')
    if src in ('mono', '1.0'):
        if dst == 'mono':
            matrix = ((1.,),)
        elif dst == 'stereo':
            matrix = ((1.,), (1.,))
        else:
            raise ValueError('No verified {} -> {} downmix matrix.'.format(src, dst))
    elif src in ('stereo', '2.0'):
        if dst == 'mono':
            matrix = ((0.5, 0.5),)
        elif dst == 'stereo':
            matrix = ((1., 0.), (0., 1.))
        else:
            raise ValueError('No verified {} -> {} downmix matrix.'.format(src, dst))
    else:
        if dst == '5.1':
            matrix = tuple(tuple(float(i == j) for j in range(6))
                           for i in range(6))
        else:
            left = (1., 0., _SURROUND_GAIN, 0., _SURROUND_GAIN, 0.)
            right = (0., 1., _SURROUND_GAIN, 0., 0., _SURROUND_GAIN)
            if dst == 'stereo':
                matrix = (left, right)
            elif dst == 'mono':
                matrix = (tuple((a + b) * 0.5 for a, b in zip(left, right)),)
            else:
                raise ValueError('No verified {} -> {} downmix matrix.'.format(src, dst))
    if normalize:
        matrix = tuple(
            tuple(gain / max(1., sum(abs(g) for g in row)) for gain in row)
            for row in matrix)
    return matrix


def parse_audio_streams(probe_output):
    """Read FFprobe JSON; retain both Qt audio ordinal and container stream ID."""
    if isinstance(probe_output, bytes):
        probe_output = probe_output.decode('utf-8', 'replace')
    if isinstance(probe_output, str):
        probe_output = json.loads(probe_output)
    tracks = []
    for raw in probe_output.get('streams', []):
        if raw.get('codec_type', 'audio') != 'audio':
            continue
        ordinal = len(tracks)
        tags = raw.get('tags') or {}
        disposition = raw.get('disposition') or {}

        def positive_integer(value):
            try:
                converted = int(value)
                return converted if converted > 0 else None
            except (TypeError, ValueError, OverflowError):
                return None

        try:
            stream_id = int(raw.get('index', ordinal))
        except (TypeError, ValueError, OverflowError):
            stream_id = ordinal
        try:
            is_default = bool(int(disposition.get('default', 0)))
        except (TypeError, ValueError, OverflowError):
            is_default = False
        tracks.append({
            'audio_index': ordinal,        # Qt setActiveAudioTrack index
            'stream_index': stream_id,  # FFprobe container stream ID
            'codec': str(raw.get('codec_name') or ''),
            'channels': positive_integer(raw.get('channels')),
            'channel_layout': str(raw.get('channel_layout') or ''),
            'sample_rate': positive_integer(raw.get('sample_rate')),
            'sample_format': str(raw.get('sample_fmt') or ''),
            'language': str(tags.get('language') or ''),
            'title': str(tags.get('title') or ''),
            'default': is_default,
        })
    return tracks


def probe_audio_streams(ffprobe, path, runner=None):
    """Probe every soundtrack in the normal media workflow, never per frame.

    Uses the project's subprocess runner for environment isolation. Blocking:
    callers should use their existing asynchronous probing job for large files.
    A supplied runner permits deterministic tests without FFmpeg installed.
    """
    if runner is None:
        from ..media.ffmpeg import run
        runner = run
    output = runner([ffprobe, '-v', 'error', '-select_streams', 'a',
                     '-show_entries',
                     ('stream=index,codec_type,codec_name,channels,'
                      'channel_layout,sample_rate,sample_fmt:'
                      'stream_tags=language,title:stream_disposition=default'),
                     '-of', 'json', path])
    return parse_audio_streams(output)


def activate_qt_audio_track(player, audio_index):
    """Request a non-default Qt6 soundtrack once tracks are available.

    Returns (state, diagnostic): default, pending, applied, unsupported,
    unavailable, or failed. A default index of zero preserves the backend's
    chosen primary soundtrack. Call on tracksChanged and LoadedMedia because
    setSource often finishes before Qt knows the available tracks.
    """
    index = int(audio_index)
    if index < 0:
        raise ValueError('Audio track index must be nonnegative.')
    if index == 0:
        return 'default', None
    get_tracks = getattr(player, 'audioTracks', None)
    activate = getattr(player, 'setActiveAudioTrack', None)
    if not callable(get_tracks) or not callable(activate):
        return 'unsupported', 'Audio track selection is unavailable on this Qt backend.'
    try:
        tracks = get_tracks()
        if not tracks:
            return 'pending', None
        if index >= len(tracks):
            return 'unavailable', 'Requested audio track {} is unavailable.'.format(index)
        activate(index)
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        return 'failed', 'Could not select audio track {}: {}'.format(index, exc)
    return 'applied', None


def route_diagnostics(route, streams=None, can_select_stream=False,
                      can_downmix=False):
    """Report desired versus available routing without claiming unsupported DSP.

    When FFprobe metadata is absent, no nonzero track is considered selected.
    Existing Qt5 backends have no stream selection or programmable matrix.
    """
    route = route if isinstance(route, AudioRoute) else AudioRoute.from_dict(route)
    tracks = list(streams or [])
    notes = []
    selected = None
    active_index = None
    if tracks:
        active_index = 0
        if route.stream_index >= len(tracks):
            notes.append('Requested audio track is unavailable; using track 0.')
        elif route.stream_index and not can_select_stream:
            notes.append('Audio track selection is unavailable on this backend.')
        else:
            active_index = route.stream_index
        selected = tracks[active_index]
    elif route.stream_index:
        notes.append('Audio track metadata is not loaded; selection is pending.')
    # If a requested track is unavailable, report the actual fallback track's
    # layout rather than metadata retained from the unavailable track.
    layout = ((selected or {}).get('channel_layout') or
              route.channel_layout or '')
    if not layout:
        channels = (selected or {}).get('channels') or route.channels
        layout = {1: 'mono', 2: 'stereo'}.get(channels, '')
    effective_output = 'source'
    matrix = None
    if route.output_layout != 'source':
        try:
            matrix = downmix_matrix(layout, route.output_layout)
        except ValueError:
            notes.append('Channel order is unknown or routing is unsupported.')
        else:
            if can_downmix:
                effective_output = route.output_layout
            else:
                notes.append('This playback backend cannot apply channel matrices.')
    return {'requested_audio_index': route.stream_index,
            'effective_audio_index': active_index,
            'selected_stream': selected, 'source_layout': layout or None,
            'requested_output_layout': route.output_layout,
            'effective_output_layout': effective_output,
            'matrix': matrix if effective_output != 'source' else None,
            'messages': notes}


def drift_ms(video_frame, fps, audio_position_ms, route=None):
    """Signed audio clock discrepancy; positive means video is ahead."""
    fps = _fraction(fps)
    if fps <= 0:
        raise ValueError('Frame rate must be positive.')
    expected = _fraction(video_frame) * 1000 / fps
    route = route or AudioRoute()
    return float(expected) - route.video_ms_for_audio_ms(audio_position_ms)
