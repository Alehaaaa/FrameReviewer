# -*- coding: utf-8 -*-
"""Directional, memory-bounded rolling video cache with threaded FFmpeg decode.

JPEG parsing and QImage decoding happen in a QThread, never in Maya's GUI
thread. QPixmap creation happens in small, rate-limited batches in the GUI
thread because pixmaps are GUI resources. A bounded worker semaphore prevents
queued QImages from accumulating without limit when Maya's UI is busy.

A new playhead location starts decoding at the playhead, grows forward first,
retains previously displayed frames behind; ongoing forward decoders survive rolling-window shifts.
There is no disk cache, poster extraction, or blocking QThread.wait on exit.
"""
from __future__ import absolute_import

import collections
import os
import subprocess
import threading
import time

from .qtcompat import QtCore, QtGui, Signal, Slot
from . import utils
from .ffmpegtools import media_environment, _no_window_kwargs

# VC_SEAMLESS_THREADED_CACHE_V4
DECODE_WIDTH = 960
CACHE_BYTES = 144 * 1024 * 1024
CACHE_FRAMES = 64
BATCH_SIZE = 40
SHIFT_STEP = 12
TRANSFER_LIMIT = 8            # Max decoded QImages waiting for GUI upload.
UPLOAD_PER_TICK = 1           # One QPixmap upload per UI tick: avoid frame spikes.
UPLOAD_INTERVAL_MS = 8
SEEK_SETTLE_MS = 45           # Coalesce fast timeline scrubs before launching FFmpeg.
MAX_RUNNING_DECODERS = 3      # Includes workers still shutting down after a seek.
DECODE_STALL_SECONDS = 12.0  # Time waiting for FFmpeg to produce more stdout.
MAX_JPEG_BYTES = 16 * 1024 * 1024


def split_jpegs(buffer):
    """Split complete JPEG images from FFmpeg's image2pipe byte stream."""
    frames = []
    offset = 0
    while True:
        start = buffer.find(b'\xff\xd8', offset)
        if start < 0:
            break
        end = buffer.find(b'\xff\xd9', start + 2)
        if end < 0:
            return frames, buffer[start:]
        frames.append(bytes(buffer[start:end + 2]))
        offset = end + 2
    return frames, bytearray()


def window_for_playhead(frame, capacity, first, last, direction=1):
    """One-third behind, two-thirds ahead of the frame in playback direction."""
    first = int(first)
    last = max(first, int(last))
    frame = int(utils.clamp(frame, first, last))
    count = min(last - first + 1, max(1, int(capacity)))
    behind = (count - 1) // 3
    ahead = count - 1 - behind
    if direction < 0:
        behind, ahead = ahead, behind
    low, high = frame - behind, frame + ahead
    if low < first:
        high += first - low
        low = first
    if high > last:
        low -= high - last
        high = last
    return max(first, low), min(last, high)


def contiguous_ranges(frames):
    """Compress actual resident frame numbers into intervals for painting."""
    ranges = []
    for frame in sorted(frames):
        if ranges and frame <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(frame, ranges[-1][1]))
        else:
            ranges.append((frame, frame))
    return ranges


class _DecodeWorker(QtCore.QThread):
    """Only background thread interacts with subprocess pipes or JPEG decode.

    Worker QObject lives in GUI thread as expected by Qt; only `run()` does
    blocking work. Signals carry QImages, never QPixmaps, across threads.
    """
    imageReady = Signal(int, int, object)        # generation, index, QImage
    decodeDone = Signal(int, bool, str, int)    # generation, success, error, frames emitted

    def __init__(self, ffmpeg, source, fps, start, end, generation, parent=None):
        super(_DecodeWorker, self).__init__(parent)
        self._ffmpeg = ffmpeg
        self._source = source
        self._fps = fps
        self.start_frame = int(start)
        self.end_frame = int(end)
        self.generation = generation
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._proc = None
        self._permits = threading.Semaphore(TRANSFER_LIMIT)
        self._read_started = None
        self._timed_out = False

    def release_image(self):
        self._permits.release()

    def cancel(self):
        """Request prompt shutdown without waiting or blocking the UI."""
        self._cancel.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()
            except (OSError, ValueError):
                pass

    def _watch_output(self, proc, stopped):
        """Unblock a stalled pipe read without involving Maya's event loop.

        Waiting for GUI upload permits is intentionally excluded: a busy GUI
        must not cause otherwise healthy FFmpeg decoding to time out.
        """
        while not stopped.wait(0.25):
            with self._lock:
                started = self._read_started
                if (started is None or self._cancel.is_set() or
                        time.monotonic() - started <= DECODE_STALL_SECONDS):
                    continue
                self._timed_out = True
                self._read_started = None
            try:
                if proc.poll() is None:
                    proc.kill()
            except (OSError, ValueError):
                pass
            return

    def run(self):
        proc = None
        completed = 0
        received = 0
        success = False
        message = ''
        stderr_parts = collections.deque(maxlen=8)
        monitor_stop = threading.Event()
        stderr_thread = None
        try:
            args = [
                self._ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
                '-ss', '{:.7f}'.format(self.start_frame / self._fps),
                '-i', self._source,
                '-an', '-vsync', '0',
                '-frames:v', str(self.end_frame - self.start_frame + 1),
                '-threads', '2',
                '-vf', "scale='min({},iw)':-2".format(DECODE_WIDTH),
                '-f', 'image2pipe', '-vcodec', 'mjpeg', '-q:v', '5', 'pipe:1',
            ]
            kwargs = dict(stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          bufsize=0, env=media_environment())
            kwargs.update(_no_window_kwargs())
            proc = subprocess.Popen(args, **kwargs)
            with self._lock:
                self._proc = proc
            def capture_stderr():
                while True:
                    data = proc.stderr.read(2048)
                    if not data:
                        break
                    stderr_parts.append(data)
            stderr_thread = threading.Thread(target=capture_stderr, daemon=True)
            stderr_thread.start()
            threading.Thread(target=self._watch_output,
                             args=(proc, monitor_stop), daemon=True).start()
            if self._cancel.is_set():
                proc.kill()
            remaining = bytearray()
            frame = self.start_frame
            while frame <= self.end_frame and not self._cancel.is_set():
                with self._lock:
                    self._read_started = time.monotonic()
                try:
                    chunk = proc.stdout.read(65536)
                finally:
                    with self._lock:
                        self._read_started = None
                if not chunk:
                    break
                remaining.extend(chunk)
                blobs, remaining = split_jpegs(remaining)
                if len(remaining) > MAX_JPEG_BYTES:
                    raise RuntimeError('FFmpeg emitted an oversized or incomplete JPEG frame.')
                for blob in blobs:
                    if self._cancel.is_set() or frame > self.end_frame:
                        break
                    image = QtGui.QImage.fromData(blob, 'JPG')
                    index = frame
                    frame += 1
                    received += 1
                    if image.isNull():
                        continue
                    while not self._cancel.is_set():
                        if self._permits.acquire(timeout=0.05):
                            break
                    else:
                        break
                    if self._cancel.is_set():
                        self._permits.release()
                        break
                    self.imageReady.emit(self.generation, index, image)
                    completed += 1
            if not self._cancel.is_set():
                try:
                    # -frames:v caps FFmpeg's output to this exact batch.
                    # Keep the read end open until FFmpeg exits: closing it as
                    # soon as the last JPEG arrives can give a healthy FFmpeg
                    # process EPIPE/SIGPIPE while it finishes flushing.
                    code = proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    code = proc.wait(timeout=2)
                    message = 'FFmpeg did not exit after delivering frames.'
                if proc.stdout is not None:
                    proc.stdout.close()
                if stderr_thread is not None:
                    stderr_thread.join(timeout=0.5)
                stderr_tail = b''.join(stderr_parts)[-1800:]
                # Some FFmpeg versions return an encoder-init error at an
                # out-of-range seek even though no input frames exist. This
                # specific "before EOF" + empty-output combination is EOF,
                # provided the request started after frame zero.
                empty_eof = (not received and self.start_frame > 0 and
                             b'Could not open encoder before EOF' in stderr_tail and
                             b'Nothing was written into output file' in stderr_tail)
                if self._timed_out:
                    message = 'FFmpeg stalled for over {:.0f} seconds without output.'.format(
                        DECODE_STALL_SECONDS)
                elif message:
                    pass
                elif code != 0 and not empty_eof:
                    message = 'FFmpeg exited with code {} ({} frames decoded).'.format(
                        code, completed)
                elif received and completed != received:
                    message = 'FFmpeg produced {} unreadable JPEG frame(s).'.format(
                        received - completed)
                elif not received and self.start_frame == 0:
                    message = 'FFmpeg produced no decodable video frames.'
                elif remaining:
                    message = 'FFmpeg finished with an incomplete JPEG frame.'
                success = not message
                if not success:
                    # stderr is consumed on its own thread so FFmpeg cannot
                    # deadlock if it emits a large amount of diagnostic output.
                    tail = stderr_tail.decode('utf-8', 'replace').strip()
                    if tail:
                        message += '\n' + tail
        except Exception as exc:
            if not self._cancel.is_set():
                message = str(exc)
        finally:
            monitor_stop.set()
            if proc is not None:
                try:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=2)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()
            with self._lock:
                self._proc = None
            self.decodeDone.emit(self.generation, success or self._cancel.is_set(),
                                 message, completed)


class VideoFrameSource(QtCore.QObject):
    """Rolling frame store and non-blocking threaded FFmpeg scheduler."""
    frameAvailable = Signal(int)
    errorText = Signal(str)
    cacheChanged = Signal(object, object)       # cached ranges, planned window
    endReached = Signal(int)                   # Actual last frame on clean early EOF.

    def __init__(self, ffmpeg, source, fps, frame_count, parent=None):
        super(VideoFrameSource, self).__init__(parent)
        self._ffmpeg = ffmpeg
        self._source = source
        self._fps = max(0.001, float(fps))
        self._count = max(1, int(frame_count))
        self.last_available_frame = self._count - 1
        self._first, self._last = 0, self._count - 1
        self._loop_start = 0
        self._loop_enabled = False
        self._direction = 1
        self._current = 0
        self._capacity = CACHE_FRAMES
        self._window_capacity = CACHE_FRAMES
        self._window = window_for_playhead(0, CACHE_FRAMES, 0, self._last)
        self._cache = collections.OrderedDict()
        self._cache_bytes = 0
        self._pending = collections.deque()     # (worker, index, QImage)
        self._worker = None
        self._completion = None
        self._workers = set()                   # includes cancellation in progress
        self._generation = 0
        self._failed = False
        self._closed = False
        self._state_timer = QtCore.QTimer(self)
        self._state_timer.setSingleShot(True)
        self._state_timer.setInterval(40)
        self._state_timer.timeout.connect(self._emit_state)
        self._upload_timer = QtCore.QTimer(self)
        self._upload_timer.setInterval(UPLOAD_INTERVAL_MS)
        self._upload_timer.timeout.connect(self._upload)
        self._seek_timer = QtCore.QTimer(self)
        self._seek_timer.setSingleShot(True)
        self._seek_timer.setInterval(SEEK_SETTLE_MS)
        self._seek_timer.timeout.connect(self._schedule)

    @property
    def cache_bytes(self):
        return self._cache_bytes

    def cached_ranges(self):
        return contiguous_ranges(self._cache)

    def cache_window(self):
        return self._window

    def set_playback_range(self, start, end):
        self._first = int(utils.clamp(start, 0, self._count - 1))
        self._last = int(utils.clamp(end, self._first, self._count - 1))
        self._loop_start = self._first
        self._move_window(self._current, force=True)
        self._schedule()

    def set_loop_start(self, frame):
        self._loop_start = int(utils.clamp(frame, self._first, self._last))
        self._mark_state()

    def set_loop(self, enabled):
        self._loop_enabled = bool(enabled)
        self._move_window(self._current, force=True)
        self._schedule()

    def get(self, frame):
        frame = int(frame)
        image = self._cache.get(frame)
        if image is not None:
            self._cache.move_to_end(frame)
        return image

    def ensure(self, frame, direction=1):
        if self._closed or self._failed:
            return
        frame = int(utils.clamp(frame, 0, self.last_available_frame))
        large_seek = abs(frame - self._current) > max(4, self._capacity // 8)
        self._direction = 1 if direction >= 0 else -1
        self._move_window(frame)
        if large_seek:
            # Frequent slider events must not create one subprocess and QThread
            # per intermediate position. Cancel obsolete work immediately and
            # decode only the latest requested frame after the slider settles.
            if (self._worker is not None and
                    not self._worker.start_frame <= frame <= self._worker.end_frame):
                self._cancel_worker()
            self._seek_timer.start()
        else:
            self._schedule()

    def _move_window(self, frame, force=False):
        self._current = int(utils.clamp(frame, 0, self.last_available_frame))
        # The marked range constrains playback, not inspection. When scrubbing
        # outside it, build a temporary cache window around the requested frame.
        if self._first <= self._current <= self._last:
            window_first, window_last = self._first, min(self._last, self.last_available_frame)
        else:
            window_first, window_last = 0, self.last_available_frame
        reserve = min(3, self._capacity - 1) if self._loop_enabled else 0
        capacity = max(1, self._capacity - reserve)
        desired = window_for_playhead(self._current, capacity,
                                      window_first, window_last, self._direction)
        if not force and self._window_capacity == capacity:
            low, high = self._window
            if low <= self._current <= high:
                if self._direction >= 0:
                    runway = high - self._current
                    ideal = capacity - 1 - (capacity - 1) // 3
                else:
                    runway = self._current - low
                    ideal = capacity - 1 - (capacity - 1) // 3
                if runway >= max(1, ideal - SHIFT_STEP):
                    return
        if force or desired != self._window or capacity != self._window_capacity:
            self._window_capacity = capacity
            self._window = desired
            self._prune()
            self._mark_state()

    def _preserve(self, frame):
        if not self._loop_enabled:
            return False
        if self._direction < 0:
            last = min(self._last, self.last_available_frame)
            return max(self._first, last - 2) <= frame <= last
        return self._loop_start <= frame <= min(
            self._loop_start + 2, self._last, self.last_available_frame)

    def _remove(self, frame):
        image = self._cache.pop(frame, None)
        if image is not None:
            self._cache_bytes -= image.width() * image.height() * 4

    def _prune(self):
        low, high = self._window
        for frame in list(self._cache):
            if not low <= frame <= high and not self._preserve(frame):
                self._remove(frame)
        while self._cache and (len(self._cache) > self._capacity or
                               self._cache_bytes > CACHE_BYTES):
            candidates = [f for f in self._cache if not self._preserve(f)]
            if not candidates:
                candidates = list(self._cache)
            victim = max(candidates, key=lambda f: (f < low or f > high,
                                                    abs(f - self._current)))
            self._remove(victim)

    def _first_missing(self):
        low, high = self._window
        current = self._current
        if current not in self._cache:
            return current, high
        if self._direction >= 0:
            # Fill from current -> forward only. The 1/3 behind is retained
            # from frames already played, not redecoded by spawning one FFmpeg
            # process per historical frame (that was a major CPU spike).
            for index in range(current + 1, high + 1):
                if index not in self._cache:
                    return index, high
        else:
            # FFmpeg is a forward decoder. For explicit reverse prefetch,
            # decode a small segment ending at the nearest missing frame.
            for index in range(current - 1, low - 1, -1):
                if index not in self._cache:
                    return max(low, index - BATCH_SIZE + 1), index
        if self._loop_enabled and self._first <= current <= self._last:
            # Fill the opposite edge before wrapping; reserve only three
            # cached frames there, inside the same existing capacity limit.
            threshold = min(15, max(4, self._capacity // 5))
            playback_last = min(self._last, self.last_available_frame)
            if self._direction >= 0 and playback_last - current <= threshold:
                start = self._loop_start
                stop = min(start + 2, playback_last)
                for index in range(start, stop + 1):
                    if index not in self._cache:
                        return index, stop
            elif self._direction < 0 and current - self._first <= threshold:
                stop = playback_last
                start = max(self._first, stop - 2)
                for index in range(stop, start - 1, -1):
                    if index not in self._cache:
                        return start, stop
        return None

    def _schedule(self):
        if self._closed or self._failed or self._seek_timer.isActive():
            return
        target = self._first_missing()
        if self._worker is not None:
            if target is None:
                return
            requested = target[0]
            worker = self._worker
            # Window shifts do not interrupt a decoder supplying the next frames.
            if worker.start_frame <= requested <= worker.end_frame:
                return
            if (self._current in self._cache and self._direction >= 0 and
                    worker.start_frame <= self._current + 1 <= worker.end_frame):
                return
            # A true seek to an uncached region takes precedence over old work.
            if self._current not in self._cache or requested < worker.start_frame or requested > worker.end_frame:
                self._cancel_worker()
        if self._worker is not None or self._pending:
            return
        if target is None:
            return
        live_workers = 0
        for worker in tuple(self._workers):
            try:
                live_workers += int(worker.isRunning())
            except RuntimeError:
                self._workers.discard(worker)
        if live_workers >= MAX_RUNNING_DECODERS:
            # Cancelled workers can still be flushing and releasing their
            # subprocess pipes. Avoid spawning unlimited decoders under scrub.
            self._seek_timer.start()
            return
        start, maximum = target
        # `maximum` already belongs to the active cache window. That window can
        # intentionally be outside the marked playback range while inspecting
        # source frames, so clamping it to `_last` can create stop < start.
        stop = min(maximum, start + BATCH_SIZE - 1, self.last_available_frame)
        if stop < start:
            return
        self._generation += 1
        worker = _DecodeWorker(self._ffmpeg, self._source, self._fps,
                               start, stop, self._generation,
                               QtCore.QCoreApplication.instance())
        worker.imageReady.connect(self._on_image)
        worker.decodeDone.connect(self._on_done)
        worker.finished.connect(self._collect_workers)
        self._workers.add(worker)
        self._worker = worker
        worker.start()

    @Slot()
    def _collect_workers(self):
        """Delete a thread only after every pending image has been uploaded.

        Deleting a QThread at finished() while queued QImages still reference
        its Python wrapper risks touching an invalid Shiboken object on upload.
        """
        for worker in tuple(self._workers):
            if worker is self._worker or any(
                    pending_worker is worker for pending_worker, _, _ in self._pending):
                continue
            try:
                if not worker.isRunning():
                    self._workers.discard(worker)
                    worker.deleteLater()
            except RuntimeError:
                self._workers.discard(worker)

    def _cancel_worker(self):
        worker = self._worker
        self._worker = None
        self._completion = None
        if worker is not None:
            worker.cancel()
        while self._pending:
            old_worker, _, _ = self._pending.popleft()
            old_worker.release_image()
        self._upload_timer.stop()

    @Slot(int, int, object)
    def _on_image(self, generation, frame, image):
        worker = self._worker
        if (self._closed or worker is None or
                worker.generation != generation):
            # The cancelled worker releases outstanding tokens as it notices
            # shutdown; prevent leaked worker-blocking transfers on a seek.
            for w in tuple(self._workers):
                if w.generation == generation:
                    w.release_image()
                    break
            return
        self._pending.append((worker, int(frame), image))
        if not self._upload_timer.isActive():
            self._upload_timer.start()

    @Slot()
    def _upload(self):
        count = 0
        while self._pending and count < UPLOAD_PER_TICK:
            worker, frame, image = self._pending.popleft()
            try:
                if (not self._closed and self._worker is worker and
                        not image.isNull()):
                    pixmap = QtGui.QPixmap.fromImage(image)
                    size = pixmap.width() * pixmap.height() * 4
                    if size > 0:
                        # Recalculate capacity whenever frame dimensions change.
                        capacity = min(CACHE_FRAMES, max(1, CACHE_BYTES // size))
                        if capacity != self._capacity:
                            self._capacity = capacity
                            self._move_window(self._current, force=True)
                        self._remove(frame)
                        self._cache[frame] = pixmap
                        self._cache_bytes += size
                        self._prune()
                        self.frameAvailable.emit(frame)
                        self._mark_state()
            finally:
                worker.release_image()
            count += 1
        if not self._pending:
            self._upload_timer.stop()
            if self._completion is not None:
                self._complete_worker(*self._completion)
            else:
                self._schedule()

    @Slot(int, bool, str, int)
    def _on_done(self, generation, success, message, decoded):
        worker = self._worker
        if worker is None or worker.generation != generation or self._closed:
            return
        if self._pending:
            # Uploads must finish before releasing the active worker. Otherwise
            # the last queued images would be silently discarded at each batch.
            self._completion = (success, message, decoded)
            return
        self._complete_worker(success, message, decoded)

    def _complete_worker(self, success, message, decoded):
        worker = self._worker
        self._completion = None
        self._worker = None
        self._collect_workers()
        if not success:
            self._failed = True
            self.errorText.emit('FFmpeg background decoding failed: ' + message)
        else:
            # FFmpeg may correctly hit end-of-stream before reaching the
            # requested frame cap. Treat a clean short batch as EOF, record
            # its upper bound, and never schedule those absent frames again.
            if worker is not None:
                requested = worker.end_frame - worker.start_frame + 1
                if decoded < requested:
                    actual_last = max(0, worker.start_frame + decoded - 1)
                    if actual_last < self.last_available_frame:
                        self.last_available_frame = actual_last
                        self._move_window(self._current, force=True)
                        self.endReached.emit(actual_last)
            self._schedule()
        self._mark_state()

    def _mark_state(self):
        if not self._closed and not self._state_timer.isActive():
            self._state_timer.start()

    def _emit_state(self):
        if not self._closed:
            self.cacheChanged.emit(self.cached_ranges(), self._window)

    def shutdown(self, wait=True):
        self._closed = True
        self._seek_timer.stop()
        self._upload_timer.stop()
        self._state_timer.stop()
        self._cancel_worker()
        workers = tuple(self._workers)
        for worker in workers:
            worker.cancel()
        stopped = True
        if wait:
            for worker in workers:
                try:
                    if worker.isRunning() and not worker.wait(2000):
                        stopped = False
                except RuntimeError:
                    pass
            self._collect_workers()
        self._cache.clear()
        self._cache_bytes = 0
        return stopped
