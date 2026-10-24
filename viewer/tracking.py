# -*- coding: utf-8 -*-
"""Optical-flow crop tracking with stable translation/similarity/affine/projective models.

All tracking takes place in the same post-flip image coordinates as the UI and
export. Optical flow uses several image features, even in 1-point mode; the
mode controls the degrees of freedom in the fitted transform. The exported
quadrilateral carries distortion information that a centre alone would lose.
OpenCV and NumPy are optional until the user explicitly requests tracking.
"""
from __future__ import absolute_import

import os
import select
import subprocess
import time

from ..core.qtcompat import QtCore, Signal
from ..media.ffmpeg import _no_window_kwargs, media_environment

try:
    import cv2
    import numpy as np
    HAVE_CV2 = True
except ImportError:
    cv2 = None
    np = None
    HAVE_CV2 = False


MODES = (
    (1, '1 Point · Position'),
    (2, '2 Points · Rotation / Scale'),
    (3, '3 Points · Affine'),
    (4, 'Perspective · 4+ Features'),
)


def _crop_corners(crop):
    x, y, w, h = crop
    return np.array([[x, y], [x + w, y], [x + w, y + h],
                     [x, y + h]], dtype=np.float64)


def _transformed_points(matrix, points):
    """Apply a homogeneous 3×3 matrix to a list of xy coordinates."""
    homogeneous = np.concatenate(
        (points, np.ones((len(points), 1), dtype=np.float64)), axis=1)
    output = np.dot(homogeneous, matrix.T)
    denominators = output[:, 2:3]
    if np.any(np.abs(denominators) < 1e-7):
        raise ValueError('Degenerate tracking perspective')
    return output[:, :2] / denominators


def _motion(previous_points, next_points, mode):
    """Find robust frame-to-frame motion; return None for insufficient matches."""
    matrix, _ = _fit_motion(previous_points, next_points, mode)
    return matrix


def _fit_motion(previous_points, next_points, mode):
    """Fit motion with an explicit measured inlier mask.

    The minimum support exceeds the algebraic minimum: a single LK corner can
    produce a translation, but cannot demonstrate that the crop still tracks.
    """
    a = np.asarray(previous_points, dtype=np.float32).reshape((-1, 2))
    b = np.asarray(next_points, dtype=np.float32).reshape((-1, 2))
    minimum = {1: 4, 2: 4, 3: 5, 4: 6}[mode]
    inliers = np.zeros(len(a), dtype=bool)
    if len(a) < minimum:
        return None, inliers
    if mode == 1:
        # Median flow and residual filtering use real correspondences, not an
        # artificial point at the crop centre.
        translation = np.median(b - a, axis=0)
        inliers = np.linalg.norm(b - a - translation, axis=1) <= 2.25
        if np.count_nonzero(inliers) < minimum:
            return None, inliers
        translation = np.median((b - a)[inliers], axis=0)
        result = np.eye(3, dtype=np.float64)
        result[:2, 2] = translation
        return result, inliers
    if mode == 2:
        affine, mask = cv2.estimateAffinePartial2D(
            a, b, method=cv2.RANSAC, ransacReprojThreshold=2.25,
            maxIters=1000, confidence=0.98)
    elif mode == 3:
        affine, mask = cv2.estimateAffine2D(
            a, b, method=cv2.RANSAC, ransacReprojThreshold=2.25,
            maxIters=1000, confidence=0.98)
    else:
        matrix, mask = cv2.findHomography(
            a, b, cv2.RANSAC, 2.25, maxIters=1000, confidence=0.98)
        if mask is not None:
            inliers = np.asarray(mask, dtype=bool).reshape(-1)
        if matrix is None or np.count_nonzero(inliers) < minimum:
            return None, inliers
        return np.asarray(matrix, dtype=np.float64), inliers
    if mask is not None:
        inliers = np.asarray(mask, dtype=bool).reshape(-1)
    if affine is None or np.count_nonzero(inliers) < minimum:
        return None, inliers
    matrix = np.eye(3, dtype=np.float64)
    matrix[:2, :] = affine
    return matrix, inliers


def _tracking_quality(index, source_frame, feature_count, tracked_count,
                      inlier_count, fb_error, reprojection_error, status):
    """JSON-safe observation; confidence is a quality heuristic, not probability."""
    lost = status.startswith('lost_')
    if status == 'reference':
        confidence = None  # No frame-to-frame motion measured yet.
    elif lost:
        confidence = 0.0
    else:
        coverage = min(1.0, inlier_count / 20.0)
        ratio = inlier_count / float(max(1, feature_count))
        accuracy = 1.0 / (1.0 + fb_error / 1.75 +
                          reprojection_error / 2.25)
        confidence = round(min(1.0, coverage * ratio * accuracy), 4)
    return {
        'frame': int(index), 'source_frame': int(source_frame),
        'feature_count': int(feature_count),
        'tracked_count': int(tracked_count),
        'inlier_count': int(inlier_count),
        'inlier_ratio': (round(inlier_count / float(tracked_count), 4)
                         if tracked_count else None),
        'fb_error_px': (round(float(fb_error), 4)
                        if fb_error is not None else None),
        'reprojection_error_px': (round(float(reprojection_error), 4)
                                  if reprojection_error is not None else None),
        'confidence': confidence, 'lost': bool(lost), 'status': status,
    }


def _plausible_motion(matrix, corners, width, height):
    """Reject LK outlier estimates that cause a one-frame crop explosion."""
    if not np.all(np.isfinite(matrix)):
        return False
    try:
        moved = _transformed_points(matrix, corners)
    except ValueError:
        return False
    size = np.linalg.norm(moved - np.roll(moved, 1, axis=0), axis=1)
    original = np.linalg.norm(corners - np.roll(corners, 1, axis=0), axis=1)
    ratios = size / np.maximum(original, 1.0)
    if np.any(ratios < 0.65) or np.any(ratios > 1.5):
        return False
    distance = np.linalg.norm(moved.mean(axis=0) - corners.mean(axis=0))
    return bool(distance <= max(40, .24 * max(width, height)))


def _smooth_quad(previous, current):
    """Light low-pass filtering to suppress subpixel jitter during playback."""
    if previous is None:
        return current
    movement = np.linalg.norm(current - previous, axis=1).max()
    # Larger intentional motion is followed promptly. Small noisy changes are
    # filtered more strongly; still frames settle without snap-back.
    alpha = .58 if movement < 1.5 else (.74 if movement < 6.0 else .91)
    filtered = previous + alpha * (current - previous)
    if movement < .12:
        return previous
    return filtered


class TrackingJob(QtCore.QThread):
    progress = Signal(int)
    # Centres are kept for existing consumers; quads encode perspective.
    result = Signal(object, object, object)  # centres, crop_size, quadrilaterals
    quality_ready = Signal(object)  # {dense playhead: per-frame quality observation}
    error = Signal(str)

    def __init__(self, ffmpeg, path, seq_files, fps, start, end, crop,
                 flip_h, flip_v, parent=None, mode=1, frame_size=None,
                 frame_numbers=None, existing_source_frames=None):
        QtCore.QThread.__init__(self, parent)
        self.ffmpeg = ffmpeg
        self.path = path
        self.seq_files = list(seq_files or [])
        # Display/playhead indices are dense; disk filenames carry original
        # signed source labels, which may be sparse or begin below zero.
        # Never index `seq_files[playhead]` after missing-frame expansion.
        if self.seq_files and (frame_numbers is not None or
                               existing_source_frames is not None):
            if frame_numbers is None or existing_source_frames is None:
                raise ValueError('Tracking requires both full timeline labels '
                                 'and existing source frame labels.')
            labels = [int(value) for value in frame_numbers]
            existing = [int(value) for value in existing_source_frames]
            if (len(existing) != len(self.seq_files) or
                    len(labels) != len(set(labels)) or
                    len(existing) != len(set(existing)) or
                    labels != sorted(labels) or existing != sorted(existing) or
                    not set(existing).issubset(labels)):
                raise ValueError('Tracking received an invalid signed/sparse '
                                 'source-frame mapping.')
            self.frame_numbers = tuple(labels)
            self._source_paths = dict(zip(existing, self.seq_files))
        else:
            self.frame_numbers = tuple(range(len(self.seq_files)))
            self._source_paths = dict(enumerate(self.seq_files))
        self.fps = max(0.001, float(fps))
        # Keep QThread.start() callable.
        self.start_frame = int(start)
        self.end_frame = int(end)
        self.crop = tuple(map(int, crop))
        self.flip_h = bool(flip_h)
        self.flip_v = bool(flip_v)
        self.mode = int(mode)
        self.frame_size = tuple(map(int, frame_size)) if frame_size else None
        self._proc = None
        self.quality_by_frame = {}
        if self.mode not in (1, 2, 3, 4):
            raise ValueError('Unsupported tracking mode')

    def source_frame_for_index(self, index):
        """Real signed source ID behind a dense review/playhead frame index."""
        if not self.seq_files:
            return int(index)
        if not 0 <= int(index) < len(self.frame_numbers):
            raise IndexError('Tracking playhead is outside the declared source '
                             'timeline at index {}.'.format(index))
        return self.frame_numbers[int(index)]

    def _sequence_frame_path(self, index):
        label = self.source_frame_for_index(index)
        path = self._source_paths.get(label)
        if path is None:
            raise ValueError('Tracking cannot estimate real motion across an '
                             'intentional missing source frame {}. Restrict '
                             'the range to actual media.'.format(label))
        if not os.path.isfile(path):
            raise ValueError('Tracking frame {} was present in the original '
                             'source map but the file is now missing: {}.'
                             .format(label, path))
        return path

    def validate_range(self):
        """Preflight artist-selected playhead range, before creating a worker."""
        if self.start_frame < 0 or self.end_frame < self.start_frame:
            raise ValueError('Tracking needs a nonnegative ascending display range.')
        if self.end_frame - self.start_frame >= 2400:
            raise ValueError('Tracking is capped at 2,400 frames per job to '
                             'bound CPU/decoder work; choose a shorter range.')
        if self.seq_files:
            for index in range(self.start_frame, self.end_frame + 1):
                self._sequence_frame_path(index)
        return True

    def stop_now(self):
        """Interrupt a stalled streaming decoder during UI shutdown."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def _movie_seek_options(self, index):
        """Efficient coarse seek followed by presentation-accurate fine seek.

        A timestamp-only input seek can skip a decoded B-frame even for CFR.
        Keep a second, output-side seek for FFmpeg's frame reordering/discard.
        """
        target_seconds = int(index) / self.fps
        preseek_seconds = max(0.0, target_seconds - 1.0)
        fine_seconds = max(0.0, (int(index) - .125) /
                           self.fps - preseek_seconds)
        before = (['-ss', '{:.9f}'.format(preseek_seconds)]
                  if preseek_seconds else [])
        after = (['-ss', '{:.9f}'.format(fine_seconds)]
                 if fine_seconds else [])
        return before, after

    def _read_frame(self, index):
        if self.seq_files:
            return cv2.imread(self._sequence_frame_path(index))
        before, after = self._movie_seek_options(index)
        cmd = [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin']
        cmd += before + ['-i', self.path] + after + [
            '-map', '0:v:0', '-frames:v', '1', '-vsync', '0',
            '-f', 'image2pipe', '-vcodec', 'mjpeg', '-q:v', '3', 'pipe:1']
        kwargs = dict(stderr=subprocess.DEVNULL)
        kwargs.update(_no_window_kwargs())
        kwargs['env'] = media_environment()
        blob = subprocess.check_output(cmd, timeout=12, **kwargs)
        return cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_COLOR)

    def _frames(self):
        """Stream contiguous video frames through one FFmpeg process.

        Seeking on every frame loses motion detail at edit/keyframe boundaries,
        consumes CPU and leads to inconsistent optical-flow input. Sequences
        are already frame-addressed and retain their regular image reader.
        """
        if self.seq_files or not self.frame_size:
            for index in range(self.start_frame, self.end_frame + 1):
                if self.isInterruptionRequested():
                    return
                yield self._read_frame(index)
            return
        width, height = self.frame_size
        frame_bytes = width * height * 3
        if frame_bytes < 3:
            raise RuntimeError('Invalid tracking frame dimensions')
        span = self.end_frame - self.start_frame + 1
        # Same CFR source-index selection as the single-frame fallback; preserve
        # native decode order without FFmpeg inserting duplicate frames.
        before, after = self._movie_seek_options(self.start_frame)
        cmd = [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin']
        cmd += before + ['-i', self.path] + after + [
               '-map', '0:v:0', '-frames:v', str(span),
               '-an', '-sn', '-vsync', '0',
               '-pix_fmt', 'bgr24', '-f', 'rawvideo', 'pipe:1']
        kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                      bufsize=0)
        kwargs.update(_no_window_kwargs())
        kwargs['env'] = media_environment()
        proc = subprocess.Popen(cmd, **kwargs)
        self._proc = proc
        try:
            for index in range(self.start_frame, self.end_frame + 1):
                pixels = bytearray()
                last_progress = time.monotonic()
                while len(pixels) < frame_bytes:
                    if self.isInterruptionRequested():
                        return
                    if os.name != 'nt':
                        readable, _, _ = select.select([proc.stdout], [], [], .20)
                        if not readable:
                            if time.monotonic() - last_progress > 12.0:
                                raise RuntimeError(
                                    'Tracking decoder stalled at frame {}'.format(index))
                            continue
                    part = os.read(proc.stdout.fileno(),
                                   min(1024 * 1024, frame_bytes - len(pixels)))
                    if not part:
                        raise RuntimeError(
                            'Tracking decoder ended early at frame {}'.format(index))
                    pixels.extend(part)
                    last_progress = time.monotonic()
                yield np.frombuffer(pixels, np.uint8).reshape(
                    (height, width, 3))
        finally:
            self.stop_now()
            proc.wait()
            try:
                proc.stdout.close()
            except (OSError, ValueError):
                pass
            self._proc = None

    @staticmethod
    def _features(gray, region, retained=None):
        """Find corners within the moving crop, avoiding existing LK points."""
        height, width = gray.shape[:2]
        mask = np.zeros((height, width), dtype=np.uint8)
        vertices = np.round(region).astype(np.int32)
        cv2.fillConvexPoly(mask, vertices, 255)
        if retained is not None:
            for p in retained.reshape((-1, 2)):
                cv2.circle(mask, tuple(np.round(p).astype(int)), 7, 0, -1)
        points = cv2.goodFeaturesToTrack(
            gray, maxCorners=120, qualityLevel=.012, minDistance=6,
            mask=mask, blockSize=5)
        return points

    def run(self):
        self.quality_by_frame = {}
        try:
            self.validate_range()
            if not HAVE_CV2:
                raise RuntimeError('Tracking needs OpenCV (cv2) and NumPy in Maya Python.')
            x, y, w, h = self.crop
            if w < 2 or h < 2:
                raise ValueError('Draw a crop area before tracking.')
            base = _crop_corners(self.crop)
            cumulative = np.eye(3, dtype=np.float64)
            smoothed = None
            previous = None
            points = None
            centers, quads, quality = {}, {}, {}
            span = self.end_frame - self.start_frame + 1
            if span <= 0:
                raise ValueError('Invalid tracking range.')
            for index, image in enumerate(self._frames(), self.start_frame):
                if self.isInterruptionRequested():
                    return
                if image is None:
                    raise RuntimeError('Could not decode frame {}'.format(index))
                if self.flip_h:
                    image = cv2.flip(image, 1)
                if self.flip_v:
                    image = cv2.flip(image, 0)
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                height, width = gray.shape[:2]
                feature_count = 0 if points is None else len(points)
                tracked_count = inlier_count = 0
                fb_error = reprojection_error = None
                status_name = 'reference' if previous is None else 'lost_no_features'
                if previous is not None and feature_count:
                    advanced, status, _ = cv2.calcOpticalFlowPyrLK(
                        previous, gray, points, None, winSize=(31, 31), maxLevel=4,
                        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, .01))
                    if advanced is not None and status is not None:
                        reverse, backward_status, _ = cv2.calcOpticalFlowPyrLK(
                            gray, previous, advanced, None, winSize=(31, 31),
                            maxLevel=4,
                            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
                                      30, .01))
                        valid = status.reshape(-1).astype(bool)
                        if reverse is not None and backward_status is not None:
                            valid &= backward_status.reshape(-1).astype(bool)
                            fb_distances = np.linalg.norm(
                                reverse.reshape(-1, 2) - points.reshape(-1, 2),
                                axis=1)
                            valid &= np.isfinite(fb_distances) & (fb_distances < 1.75)
                        else:
                            valid[:] = False
                        before = points.reshape(-1, 2)[valid]
                        after = advanced.reshape(-1, 2)[valid]
                        tracked_count = len(before)
                        if tracked_count:
                            fb_error = float(np.median(fb_distances[valid]))
                        proposed, mask = _fit_motion(before, after, self.mode)
                        inlier_count = int(np.count_nonzero(mask))
                        last_quad = _transformed_points(cumulative, base)
                        if proposed is not None and inlier_count:
                            predicted = _transformed_points(proposed, before[mask])
                            reprojection_error = float(np.median(np.linalg.norm(
                                predicted - after[mask], axis=1)))
                        adequate = (proposed is not None and
                                    inlier_count / float(max(1, tracked_count)) >= .35)
                        plausible = (adequate and _plausible_motion(
                            proposed, last_quad, width, height))
                        if plausible:
                            cumulative = np.dot(proposed, cumulative)
                            status_name = 'tracked'
                            # Keep only model-consistent motion into the next
                            # frame; outliers must not poison subsequent LK.
                            points = after[mask].reshape((-1, 1, 2)).astype(np.float32)
                        else:
                            status_name = ('lost_insufficient_inliers' if not adequate
                                           else 'lost_implausible_motion')
                            points = None
                    else:
                        points = None
                        status_name = 'lost_flow'
                current_quad = _transformed_points(cumulative, base)
                # A lost frame must hold exactly the last measured, displayed
                # position. Running the smoother on a failure would fabricate
                # catch-up motion from a preceding frame.
                if not status_name.startswith('lost_'):
                    smoothed = _smooth_quad(smoothed, current_quad)
                quads[index] = [tuple(map(float, point)) for point in smoothed]
                centers[index] = tuple(map(float, smoothed.mean(axis=0)))
                quality[index] = _tracking_quality(
                    index, self.source_frame_for_index(index), feature_count,
                    tracked_count, inlier_count, fb_error, reprojection_error,
                    status_name)
                detected_count = 0
                if points is None or len(points) < 30 or index % 14 == 0:
                    fresh = self._features(gray, current_quad, points)
                    if fresh is not None:
                        detected_count = len(fresh)
                        points = fresh if points is None or not len(points) else np.concatenate(
                            (points, fresh), axis=0)
                quality[index]['detected_count'] = int(detected_count)
                quality[index]['seeded_count'] = (0 if points is None else int(len(points)))
                if previous is None:
                    quality[index]['feature_count'] = quality[index]['seeded_count']
                previous = gray
                if index % 5 == 0 or index == self.end_frame:
                    self.progress.emit(int(100 * (index - self.start_frame + 1) / span))
            if self.isInterruptionRequested():
                return
            self.quality_by_frame = quality
            self.quality_ready.emit(dict(quality))
            self.result.emit(centers, (w, h), quads)
        except Exception as exc:
            self.error.emit(str(exc))
