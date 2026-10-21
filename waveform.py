# -*- coding: utf-8 -*-
"""Audio waveform peaks via a non-blocking QProcess ffmpeg decode.

Mirrors the professional approach: decode the audio mono and resampled to a
very low rate (1000 Hz), so a whole clip is only a few thousand samples. Peaks
are bucketed and normalised by the 98th percentile so one loud transient does
not flatten the rest. numpy is used when present, with a pure-Python fallback.
"""
from __future__ import absolute_import

import array
import collections
import os
import sys

from .qtcompat import QtCore, Signal
from .ffmpegtools import configure_process

try:
    import numpy as _np
except Exception:
    _np = None

SAMPLE_RATE = 1000
MAX_BINS = 2000


class WaveformJob(QtCore.QObject):
    ready = Signal(object)      # list[float] in 0..1, or [] when no audio

    def __init__(self, ffmpeg, parent=None):
        QtCore.QObject.__init__(self, parent)
        self._ffmpeg = ffmpeg
        self._proc = None
        self._key = None
        self._cache = collections.OrderedDict()

    def request(self, source, duration):
        self.cancel()
        try:
            stat = os.stat(source)
            self._key = (os.path.realpath(source), stat.st_size,
                         getattr(stat, 'st_mtime_ns', int(stat.st_mtime * 1e9)),
                         round(duration, 3))
        except OSError:
            self._key = (source, None, None, round(duration, 3))
        cached = self._cache.get(self._key)
        if cached is not None:
            self._cache.move_to_end(self._key)
            self.ready.emit(cached)
            return
        # Cap decoded PCM to ~1 MB for typical long clips.
        sample_rate = max(80, min(SAMPLE_RATE, int(500000 / max(1.0, duration))))
        proc = configure_process(QtCore.QProcess(QtCore.QCoreApplication.instance()))
        proc.finished.connect(lambda *_: self._finished(proc))
        proc.errorOccurred.connect(lambda *_: self._failed(proc))
        proc.setProgram(self._ffmpeg)
        proc.setArguments([
            '-v', 'error', '-ss', '0', '-i', source, '-t', '{:.6f}'.format(max(0.1, duration)),
            '-vn', '-threads', '1', '-ac', '1', '-ar', str(sample_rate),
            '-f', 's16le', 'pipe:1'])
        self._proc = proc
        proc.start()

    def _failed(self, proc):
        if proc is self._proc and proc.error() == QtCore.QProcess.FailedToStart:
            self._proc = None
            proc.deleteLater()
            self.ready.emit([])

    def _finished(self, proc):
        if proc is not self._proc:
            proc.deleteLater()
            return
        raw = bytes(proc.readAllStandardOutput())
        success = (proc.exitStatus() == QtCore.QProcess.NormalExit and
                   proc.exitCode() == 0)
        proc.deleteLater()
        self._proc = None
        if not success:
            self.ready.emit([])
            return
        peaks = self._peaks(raw)
        if self._key is not None:
            self._cache[self._key] = peaks
            self._cache.move_to_end(self._key)
            while len(self._cache) > 4:
                self._cache.popitem(last=False)
        self.ready.emit(peaks)

    @staticmethod
    def _peaks(raw):
        if len(raw) < 4:
            return []
        if _np is not None:
            samples = _np.abs(_np.frombuffer(raw, dtype='<i2').astype('float32')) / 32768.0
            size = samples.size
            if size == 0:
                return []
            bins = int(min(MAX_BINS, max(64, size)))
            edges = _np.linspace(0, size, bins + 1).astype('int64')
            peaks = _np.empty(bins, dtype='float32')
            for i in range(bins):
                lo, hi = int(edges[i]), max(int(edges[i]) + 1, int(edges[i + 1]))
                peaks[i] = samples[lo:hi].max()
            norm = max(0.05, float(_np.percentile(peaks, 98)))
            return [min(1.0, float(p) / norm) for p in peaks]

        samples = array.array('h')
        samples.frombytes(raw[:len(raw) - (len(raw) % 2)])
        if sys.byteorder == 'big':
            samples.byteswap()
        size = len(samples)
        if size == 0:
            return []
        bins = int(min(MAX_BINS, max(64, size)))
        peaks = []
        for i in range(bins):
            lo = size * i // bins
            hi = max(lo + 1, size * (i + 1) // bins)
            top = 0
            for j in range(lo, hi):
                value = samples[j]
                value = -value if value < 0 else value
                if value > top:
                    top = value
            peaks.append(top / 32768.0)
        ordered = sorted(peaks)
        norm = max(0.05, ordered[int(0.98 * (len(ordered) - 1))])
        return [min(1.0, p / norm) for p in peaks]

    def cancel(self, wait=False):
        proc = self._proc
        self._proc = None
        self._key = None
        if proc is not None:
            try:
                if wait:
                    proc.finished.disconnect()
                    proc.errorOccurred.disconnect()
                if proc.state() != QtCore.QProcess.NotRunning:
                    proc.kill()
                    if wait:
                        proc.waitForFinished(1500)
                proc.deleteLater()
            except RuntimeError:
                pass
