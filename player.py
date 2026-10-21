# -*- coding: utf-8 -*-
"""Stable, frame-addressed playback with a single presentation surface.

Qt6 uses native decoding through QVideoSink, copying decoded frames to the
same pixmap item used by stills and image sequences. If that decoder does not
deliver a frame, or multimedia is unavailable (including Maya's Qt5 builds),
playback moves to the bounded asynchronous FFmpeg frame source.

The visible image is never cleared during pause, seek, trim-loop or buffering.
"""
from __future__ import absolute_import

from .qtcompat import (QtCore, QtGui, Signal, HAVE_MULTIMEDIA,
                       QMediaPlayer, QMediaContent, QVideoSink, QAudioOutput)
from . import utils
from .frames import SequenceSource, FramePump
from .rolling_cache import VideoFrameSource


# Keep a source alive if a decoder thread misses the shutdown deadline. Maya
# may destroy its owning window even when callers correctly return False, and
# abandoning the last Python reference can otherwise dispose of a live QThread.
_PENDING_SOURCE_SHUTDOWNS = set()


class Player(QtCore.QObject):
    frameChanged = Signal(int)
    playingChanged = Signal(bool)
    loaded = Signal()
    cacheChanged = Signal(object, object)
    bufferProgressChanged = Signal(int)
    errorText = Signal(str)

    def __init__(self, ffmpeg, surface, parent=None):
        QtCore.QObject.__init__(self, parent)
        self._ffmpeg = ffmpeg
        self.surface = surface

        self.info = None
        self.fps = 24.0
        self.frame_count = 1
        self.current_frame = 0
        self.in_frame = 0
        self.out_frame = 0
        self.loop = False
        self.speed = 1.0
        self.volume = 0.5
        self.muted = False
        self.playing = False

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

    def load(self, info):
        self.stop_all(wait=True)
        self.info = info
        self.fps = info.fps
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

        if not info.is_sequence and HAVE_MULTIMEDIA and QVideoSink is not None:
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

    def _build_qplayer(self, path):
        self._sink = QVideoSink(self)
        self._sink.videoFrameChanged.connect(self._on_video_frame)
        self._qplayer = QMediaPlayer(self)
        self._qplayer.setVideoOutput(self._sink)
        if QAudioOutput is not None:
            self._audio = QAudioOutput(self)
            self._audio.setVolume(0.0 if self.muted else self.volume)
            self._qplayer.setAudioOutput(self._audio)
        self._qplayer.positionChanged.connect(self._on_position)
        self._qplayer.mediaStatusChanged.connect(self._on_media_status)
        if hasattr(self._qplayer, 'bufferProgressChanged'):
            self._qplayer.bufferProgressChanged.connect(self._on_buffer_progress)
        if hasattr(self._qplayer, 'errorOccurred'):
            self._qplayer.errorOccurred.connect(self._on_decoder_error)
        self._qplayer.setSource(QtCore.QUrl.fromLocalFile(path))
        self._qplayer.setPlaybackRate(self.speed)
        if self._audio is None:
            if hasattr(self._qplayer, 'setVolume'):
                self._qplayer.setVolume(int(round(self.volume * 100)))
            if hasattr(self._qplayer, 'setMuted'):
                self._qplayer.setMuted(self.muted)

    def _activate_frames(self, frame):
        """Create a fresh fallback after destroying the old decoder."""
        self._watchdog.stop()
        self._preparing = False
        self._seek_target_us = None
        self._backend = 'frames'
        self._stop_qplayer()
        info = self.info
        if not info.is_sequence:
            self._build_fallback_audio(info.path, frame)
        if info.is_sequence:
            self._source = SequenceSource(info.sequence_files, self)
        else:
            self._source = VideoFrameSource(
                self._ffmpeg, info.path, info.fps, info.frame_count, self)
            self._source.errorText.connect(self._on_frame_error)
            self._source.endReached.connect(self._on_source_end)
        self._source.frameAvailable.connect(self._on_frame_available)
        self._source.cacheChanged.connect(self.cacheChanged.emit)
        self.cacheChanged.emit([], None)
        self.bufferProgressChanged.emit(0)
        # A native-decoder fallback can happen after trim/loop were already
        # configured. Give the new cache the same range and loop direction so
        # it prefetches the opposite edge before the first wrap.
        if hasattr(self._source, 'set_playback_range'):
            self._source.set_playback_range(self.in_frame, self.out_frame)
        self._source.set_loop_start(self.in_frame)
        if hasattr(self._source, 'set_loop'):
            self._source.set_loop(self.loop)
        self._pump = FramePump(self._source, self.fps, self.frame_count, self)
        if self._audio_player is not None:
            self._pump.set_external_clock(self._fallback_audio_position,
                                          self._wrap_fallback_audio)
        self._pump.in_frame = self.in_frame
        self._pump.out_frame = self.out_frame
        self._pump.loop = self.loop
        self._pump.set_speed(self.speed)
        self._pump.frameChanged.connect(self._on_pump_frame)
        self._pump.stopped.connect(self._on_pump_stopped)
        self._pump.seek(frame, restrict_to_range=False)
        self._on_frame_available(frame)

    def _build_fallback_audio(self, path, frame):
        """Play the soundtrack while FFmpeg supplies video frames.

        Qt5 has QMediaPlayer audio even though it lacks Qt6's QVideoSink.
        Keep this player separate from the native video decoder so a video
        decoding failure does not silence otherwise playable audio.
        """
        self._stop_fallback_audio()
        if not HAVE_MULTIMEDIA or QMediaPlayer is None:
            return
        try:
            audio = QMediaPlayer(self)
            self._audio_player = audio
            audio.mediaStatusChanged.connect(self._on_audio_status)
            if hasattr(audio, 'errorOccurred'):
                audio.errorOccurred.connect(self._on_audio_error)
            elif hasattr(audio, 'error'):
                audio.error.connect(self._on_audio_error)
            if QAudioOutput is not None and hasattr(audio, 'setAudioOutput'):
                self._audio_output = QAudioOutput(self)
                audio.setAudioOutput(self._audio_output)
            self._apply_fallback_audio_volume()
            audio.setPlaybackRate(self.speed)
            if hasattr(audio, 'setNotifyInterval'):
                audio.setNotifyInterval(40)
            self._audio_pending_position = int(utils.frame_to_ms(frame, self.fps))
            url = QtCore.QUrl.fromLocalFile(path)
            if hasattr(audio, 'setSource'):
                audio.setSource(url)
            else:
                audio.setMedia(QMediaContent(url))
        except Exception as exc:
            self._stop_fallback_audio()
            self.errorText.emit('Audio playback unavailable: {}'.format(exc))

    def _apply_fallback_audio_volume(self):
        effective_volume = 0.0 if self.muted else self.volume
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
            if self.loop and self.playing and self._pump is not None:
                # Qt backends can crash if we seek/play QMediaPlayer from
                # inside its own EndOfMedia notification.
                self._audio_eof_timer.start(0)
            return
        if status in (statuses.LoadedMedia, statuses.BufferedMedia):
            first_ready = not self._audio_ready
            self._audio_ready = True
            if self._audio_pending_position is not None:
                target = self._audio_pending_position
                self._audio_pending_position = None
                self._audio_seek_target_ms = target
                self._audio_seek_clock.restart()
                self._audio_player.setPosition(target)
            if first_ready:
                self._sync_fallback_audio()

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
        self._stop_fallback_audio()
        self.errorText.emit('Audio playback unavailable: ' + message)

    def _finish_audio_eof_loop(self):
        if (self._backend == 'frames' and self._audio_player is not None and
                self.playing and self.loop and self._pump is not None):
            self._wrap_fallback_audio(self.in_frame)
            self._pump.seek(self.in_frame)

    def _sync_fallback_audio(self, force=False):
        audio = self._audio_player
        if self._backend != 'frames' or audio is None or self._pump is None:
            return
        target = int(utils.frame_to_ms(self.current_frame, self.fps))
        if not self._audio_ready:
            self._audio_pending_position = target
            return
        if force:
            # Coalesce the high-frequency position changes sent while a user
            # drags the timeline. Qt multimedia backend seeks are expensive
            # and rapidly interrupting them can destabilize Maya's Qt5 codecs.
            self._audio_seek_target_ms = target
            self._audio_seek_clock.restart()
            self._audio_requested_position = target
            self._audio_seek_timer.start()
        should_play = self.playing and self._pump.is_playing()
        if should_play != self._audio_playing:
            self._audio_playing = should_play
            if should_play:
                audio.play()
            else:
                audio.pause()

    def _flush_fallback_audio_seek(self):
        target = self._audio_requested_position
        self._audio_seek_timer.stop()
        self._audio_requested_position = None
        if target is None:
            return
        if self._audio_player is not None and self._audio_ready:
            self._audio_seek_target_ms = target
            self._audio_seek_clock.restart()
            self._audio_player.setPosition(target)
        else:
            self._audio_pending_position = target

    def _fallback_audio_position(self):
        """Return the free-running soundtrack clock, even while video buffers."""
        if not self._audio_ready or self._audio_player is None or not self.playing:
            return None
        position = int(self._audio_player.position())
        target = self._audio_seek_target_ms
        if target is not None:
            if (abs(position - target) > 120 and
                    self._audio_seek_clock.elapsed() < 800):
                return target
            self._audio_seek_target_ms = None
        return position

    def _wrap_fallback_audio(self, frame):
        audio = self._audio_player
        if audio is None:
            return
        self._audio_eof_timer.stop()
        target = int(utils.frame_to_ms(frame, self.fps))
        if not self._audio_ready:
            self._audio_pending_position = target
            return
        self._audio_seek_timer.stop()
        self._audio_requested_position = None
        self._audio_seek_target_ms = target
        self._audio_seek_clock.restart()
        audio.setPosition(target)
        if self.playing and not self._audio_playing:
            self._audio_playing = True
            audio.play()

    def _stop_fallback_audio(self):
        audio = self._audio_player
        output = self._audio_output
        self._audio_player = None
        self._audio_output = None
        self._audio_ready = False
        self._audio_playing = False
        self._audio_pending_position = None
        self._audio_requested_position = None
        self._audio_seek_target_ms = None
        self._audio_seek_clock.invalidate()
        self._audio_seek_timer.stop()
        self._audio_eof_timer.stop()
        self._audio_error_timer.stop()
        self._audio_error_source = None
        self._audio_error_message = ''
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
        try:
            image = video_frame.toImage()
            if image.isNull():
                return
            if self._seek_target_us is not None:
                timestamp = video_frame.startTime()
                if timestamp >= 0:
                    # Frames from the old decoder queue must never overwrite the
                    # last good frame during a seek, especially at loop wrap.
                    if abs(timestamp - self._seek_target_us) > 100000:
                        return
                self._seek_target_us = None
            self.surface.set_display(QtGui.QPixmap.fromImage(image))
        except Exception:
            return
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
        if self._loop_seeking:
            if frame <= self.in_frame + 1:
                self._loop_seeking = False
            else:
                return
        if self.playing and frame >= self.out_frame:
            if self.loop and self.out_frame > self.in_frame:
                self._loop_seeking = True
                self._seek_target_us = self._frame_to_us(self.in_frame)
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
        if loaded and self._pending_position is not None:
            self._qplayer.setPosition(int(self._pending_position))
            self._pending_position = None
        if ended:
            if self.loop and self.playing:
                self._loop_seeking = False
                self._seek_target_us = self._frame_to_us(self.in_frame)
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
        if self._backend == 'qt' and (self.playing or self._preparing) and not self._got_video_frame:
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
        self.current_frame = frame
        pixmap = self._pump.pixmap() if self._pump is not None else None
        if pixmap is not None:
            self.surface.set_display(pixmap)
        self._emit_frame(frame)

    def _on_frame_available(self, frame):
        sender = self.sender()
        if (self._backend != 'frames' or frame != self.current_frame or
                (sender is not None and sender is not self._source)):
            return
        pixmap = self._source.get(frame) if self._source is not None else None
        if pixmap is not None:
            self.surface.set_display(pixmap)

    def play(self):
        if self.info is None or self.playing:
            return
        if self.in_frame >= self.out_frame:
            self.seek(self.in_frame)
            self._set_playing(False)
            return
        if ((self.current_frame < self.in_frame or self.current_frame >= self.out_frame)
                and self.out_frame > self.in_frame):
            self.seek(self.in_frame)
        self._preparing = False
        self._set_playing(True)
        if self._backend == 'qt':
            self._got_video_frame = False
            self._watchdog.start(2500)
            self._qplayer.play()
        elif self._pump is not None:
            self._pump.play()
            self._flush_fallback_audio_seek()
            self._sync_fallback_audio()

    def _on_pump_stopped(self):
        sender = self.sender()
        if sender is None or sender is self._pump:
            self._set_playing(False)
            self._sync_fallback_audio()

    def pause(self):
        if self._backend == 'qt' and self._qplayer is not None:
            self._qplayer.pause()
        elif self._pump is not None:
            self._pump.pause()
            self._sync_fallback_audio()
        self._watchdog.stop()
        self._preparing = False
        self._set_playing(False)

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
        self.current_frame = frame
        if self._backend == 'qt' and self._qplayer is not None:
            self._loop_seeking = False
            pos = int(utils.frame_to_ms(frame, self.fps))
            self._seek_target_us = self._frame_to_us(frame)
            if self._pending_position is not None:
                self._pending_position = pos
            else:
                self._qplayer.setPosition(pos)
        elif self._pump is not None:
            self._pump.seek(frame, restrict_to_range=False)
            self._sync_fallback_audio(force=changed)
        self._emit_frame(frame)

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
            self.seek(lo)

    def set_loop(self, enabled):
        self.loop = bool(enabled)
        if self._pump is not None:
            self._pump.loop = self.loop
        if self._source is not None and hasattr(self._source, 'set_loop'):
            self._source.set_loop(self.loop)

    def set_speed(self, speed):
        self.speed = max(0.01, float(speed))
        if self._backend == 'qt' and self._qplayer is not None:
            self._qplayer.setPlaybackRate(self.speed)
        elif self._pump is not None:
            self._pump.set_speed(self.speed)
        if self._audio_player is not None:
            self._audio_player.setPlaybackRate(self.speed)

    def set_volume(self, value):
        self.volume = float(utils.clamp(value, 0.0, 1.0))
        if self._audio is not None:
            self._audio.setVolume(0.0 if self.muted else self.volume)
        elif self._qplayer is not None and hasattr(self._qplayer, 'setVolume'):
            self._qplayer.setVolume(int(round(self.volume * 100)))
        self._apply_fallback_audio_volume()

    def set_muted(self, muted):
        self.muted = bool(muted)
        if self._audio is not None:
            self._audio.setVolume(0.0 if self.muted else self.volume)
        elif self._qplayer is not None and hasattr(self._qplayer, 'setMuted'):
            self._qplayer.setMuted(self.muted)
        self._apply_fallback_audio_volume()

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
        self._watchdog.stop()
        self._fallback_timer.stop()
        self._fallback_reason = ''
        self._preparing = False
        self._backend = None
        self._seek_target_us = None
        self._set_playing(False)
        self._stop_fallback_audio()
        self._stop_qplayer()

        # Drop references first, then detach signals, so a frame or cache update
        # already queued from the old pump/source can never reach the player (or
        # the surface) after a new clip has started loading.
        pump = self._pump
        self._pump = None
        if pump is not None:
            try:
                pump.frameChanged.disconnect(self._on_pump_frame)
                pump.stopped.disconnect(self._on_pump_stopped)
            except (RuntimeError, TypeError):
                pass
            pump.pause()
            pump.deleteLater()

        stopped = True
        source = self._source
        self._source = None
        # Retry older sources across subsequent stop/load/reload attempts. The
        # previous shutdown may have returned False while FFmpeg was exiting.
        for pending_source in tuple(_PENDING_SOURCE_SHUTDOWNS):
            if pending_source.shutdown() is False:
                stopped = False
            else:
                _PENDING_SOURCE_SHUTDOWNS.discard(pending_source)
                pending_source.deleteLater()
        if source is not None:
            try:
                source.frameAvailable.disconnect(self._on_frame_available)
                source.cacheChanged.disconnect(self.cacheChanged.emit)
                if hasattr(source, 'errorText'):
                    source.errorText.disconnect(self._on_frame_error)
                if hasattr(source, 'endReached'):
                    source.endReached.disconnect(self._on_source_end)
            except (RuntimeError, TypeError):
                pass
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
