# -*- coding: utf-8 -*-
"""Progressive, frame-spaced thumbnails without full-file decoding.

One asynchronous FFmpeg seek extracts each movie thumbnail. Long movies are
never decoded in their entirety just to draw a small strip. A few completed
strips are cached in memory and keyed by media identity + timeline geometry.
"""
from __future__ import absolute_import

import collections
import os

from .qtcompat import QtCore, QtGui, Signal
from .ffmpegtools import configure_process


class ThumbnailStripJob(QtCore.QObject):
    started = Signal(int)
    produced = Signal(int, object)
    finished = Signal()

    def __init__(self, ffmpeg, parent=None):
        QtCore.QObject.__init__(self, parent)
        self._ffmpeg = ffmpeg
        self._proc = None
        self._generation = 0
        self._seq_timer = QtCore.QTimer(self)
        self._seq_timer.setSingleShot(True)
        self._seq_timer.timeout.connect(self._step)
        self._entries = []
        self._cache = collections.OrderedDict()
        self._info = None
        self._count = 0
        self._ordinal = 0
        self._height = 36
        self._key = None

    def request(self, info, count, thumb_h):
        self.cancel()
        self._info = info
        self._count = max(2, int(count))
        self._height = max(24, int(thumb_h))
        self._ordinal = 0
        self._entries = []
        try:
            stat = os.stat(info.path)
            fingerprint = (os.path.realpath(info.path), stat.st_size,
                           getattr(stat, 'st_mtime_ns', int(stat.st_mtime * 1e9)))
        except OSError:
            fingerprint = (info.path, None, None)
        if info.is_sequence:
            # The chosen sample can stay unchanged while other frames are edited.
            # Match the source identity to all frames used by this strip.
            samples = []
            for i in range(self._count):
                path = info.sequence_files[min(len(info.sequence_files) - 1,
                                               int((i + .5) * len(info.sequence_files) / self._count))]
                try:
                    st = os.stat(path)
                    samples.append((path, st.st_size, getattr(st, 'st_mtime_ns',
                                                             int(st.st_mtime * 1e9))))
                except OSError:
                    samples.append((path, None, None))
            fingerprint = tuple(samples)
        if info.is_sequence:
            # Every source member contributes to invalidation, not only the
            # first frame. Avoid opening/decoding the images just for this.
            members = []
            for member in info.sequence_files:
                try:
                    st = os.stat(member)
                    members.append((member, st.st_size, getattr(
                        st, 'st_mtime_ns', int(st.st_mtime * 1e9))))
                except OSError:
                    members.append((member, None, None))
            fingerprint = tuple(members)
        self._key = (fingerprint, self._count, self._height)
        cached = self._cache.get(self._key)
        self.started.emit(self._count)
        if cached is not None:
            self._cache.move_to_end(self._key)
            for ordinal, pixmap in cached:
                self.produced.emit(ordinal, pixmap)
            self.finished.emit()
            return
        self._seq_timer.start(0)

    def _step(self):
        if self._info is None:
            return
        if self._ordinal >= self._count:
            self._finish()
            return
        frame = int(self._ordinal)
        info = self._info
        if info.is_sequence:
            files = info.sequence_files
            index = min(len(files) - 1, int((frame + .5) * len(files) / self._count))
            pixmap = QtGui.QPixmap(files[index])
            if not pixmap.isNull():
                self._publish(frame, pixmap.scaledToHeight(
                    self._height, QtCore.Qt.SmoothTransformation))
            self._ordinal += 1
            self._seq_timer.start(0)
            return

        # Seek into the input for a single frame. Constrain decoder parallelism:
        # the thumbnail work must not starve the active playback decoder.
        seconds = (frame + .5) * info.duration / self._count
        proc = configure_process(QtCore.QProcess(QtCore.QCoreApplication.instance()))
        token = self._generation
        proc.finished.connect(lambda *_: self._video_done(proc, token))
        proc.errorOccurred.connect(lambda *_: self._failed(proc, token))
        proc.finished.connect(proc.deleteLater)
        proc.setProgram(self._ffmpeg)
        proc.setArguments([
            '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-ss', '{:.6f}'.format(seconds), '-i', info.path,
            '-threads', '1', '-an', '-frames:v', '1',
            '-vf', 'scale=-2:{}'.format(self._height),
            '-f', 'image2pipe', '-vcodec', 'mjpeg', '-q:v', '5', 'pipe:1'])
        self._proc = proc
        proc.start()

    def _publish(self, ordinal, pixmap):
        self._entries.append((ordinal, pixmap))
        self.produced.emit(ordinal, pixmap)

    def _video_done(self, proc, token):
        if proc is not self._proc or token != self._generation:
            return
        self._proc = None
        data = bytes(proc.readAllStandardOutput())
        pixmap = QtGui.QPixmap()
        if data and pixmap.loadFromData(data, 'JPG') and not pixmap.isNull():
            self._publish(self._ordinal, pixmap)
        self._ordinal += 1
        self._seq_timer.start(0)

    def _failed(self, proc, token):
        if (proc is self._proc and token == self._generation
                and proc.error() == QtCore.QProcess.FailedToStart):
            self._proc = None
            proc.deleteLater()
            self._ordinal += 1
            self._seq_timer.start(0)

    def _finish(self):
        self._seq_timer.stop()
        if self._key is not None:
            self._cache[self._key] = list(self._entries)
            self._cache.move_to_end(self._key)
            while len(self._cache) > 4:
                self._cache.popitem(last=False)
        self.finished.emit()

    def cancel(self, wait=False):
        self._generation += 1
        self._seq_timer.stop()
        proc = self._proc
        self._proc = None
        self._info = None
        self._entries = []
        if proc is not None:
            try:
                if wait:
                    # Remove closure callbacks before the owning UI can die.
                    proc.finished.disconnect()
                    proc.errorOccurred.disconnect()
                if proc.state() != QtCore.QProcess.NotRunning:
                    proc.kill()
                    if wait:
                        proc.waitForFinished(1500)
                proc.deleteLater()
            except RuntimeError:
                pass
