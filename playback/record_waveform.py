# -*- coding: utf-8 -*-
"""Cancellable v4 record-PCM waveform preview, without audio-device output.

Reads an immutable ExportSpec and the same independently verified PCM source
assembly used by managed v4 EDL/OTIO WAV export. One bin = one exact record
frame. No export path is opened, no sidecar or Session state is modified.
"""
from __future__ import absolute_import

import array
from fractions import Fraction
import os
import shutil
import sys
import threading

from ..core.qtcompat import QtCore, Signal, Slot
from ..exporting.spec import ExportSpec, managed_record_timeline_plan
from ..exporting.record_audio import (
    _record_pcm, RATE, CHANNELS, BYTES_PER_SAMPLE_FRAME)


# Maya may destroy an owning dialog while a decoder thread is winding down.
# Keep QThreads alive independently until finished, even after dialog close.
_RETAINED_RECORD_WAVEFORM_THREADS = set()


def _source_label(member, route):
    """Label which frozen PCM selection produced the preview, per edit."""
    source_id = member.get('source_id', '')
    if route['kind'] == 'external':
        detail = 'external WAV: {}'.format(
            os.path.basename(route['external_path']))
    elif route['kind'] == 'embedded':
        detail = 'embedded audio {} (stream {})'.format(
            route['audio_ordinal'], route['stream_index'])
    else:
        detail = 'silence'
    return '{} · {}'.format(source_id, detail) if source_id else detail


def _sample_floor(frame, fps):
    return (Fraction(frame) * RATE / fps).__floor__()


def _normalized_stereo(raw):
    """Signed min/max and true peak, full-scale fixed at 32768 (no auto gain)."""
    if not raw or len(raw) % BYTES_PER_SAMPLE_FRAME:
        raise ValueError('Record waveform bin must contain whole stereo s16 samples.')
    samples = array.array('h')
    samples.frombytes(raw)
    if sys.byteorder != 'little':
        samples.byteswap()
    left_min = right_min = 32767
    left_max = right_max = -32768
    left_abs = right_abs = 0
    for index in range(0, len(samples), CHANNELS):
        left, right = samples[index], samples[index + 1]
        left_min = min(left_min, left)
        left_max = max(left_max, left)
        right_min = min(right_min, right)
        right_max = max(right_max, right)
        left_abs = max(left_abs, abs(left))
        right_abs = max(right_abs, abs(right))
    return {'left_min': left_min / 32768., 'left_max': left_max / 32768.,
            'right_min': right_min / 32768., 'right_max': right_max / 32768.,
            'left_peak': left_abs / 32768., 'right_peak': right_abs / 32768.,
            'peak': max(left_abs, right_abs) / 32768.}


def _frozen_audio_overlays(record):
    """Read the exact legacy singleton or the bounded ordered v2 overlay set.

    The decoder independently verifies their source proofs; this lightweight
    check prevents ambiguous provenance or malformed waveform labels even in
    code paths that supply decoded PCM from a test/alternate backend.
    """
    if not isinstance(record, dict):
        raise ValueError('Record waveform audio plan must be frozen.')
    if 'overlay' in record and 'overlays' in record:
        raise ValueError('Record waveform cannot combine overlay and overlays.')
    if 'overlays' in record:
        events = record['overlays']
        if type(events) is not list or not 1 <= len(events) <= 3:
            raise ValueError('Record waveform requires 1..3 ordered audio overlays.')
    elif 'overlay' in record:
        events = [record['overlay']]
    else:
        return []
    from ..exporting.record_audio_overlay import OVERLAY_KEYS
    occurrences = set()
    for event in events:
        if not isinstance(event, dict) or set(event) != OVERLAY_KEYS:
            raise ValueError('Record waveform needs complete frozen audio overlays.')
        identifier = event['occurrence_id']
        if (not isinstance(identifier, str) or not identifier or
                identifier in occurrences):
            raise ValueError('Record waveform requires unique audio overlay IDs.')
        occurrences.add(identifier)
        for name in ('record_start_frame', 'source_in_frame',
                     'duration_frames', 'gain_num', 'gain_den'):
            if type(event[name]) is not int:
                raise ValueError('Record waveform audio overlay timing must be integral.')
        if (not 0 <= event['record_start_frame'] < 120 or
                not 0 <= event['source_in_frame'] < 120 or
                not 1 <= event['duration_frames'] <= 120 or
                event['record_start_frame'] + event['duration_frames'] > 120 or
                event['gain_den'] != 2 or event['gain_num'] not in (0, 1, 2)):
            raise ValueError('Record waveform audio overlay bounds/gain are invalid.')
    return events


def build_record_waveform(spec, ffmpeg, interrupted=None, set_process=None):
    """Pure, bounded record-aligned peak model from verified selected PCM.

    Intended for a worker QThread. Do not invoke on Maya's GUI thread. Each
    output bin is signed stereo amplitude, fixed relative to PCM full scale.
    Gaps and explicitly muted edits remain *zero*, never normalized upward.
    """
    if not isinstance(spec, ExportSpec):
        raise TypeError('Record waveform requires a frozen ExportSpec.')
    frozen = spec.to_context().get('composite_export')
    if (not isinstance(frozen, dict) or frozen.get('version') != 4 or
            frozen.get('audio_policy') != 'wav_pcm_v1'):
        raise ValueError('Record waveform requires a proven v4 wav_pcm_v1 export.')
    if not isinstance(ffmpeg, str) or not ffmpeg or not os.path.isfile(ffmpeg):
        raise ValueError('Record waveform requires an available FFmpeg binary.')
    if interrupted and interrupted():
        raise RuntimeError('Record waveform cancelled.')
    members = frozen['members']
    routes = frozen['record_audio']['routes']
    overlays = _frozen_audio_overlays(frozen['record_audio'])
    member_occurrences = {member['occurrence_id'] for member in members}
    if any(event['occurrence_id'] in member_occurrences for event in overlays):
        raise ValueError('Audio overlay occurrence must be independent of video edits.')
    rows = managed_record_timeline_plan(spec)
    if len(rows) != spec.count or spec.count > 120:
        raise ValueError('Waveform record frame count is inconsistent or unbounded.')
    raw, sources = _record_pcm(spec, ffmpeg, interrupted=interrupted,
                               set_process=set_process)
    if interrupted and interrupted():
        raise RuntimeError('Record waveform cancelled.')
    fps = Fraction(spec.fps)
    base = _sample_floor(spec.start, fps)
    expected_count = _sample_floor(spec.end + 1, fps) - base
    if len(raw) != expected_count * BYTES_PER_SAMPLE_FRAME:
        raise RuntimeError('Record waveform PCM differs from exact timeline duration.')
    bins = []
    for row in rows:
        if interrupted and interrupted():
            raise RuntimeError('Record waveform cancelled.')
        frame = row['display_index']
        absolute_start = _sample_floor(frame, fps)
        absolute_end = _sample_floor(frame + 1, fps)
        lo, hi = absolute_start - base, absolute_end - base
        if not (0 <= lo < hi <= expected_count):
            raise RuntimeError('Record waveform bin exceeds proven PCM bounds.')
        samples = raw[lo * BYTES_PER_SAMPLE_FRAME:
                      hi * BYTES_PER_SAMPLE_FRAME]
        amplitudes = _normalized_stereo(samples)
        if row['kind'] == 'record_gap':
            occurrence_id = source_id = source_frame = None
            route_kind = 'silence'
            source_label = 'Record gap · silence'
        else:
            lineage = row['members'][0]
            member_index = lineage['member_index']
            member, route = members[member_index], routes[member_index]
            occurrence_id = member['occurrence_id']
            source_id = member['source_id']
            source_frame = lineage.get('source_frame')
            route_kind = route['kind']
            source_label = _source_label(member, route)
            if lineage['kind'] == 'outside_trim':
                route_kind = 'silence'
                source_label += ' · outside trim, silence'
        active_overlays = [event for event in overlays
                           if event['record_start_frame'] <= frame <
                           event['record_start_frame'] + event['duration_frames']]
        if active_overlays:
            route_kind = ('overlay' if route_kind == 'silence' else 'mixed')
            for event in active_overlays:
                source_label += (' · audio-only layer {} (source +{}, gain {}/{})'
                                 .format(event['occurrence_id'],
                                         event['source_in_frame'] + frame -
                                         event['record_start_frame'],
                                         event['gain_num'], event['gain_den']))
        # An authored zero-gain event is still part of waveform provenance,
        # but it cannot produce samples over otherwise silent record frames.
        if (amplitudes['peak'] != 0 and
                (route_kind == 'silence' or
                 (route_kind == 'overlay' and
                  not any(event['gain_num'] for event in active_overlays)))):
            raise RuntimeError('Record waveform silence differs from reconstructed PCM.')
        overlay_ids = [event['occurrence_id'] for event in active_overlays]
        bins.append(dict({
            'record_frame': frame, 'kind': row['kind'],
            'occurrence_id': occurrence_id, 'source_id': source_id,
            'source_frame': source_frame, 'route_kind': route_kind,
            'source_label': source_label,
            'overlay_occurrence_id': (overlay_ids[0] if overlay_ids else None),
            'overlay_occurrence_ids': overlay_ids,
            'start_sample': absolute_start, 'end_sample': absolute_end,
            'sample_count': hi - lo}, **amplitudes))
    labels = ['{}: {}'.format(m['occurrence_id'], _source_label(m, r))
              for m, r in zip(members, routes)]
    return {'version': 1, 'label': 'Record PCM 48 kHz stereo · ' +
            ' | '.join(labels), 'sample_rate': RATE, 'channels': CHANNELS,
            'fps': spec.fps, 'start': spec.start, 'end': spec.end,
            'sample_frames': expected_count, 'bins': bins,
            # This is provenance of the selected tracks, not playable audio.
            'selected_sources': sources, 'speaker_playback': False}


class _RecordWaveformThread(QtCore.QThread):
    def __init__(self, spec, ffmpeg, generation):
        QtCore.QThread.__init__(self)
        self.spec = spec
        self.ffmpeg = ffmpeg
        self.generation = generation
        self.result = None
        self.error = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._process = None

    def cancel(self):
        self._cancel.set()
        self.requestInterruption()
        with self._lock:
            proc = self._process
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()
            except OSError:
                pass

    def _set_process(self, process):
        with self._lock:
            self._process = process
            should_cancel = self._cancel.is_set()
        if should_cancel and process is not None:
            try:
                if process.poll() is None:
                    process.kill()
            except OSError:
                pass

    def run(self):
        try:
            result = build_record_waveform(
                self.spec, self.ffmpeg, interrupted=self._cancel.is_set,
                set_process=self._set_process)
            if not self._cancel.is_set():
                self.result = result
        except Exception as exc:
            if not self._cancel.is_set():
                self.error = str(exc)
        finally:
            self._set_process(None)


def _retire_thread(worker):
    """Remove a finished worker on the UI event loop after its last signal."""
    QtCore.QTimer.singleShot(0, lambda: _RETAINED_RECORD_WAVEFORM_THREADS.discard(worker))


class RecordWaveformPreview(QtCore.QObject):
    """UI-thread owner for one active, last-request-wins v4 PCM preview.

    request(spec, ffmpeg) -> generation. Connect ready(int,object) and
    failed(int,str). cancel()/shutdown() invalidate generations. The current
    worker exits before any pending newer job launches; stale results never
    reach public callbacks. No sidecar, audio device or Session writes.
    """
    ready = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, parent=None):
        QtCore.QObject.__init__(self, parent)
        self.generation = 0
        self._worker = None
        self._pending = None
        self._shutdown = False

    def request(self, spec, ffmpeg):
        if self._shutdown:
            raise RuntimeError('Record waveform preview was shut down.')
        if not isinstance(spec, ExportSpec):
            raise TypeError('Record waveform needs an immutable ExportSpec.')
        # Capture immutable context before the worker thread is scheduled.
        # from_context reconstructs frozen nested state, not a live Session.
        frozen = ExportSpec.from_context(
            spec.to_context(), source_path=spec.source_path,
            source_id=spec.source_id)
        ffmpeg = ffmpeg or shutil.which('ffmpeg')
        self.generation += 1
        self._pending = (self.generation, frozen, ffmpeg)
        worker = self._worker
        if worker is not None:
            worker.cancel()
        else:
            self._start_pending()
        return self.generation

    def _start_pending(self):
        if self._worker is not None or self._shutdown or self._pending is None:
            return
        generation, spec, ffmpeg = self._pending
        self._pending = None
        worker = _RecordWaveformThread(spec, ffmpeg, generation)
        self._worker = worker
        _RETAINED_RECORD_WAVEFORM_THREADS.add(worker)
        worker.finished.connect(self._on_finished)
        worker.finished.connect(lambda w=worker: _retire_thread(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    @Slot()
    def _on_finished(self):
        worker = self.sender()
        if worker is not self._worker:
            return
        self._worker = None
        if not self._shutdown and worker.generation == self.generation:
            if worker.result is not None:
                self.ready.emit(worker.generation, worker.result)
            elif worker.error is not None:
                self.failed.emit(worker.generation, worker.error)
        self._start_pending()

    def cancel(self):
        self.generation += 1
        self._pending = None
        if self._worker is not None:
            self._worker.cancel()
        return self.generation

    def shutdown(self, wait=False, timeout_ms=3000):
        """Invalidate everything; optional bounded wait for explicit teardown.

        Qt cannot safely destroy an in-flight QThread. If the worker is still
        running after the deadline, the module retains it until completion.
        """
        self._shutdown = True
        self.cancel()
        worker = self._worker
        if wait and worker is not None:
            worker.wait(max(0, min(3000, int(timeout_ms))))
            app = QtCore.QCoreApplication.instance()
            if app is not None:
                app.processEvents()
        return worker is None or not worker.isRunning()


__all__ = ['RecordWaveformPreview', 'build_record_waveform']
