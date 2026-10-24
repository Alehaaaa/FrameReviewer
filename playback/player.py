# -*- coding: utf-8 -*-
"""Stable, frame-addressed playback with a single presentation surface.

Qt6 uses native decoding through QVideoSink, copying decoded frames to the
same pixmap item used by stills and image sequences. If that decoder does not
deliver a frame, or multimedia is unavailable (including Maya's Qt5 builds),
playback moves to the bounded asynchronous FFmpeg frame source.

The visible image is never cleared during pause, seek, trim-loop or buffering.
"""
from __future__ import absolute_import

from ..core.qtcompat import (QtCore, QtGui, Signal, HAVE_MULTIMEDIA,
                       QMediaPlayer, QMediaContent, QVideoSink, QAudioOutput)
from ..core import utils
from .frames import SequenceSource, FramePump
from .rolling_cache import VideoFrameSource
from .cache_policy import CachePolicy
from .audio_routing import AudioRoute, drift_ms
from . import cache_policy as playback_policy_module


# Keep a source alive if a decoder thread misses the shutdown deadline. Maya
# may destroy its owning window even when callers correctly return False, and
# abandoning the last Python reference can otherwise dispose of a live QThread.
_PENDING_SOURCE_SHUTDOWNS = set()


class _CompositeInfo(object):
    """Minimal MediaInfo stand-in for the composite playback path."""
    is_sequence = True
    audio = {}
    audio_description = 'Composition (no audio)'

    def __init__(self, width, height, fps, frame_count):
        self.path = '<composition>'
        self.width = int(width)
        self.height = int(height)
        self.fps = max(0.001, float(fps))
        self.frame_count = max(1, int(frame_count))
        self.sequence_files = []

    @property
    def duration(self):
        return self.frame_count / self.fps


class Player(QtCore.QObject):
    frameChanged = Signal(int)
    # A decoder can request/advance frames before pixels exist. Consumers such
    # as scopes should refresh only after this signal, once an actual image has
    # reached the presentation surface.
    framePresented = Signal(int)
    playingChanged = Signal(bool)
    loaded = Signal()
    cacheChanged = Signal(object, object)
    bufferProgressChanged = Signal(int)
    errorText = Signal(str)
    recordAudioStateChanged = Signal(str)
    recordAudioError = Signal(str)

    def __init__(self, ffmpeg, surface, parent=None, cache_policy=None):
        QtCore.QObject.__init__(self, parent)
        self._ffmpeg = ffmpeg
        self.surface = surface
        self.cache_policy = cache_policy or CachePolicy()

        self.info = None
        self.fps = 24.0
        self._audio_fps = 24.0
        self.frame_count = 1
        self.current_frame = 0
        self.in_frame = 0
        self.out_frame = 0
        self.loop = False
        self.loop_mode = playback_policy_module.ONCE
        self.speed = 1.0
        self.volume = 0.5
        self.muted = False
        self.audio_route = AudioRoute()
        self.playing = False
        self.playback_policy = playback_policy_module.REALTIME

        self._backend = None
        self._qplayer = None
        self._sink = None
        self._audio = None
        self._audio_player = None
        self._audio_output = None
        self._audio_ready = False
        self._audio_playing = False
        self._audio_pending_position = None
        self._audio_requested_position = None
        self._audio_seek_target_ms = None
        self._audio_ended = False
        self._audio_last_progress_ms = None
        self._audio_clock_stalled = False
        self._audio_stall_realign_attempted = False
        # Qt media seeks are asynchronous and often expensive. Throttle drift
        # corrections until the last seek is acknowledged or times out.
        self._audio_correction_cooldown_ms = 1000
        self._audio_last_seek_clock = QtCore.QElapsedTimer()
        self._audio_seek_clock = QtCore.QElapsedTimer()
        self._audio_seek_timer = QtCore.QTimer(self)
        self._audio_seek_timer.setSingleShot(True)
        self._audio_seek_timer.setInterval(75)
        self._audio_seek_timer.timeout.connect(self._flush_fallback_audio_seek)
        self._audio_eof_timer = QtCore.QTimer(self)
        self._audio_eof_timer.setSingleShot(True)
        self._audio_eof_timer.timeout.connect(self._finish_audio_eof_loop)
        self._audio_error_timer = QtCore.QTimer(self)
        self._audio_error_timer.setSingleShot(True)
        self._audio_error_timer.timeout.connect(self._finish_audio_error)
        self._audio_error_source = None
        self._audio_error_message = ''
        self._audio_active_path = ''
        self._audio_using_embedded_fallback = False
        self._audio_track_state = 'default'
        self._audio_track_warning = ''
        self._pump = None
        self._source = None
        self._pending_position = None
        self._got_video_frame = False
        self._preparing = False
        self._seek_target_us = None
        self._loop_seeking = False
        self._last_emitted = -1
        self._watchdog = QtCore.QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._check_decoder)
        self._fallback_timer = QtCore.QTimer(self)
        self._fallback_timer.setSingleShot(True)
        self._fallback_timer.timeout.connect(self._finish_native_fallback)
        self._fallback_reason = ''
        # A queued Qt signal may still arrive after disconnect(), especially
        # from a decoder QThread. Each new media/backend owns a generation;
        # callbacks from a previous one must never touch the new surface.
        self._media_generation = 0
        # A seek may be superseded even without changing the media. Capture
        # this token across synchronous source reads: cacheChanged listeners
        # can reenter seek/load while get()/get_payload() are running.
        self._frame_request_serial = 0
        self._signal_bindings = {'native': [], 'source': [], 'pump': [],
                                 'audio': []}
        self._native_seek_latch_us = None
        self._native_presented_us = None
        self._native_seek_serial = 0
        self._native_seek_clock = QtCore.QElapsedTimer()
        self._native_requested_position = None
        self._native_scrub_timer = QtCore.QTimer(self)
        self._native_scrub_timer.setSingleShot(True)
        self._native_scrub_timer.setInterval(45)
        self._native_scrub_timer.timeout.connect(self._flush_native_seek)
        self._audio_clock_floor_ms = None
        self._record_audio_transport = None  # explicit opt-in, NEVER auto-mixed
        self._record_audio_spec = None
        self._record_audio_source = None
        self._record_audio_refusal = ''

    @staticmethod
    def _record_audio_matches_source(spec, source):
        """Bind frozen per-occurrence WAV cuts to the ACTUAL video decoders.

        Otherwise a valid WAV for show A could audibly play over different
        show B frames merely because both records happen to be five frames.
        This cheap check is safe on the UI thread; source SHA/media verification
        remains the background `_record_pcm` worker's responsibility.
        """
        from fractions import Fraction
        from .composition import SEQUENCE
        record = spec.to_context().get('composite_export') or {}
        frozen = record.get('members')
        comp = getattr(source, 'composition', None)
        if (not isinstance(frozen, list) or comp is None or
                comp.mode != SEQUENCE or not comp.record_timeline or
                len(frozen) != len(comp.members) or
                source.frame_count != spec.count or
                Fraction(source._session_fps) != Fraction(spec.fps)):
            return False
        for index, (live, member) in enumerate(zip(comp.members, frozen)):
            decoder_index = source._source_index(index)
            if not 0 <= decoder_index < len(source._sources):
                return False
            decoder = source._sources[decoder_index]
            actual_rate = (Fraction(live.rate) if live.rate is not None else
                           Fraction(source._member_fps[decoder_index]) /
                           Fraction(source._session_fps))
            if (live.source_id != member.get('source_id') or
                    live.frame_count != member.get('record_length') or
                    live.source_in != member.get('source_in') or
                    live.session_offset != member.get('session_offset') or
                    live.trim_in != member.get('trim_in') or
                    live.trim_out != member.get('trim_out') or
                    live.hold != member.get('hold') or
                    actual_rate != Fraction(member.get('rate')) or
                    Fraction(source._member_fps[decoder_index]) !=
                    Fraction(member.get('source_fps'))):
                return False
            if member.get('input_kind') == 'movie':
                if getattr(decoder, '_source', None) != member.get('source_path'):
                    return False
            elif member.get('input_kind') == 'image_sequence':
                # Avoid suggesting media equality from an occurrence name.
                paths = member.get('files')
                if paths is not None and tuple(paths) != tuple(
                        getattr(decoder, '_files', ())):
                    return False
            else:
                return False
        return True

    def record_audio_capabilities(self):
        """Truthful record audition state; not proof of speaker onset/latency."""
        transport = self._record_audio_transport
        return (transport.capabilities() if transport is not None else {
            'state': 'unsupported' if self._record_audio_refusal else 'disabled',
            'backend': None,
            'reason': self._record_audio_refusal, 'sample_frames': None,
            'hardware_latency_calibrated': False,
            'speaker_audibility_proven': False,
            'record_is_video_master': True})

    def enable_record_audio(self, spec, ffmpeg):
        """Opt into asynchronously verified, bounded v4 record PCM audition.

        Return True only when a valid request is accepted; asynchronous state
        still starts at 'preparing', NEVER 'audibly ready'. Connect
        recordAudioStateChanged and recordAudioError to display actual outcome.
        No GUI-thread FFmpeg decode, no extra playhead and no Session mutation.
        """
        from .record_audio_transport import RecordAudioTransport
        from .composition import SEQUENCE
        from fractions import Fraction
        self.disable_record_audio()
        source = self._source
        comp = getattr(source, 'composition', None)
        try:
            if (self._backend != 'frames' or self._pump is None or
                    comp is None or comp.mode != SEQUENCE or
                    not comp.record_timeline or
                    Fraction(source._session_fps) != Fraction(24000, 1001) or
                    self.frame_count != spec.count or
                    not self._record_audio_matches_source(spec, source) or
                    self._audio_player is not None or self._qplayer is not None or
                    self.audio_route.external_path or self.audio_route.delay_ms != 0 or
                    self.speed != 1. or
                    self.loop_mode == playback_policy_module.PING_PONG):
                raise ValueError('Record PCM audition requires a single 24000/1001 '
                                 'video-master record composition without other audio '
                                 'routes, reverse playback, speed effects or ping-pong.')
        except (AttributeError, TypeError, ValueError, ZeroDivisionError) as exc:
            self._record_audio_refusal = str(exc)
            self.recordAudioStateChanged.emit('unsupported')
            self.recordAudioError.emit(str(exc))
            self.errorText.emit(str(exc))
            return False
        transport = RecordAudioTransport(self)
        self._record_audio_transport = transport
        self._record_audio_spec = spec
        self._record_audio_source = source
        transport.stateChanged.connect(self._on_record_audio_state)
        transport.errorText.connect(self.recordAudioError.emit)
        transport.errorText.connect(self.errorText.emit)
        transport.set_gain(self.volume, self.muted)
        return transport.prepare(spec, ffmpeg)

    def _on_record_audio_state(self, state):
        if state == 'ready' and not self._record_audio_binding_valid():
            self.disable_record_audio()
            self.recordAudioError.emit('Frozen audio cuts no longer match the live record video.')
            return
        self.recordAudioStateChanged.emit(state)
        if state == 'ready':
            self._sync_record_audio()

    def disable_record_audio(self):
        transport = self._record_audio_transport
        self._record_audio_transport = None
        self._record_audio_spec = None
        self._record_audio_source = None
        self._record_audio_refusal = ''
        if transport is not None:
            transport.disable()
            transport.deleteLater()

    def _sync_record_audio(self):
        transport = self._record_audio_transport
        if transport is not None:
            if not self._record_audio_binding_valid():
                self.disable_record_audio()
                self.recordAudioError.emit('Record video/source changed; audio audition disabled.')
                return
            transport.sync(int(self.current_frame), self.playing,
                           speed=self.speed, loop_mode=self.loop_mode,
                           buffering=(self._pump.is_buffering()
                                      if self._pump is not None else False),
                           policy=self.playback_policy)

    def _record_audio_binding_valid(self):
        try:
            return (self._record_audio_source is self._source and
                    self._record_audio_spec is not None and
                    self._record_audio_matches_source(
                        self._record_audio_spec, self._source))
        except (AttributeError, TypeError, ValueError, IndexError,
                ZeroDivisionError, OverflowError):
            return False

    def _bind_media_signal(self, group, signal, callback, owner=None):
        """Generation-bound Qt connection; keep exact callable for disconnect."""
        generation = self._media_generation

        def guarded(*args):
            if generation != self._media_generation:
                return
            if owner is not None and owner is not getattr(self, {
                    'native': '_qplayer', 'source': '_source',
                    'pump': '_pump', 'audio': '_audio_player'}[group], None):
                return
            callback(*args)

        signal.connect(guarded)
        self._signal_bindings[group].append((signal, guarded))

    def _unbind_media_signals(self, group):
        bindings = self._signal_bindings[group]
        self._signal_bindings[group] = []
        for signal, callback in bindings:
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError, AttributeError):
                pass

    def _bind_source_signals(self):
        source = self._source
        self._bind_media_signal('source', source.frameAvailable,
                                self._on_frame_available, source)
        self._bind_media_signal('source', source.cacheChanged,
                                self.cacheChanged.emit, source)
        if hasattr(source, 'errorText'):
            self._bind_media_signal('source', source.errorText,
                                    self._on_frame_error, source)
        if hasattr(source, 'endReached'):
            self._bind_media_signal('source', source.endReached,
                                    self._on_source_end, source)

    def _bind_pump_signals(self):
        pump = self._pump
        self._bind_media_signal('pump', pump.frameChanged,
                                self._on_pump_frame, pump)
        self._bind_media_signal('pump', pump.stopped,
                                self._on_pump_stopped, pump)
        self._bind_media_signal('pump', pump.bufferingChanged,
                                self._on_pump_buffering, pump)

    def load(self, info, audio_route=None):
        self.stop_all(wait=True)
        if audio_route is not None:
            self.audio_route = (audio_route if isinstance(audio_route, AudioRoute)
                                else AudioRoute.from_dict(audio_route))
        self.info = info
        self.fps = info.fps
        self._audio_fps = getattr(info, 'fps_rational', None) or info.fps
        self.frame_count = info.frame_count
        self.current_frame = 0
        self.in_frame = 0
        self.out_frame = info.frame_count - 1
        self.playing = False
        self._last_emitted = -1
        self._pending_position = None
        self._got_video_frame = False
        self._preparing = False
        self._seek_target_us = None
        self._loop_seeking = False
        self.surface.set_native_size(info.width, info.height)
        self.surface.clear()

        if (not self.audio_route.external_path and
                self.audio_route.delay_ms == 0 and
                self.playback_policy == playback_policy_module.REALTIME and
                not info.is_sequence and
                HAVE_MULTIMEDIA and QVideoSink is not None):
            try:
                self._build_qplayer(info.path)
                self._backend = 'qt'
                self.cacheChanged.emit([], None)
                self.bufferProgressChanged.emit(0)
                self._pending_position = 0
                # Let the decoder present its first real frame, then pause.
                # This uses the regular player pipeline, not a still extractor.
                self._preparing = True
                self._watchdog.start(2500)
                self._qplayer.play()
            except Exception as exc:
                self._stop_qplayer()
                self.errorText.emit('Native decoder unavailable: {}'.format(exc))
                self._activate_frames(0)
        else:
            self._activate_frames(0)
        self._emit_frame(0)
        self.loaded.emit()

    def load_frames_source(self, source, fps, frame_count, width, height):
        """Drive playback from a prebuilt frame source (e.g. a composite).

        Bypasses the normal decoder selection: the given ``source`` only needs
        the SequenceSource interface (``get``/``ensure``/``shutdown`` and the
        ``frameAvailable``/``cacheChanged`` signals). Used for the software
        grid/difference composition path.
        """
        self.stop_all(wait=True)
        self.info = _CompositeInfo(width, height, fps, frame_count)
        self.fps = max(0.001, float(fps))
        self._audio_fps = fps
        self.frame_count = max(1, int(frame_count))
        self.current_frame = 0
        self._last_emitted = -1
        self.in_frame = 0
        self.out_frame = self.frame_count - 1
        self.playing = False
        self._backend = 'frames'
        self._stop_qplayer()
        self.surface.set_native_size(width, height)
        self.surface.clear()

        self._source = source
        self._bind_source_signals()
        self.cacheChanged.emit([], None)
        self.bufferProgressChanged.emit(0)
        self._source.set_loop_start(self.in_frame)
        if hasattr(self._source, 'set_loop_mode'):
            self._source.set_loop_mode(self.loop_mode)
        elif hasattr(self._source, 'set_loop'):
            self._source.set_loop(self.loop)
        self._pump = FramePump(self._source, self.fps, self.frame_count, self)
        self._pump.set_playback_policy(self.playback_policy)
        self._pump.in_frame = self.in_frame
        self._pump.out_frame = self.out_frame
        self._pump.loop = self.loop
        self._pump.set_loop_mode(self.loop_mode)
        self._pump.set_speed(self.speed)
        self._bind_pump_signals()
        self._pump.seek(0, restrict_to_range=False)
        self._emit_frame(0)
        self.loaded.emit()

    def _build_qplayer(self, path):
        self._sink = QVideoSink(self)
        self._bind_media_signal('native', self._sink.videoFrameChanged,
                                self._on_video_frame)
        self._qplayer = QMediaPlayer(self)
        self._qplayer.setVideoOutput(self._sink)
        if QAudioOutput is not None:
            self._audio = QAudioOutput(self)
            self._audio.setVolume(0.0 if self.muted else self.volume)
            self._qplayer.setAudioOutput(self._audio)
        self._bind_media_signal('native', self._qplayer.positionChanged,
                                self._on_position, self._qplayer)
        self._bind_media_signal('native', self._qplayer.mediaStatusChanged,
                                self._on_media_status, self._qplayer)
        if hasattr(self._qplayer, 'bufferProgressChanged'):
            self._bind_media_signal('native', self._qplayer.bufferProgressChanged,
                                    self._on_buffer_progress, self._qplayer)
        if hasattr(self._qplayer, 'errorOccurred'):
            self._bind_media_signal('native', self._qplayer.errorOccurred,
                                    self._on_decoder_error, self._qplayer)
        if hasattr(self._qplayer, 'tracksChanged'):
            self._bind_media_signal('native', self._qplayer.tracksChanged,
                                    self._on_audio_tracks_changed, self._qplayer)
        self._qplayer.setSource(QtCore.QUrl.fromLocalFile(path))
        self._qplayer.setPlaybackRate(self.speed)
        if self._audio is None:
            if hasattr(self._qplayer, 'setVolume'):
                self._qplayer.setVolume(int(round(self.volume * 100)))
            if hasattr(self._qplayer, 'setMuted'):
                self._qplayer.setMuted(self.muted)

    def _activate_frames(self, frame):
        """Create a fresh fallback after destroying the old decoder."""
        self._media_generation += 1
        self._native_seek_latch_us = None
        self._native_presented_us = None
        self._native_seek_clock.invalidate()
        self._watchdog.stop()
        self._preparing = False
        self._seek_target_us = None
        self._backend = 'frames'
        self._stop_qplayer()
        info = self.info
        if not info.is_sequence or self.audio_route.external_path:
            self._build_fallback_audio(self.audio_route.file(info.path), frame)
        if info.is_sequence:
            identity = ({'source_id': info.source_id}
                        if getattr(info, 'source_id', '') else {})
            if (isinstance(info.metadata, dict) and
                    info.metadata.get('exr_selection_explicit')):
                identity.update({key: info.metadata[key]
                                 for key in ('exr_layer', 'exr_part')
                                 if key in info.metadata})
            if (isinstance(info.metadata, dict) and
                    info.metadata.get('exr_display_canvas_rectified')):
                identity['exr_display_canvas'] = True
                identity['exr_canvas_contract'] = {
                    'display_window': info.metadata['display_window'],
                    'pixel_aspect': info.pixel_aspect_ratio}
            if getattr(info, 'missing_policy', 'skip') == 'skip':
                # Preserve the historical constructor contract used by studio
                # sources, tests and plug-in SequenceSource implementations.
                self._source = SequenceSource(
                    info.sequence_files, self, self.cache_policy, **identity)
            else:
                self._source = SequenceSource(
                    info.sequence_files, self, self.cache_policy,
                    source_frames=info.sequence_source_frames,
                    include_holes=True, missing_policy=info.missing_policy,
                    declared_frames=info.frame_numbers,
                    source_size=(info.width, info.height), **identity)
        else:
            self._source = VideoFrameSource(
                self._ffmpeg, info.path, info.fps, info.frame_count, self,
                self.cache_policy)
            self._source.errorText.connect(self._on_frame_error)
            self._source.endReached.connect(self._on_source_end)
        self._bind_source_signals()
        self.cacheChanged.emit([], None)
        self.bufferProgressChanged.emit(0)
        # A native-decoder fallback can happen after trim/loop were already
        # configured. Give the new cache the same range and loop direction so
        # it prefetches the opposite edge before the first wrap.
        if hasattr(self._source, 'set_playback_range'):
            self._source.set_playback_range(self.in_frame, self.out_frame)
        self._source.set_loop_start(self.in_frame)
        if hasattr(self._source, 'set_loop_mode'):
            self._source.set_loop_mode(self.loop_mode)
        elif hasattr(self._source, 'set_loop'):
            self._source.set_loop(self.loop)
        self._pump = FramePump(self._source, self.fps, self.frame_count, self)
        self._pump.set_playback_policy(self.playback_policy)
        if self._audio_player is not None:
            self._pump.set_external_clock(self._fallback_audio_position,
                                          self._wrap_fallback_audio)
        self._pump.in_frame = self.in_frame
        self._pump.out_frame = self.out_frame
        self._pump.loop = self.loop
        self._pump.set_loop_mode(self.loop_mode)
        self._pump.set_speed(self.speed)
        self._bind_pump_signals()
        self._pump.seek(frame, restrict_to_range=False)

    def _build_fallback_audio(self, path, frame):
        """Play the soundtrack while FFmpeg supplies video frames.

        Qt5 has QMediaPlayer audio even though it lacks Qt6's QVideoSink.
        Keep this player separate from the native video decoder so a video
        decoding failure does not silence otherwise playable audio.
        """
        self._stop_fallback_audio()
        if not HAVE_MULTIMEDIA or QMediaPlayer is None:
            return
        self._audio_active_path = path
        self._audio_using_embedded_fallback = bool(
            self.audio_route.external_path and
            path != self.audio_route.external_path)
        try:
            audio = QMediaPlayer(self)
            self._audio_player = audio
            self._bind_media_signal('audio', audio.mediaStatusChanged,
                                    self._on_audio_status, audio)
            if hasattr(audio, 'errorOccurred'):
                self._bind_media_signal('audio', audio.errorOccurred,
                                        self._on_audio_error, audio)
            elif hasattr(audio, 'error'):
                self._bind_media_signal('audio', audio.error,
                                        self._on_audio_error, audio)
            if hasattr(audio, 'tracksChanged'):
                self._bind_media_signal('audio', audio.tracksChanged,
                                        self._on_audio_tracks_changed, audio)
            if QAudioOutput is not None and hasattr(audio, 'setAudioOutput'):
                self._audio_output = QAudioOutput(self)
                audio.setAudioOutput(self._audio_output)
            self._apply_fallback_audio_volume()
            audio.setPlaybackRate(max(0.01, abs(self.speed)))
            if hasattr(audio, 'setNotifyInterval'):
                audio.setNotifyInterval(40)
            self._audio_pending_position = self.audio_route.audio_ms_for_video_frame(
                frame, self._audio_fps)
            url = QtCore.QUrl.fromLocalFile(path)
            if hasattr(audio, 'setSource'):
                audio.setSource(url)
            else:
                audio.setMedia(QMediaContent(url))
            # Qt6 only discovers audioTracks asynchronously after setSource.
            # Calling setActiveAudioTrack here alone silently loses the choice.
            self._on_audio_tracks_changed()
        except Exception as exc:
            self._stop_fallback_audio()
            self.errorText.emit('Audio playback unavailable: {}'.format(exc))

    def _apply_fallback_audio_volume(self):
        effective_volume = (0.0 if self.muted or self.speed < 0 or
                            self.loop_mode == playback_policy_module.PING_PONG
                            else self.volume)
        if self._audio_output is not None:
            self._audio_output.setVolume(effective_volume)
        elif self._audio_player is not None:
            self._audio_player.setVolume(int(round(effective_volume * 100)))
            if hasattr(self._audio_player, 'setMuted'):
                self._audio_player.setMuted(self.muted)

    def _on_audio_status(self, status):
        if self._backend != 'frames' or self._audio_player is None:
            return
        sender = self.sender()
        if sender is not None and sender is not self._audio_player:
            return
        statuses = getattr(QMediaPlayer, 'MediaStatus', QMediaPlayer)
        if status == statuses.EndOfMedia:
            self._audio_playing = False
            self._audio_ended = True
            self._audio_clock_stalled = False
            self._audio_last_progress_ms = None
            if (self.loop_mode == playback_policy_module.LOOP and
                    self.speed > 0 and self.playing and self._pump is not None and
                    self.current_frame >= self.out_frame - 1):
                # Qt backends can crash if we seek/play QMediaPlayer from
                # inside its own EndOfMedia notification. Audio can be shorter
                # than video: *never* rewind video just because sound ended.
                self._audio_eof_timer.start(0)
            return
        if status in (statuses.LoadedMedia, statuses.BufferedMedia):
            self._on_audio_tracks_changed()
            first_ready = not self._audio_ready
            self._audio_ready = True
            if self._audio_pending_position is not None:
                target = self._audio_pending_position
                self._audio_pending_position = None
                self._seek_fallback_audio(target, coalesce=False)
            if first_ready:
                self._sync_fallback_audio()

    def _on_audio_tracks_changed(self, *args):
        """Apply the requested Qt6 track after decoder metadata becomes ready."""
        audio = (self._audio_player if self._backend == 'frames'
                 else self._qplayer)
        if audio is None:
            return
        index = self.audio_route.stream_index
        if not index:
            # Returning from track B to the default/first audio track must
            # *actually* switch Qt6 back to ordinal zero. Merely changing the
            # route metadata left the previously selected soundtrack audible.
            get_tracks = getattr(audio, 'audioTracks', None)
            activate = getattr(audio, 'setActiveAudioTrack', None)
            if callable(get_tracks) and callable(activate):
                try:
                    tracks = get_tracks()
                    if tracks:
                        getter = getattr(audio, 'activeAudioTrack', None)
                        active = getter() if callable(getter) else None
                        if active != 0:
                            activate(0)
                except (RuntimeError, TypeError, AttributeError, ValueError) as exc:
                    self._audio_track_state = 'failed'
                    warning = 'Cannot restore default audio track: {}'.format(exc)
                    if warning != self._audio_track_warning:
                        self.errorText.emit(warning)
                    self._audio_track_warning = warning
                    return
            self._audio_track_state = 'default'
            self._audio_track_warning = ''
            return
        get_tracks = getattr(audio, 'audioTracks', None)
        activate = getattr(audio, 'setActiveAudioTrack', None)
        if not callable(get_tracks) or not callable(activate):
            state = 'unsupported'
            warning = 'Audio stream selection is unavailable on this Qt backend.'
        else:
            try:
                tracks = get_tracks()
                if not tracks:
                    self._audio_track_state = 'pending'
                    return
                if index >= len(tracks):
                    state = 'unavailable'
                    warning = 'Requested audio track {} is unavailable.'.format(index)
                else:
                    # Track numbers are audio ordinals, not FFprobe container IDs.
                    getter = getattr(audio, 'activeAudioTrack', None)
                    active = getter() if callable(getter) else None
                    # BufferedMedia can fire repeatedly while scrubbing. Do
                    # not flush a working audio decoder on every such event.
                    if active != index:
                        activate(index)
                    state = 'applied'
                    warning = ''
            except (RuntimeError, TypeError, AttributeError, ValueError) as exc:
                state = 'failed'
                warning = 'Audio track selection failed: {}'.format(exc)
        self._audio_track_state = state
        if warning and self._audio_track_warning != warning:
            self.errorText.emit(warning)
        self._audio_track_warning = warning

    def _on_audio_error(self, *args):
        sender = self.sender()
        if (self._backend != 'frames' or self._audio_player is None or
                (sender is not None and sender is not self._audio_player)):
            return
        self._audio_error_source = self._audio_player
        self._audio_error_message = (self._audio_player.errorString() or
                                     'unsupported audio decoder')
        self._audio_error_timer.start(0)

    def _finish_audio_error(self):
        if self._audio_player is None or self._audio_player is not self._audio_error_source:
            return
        message = self._audio_error_message
        failed_external = bool(
            self.audio_route.external_path and
            self._audio_active_path == self.audio_route.external_path)
        embedded = (self.info is not None and
                    getattr(self.info, 'path', '') != '<composition>' and
                    bool(getattr(self.info, 'audio', {}).get('channels')))
        self._stop_fallback_audio()
        if failed_external and embedded:
            self.errorText.emit(
                'External audio failed ({}); using embedded audio.'.format(message))
            self._build_fallback_audio(self.info.path, self.current_frame)
            return
        self.errorText.emit('Audio playback unavailable: ' + message)

    def _finish_audio_eof_loop(self):
        if (self._backend == 'frames' and self._audio_player is not None and
                self.playing and self.loop_mode == playback_policy_module.LOOP
                and self.speed > 0 and self._pump is not None and
                self.current_frame >= self.out_frame - 1):
            self._wrap_fallback_audio(self.in_frame)
            self._pump.seek(self.in_frame)

    def _sync_fallback_audio(self, force=False):
        audio = self._audio_player
        if self._backend != 'frames' or audio is None or self._pump is None:
            return
        target = self.audio_route.audio_ms_for_video_frame(
            self.current_frame, self._audio_fps)
        if not self._audio_ready:
            self._audio_pending_position = target
            return
        if force:
            self._seek_fallback_audio(target, coalesce=True)
        video_ms = utils.frame_to_ms(self.current_frame, self.fps)
        should_play = (self.playing and not self._audio_ended and
                       self.speed > 0 and
                       self.loop_mode != playback_policy_module.PING_PONG and
                       self._pump.is_playing() and
                       # Before a positive soundtrack delay elapses, drive
                       # video on its monotonic clock, not the paused audio
                       # clock; start the soundtrack at the delay boundary.
                       video_ms >= self.audio_route.delay_ms and
                       # REALTIME: audio is the clock. Pausing the soundtrack
                       # on every video cache miss makes Qt5 repeatedly drain
                       # and refill its output queue, audibly stuttering.
                       # PLAY_ALL: video is master, so pause on missing frames.
                       (self.playback_policy == playback_policy_module.REALTIME or
                        not self._pump.is_buffering()))
        if should_play != self._audio_playing:
            self._audio_playing = should_play
            # A deliberate pause is not a stalled device. The same elapsed
            # timer tracks asynchronous seeks and live audio heartbeat; only
            # reset the latter when no seek acknowledgement is outstanding.
            if self._audio_seek_target_ms is None:
                self._audio_last_progress_ms = None
                self._audio_seek_clock.invalidate()
                self._audio_clock_stalled = False
                self._audio_stall_realign_attempted = False
            if should_play:
                audio.play()
            else:
                audio.pause()

    def _seek_fallback_audio(self, target, coalesce=True):
        """Request an audio seek, avoiding repeated asynchronous decoder flushes.

        Scrubbing is coalesced to the most recent target. When a realignment is
        needed immediately (startup, loop wrap or large drift), do not issue a
        second seek for the same in-flight target. QMediaPlayer position updates
        can lag a seek by hundreds of milliseconds on Maya's Qt5 backends.
        """
        target = max(0, int(target))
        audio = self._audio_player
        if audio is None or not self._audio_ready:
            self._audio_pending_position = target
            return
        if (self._audio_requested_position == target and
                self._audio_seek_timer.isActive()):
            return
        if (self._audio_seek_target_ms == target and
                self._audio_seek_clock.isValid() and
                self._audio_seek_clock.elapsed() < self._audio_correction_cooldown_ms):
            return
        if coalesce:
            self._audio_requested_position = target
            self._audio_seek_timer.start()
            return
        self._audio_seek_timer.stop()
        self._audio_requested_position = None
        # Calling setPosition at an already accepted target on each pause /
        # resume generates expensive flushes and sometimes audible pops.
        if abs(int(audio.position()) - target) <= 15:
            self._audio_seek_target_ms = None
            self._audio_ended = False
            return
        self._audio_seek_target_ms = target
        self._audio_seek_clock.restart()
        self._audio_last_seek_clock.restart()
        self._audio_clock_floor_ms = None
        self._audio_last_progress_ms = None
        # A one-shot stall repair must remain quarantined until the new audio
        # position catches the already advancing video clock. Otherwise an
        # old queued Qt position sample can pull video backwards on seek ack.
        self._audio_clock_stalled = self._audio_stall_realign_attempted
        self._audio_ended = False
        audio.setPosition(target)

    def _ack_fallback_audio_seek(self, position):
        """Require the device to land before accepting another drift correction.

        Qt5 QMediaPlayer can report its old position for seconds after a seek.
        FramePump should use the monotonic video clock meanwhile rather than
        causing a new decoder flush every cooldown period.
        """
        target = self._audio_seek_target_ms
        if target is None:
            return True
        if abs(int(position) - target) > 120:
            return False
        self._audio_seek_target_ms = None
        self._audio_last_progress_ms = int(position)
        self._audio_seek_clock.restart()
        return True

    def _flush_fallback_audio_seek(self):
        target = self._audio_requested_position
        self._audio_seek_timer.stop()
        self._audio_requested_position = None
        if target is None:
            return
        self._seek_fallback_audio(target, coalesce=False)
        # A manual seek after an EOF can unblock the soundtrack. BufferedMedia
        # need not fire again, so restore the play/pause state here, without
        # a second setPosition or an additional transport timer.
        if self._audio_player is not None and self._backend == 'frames':
            self._sync_fallback_audio()

    def _fallback_audio_position(self):
        """Return the free-running soundtrack clock, even while video buffers."""
        if (not self._audio_ready or self._audio_player is None or
                not self.playing or self.speed <= 0 or
                not self._audio_playing or
                self.loop_mode == playback_policy_module.PING_PONG or
                self._audio_seek_timer.isActive()):
            return None
        if self._audio_ended:
            return None
        position = int(self._audio_player.position())
        if position < 0 or not self._ack_fallback_audio_seek(position):
            return None
        video_ms = self.audio_route.video_ms_for_audio_ms(position)
        last = self._audio_last_progress_ms
        if last is None or position > last + 3:
            self._audio_last_progress_ms = position
            self._audio_seek_clock.restart()  # reuse existing seek timer as heartbeat
        elif (self._audio_seek_clock.isValid() and
              self._audio_seek_clock.elapsed() >= 800):
            # A stopped/unresponsive soundtrack must not indefinitely pin the
            # FramePump master to a single audio position. Its own elapsed
            # clock remains available and no extra playback clock is created.
            self._audio_clock_stalled = True
        if self._audio_clock_stalled:
            # Once video has run on its own clock, never rewind it to late
            # audio packets. Rejoin only when the soundtrack is within a
            # bounded distance of the *current* video playhead.
            video_now = utils.frame_to_ms(self.current_frame, self.fps)
            if (last is None or position <= last + 3 or
                    abs(video_ms - video_now) > 160):
                return None
            self._audio_clock_stalled = False
            self._audio_stall_realign_attempted = False
            self._audio_clock_floor_ms = None
        if self._audio_clock_floor_ms is not None:
            # Qt5 sometimes reports a backwards position sample after audio
            # device callbacks; do not let that make frames scrub backwards.
            video_ms = max(self._audio_clock_floor_ms, video_ms)
        self._audio_clock_floor_ms = video_ms
        return video_ms

    def _wrap_fallback_audio(self, frame):
        audio = self._audio_player
        if audio is None:
            return
        self._audio_eof_timer.stop()
        self._audio_clock_floor_ms = None
        self._audio_ended = False
        self._audio_clock_stalled = False
        self._audio_last_progress_ms = None
        self._audio_stall_realign_attempted = False
        target = self.audio_route.audio_ms_for_video_frame(
            frame, self._audio_fps)
        if not self._audio_ready:
            self._audio_pending_position = target
            return
        self._audio_seek_timer.stop()
        self._audio_requested_position = None
        self._seek_fallback_audio(target, coalesce=False)
        # Delay still applies after a trim/loop wrap: never start sound at
        # record frame zero if its positive offset has not elapsed. Likewise
        # a paused transport must remain paused after an EOF callback.
        self._sync_fallback_audio()

    def _stop_fallback_audio(self):
        audio = self._audio_player
        output = self._audio_output
        self._audio_player = None
        self._audio_output = None
        self._unbind_media_signals('audio')
        self._audio_ready = False
        self._audio_playing = False
        self._audio_pending_position = None
        self._audio_requested_position = None
        self._audio_seek_target_ms = None
        self._audio_clock_floor_ms = None
        self._audio_ended = False
        self._audio_clock_stalled = False
        self._audio_last_progress_ms = None
        self._audio_stall_realign_attempted = False
        self._audio_seek_clock.invalidate()
        self._audio_last_seek_clock.invalidate()
        self._audio_seek_timer.stop()
        self._audio_eof_timer.stop()
        self._audio_error_timer.stop()
        self._audio_error_source = None
        self._audio_error_message = ''
        self._audio_active_path = ''
        self._audio_using_embedded_fallback = False
        self._audio_track_state = 'default'
        self._audio_track_warning = ''
        if audio is not None:
            try:
                audio.stop()
            except (RuntimeError, TypeError):
                pass
            audio.deleteLater()
        if output is not None:
            output.deleteLater()

    def _on_buffer_progress(self, progress):
        sender = self.sender()
        if (self._backend == 'qt' and
                (sender is None or sender is self._qplayer)):
            self.bufferProgressChanged.emit(int(
                utils.clamp(round(float(progress) * 100), 0, 100)))

    def _on_video_frame(self, video_frame):
        sender = self.sender()
        if (self._backend != 'qt' or
                (sender is not None and sender is not self._sink) or
                not video_frame.isValid()):
            return
        generation = self._media_generation
        seek_serial = self._native_seek_serial
        try:
            timestamp = video_frame.startTime()
            frame_tolerance = max(1, int(round(1000000.0 / max(0.001, self.fps))))
            if timestamp >= 0:
                if (self._native_seek_latch_us is not None and
                        timestamp < self._native_seek_latch_us - frame_tolerance):
                    return
                if (self._native_seek_latch_us is not None and
                        self._native_seek_clock.isValid()):
                    # A backward scrub can leave *future* frames from the old
                    # decoder queue as well as earlier ones. The latest seek
                    # is an upper bound while paused; during playback allow
                    # only frames reachable since that seek was requested.
                    elapsed_us = (self._native_seek_clock.elapsed() * 1000
                                  if self.playing else 0)
                    reach = (self._native_seek_latch_us +
                             int(elapsed_us * max(0, self.speed)) +
                             2 * frame_tolerance)
                    if timestamp > reach:
                        return
                if (self._native_presented_us is not None and
                        timestamp < self._native_presented_us - frame_tolerance):
                    return
            image = video_frame.toImage()
            if image.isNull():
                return
            if self._seek_target_us is not None:
                if timestamp >= 0:
                    # Frames from the old decoder queue must never overwrite the
                    # last good frame during a seek, especially at loop wrap.
                    if abs(timestamp - self._seek_target_us) > frame_tolerance:
                        return
                else:
                    # Unknown timestamp cannot be correlated to the newest
                    # scrubbing request. Keep the previous stable picture.
                    return
                self._seek_target_us = None
            self.surface.set_display(QtGui.QPixmap.fromImage(image))
        except Exception:
            return
        if generation != self._media_generation or self._backend != 'qt':
            return
        if seek_serial != self._native_seek_serial:
            return
        if timestamp >= 0:
            self._native_presented_us = timestamp
        presented_frame = (int(utils.clamp(utils.ms_to_frame(
            timestamp / 1000.0, self.fps), 0, self.frame_count - 1))
            if timestamp >= 0 else self.current_frame)
        self.framePresented.emit(presented_frame)
        if generation != self._media_generation or self._backend != 'qt':
            return
        if seek_serial != self._native_seek_serial:
            return
        if (self.playing and self._native_seek_latch_us is not None and
                self._native_seek_clock.isValid() and
                self._native_seek_clock.elapsed() >= 600):
            # Past the short decoder-flush quarantine, preserve the normal
            # forward timestamp guard without constraining future pauses to
            # the frame at which a long-ago seek originally started.
            self._native_seek_latch_us = None
        self._got_video_frame = True
        self._watchdog.stop()
        if self._preparing and not self.playing:
            self._preparing = False
            self._qplayer.pause()

    def _on_position(self, ms):
        sender = self.sender()
        if (self._backend != 'qt' or
                (sender is not None and sender is not self._qplayer)):
            return
        frame = int(utils.clamp(utils.ms_to_frame(ms, self.fps), 0, self.frame_count - 1))
        # Qt may deliver a stale positionChanged from *before* a scrub after
        # setPosition() has been called again. Do not rewind the requested Maya
        # frame/playhead while the newest decoded image is still in flight.
        if self._native_seek_latch_us is not None:
            slack = int(round(1000000.0 / max(0.001, self.fps)))
            if int(ms) * 1000 < self._native_seek_latch_us - slack:
                return
            elapsed_us = (self._native_seek_clock.elapsed() * 1000
                          if self.playing and self._native_seek_clock.isValid()
                          else 0)
            if (int(ms) * 1000 > self._native_seek_latch_us +
                    int(elapsed_us * max(0, self.speed)) + 2 * slack):
                return
        if self._loop_seeking:
            if frame <= self.in_frame + 1:
                self._loop_seeking = False
            else:
                return
        if self.playing and frame >= self.out_frame:
            if self.loop and self.out_frame > self.in_frame:
                self._loop_seeking = True
                self._native_seek_serial += 1
                self._seek_target_us = self._frame_to_us(self.in_frame)
                self._native_seek_latch_us = self._seek_target_us
                self._native_presented_us = None
                self._native_seek_clock.restart()
                self._qplayer.setPosition(int(utils.frame_to_ms(self.in_frame, self.fps)))
                return
            self._pause_at(self.out_frame)
            return
        self.current_frame = frame
        self._emit_frame(frame)

    def _on_media_status(self, status):
        sender = self.sender()
        if (self._backend != 'qt' or
                (sender is not None and sender is not self._qplayer)):
            return
        try:
            loaded = status in (QMediaPlayer.MediaStatus.LoadedMedia,
                                QMediaPlayer.MediaStatus.BufferedMedia)
            ended = status == QMediaPlayer.MediaStatus.EndOfMedia
        except AttributeError:
            return
        if loaded:
            self._on_audio_tracks_changed()
        if loaded and self._pending_position is not None:
            self._qplayer.setPosition(int(self._pending_position))
            self._pending_position = None
        if ended:
            if self.loop and self.playing:
                self._loop_seeking = False
                self._native_seek_serial += 1
                self._seek_target_us = self._frame_to_us(self.in_frame)
                self._native_seek_latch_us = self._seek_target_us
                self._native_presented_us = None
                self._native_seek_clock.restart()
                self._qplayer.setPosition(int(utils.frame_to_ms(self.in_frame, self.fps)))
                # Re-arm the stall watchdog: if the decoder does not resume after
                # the loop wrap, fall back to FFmpeg instead of freezing.
                self._got_video_frame = False
                self._watchdog.start(2500)
                self._qplayer.play()
            elif self.playing:
                self._pause_at(self.out_frame)

    def _on_decoder_error(self, *args):
        sender = self.sender()
        if (self._backend != 'qt' or self.info is None or
                (sender is not None and sender is not self._qplayer)):
            return
        message = self._qplayer.errorString() or 'Native video decoder error'
        self._defer_native_fallback('Using FFmpeg decoder: ' + message)

    def _defer_native_fallback(self, reason=''):
        if self._backend != 'qt' or self._fallback_timer.isActive():
            return
        # Do not stop/delete QMediaPlayer from inside its own error or media
        # callback. Qt multimedia backends may still be unwinding the signal.
        self._fallback_reason = reason
        self._watchdog.stop()
        self._fallback_timer.start(0)

    def _finish_native_fallback(self):
        if self._backend != 'qt' or self.info is None:
            return
        reason = self._fallback_reason
        self._fallback_reason = ''
        was_playing = self.playing
        self._activate_frames(self.current_frame)
        if was_playing:
            self._pump.play()
            self._sync_fallback_audio()
        if reason:
            self.errorText.emit(reason)

    def _check_decoder(self):
        waiting_for_video = self.playing or self._preparing
        if (self._backend == 'qt' and waiting_for_video and
                not self._got_video_frame):
            self._defer_native_fallback()

    def _on_frame_error(self, message):
        sender = self.sender()
        if sender is not None and sender is not self._source:
            return
        self.pause()
        self.errorText.emit(message)

    def _on_source_end(self, last_frame):
        """Keep playback moving when metadata overstates the available frames."""
        sender = self.sender()
        if (self._backend != 'frames' or self._pump is None or
                (sender is not None and sender is not self._source)):
            return
        if self._pump.frame > last_frame:
            self._pump.seek(last_frame, restrict_to_range=False)

    def _on_pump_frame(self, frame):
        sender = self.sender()
        if sender is not None and sender is not self._pump:
            return
        generation = self._media_generation
        source = self._source
        # FramePump.seek() acknowledges Player.seek() synchronously. That
        # callback is the SAME request: invalidating its serial causes the
        # outer seek to skip explicit PCM pause/re-anchor and leaves audio at
        # the previous record frame. Playback-driven frame changes must still
        # invalidate any earlier in-flight presentation.
        if frame != self.current_frame:
            self._frame_request_serial += 1
        request_serial = self._frame_request_serial
        previous_frame = self.current_frame
        self.current_frame = frame
        if (frame < previous_frame and self.playing and self.speed > 0 and
                self.loop_mode == playback_policy_module.LOOP and
                self._audio_player is not None):
            # When short external sound ended before the video, it is no longer
            # an audio clock; FramePump eventually wraps on its own elapsed
            # clock. Re-arm sound at that VIDEO boundary, never on audio EOF.
            self._wrap_fallback_audio(frame)
        pixmap = self._pump.pixmap() if self._pump is not None else None
        if not self._current_frames_request(generation, source, frame,
                                            request_serial):
            return
        if pixmap is not None:
            self._present_frames_pixmap(frame, pixmap, generation, source,
                                        request_serial)
        else:
            self._present_sequence_decode_failure(frame, generation, source,
                                                   request_serial)
        if not self._current_frames_request(generation, source, frame,
                                            request_serial):
            return
        self._sync_record_audio()
        # A positive audio delay leaves the audio clock paused until the video
        # reaches the offset. Check on actual displayed frames (not every Qt
        # timer tick), so the soundtrack starts once at the correct boundary.
        if (self._audio_player is not None and self._audio_ready and
                not self._audio_playing and self.playing and self.speed > 0 and
                self.audio_route.delay_ms > 0 and
                utils.frame_to_ms(frame, self.fps) >= self.audio_route.delay_ms):
            self._sync_fallback_audio()
        if (self.playback_policy == playback_policy_module.PLAY_ALL and
                self.speed > 0 and
                self.loop_mode != playback_policy_module.PING_PONG and
                self._audio_ready and self._audio_player is not None and
                self._audio_playing):
            # Play All Frames is video-master. Qt has no portable time-stretch
            # control, so correct only material drift; tiny continuous seeks
            # sound worse than allowing sub-frame timing error.
            target = self.audio_route.audio_ms_for_video_frame(
                frame, self._audio_fps)
            reported = int(self._audio_player.position())
            if (self._ack_fallback_audio_seek(reported) and
                    abs(reported - target) > 150 and
                    not self._audio_seek_timer.isActive() and
                    (not self._audio_last_seek_clock.isValid() or
                     self._audio_last_seek_clock.elapsed() >=
                     self._audio_correction_cooldown_ms)):
                self._seek_fallback_audio(target, coalesce=False)
        elif (self.playback_policy == playback_policy_module.REALTIME and
              self._audio_clock_stalled and self.playing and self.speed > 0 and
              self._audio_player is not None and self._audio_ready and
              not self._audio_stall_realign_attempted and
              self._audio_seek_target_ms is None and
              not self._audio_seek_timer.isActive()):
            target = self.audio_route.audio_ms_for_video_frame(
                frame, self._audio_fps)
            # One recovery attempt after video advances noticeably beyond the
            # frozen sound. A failing device is not flushed on every frame.
            if target - int(self._audio_player.position()) > 250:
                self._audio_stall_realign_attempted = True
                self._seek_fallback_audio(target, coalesce=False)
        if self._current_frames_request(generation, source, frame,
                                        request_serial):
            self._emit_frame(frame)

    def _on_frame_available(self, frame):
        sender = self.sender()
        if (self._backend != 'frames' or frame != self.current_frame or
                (sender is not None and sender is not self._source)):
            return
        generation = self._media_generation
        source = self._source
        request_serial = self._frame_request_serial
        pixmap = source.get(frame) if source is not None else None
        if not self._current_frames_request(generation, source, frame,
                                            request_serial):
            return
        if pixmap is not None:
            self._present_frames_pixmap(frame, pixmap, generation, source,
                                        request_serial)
        else:
            self._present_sequence_decode_failure(frame, generation, source,
                                                   request_serial)

    def _current_frames_request(self, generation, source, frame, serial):
        """A decoder result still belongs to the requested frame and source."""
        return (generation == self._media_generation and
                serial == self._frame_request_serial and
                self._backend == 'frames' and
                source is self._source and frame == self.current_frame)

    def _present_sequence_decode_failure(self, frame, generation=None,
                                          source=None, request_serial=None):
        """Distinguish an actual broken image from an authored sparse hole.

        An asynchronous movie/cache miss is not a decoder failure and must
        never clear a valid frame just because a worker is still decoding.
        """
        if generation is None:
            generation = self._media_generation
        if source is None:
            source = self._source
        if request_serial is None:
            request_serial = self._frame_request_serial
        if not self._current_frames_request(generation, source, frame,
                                            request_serial):
            return
        status_getter = getattr(source, 'frame_status', None)
        if status_getter is None:
            return
        status = status_getter(frame)
        if not self._current_frames_request(generation, source, frame,
                                            request_serial):
            return
        if not isinstance(status, dict) or status.get('kind') != 'decode_error':
            return
        reason = status.get('reason', 'Image could not be decoded')
        origin = status.get('source_frame', frame)
        identity = (self._media_generation, int(frame), str(reason))
        if getattr(self, '_last_decode_failure', None) == identity:
            return
        self._last_decode_failure = identity
        if self.playing:
            self.pause()
            if not self._current_frames_request(generation, source, frame,
                                                request_serial):
                return
        present = getattr(self.surface, 'set_unavailable_frame', None)
        if present is not None:
            present(origin, reason)
            self.framePresented.emit(frame)
        self.errorText.emit('Decode failure for source frame {}: {}'.format(
            origin, reason))

    def _present_frames_pixmap(self, frame, pixmap, generation=None,
                               source=None, request_serial=None):
        """Publish decoded pixels and matching high-precision data atomically.

        SequenceSource may expose get_payload(frame) in newer integrations.
        Viewers without precision support remain untouched; never request
        payloads for a stale frame or fabricate a separate review playhead.
        """
        if generation is None:
            generation = self._media_generation
        if source is None:
            source = self._source
        if request_serial is None:
            request_serial = self._frame_request_serial
        if not self._current_frames_request(generation, source, frame,
                                            request_serial):
            return
        set_payload = getattr(self.surface, 'set_precision_payload', None)
        getter = getattr(source, 'get_payload', None)
        # Read source-owned precision while the identity still matches. The
        # read may itself emit cache signals and synchronously replace media;
        # never publish the old pixmap on the new surface in that case.
        payload = getter(frame) if set_payload is not None and getter is not None else None
        if not self._current_frames_request(generation, source, frame,
                                            request_serial):
            return
        self._last_decode_failure = None
        self.surface.set_display(pixmap)
        if not self._current_frames_request(generation, source, frame,
                                            request_serial):
            return
        if set_payload is not None and getter is not None:
            set_payload(payload)
            if not self._current_frames_request(generation, source, frame,
                                                request_serial):
                return
        self.framePresented.emit(frame)

    def play(self):
        if self.info is None or self.playing:
            return
        if self.in_frame >= self.out_frame:
            self.seek(self.in_frame)
            self._set_playing(False)
            return
        if ((self.current_frame < self.in_frame or self.current_frame > self.out_frame or
             (self.speed >= 0 and self.current_frame >= self.out_frame) or
             (self.speed < 0 and self.current_frame <= self.in_frame))
                and self.out_frame > self.in_frame):
            self.seek(self.in_frame if self.speed >= 0 else self.out_frame)
        self._preparing = False
        self._set_playing(True)
        if self._backend == 'qt':
            # The latest drag target must land before resuming audio/video;
            # otherwise Qt briefly plays the previous decoder position.
            self._flush_native_seek()
            self._got_video_frame = False
            self._watchdog.start(2500)
            self._qplayer.play()
        elif self._pump is not None:
            self._pump.play()
            self._flush_fallback_audio_seek()
            self._sync_fallback_audio()
            self._sync_record_audio()

    def _on_pump_stopped(self):
        sender = self.sender()
        if sender is None or sender is self._pump:
            self._set_playing(False)
            self._sync_fallback_audio()
            self._sync_record_audio()

    def _on_pump_buffering(self, buffering):
        """Keep audio locked to video across startup and cache refills."""
        sender = self.sender()
        if sender is None or sender is self._pump:
            self._sync_fallback_audio()
            self._sync_record_audio()

    def pause(self):
        if self._backend == 'qt' and self._qplayer is not None:
            self._qplayer.pause()
            if (self._native_presented_us is not None and
                    self._native_seek_clock.isValid() and
                    self._native_seek_clock.elapsed() >= 600):
                self._native_seek_latch_us = None
        elif self._pump is not None:
            self._pump.pause()
            self._sync_fallback_audio()
        self._watchdog.stop()
        self._preparing = False
        self._set_playing(False)
        self._sync_record_audio()

    def toggle(self):
        if self.playing:
            self.pause()
        else:
            self.play()

    def _pause_at(self, frame):
        if self._backend == 'qt' and self._qplayer is not None:
            self._qplayer.pause()
        self._watchdog.stop()
        self._set_playing(False)
        self.current_frame = int(utils.clamp(frame, 0, self.frame_count - 1))
        self._emit_frame(self.current_frame)

    def seek(self, frame):
        if self.info is None:
            return
        # Manual navigation may inspect the whole source outside the marked
        # playback/export range. Playback itself still starts and loops in-range.
        frame = int(utils.clamp(frame, 0, self.frame_count - 1))
        changed = frame != self.current_frame
        if not changed:
            # Maya's deferred timeline observer may reissue the same frame on
            # each poll. Never amplify that into decoder seeks or clock resets.
            # But explicit same-frame refresh is also used by window's live
            # comparison changes: repaint the existing cached image/payload.
            if self._backend == 'frames' and self._source is not None:
                self._source.ensure(frame)
                self._on_frame_available(frame)
            return
        self._frame_request_serial += 1
        generation = self._media_generation
        source = self._source
        request_serial = self._frame_request_serial
        self.current_frame = frame
        if self._backend == 'qt' and self._qplayer is not None:
            self._loop_seeking = False
            pos = int(utils.frame_to_ms(frame, self.fps))
            self._native_seek_serial += 1
            self._seek_target_us = self._frame_to_us(frame)
            self._native_seek_latch_us = self._seek_target_us
            self._native_presented_us = None
            self._native_seek_clock.restart()
            if self._pending_position is not None:
                self._pending_position = pos
            else:
                # Dragging can generate hundreds of Qt seek requests/second;
                # collapsing to one latest-target seek avoids decoder thrash.
                self._native_requested_position = pos
                if not self._native_scrub_timer.isActive():
                    self._native_scrub_timer.start()
        elif self._pump is not None:
            # Manual seek replaces the old stalled playhead entirely. A fresh
            # soundtrack target must not inherit quarantine from that frame.
            self._audio_clock_stalled = False
            self._audio_stall_realign_attempted = False
            self._audio_last_progress_ms = None
            self._pump.seek(frame, restrict_to_range=False)
            if not self._current_frames_request(generation, source, frame,
                                                request_serial):
                return
            self._sync_fallback_audio(force=changed)
            # Explicit video-master seek resets the device's queued PCM tail.
            if self._record_audio_transport is not None:
                self._record_audio_transport.pause()
                self._sync_record_audio()
        if (self._media_generation == generation and
                self._frame_request_serial == request_serial and
                frame == self.current_frame):
            self._emit_frame(frame)

    def _flush_native_seek(self):
        self._native_scrub_timer.stop()
        pos = self._native_requested_position
        self._native_requested_position = None
        if pos is None or self._backend != 'qt' or self._qplayer is None:
            return
        if self._pending_position is not None:
            self._pending_position = pos
        else:
            self._qplayer.setPosition(int(pos))

    def set_range(self, in_frame, out_frame):
        lo = int(utils.clamp(min(in_frame, out_frame), 0, self.frame_count - 1))
        hi = int(utils.clamp(max(in_frame, out_frame), lo, self.frame_count - 1))
        self.in_frame, self.out_frame = lo, hi
        if self._pump is not None:
            self._pump.in_frame = lo
            self._pump.out_frame = hi
        if self._source is not None:
            if hasattr(self._source, 'set_playback_range'):
                self._source.set_playback_range(lo, hi)
            self._source.set_loop_start(lo)
        # Preserve out-of-range inspection while paused. Changing trim edges
        # around the current playback position does not require seeking audio.
        if self.playing and not lo <= self.current_frame <= hi:
            self.seek(lo if self.speed >= 0 else hi)

    def set_loop(self, enabled):
        self.set_loop_mode(playback_policy_module.LOOP if enabled
                           else playback_policy_module.ONCE)

    def set_loop_mode(self, mode):
        """Set Once, Loop, or Ping-Pong transport without changing the playhead."""
        self.loop_mode = playback_policy_module.validate_loop_mode(mode)
        self.loop = mode != playback_policy_module.ONCE
        if self._pump is not None:
            self._pump.set_loop_mode(mode)
        if self._source is not None:
            if hasattr(self._source, 'set_loop_mode'):
                self._source.set_loop_mode(mode)
            elif hasattr(self._source, 'set_loop'):
                self._source.set_loop(mode != playback_policy_module.ONCE)
        if mode == playback_policy_module.PING_PONG and self._backend == 'qt':
            was_playing = self.playing
            self._activate_frames(self.current_frame)
            if was_playing and self._pump is not None:
                self._pump.play()
        if self._pump is not None:
            self._pump.set_external_clock(
                self._fallback_audio_position if self.speed > 0 and
                mode != playback_policy_module.PING_PONG and
                self._audio_player is not None else None,
                self._wrap_fallback_audio)
        self._apply_fallback_audio_volume()
        self._sync_fallback_audio()
        self._sync_record_audio()

    def set_speed(self, speed):
        speed = float(speed)
        previous_speed = self.speed
        self.speed = (0.0 if speed == 0 else
                      (-1 if speed < 0 else 1) * max(0.01, abs(speed)))
        if self.speed <= 0 and self._backend == 'qt':
            was_playing = self.playing
            self._activate_frames(self.current_frame)
            if was_playing and self._pump is not None:
                self._pump.play()
        if self._backend == 'qt' and self._qplayer is not None:
            self._qplayer.setPlaybackRate(max(0.01, self.speed))
        elif self._pump is not None:
            self._pump.set_speed(self.speed)
            self._pump.set_external_clock(
                self._fallback_audio_position if self.speed > 0 and
                self.loop_mode != playback_policy_module.PING_PONG and
                self._audio_player is not None else None,
                self._wrap_fallback_audio)
        if self._audio_player is not None:
            self._audio_player.setPlaybackRate(max(0.01, abs(self.speed)))
        self._apply_fallback_audio_volume()
        self._sync_fallback_audio(force=(previous_speed <= 0 < self.speed))
        self._sync_record_audio()
        if self.speed == 0.0 and self.playing:
            self.pause()

    def set_playback_policy(self, policy):
        """Select realtime frame dropping or sequential play-all playback."""
        playback_policy_module.validate_playback_policy(policy)
        if policy == self.playback_policy:
            return
        was_playing = self.playing
        frame = self.current_frame
        self.pause()
        self.playback_policy = policy
        if self._pump is not None:
            self._pump.set_playback_policy(policy)
        elif policy == 'play_all' and self.info is not None:
            # Native multimedia owns its clock and may skip frames. Move to the
            # deterministic frame backend when every frame was requested.
            self._activate_frames(frame)
        self._sync_fallback_audio()
        self._sync_record_audio()
        if was_playing:
            self.play()

    def set_cache_mode(self, mode):
        from .cache_policy import CACHE_MODES
        if mode not in CACHE_MODES:
            raise ValueError('Unknown cache mode: {}'.format(mode))
        self.cache_policy.cache_mode = mode
        if self._source is not None and hasattr(self._source, 'set_cache_mode'):
            self._source.set_cache_mode(mode)

    def set_audio_route(self, route):
        """Select the one audible source while retaining the review playhead.

        Audio is recreated through existing Qt/FFmpeg transport. Video remains
        the only playhead and no additional timers or player clocks are added.
        """
        route = route if isinstance(route, AudioRoute) else AudioRoute.from_dict(route)
        if self._record_audio_transport is not None and (
                route.external_path or route.delay_ms != 0 or
                route.to_dict() != self.audio_route.to_dict()):
            # Never mix a new independent route/player with record PCM.
            self.disable_record_audio()
        if route.to_dict() == self.audio_route.to_dict():
            return
        previous_route = self.audio_route
        self.audio_route = route
        if self.info is None:
            return
        if self.info.path == '<composition>':
            if route.external_path and self._backend == 'frames':
                self._build_fallback_audio(route.external_path, self.current_frame)
                if self._pump is not None:
                    self._pump.set_external_clock(self._fallback_audio_position,
                                                  self._wrap_fallback_audio)
            else:
                self._stop_fallback_audio()
                if self._pump is not None:
                    self._pump.set_external_clock(None)
            self._sync_fallback_audio(force=True)
            return
        if self._backend == 'qt' and (route.external_path or route.delay_ms):
            # One native QMediaPlayer owns both A/V clocks. It cannot add a
            # signed offset or select an external soundtrack independently.
            # Move only the decoder backend; preserve video range, playhead,
            # loop direction and current transport without reloading the UI.
            was_playing = self.playing
            self._activate_frames(self.current_frame)
            if was_playing and self._pump is not None:
                self._pump.play()
            self._sync_fallback_audio(force=True)
            return
        if self._backend == 'frames':
            track_changed = route.stream_index != previous_route.stream_index
            if route.external_path != previous_route.external_path:
                path = route.external_path or (
                    '' if self.info.is_sequence else self.info.path)
                if path:
                    self._build_fallback_audio(path, self.current_frame)
                else:
                    self._stop_fallback_audio()
                if self._pump is not None:
                    self._pump.set_external_clock(
                        self._fallback_audio_position if self._audio_player is not None
                        else None, self._wrap_fallback_audio)
            elif track_changed:
                self._on_audio_tracks_changed()
            if (route.delay_ms != previous_route.delay_ms or
                    track_changed and self._audio_track_state == 'applied'):
                # Invalidate the old offset-dependent heartbeat and EOF state
                # without recreating a healthy Qt audio decoder on every drag.
                self._audio_ended = False
                self._audio_clock_floor_ms = None
                self._audio_clock_stalled = False
                self._audio_stall_realign_attempted = False
                self._audio_last_progress_ms = None
            self._sync_fallback_audio(force=(
                route.delay_ms != previous_route.delay_ms or
                route.external_path != previous_route.external_path or
                track_changed and self._audio_track_state == 'applied'))
        elif self._backend == 'qt':
            self._on_audio_tracks_changed()

    def audio_route_capabilities(self):
        """Read-only backend *capabilities*, not a promise of device accuracy.

        Qt5 audioTrack selection and DSP channel matrices have no supported
        API in Maya. Qt6 tracks can be selected, but only after discovery.
        Hardware latency, buffer compensation and exact device clock timing
        are neither observable nor calibrated in this application.
        """
        native = self._backend == 'qt'
        decoder = self._qplayer if native else self._audio_player
        return {'backend': 'native' if native else
                'fallback' if self._backend == 'frames' else 'unloaded',
                'signed_delay_supported': not native and self._backend == 'frames',
                'delay_active': bool(not native and decoder is not None and
                                     self.audio_route.delay_ms),
                'selected_audio_track_supported': bool(
                    decoder is not None and
                    callable(getattr(decoder, 'audioTracks', None)) and
                    callable(getattr(decoder, 'setActiveAudioTrack', None))),
                'programmable_channel_matrix_supported': False,
                'hardware_latency_calibrated': False,
                'active_audio_path': (self.info.path if native and self.info is not None
                                      else self._audio_active_path),
                'track_state': self._audio_track_state}

    def audio_telemetry(self):
        # Native QMediaPlayer shares its video/audio clock. Only the separate
        # fallback player has an independently measurable A/V offset.
        native = self._backend == 'qt' and self._qplayer is not None
        audio = self._qplayer if native else self._audio_player
        ready = bool(audio is not None and (native or self._audio_ready))
        position = int(audio.position()) if ready else None
        active_track = None
        if audio is not None:
            getter = getattr(audio, 'activeAudioTrack', None)
            if callable(getter):
                try:
                    active_track = int(getter())
                except (TypeError, ValueError, RuntimeError):
                    pass
        # A queued seek or paused sound is not a usable drift measurement.
        clock_valid = bool(not native and position is not None and
                           self._audio_playing and
                           self.playing and self.speed > 0 and
                           not self._audio_seek_timer.isActive() and
                           self._audio_seek_target_ms is None and
                           not self._audio_clock_stalled and
                           not self._audio_ended)
        clock_reason = ('native_shared_clock' if native else
                        'unavailable' if not ready else
                        'ended' if self._audio_ended else
                        'seek_pending' if (self._audio_seek_target_ms is not None or
                                           self._audio_seek_timer.isActive()) else
                        'stalled' if self._audio_clock_stalled else
                        'paused' if not self._audio_playing or not self.playing else
                        'active')
        return {'route': self.audio_route.to_dict(),
                'backend': 'native' if native else 'fallback',
                'using_embedded_fallback': self._audio_using_embedded_fallback,
                'capabilities': self.audio_route_capabilities(),
                'effective_output_layout': 'source',
                'output_layout_warning': (
                    'Qt multimedia does not support a programmable channel downmix; '
                    'the output device/backend controls speaker routing.'
                    if self.audio_route.output_layout != 'source' else ''),
                'ready': ready,
                'playing': bool(self.playing if native else self._audio_playing),
                'track_state': self._audio_track_state,
                'active_audio_track': active_track,
                'track_warning': self._audio_track_warning,
                'clock_valid': clock_valid,
                'clock_stalled': self._audio_clock_stalled,
                'clock_reason': clock_reason,
                'audio_position_ms': position,
                'drift_ms': (drift_ms(self.current_frame, self.fps, position,
                                       self.audio_route) if clock_valid else None)}

    def diagnostics_snapshot(self):
        """Small support/diagnostic snapshot safe to request during playback."""
        source = self._source
        cache = (source.cache_snapshot()
                 if source is not None and hasattr(source, 'cache_snapshot')
                 else None)
        pump = (self._pump.stats_snapshot()
                if self._pump is not None else None)
        return dict(backend=self._backend, policy=self.playback_policy,
                    fps=self.fps, frame=self.current_frame,
                    playing=self.playing, speed=self.speed,
                    loop_mode=self.loop_mode, cache=cache, playback=pump,
                    audio=(dict(self.info.audio) if self.info is not None
                           else {}),
                    audio_routing=self.audio_telemetry(),
                    audio_description=(self.info.audio_description
                                       if self.info is not None else 'No media'),
                    cache_policy=self.cache_policy.as_dict())

    def set_volume(self, value):
        self.volume = float(utils.clamp(value, 0.0, 1.0))
        if self._audio is not None:
            self._audio.setVolume(0.0 if self.muted else self.volume)
        elif self._qplayer is not None and hasattr(self._qplayer, 'setVolume'):
            self._qplayer.setVolume(int(round(self.volume * 100)))
        self._apply_fallback_audio_volume()
        if self._record_audio_transport is not None:
            self._record_audio_transport.set_gain(self.volume, self.muted)

    def set_muted(self, muted):
        self.muted = bool(muted)
        if self._audio is not None:
            self._audio.setVolume(0.0 if self.muted else self.volume)
        elif self._qplayer is not None and hasattr(self._qplayer, 'setMuted'):
            self._qplayer.setMuted(self.muted)
        self._apply_fallback_audio_volume()
        if self._record_audio_transport is not None:
            self._record_audio_transport.set_gain(self.volume, self.muted)

    def _set_playing(self, value):
        if value != self.playing:
            self.playing = value
            self.playingChanged.emit(value)

    def _emit_frame(self, frame):
        if frame != self._last_emitted:
            self._last_emitted = frame
            self.frameChanged.emit(frame)

    def _frame_to_us(self, frame):
        """Frame index to microseconds, matching QVideoFrame.startTime() units."""
        return int(round(frame / self.fps * 1000000))

    def _stop_qplayer(self):
        self._native_scrub_timer.stop()
        self._native_requested_position = None
        self._unbind_media_signals('native')
        if self._qplayer is not None:
            try:
                self._qplayer.stop()
                self._qplayer.setVideoOutput(None)
            except (RuntimeError, TypeError):
                pass
            self._qplayer.deleteLater()
            self._qplayer = None
        if self._sink is not None:
            self._sink.deleteLater()
            self._sink = None
        if self._audio is not None:
            self._audio.deleteLater()
            self._audio = None

    def stop_all(self, wait=True):
        self._media_generation += 1
        self._frame_request_serial += 1
        self.disable_record_audio()
        self._watchdog.stop()
        self._fallback_timer.stop()
        self._fallback_reason = ''
        self._preparing = False
        self._backend = None
        self._seek_target_us = None
        self._native_seek_latch_us = None
        self._native_presented_us = None
        self._native_seek_serial += 1
        self._native_seek_clock.invalidate()
        self._set_playing(False)
        self._stop_fallback_audio()
        self._stop_qplayer()

        # Drop references first, then detach signals, so a frame or cache update
        # already queued from the old pump/source can never reach the player (or
        # the surface) after a new clip has started loading.
        pump = self._pump
        self._pump = None
        self._unbind_media_signals('pump')
        if pump is not None:
            pump.pause()
            pump.deleteLater()

        stopped = True
        source = self._source
        self._source = None
        self._unbind_media_signals('source')
        # Retry older sources across subsequent stop/load/reload attempts. The
        # previous shutdown may have returned False while FFmpeg was exiting.
        for pending_source in tuple(_PENDING_SOURCE_SHUTDOWNS):
            if pending_source.shutdown() is False:
                stopped = False
            else:
                _PENDING_SOURCE_SHUTDOWNS.discard(pending_source)
                pending_source.deleteLater()
        if source is not None:
            if source.shutdown() is False:
                # Preserve a strong reference beyond the lifetime of the Maya
                # window; a later stop_all() will retry and delete it only when
                # every decode thread is actually finished.
                _PENDING_SOURCE_SHUTDOWNS.add(source)
                app = QtCore.QCoreApplication.instance()
                if app is not None:
                    source.setParent(app)
                stopped = False
            else:
                source.deleteLater()

        self.cacheChanged.emit([], None)
        self.bufferProgressChanged.emit(0)
        return stopped

    def shutdown(self):
        return self.stop_all(wait=True)
