# -*- coding: utf-8 -*-
"""Progressive, frame-spaced thumbnails without full-file decoding.

Movie thumbnails are extracted with keyframe-accurate fast seeks
(``-noaccurate_seek``), so FFmpeg lands on the nearest keyframe instead of
decoding forward to an exact timestamp -- the dominant cost on long-GOP codecs.
A small pool of single-frame extractions runs concurrently so the strip fills in
parallel rather than one blocking seek at a time, while each process stays
``-threads 1`` so the work cannot swamp the active playback decoder. Long movies
are still never decoded in their entirety just to draw a small strip.

Image-sequence thumbnails decode at reduced size through ``QImageReader`` (so
libjpeg/libpng produce a small image directly instead of a full-resolution
decode plus downscale) and route EXR frames through the dedicated reader, which
Qt cannot open on its own.

A few completed strips are cached in memory and keyed by media identity +
timeline geometry.
"""
from __future__ import absolute_import

import collections
import os

from ..core.qtcompat import QtCore, QtGui, Signal
from ..media.ffmpeg import configure_process
from ..media import exr


def _default_concurrency():
    try:
        cpus = os.cpu_count() or 2
    except Exception:
        cpus = 2
    return max(1, min(4, cpus))


class ThumbnailStripJob(QtCore.QObject):
    started = Signal(int)
    produced = Signal(int, object)
    finished = Signal()

    def __init__(self, ffmpeg, parent=None, concurrency=None):
        QtCore.QObject.__init__(self, parent)
        self._ffmpeg = ffmpeg
        self._concurrency = concurrency or _default_concurrency()
        self._generation = 0
        self._seq_timer = QtCore.QTimer(self)
        self._seq_timer.setSingleShot(True)
        self._seq_timer.timeout.connect(self._sequence_step)
        self._entries = []
        self._cache = collections.OrderedDict()
        self._info = None
        self._count = 0
        self._ordinal = 0          # next sequence sample to decode
        self._next_ordinal = 0     # next movie sample to launch
        self._active = {}          # QProcess -> ordinal, movies in flight
        self._height = 36
        self._key = None

    def request(self, info, count, thumb_h):
        self.cancel()
        self._info = info
        self._count = max(2, int(count))
        self._height = max(24, int(thumb_h))
        self._ordinal = 0
        self._next_ordinal = 0
        self._entries = []
        self._key = (self._fingerprint(info), self._count, self._height)
        cached = self._cache.get(self._key)
        self.started.emit(self._count)
        if cached is not None:
            self._cache.move_to_end(self._key)
            for ordinal, pixmap in cached:
                self.produced.emit(ordinal, pixmap)
            self.finished.emit()
            return
        if info.is_sequence:
            self._seq_timer.start(0)
        else:
            self._launch_movie_batch()

    def _fingerprint(self, info):
        """Identity that invalidates the cached strip when any source changes.

        Every sequence member contributes, not only the sampled frames, so an
        edit to an unsampled frame still refreshes the strip. Image contents are
        never opened here; only cheap ``stat`` metadata is used.
        """
        def stamp(path):
            try:
                st = os.stat(path)
                return (path, st.st_size,
                        getattr(st, 'st_mtime_ns', int(st.st_mtime * 1e9)))
            except OSError:
                return (path, None, None)

        if info.is_sequence:
            return tuple(stamp(member) for member in info.sequence_files)
        return (stamp(os.path.realpath(info.path)),)

    # -- image sequences ---------------------------------------------------
    def _sequence_step(self):
        if self._info is None:
            return
        if self._ordinal >= self._count:
            self._finish()
            return
        files = self._info.sequence_files
        index = min(len(files) - 1,
                    int((self._ordinal + .5) * len(files) / self._count))
        pixmap = self._decode_scaled(files[index], self._height)
        if pixmap is not None and not pixmap.isNull():
            self._publish(self._ordinal, pixmap)
        self._ordinal += 1
        self._seq_timer.start(0)

    def _decode_scaled(self, path, height):
        """Decode one still at (approximately) ``height`` px, cheaply.

        EXR goes through the dedicated reader; everything else uses a scaled
        ``QImageReader`` decode so large plates are never fully materialized.
        """
        if exr.is_exr(path):
            pixmap = exr.read_pixmap(path, max(64, height * 3))
            if pixmap is None or pixmap.isNull():
                return None
            if pixmap.height() != height:
                pixmap = pixmap.scaledToHeight(height, QtCore.Qt.SmoothTransformation)
            return pixmap
        reader = QtGui.QImageReader(path)
        size = reader.size()
        if size.isValid() and size.height() > height:
            width = max(1, int(size.width() * height / size.height()))
            reader.setScaledSize(QtCore.QSize(width, height))
        image = reader.read()
        if image.isNull():
            return None
        pixmap = QtGui.QPixmap.fromImage(image)
        if not pixmap.isNull() and pixmap.height() != height:
            pixmap = pixmap.scaledToHeight(height, QtCore.Qt.SmoothTransformation)
        return pixmap

    # -- movies ------------------------------------------------------------
    def _launch_movie_batch(self):
        for _ in range(self._concurrency):
            self._launch_next_movie()
        if not self._active:
            self._finish()

    def _launch_next_movie(self):
        if self._info is None or self._next_ordinal >= self._count:
            return
        ordinal = self._next_ordinal
        self._next_ordinal += 1
        info = self._info
        seconds = (ordinal + .5) * info.duration / self._count
        proc = configure_process(QtCore.QProcess(QtCore.QCoreApplication.instance()))
        token = self._generation
        proc.finished.connect(lambda *_: self._movie_done(proc, ordinal, token))
        proc.errorOccurred.connect(lambda *_: self._movie_error(proc, ordinal, token))
        proc.finished.connect(proc.deleteLater)
        proc.setProgram(self._ffmpeg)
        # ``-noaccurate_seek`` lands on the nearest keyframe before the input is
        # opened, skipping the forward decode an accurate seek would perform.
        proc.setArguments([
            '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-noaccurate_seek', '-ss', '{:.6f}'.format(seconds), '-i', info.path,
            '-threads', '1', '-an', '-frames:v', '1',
            '-vf', 'scale=-2:{}'.format(self._height),
            '-f', 'image2pipe', '-vcodec', 'mjpeg', '-q:v', '5', 'pipe:1'])
        self._active[proc] = ordinal
        proc.start()

    def _movie_done(self, proc, ordinal, token):
        if token != self._generation or proc not in self._active:
            return
        del self._active[proc]
        data = bytes(proc.readAllStandardOutput())
        pixmap = QtGui.QPixmap()
        if data and pixmap.loadFromData(data, 'JPG') and not pixmap.isNull():
            self._publish(ordinal, pixmap)
        self._advance_movies()

    def _movie_error(self, proc, ordinal, token):
        if token != self._generation or proc not in self._active:
            return
        if proc.error() == QtCore.QProcess.FailedToStart:
            del self._active[proc]
            proc.deleteLater()
            self._advance_movies()

    def _advance_movies(self):
        self._launch_next_movie()
        if not self._active and self._next_ordinal >= self._count:
            self._finish()

    # -- shared ------------------------------------------------------------
    def _publish(self, ordinal, pixmap):
        self._entries.append((ordinal, pixmap))
        self.produced.emit(ordinal, pixmap)

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
        procs = list(self._active)
        self._active = {}
        self._info = None
        self._entries = []
        for proc in procs:
            try:
                # Always disconnect before deleting so a killed proc's queued
                # finished/errorOccurred cannot fire into a deleted C++ object.
                proc.finished.disconnect()
                proc.errorOccurred.disconnect()
            except RuntimeError:
                pass
            try:
                if proc.state() != QtCore.QProcess.NotRunning:
                    proc.kill()
                    if wait:
                        proc.waitForFinished(1500)
                proc.deleteLater()
            except RuntimeError:
                pass
