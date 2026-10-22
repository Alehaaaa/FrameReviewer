# -*- coding: utf-8 -*-
"""Bounded frame caches and asynchronous frame decoding for Maya.

FFmpeg pipes compressed JPEGs directly into memory. There are no temporary
frame directories, per-frame subprocesses, blocking thread joins, or polling
of the filesystem. Decoder requests are guarded by process identity so stale output
cannot enter the cache after a seek or media change.
"""
from __future__ import absolute_import

import collections

from .qtcompat import QtCore, QtGui, Signal
from . import utils

DECODE_WIDTH = 960
CACHE_BYTES = 144 * 1024 * 1024


class SequenceSource(QtCore.QObject):
    """Sequence reader with scaled images and a byte-bounded display cache."""
    frameAvailable = Signal(int)
    cacheChanged = Signal(object, object)

    def __init__(self, files, parent=None):
        QtCore.QObject.__init__(self, parent)
        self._files = list(files)
        self._cache = collections.OrderedDict()
        self._cache_bytes = 0

    def get(self, frame):
        if frame in self._cache:
            self._cache.move_to_end(frame)
            return self._cache[frame]
        if not (0 <= frame < len(self._files)):
            return None
        reader = QtGui.QImageReader(self._files[frame])
        original = reader.size()
        if original.isValid() and original.width() > DECODE_WIDTH:
            target_height = max(1, int(original.height() * DECODE_WIDTH / original.width()))
            reader.setScaledSize(QtCore.QSize(DECODE_WIDTH, target_height))
        image = reader.read()
        if image.isNull():
            return None
        if image.width() > DECODE_WIDTH:
            image = image.scaledToWidth(DECODE_WIDTH, QtCore.Qt.SmoothTransformation)
        pixmap = QtGui.QPixmap.fromImage(image)
        self._cache[frame] = pixmap
        self._cache_bytes += pixmap.width() * pixmap.height() * 4
        while len(self._cache) > 32 or self._cache_bytes > CACHE_BYTES:
            _, evicted = self._cache.popitem(last=False)
            self._cache_bytes -= evicted.width() * evicted.height() * 4
        frames = sorted(self._cache)
        ranges = []
        for f in frames:
            if ranges and f <= ranges[-1][1] + 1:
                ranges[-1] = (ranges[-1][0], f)
            else:
                ranges.append((f, f))
        self.cacheChanged.emit(ranges, None)
        return pixmap

    def ensure(self, frame):
        return

    def set_loop_start(self, frame):
        return

    def cache_snapshot(self):
        return dict(mode='sequence', frames=sorted(self._cache),
                    cached=len(self._cache), capacity=32, bytes=self._cache_bytes,
                    decoded=len(self._cache), total=min(32, len(self._files)))

    def shutdown(self):
        self._cache.clear()
        self._cache_bytes = 0


# VC_ROLLING_CACHE_RANGE_V1

class FramePump(QtCore.QObject):
    """Elapsed-clock playback that holds the last good frame during buffering."""
    frameChanged = Signal(int)
    stopped = Signal()
    bufferingChanged = Signal(bool)

    def __init__(self, source, fps, frame_count, parent=None):
        QtCore.QObject.__init__(self, parent)
        self._source = source
        self._fps = max(0.001, float(fps))
        self._last_frame = max(0, int(frame_count) - 1)
        self._timer = QtCore.QTimer(self)
        self._timer.setTimerType(QtCore.Qt.PreciseTimer)
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._tick)
        self._clock = QtCore.QElapsedTimer()
        self._anchor = 0
        self.frame = 0
        self.in_frame = 0
        self.out_frame = 0
        self.loop = False
        self.speed = 1.0
        self._buffering = False
        self._external_clock = None
        self._external_wrap = None
        # Decode a short contiguous runway before starting or resuming. This
        # absorbs FFmpeg/GUI scheduling jitter without adding perceptible wait.
        self._runway_frames = max(3, min(12, int(round(self._fps * 0.25))))

    def set_external_clock(self, get_ms, on_wrap=None):
        """Use soundtrack time as playback master when audio is available."""
        self._external_clock = get_ms
        self._external_wrap = on_wrap

    def is_buffering(self):
        return self._buffering

    def _set_buffering(self, buffering):
        buffering = bool(buffering)
        if self._buffering != buffering:
            self._buffering = buffering
            self.bufferingChanged.emit(buffering)

    def is_playing(self):
        return self._timer.isActive()

    def _has_runway(self):
        checker = getattr(self._source, 'contiguous_available', None)
        if checker is not None:
            return checker(self.frame, self._runway_frames, 1)
        return self._source.get(self.frame) is not None

    def _effective_out(self):
        """Honor a clean FFmpeg EOF earlier than ffprobe's frame estimate."""
        return min(self.out_frame,
                   getattr(self._source, 'last_available_frame', self._last_frame))

    def play(self):
        if self._timer.isActive():
            return
        end = self._effective_out()
        if self.in_frame >= end:
            self.seek(end, restrict_to_range=False)
            self.stopped.emit()
            return
        if self.frame < self.in_frame or self.frame >= end:
            self.seek(self.in_frame)
        self._anchor = self.frame
        self._clock.start()
        self._source.ensure(self.frame)
        self._set_buffering(not self._has_runway())
        self._timer.start()

    def pause(self):
        if self._timer.isActive():
            self._timer.stop()
            self.stopped.emit()
        self._set_buffering(False)

    def set_speed(self, speed):
        self.speed = max(0.01, float(speed))
        if self.is_playing():
            self._anchor = self.frame
            self._clock.restart()

    def seek(self, frame, restrict_to_range=True):
        low = self.in_frame if restrict_to_range else 0
        high = self.out_frame if restrict_to_range else self._last_frame
        high = min(high, getattr(self._source, 'last_available_frame', self._last_frame))
        low = min(low, high)
        self.frame = int(utils.clamp(frame, low, high))
        self._source.ensure(self.frame)
        self._set_buffering(not self._has_runway())
        if self.is_playing():
            self._anchor = self.frame
            self._clock.restart()
        self.frameChanged.emit(self.frame)

    def _tick(self):
        end = self._effective_out()
        if self.in_frame >= end:
            self.pause()
            return
        if self._buffering:
            self._source.ensure(self.frame)
            if not self._has_runway():
                return
            self._set_buffering(False)
            self._anchor = self.frame
            self._clock.restart()
        if self._external_clock is not None:
            position_ms = self._external_clock()
            if position_ms is not None:
                candidate = utils.ms_to_frame(position_ms, self._fps)
                if candidate > end:
                    if self.loop:
                        if self._external_wrap is not None:
                            self._external_wrap(self.in_frame)
                        self.seek(self.in_frame)
                    else:
                        self.seek(end, restrict_to_range=False)
                        self.pause()
                    return
                candidate = int(utils.clamp(candidate, self.in_frame, end))
                self._source.ensure(candidate)
                if self._source.get(candidate) is None:
                    # Pause both clocks until a contiguous runway is ready;
                    # resuming one frame at a time creates visible judder.
                    self._set_buffering(True)
                    return
                self._set_buffering(False)
                if candidate != self.frame:
                    self.frame = candidate
                    self.frameChanged.emit(self.frame)
                return
        if self.frame > end:
            self.seek(end, restrict_to_range=False)
        if not self.loop and self.frame >= end:
            self.pause()
            return
        if self._source.get(self.frame) is None:
            self._set_buffering(True)
            self._source.ensure(self.frame)
            self._anchor = self.frame
            self._clock.restart()
            return
        advance = int(self._clock.elapsed() * self._fps * self.speed / 1000.0)
        candidate = self._anchor + advance
        if candidate <= self.frame:
            self._set_buffering(False)
            self._source.ensure(self.frame)
            return
        if candidate > end:
            if not self.loop:
                candidate = end
            else:
                candidate = self.in_frame + (candidate - end - 1) % (
                    end - self.in_frame + 1)
        self._source.ensure(self.frame)
        if self._source.get(candidate) is None:
            self._set_buffering(True)
            # Seek ahead of the existing decode window when a high playback
            # rate or GUI hiccup skips over the prefetched frames. Otherwise
            # the clock can repeatedly wait for a frame nobody requested.
            self._source.ensure(candidate)
            self._anchor = self.frame
            self._clock.restart()
            return
        self._set_buffering(False)
        self.frame = candidate
        self.frameChanged.emit(self.frame)
        if not self.loop and self.frame >= end:
            self.pause()
        else:
            self._source.ensure(self.frame)

    def pixmap(self):
        return self._source.get(self.frame)
