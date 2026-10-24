# -*- coding: utf-8 -*-
"""One opt-in, bounded Qt5 PCM audition of a verified v4 record timeline.

All FFmpeg decoding, source hashes, selected PCM/stream checks and assembly
run in a cancellable QThread, never in Maya's GUI thread. The only playable
bytes come from exporting.record_audio._record_pcm. Qt5 QAudioOutput uses one
QBuffer of proven 48kHz signed little-endian stereo samples, with a single
video-owned playhead. No sidecars, published files, autosaves, or hidden
independent record clock. QAudioOutput does NOT prove hardware sample-accurate
onset, output latency, clock lock or audible speakers.
"""
from __future__ import absolute_import

from fractions import Fraction
import hashlib
import os
import threading
import time

from ..core.qtcompat import QtCore, Signal


MAX_RECORD_FRAMES = 120
MAX_RECORD_SAMPLES = 241000
MAX_PCM_BYTES = MAX_RECORD_SAMPLES * 4
MAX_DECODED_SOURCE_SAMPLES = 480000  # 10 s per actual selected track
MAX_SOURCE_FILE_BYTES = 256 * 1024 * 1024
_LIVE_THREADS = set()  # QThread object survives closing the owning Maya window


def sample_offset(frame, fps=(24000, 1001)):
    """Exact absolute record frame -> signed stereo 48k PCM sample offset."""
    if type(frame) is not int or frame < 0:
        raise ValueError('Record PCM seek requires nonnegative integral frame.')
    fps = Fraction(*fps) if isinstance(fps, (tuple, list)) else Fraction(fps)
    if fps != Fraction(24000, 1001):
        raise ValueError('Record PCM audition requires 24000/1001 fps.')
    return (Fraction(frame * 48000, 1) / fps).__floor__()


def _frozen_intent(spec):
    """Cheap synchronous gate; heavyweight v4 proof is WORKER-ONLY."""
    from ..exporting.spec import ExportSpec
    if not isinstance(spec, ExportSpec):
        raise ValueError('Record audition needs immutable ExportSpec.')
    ctx = spec.to_context()
    record = ctx.get('composite_export')
    if (not isinstance(record, dict) or record.get('version') != 4 or
            record.get('mode') != 'sequence' or
            record.get('audio_policy') != 'wav_pcm_v1' or
            not isinstance(record.get('record_audio'), dict) or
            spec.start != 0 or not 1 <= spec.count <= MAX_RECORD_FRAMES or
            spec.end != spec.count - 1 or spec.sequence is not True or
            spec.stage not in ('working', 'display') or
            Fraction(spec.fps) != Fraction(24000, 1001) or
            record['record_audio'].get('sample_rate') != 48000 or
            record['record_audio'].get('channels') != 2 or
            record['record_audio'].get('sample_format') != 's16le'):
        raise ValueError('Audition needs complete 0-origin v4 24000/1001 record'
                         ' with independently frozen stereo 48k PCM routing.')
    if sample_offset(spec.count) * 4 > MAX_PCM_BYTES:
        raise ValueError('Record PCM exceeds bounded audition memory.')
    members = record.get('members')
    routes = record['record_audio'].get('routes')
    if (not isinstance(members, (list, tuple)) or
            not isinstance(routes, list) or
            len(members) != len(routes) or not 2 <= len(routes) <= 8):
        raise ValueError('Record audition requires 2..8 frozen audio routes.')
    for member, route in zip(members, routes):
        if not isinstance(member, dict) or not isinstance(route, dict):
            raise ValueError('Invalid frozen record audio member/route.')
        kind = route.get('kind')
        if kind == 'silence':
            continue
        try:
            if kind == 'external':
                proof = route['external_proof']
                samples = proof['sample_frames']
                source_size = proof['source_size_bytes']
            elif kind == 'embedded':
                proof = member['movie_proof']
                track = proof['audio_tracks'][route['audio_ordinal']]
                samples = (Fraction(str(track['duration'])) * 48000).__ceil__()
                source_size = proof['source_size_bytes']
            else:
                raise ValueError('Unsupported selected record audio route.')
            if (type(samples) is not int or
                    not 1 <= samples <= MAX_DECODED_SOURCE_SAMPLES or
                    type(source_size) is not int or
                    not 44 <= source_size <= MAX_SOURCE_FILE_BYTES):
                raise ValueError('Selected record PCM source exceeds 10-second '
                                 'decode or 256 MiB file safety budget.')
        except (KeyError, IndexError, TypeError, ZeroDivisionError, OverflowError) as exc:
            raise ValueError('Frozen PCM source duration/size proof is incomplete: {}'.format(exc))
    return ctx


def qt5_device_format(qt_multimedia=None):
    """Check a REAL supported Qt5 output device; no guessed format fallback."""
    if qt_multimedia is None:
        try:
            from PySide2 import QtMultimedia as qt_multimedia
        except ImportError:
            raise RuntimeError('Qt5 QAudioOutput/PySide2 is unavailable.')
    if not all(hasattr(qt_multimedia, name) for name in
               ('QAudioOutput', 'QAudioFormat', 'QAudioDeviceInfo', 'QAudio')):
        raise RuntimeError('Qt5 PCM QAudioOutput backend is unavailable.')
    fmt = qt_multimedia.QAudioFormat()
    fmt.setCodec('audio/pcm')
    fmt.setSampleRate(48000)
    fmt.setChannelCount(2)
    fmt.setSampleSize(16)
    fmt.setSampleType(qt_multimedia.QAudioFormat.SignedInt)
    fmt.setByteOrder(qt_multimedia.QAudioFormat.LittleEndian)
    device = qt_multimedia.QAudioDeviceInfo.defaultOutputDevice()
    if device.isNull() or not device.isFormatSupported(fmt):
        raise RuntimeError('No Qt5 audio output device supports signed 48kHz stereo PCM16.')
    return qt_multimedia, device, fmt


class _PCMBuildThread(QtCore.QThread):
    built = Signal(int, object, object)  # generation, immutable bytes, proof
    failed = Signal(int, str)

    def __init__(self, spec, ffmpeg, generation, parent=None):
        super(_PCMBuildThread, self).__init__(parent)
        self.spec = spec
        self.ffmpeg = ffmpeg
        self.generation = int(generation)
        self._cancel = threading.Event()
        self._proc_lock = threading.Lock()
        self._proc = None

    def _set_process(self, process):
        with self._proc_lock:
            self._proc = process
            if process is not None and self._cancel.is_set():
                try:
                    process.kill()
                except OSError:
                    pass

    def cancel(self):
        self._cancel.set()
        with self._proc_lock:
            proc = self._proc
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()
            except OSError:
                pass

    def run(self):
        try:
            from ..exporting.record_audio import (_record_pcm,
                                                   validate_record_audio_plan)
            from ..exporting.spec import managed_record_timeline_plan
            ctx = _frozen_intent(self.spec)
            validate_record_audio_plan(self.spec, ctx['composite_export'])
            plan = managed_record_timeline_plan(self.spec)
            if len(plan) != self.spec.count:
                raise ValueError('Immutable record timeline frame count changed.')
            if self._cancel.is_set():
                return
            pcm, selected = _record_pcm(
                self.spec, self.ffmpeg, interrupted=self._cancel.is_set,
                set_process=self._set_process)
            expected = sample_offset(self.spec.count) * 4
            if len(pcm) != expected or len(pcm) > MAX_PCM_BYTES:
                raise RuntimeError('Reconstructed PCM sample duration/size differs from record.')
            if self._cancel.is_set():
                return
            # Independently re-evaluate the frozen edit/sample mapping and
            # source proofs BEFORE the QAudioOutput ever sees the bytes.
            double, selected_again = _record_pcm(
                self.spec, self.ffmpeg, interrupted=self._cancel.is_set,
                set_process=self._set_process)
            if self._cancel.is_set():
                return
            if double != pcm or selected_again != selected:
                raise RuntimeError('Independent record sample reconstruction disagrees.')
            self.built.emit(self.generation, pcm, {
                'sample_frames': len(pcm) // 4,
                'pcm_sha256': hashlib.sha256(pcm).hexdigest(),
                'selected_sources': selected,
                'frame_count': self.spec.count,
                'fps': self.spec.fps,
                'hardware_latency_calibrated': False})
        except Exception as exc:
            if not self._cancel.is_set():
                self.failed.emit(self.generation, str(exc))
        finally:
            self._set_process(None)


class RecordAudioTransport(QtCore.QObject):
    stateChanged = Signal(str)
    errorText = Signal(str)

    def __init__(self, parent=None, audio_factory=None):
        QtCore.QObject.__init__(self, parent)
        self._factory = audio_factory  # test injection; actual playback uses Qt5
        self._generation = 0
        self._thread = None
        self._output = None
        self._buffer = None
        self._bytes = None
        self._proof = None
        self._reason = ''
        self.state = 'disabled'
        self._anchor = None
        self._last_frame = None
        self._last_realign = 0.
        self._speed = 1.
        self._volume = .5
        self._muted = False

    def _state(self, state, reason=''):
        self._reason = str(reason)
        if state != self.state:
            self.state = state
            self.stateChanged.emit(state)

    def capabilities(self):
        return {'state': self.state, 'reason': self._reason,
                'backend': 'Qt5 QAudioOutput PCM16' if self._output is not None else None,
                'sample_rate': 48000, 'channels': 2,
                'sample_frames': (self._proof or {}).get('sample_frames'),
                'source_proof': dict(self._proof or {}),
                'hardware_latency_calibrated': False,
                'speaker_audibility_proven': False,
                'record_is_video_master': True}

    def prepare(self, spec, ffmpeg):
        self.disable()
        try:
            _frozen_intent(spec)
            if not isinstance(ffmpeg, str) or not os.path.isfile(ffmpeg) or \
                    not os.access(ffmpeg, os.X_OK):
                raise ValueError('A working FFmpeg executable is required.')
            if self._factory is None:
                qt5_device_format()
        except Exception as exc:
            self._state('unsupported', str(exc))
            self.errorText.emit(str(exc))
            return False
        self._generation += 1
        gen = self._generation
        worker = _PCMBuildThread(spec, ffmpeg, gen)
        self._thread = worker
        _LIVE_THREADS.add(worker)
        def cleanup():
            _LIVE_THREADS.discard(worker)
            if self._thread is worker:
                self._thread = None
            worker.deleteLater()
        worker.finished.connect(cleanup)
        worker.built.connect(self._on_built)
        worker.failed.connect(self._on_failed)
        self._state('preparing')
        worker.start()
        return True

    def _on_failed(self, generation, message):
        if generation != self._generation:
            return
        self._stop_device()
        self._state('failed', message)
        self.errorText.emit(message)

    def _on_built(self, generation, pcm, proof):
        if generation != self._generation or self.state != 'preparing':
            return
        if (not isinstance(pcm, bytes) or len(pcm) != proof['sample_frames'] * 4
                or not pcm or len(pcm) > MAX_PCM_BYTES or
                hashlib.sha256(pcm).hexdigest() != proof['pcm_sha256']):
            self._on_failed(generation, 'Record PCM proof mismatch before audio device.')
            return
        try:
            if self._factory is None:
                multimedia, device, fmt = qt5_device_format()
                output = multimedia.QAudioOutput(device, fmt, self)
            else:
                output = self._factory(self)
            self._output = output
            if self._factory is None and hasattr(output, 'stateChanged'):
                # Drop notifications from an old/reloaded device. Never claim
                # active sound after a Qt driver reports an actual output error.
                output.stateChanged.connect(
                    lambda state, owner=output, epoch=generation:
                    self._on_output_state(owner, epoch, state))
            self._bytes = QtCore.QByteArray(pcm)
            self._buffer = QtCore.QBuffer(self)
            self._buffer.setData(self._bytes)
            if not self._buffer.open(QtCore.QIODevice.ReadOnly):
                raise RuntimeError('Qt5 PCM buffer cannot open.')
            self._proof = dict(proof)
            self._state('ready')
        except Exception as exc:
            self._on_failed(generation, 'Qt5 PCM audio backend unavailable: {}'.format(exc))

    def _on_output_state(self, owner, generation, state):
        if (owner is not self._output or generation != self._generation or
                self.state not in ('playing', 'ready')):
            return
        try:
            from PySide2 import QtMultimedia
            if state == QtMultimedia.QAudio.StoppedState and \
                    owner.error() != QtMultimedia.QAudio.NoError:
                self._on_failed(generation,
                                'Qt5 PCM output device error: {}'.format(
                                    owner.error()))
        except (ImportError, RuntimeError, AttributeError):
            return

    def _stop_device(self):
        if self._output is not None:
            try:
                self._output.stop()
            except (RuntimeError, AttributeError):
                pass
            if hasattr(self._output, 'deleteLater'):
                self._output.deleteLater()
        self._output = None
        if self._buffer is not None:
            self._buffer.close()
            self._buffer.deleteLater()
        self._buffer = None
        self._bytes = None
        self._proof = None
        self._anchor = None
        self._last_frame = None

    def disable(self):
        self._generation += 1
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.cancel()  # no blocking join or Qt teardown of a live worker
        self._stop_device()
        self._state('disabled')

    def set_gain(self, volume, muted):
        self._volume = max(0., min(1., float(volume)))
        self._muted = bool(muted)
        if self._output is not None:
            self._output.setVolume(0. if muted else self._volume)

    def _begin(self, frame):
        if self._output is None or self._buffer is None or self._proof is None:
            return
        offset = sample_offset(frame) * 4
        if not 0 <= offset < len(self._bytes):
            self.pause()
            return
        self._output.stop()
        if not self._buffer.seek(offset):
            self._on_failed(self._generation, 'Qt5 PCM cannot seek exact record sample.')
            return
        self._output.setVolume(0. if self._muted else self._volume)
        self._output.start(self._buffer)
        try:
            from PySide2 import QtMultimedia
            if self._factory is None and (self._output.state() ==
                    QtMultimedia.QAudio.StoppedState and
                    self._output.error() != QtMultimedia.QAudio.NoError):
                raise RuntimeError('Qt5 PCM device refused stream: {}'.format(
                    self._output.error()))
        except ImportError:
            pass
        except RuntimeError as exc:
            self._on_failed(self._generation, str(exc))
            return
        self._anchor = int(frame)
        self._last_frame = int(frame)
        self._last_realign = time.monotonic()
        self._state('playing')

    def pause(self):
        if self._output is not None:
            self._output.stop()  # no hidden buffer tail while video is paused
        self._anchor = None
        if self.state == 'playing':
            self._state('paused')

    def sync(self, frame, playing, speed=1., loop_mode='once',
             buffering=False, policy='realtime'):
        """Video-master transport; restart only on seek/wrap/material drift.

        ``frame`` is a *record* index, not a source-trim or movie decode index.
        Clock drift estimate is approximate: QAudioOutput.processedUSecs() is
        not a measurement of speaker output onset or hardware FIFO latency.
        """
        self._speed = speed
        if self.state not in ('ready', 'paused', 'playing'):
            return
        count = self._proof['frame_count']
        if not (type(frame) is int and 0 <= frame < count and playing and
                speed == 1. and loop_mode != 'ping_pong' and
                (policy != 'play_all' or not buffering)):
            self.pause()
            return
        if self.state != 'playing' or self._anchor is None or \
                (self._last_frame is not None and frame < self._last_frame):
            self._begin(frame)
            return
        # FramePump is the single video master. A large manual jump can move
        # forward while sound is still buffered at the old frame; re-anchor.
        expected = (sample_offset(frame) - sample_offset(self._anchor)) * 1000000 // 48000
        elapsed = max(0, int(self._output.processedUSecs())) if callable(
            getattr(self._output, 'processedUSecs', None)) else expected
        now = time.monotonic()
        if (abs(elapsed - expected) > 180000 and
                now - self._last_realign >= .75):
            self._begin(frame)
        else:
            self._last_frame = frame


__all__ = ['RecordAudioTransport', 'sample_offset', 'qt5_device_format']
