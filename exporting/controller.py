# -*- coding: utf-8 -*-
"""Export dialog and the ffmpeg export job.

Untracked exports run a single ffmpeg filter chain. Tracked exports stream raw
decoded frames into memory, crop each around the tracked centre, and re-encode
-- never dumping the full-resolution source to disk.
"""
from __future__ import absolute_import

import os
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction

from ..core.qtcompat import QtCore, QtWidgets, Signal
from ..media.ffmpeg import _no_window_kwargs, media_environment
from ..media import missing_placeholder
from ..core import utils
from ..viewer import tracking
from .. import style
from .spec import (ExportSpec, verify_output, validate_managed_export,
                   managed_input_plan, managed_movie_plan, managed_composite_plan,
                   managed_geometry_plan, managed_composite_geometry_plan,
                   validate_managed_movie_audio, MANAGED_MOVIE_EXTENSIONS,
                   _has_alpha_format, _record_audio_track_proofs)
from . import burnin

JPEG_QUALITY = 98


class ExportOptionsDialog(QtWidgets.QDialog):
    def __init__(self, parent, input_path, default_dir, stage=None):
        QtWidgets.QDialog.__init__(self, parent)
        # The current FrameReviewer exports command opens this dialog with its
        # stage already selected in the Export side-pane. Snapshot that choice
        # so the offered filenames can actually represent the chosen pipeline.
        if stage is None:
            selector = getattr(parent, 'export_color_stage', None)
            stage = selector.currentData() if selector is not None else 'source'
        self.color_stage = stage if stage in ('source', 'working', 'display') else 'source'
        self.image_extension = ('.exr' if self.color_stage == 'working' else
                                '.png' if self.color_stage == 'display' else '.jpg')
        self.setWindowTitle('Export Media')
        self.setMinimumWidth(540)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        root.addWidget(QtWidgets.QLabel('Output name'))
        self.name_le = QtWidgets.QLineEdit(utils.name_from_path(input_path))
        root.addWidget(self.name_le)

        modes = QtWidgets.QHBoxLayout()
        self.video_cb = QtWidgets.QCheckBox('Video .mp4')
        self.sequence_cb = QtWidgets.QCheckBox(
            'Image sequence {}{}'.format(self.image_extension,
                                          ' (float32 HDR)' if self.color_stage == 'working'
                                          else ' (16-bit)' if self.color_stage == 'display'
                                          else ''))
        self.video_cb.setChecked(self.color_stage != 'working')
        self.sequence_cb.setChecked(self.color_stage == 'working')
        self.video_cb.setEnabled(self.color_stage != 'working')
        modes.addWidget(self.video_cb)
        modes.addWidget(self.sequence_cb)
        modes.addStretch()
        root.addLayout(modes)

        root.addWidget(QtWidgets.QLabel('Export directory'))
        directory = QtWidgets.QHBoxLayout()
        self.dir_le = QtWidgets.QLineEdit(default_dir)
        self.dir_le.setReadOnly(True)
        self.dir_btn = QtWidgets.QPushButton('Choose…')
        directory.addWidget(self.dir_le, 1)
        directory.addWidget(self.dir_btn)
        root.addLayout(directory)

        self.warning = QtWidgets.QLabel()
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet('color:%s;' % style.WARNING)
        self.warning.hide()
        root.addWidget(self.warning)
        self.overwrite_cb = QtWidgets.QCheckBox('Overwrite existing selected outputs')
        self.overwrite_cb.hide()
        root.addWidget(self.overwrite_cb)

        actions = QtWidgets.QHBoxLayout()
        actions.addStretch()
        cancel = QtWidgets.QPushButton('Cancel')
        self.export_btn = QtWidgets.QPushButton('Export')
        actions.addWidget(cancel)
        actions.addWidget(self.export_btn)
        root.addLayout(actions)

        self.name_le.textChanged.connect(self._refresh)
        self.video_cb.toggled.connect(self._refresh)
        self.sequence_cb.toggled.connect(self._refresh)
        self.overwrite_cb.toggled.connect(self._refresh)
        self.dir_btn.clicked.connect(self._choose_dir)
        cancel.clicked.connect(self.reject)
        self.export_btn.clicked.connect(self._accept_checked)
        self._refresh()

    def name(self):
        return self.name_le.text().strip()

    def directory(self):
        return self.dir_le.text()

    def paths(self):
        directory, name = self.directory(), self.name()
        result = []
        if self.video_cb.isChecked():
            result.append(os.path.join(directory, name + '.mp4'))
        if self.sequence_cb.isChecked():
            result.append(os.path.join(directory, name,
                                       name + '.####' + self.image_extension))
        return result

    def conflicts(self):
        directory, name = self.directory(), self.name()
        found = []
        if not utils.valid_name(name) or not os.path.isdir(directory):
            return found
        if self.video_cb.isChecked():
            path = os.path.join(directory, name + '.mp4')
            if os.path.lexists(path):
                found.append(path)
        if self.sequence_cb.isChecked():
            path = os.path.join(directory, name)
            if os.path.lexists(path):
                found.append(path)
        return found

    def _choose_dir(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, 'Export directory', self.directory())
        if path:
            self.dir_le.setText(path)
            self._refresh()

    def _refresh(self, *args):
        valid = (utils.valid_name(self.name()) and os.path.isdir(self.directory())
                 and (self.video_cb.isChecked() or self.sequence_cb.isChecked()))
        conflicts = self.conflicts() if valid else []
        if not valid:
            message = 'Enter a valid name, directory, and select at least one output.'
        elif conflicts:
            message = 'Existing output will be replaced:\n' + '\n'.join(conflicts)
        else:
            message = ''
        self.warning.setText(message)
        self.warning.setVisible(bool(message))
        self.overwrite_cb.setVisible(bool(conflicts))
        if not conflicts and self.overwrite_cb.isChecked():
            self.overwrite_cb.blockSignals(True)
            self.overwrite_cb.setChecked(False)
            self.overwrite_cb.blockSignals(False)
        self.export_btn.setEnabled(bool(valid and (not conflicts or self.overwrite_cb.isChecked())))

    def _accept_checked(self):
        self._refresh()
        if self.export_btn.isEnabled():
            self.accept()


class ExportJob(QtCore.QThread):
    progress = Signal(int)
    result = Signal()
    error = Signal(str)

    def __init__(self, ffmpeg, source, seq_files, width, height, context, parent=None):
        QtCore.QThread.__init__(self, parent)
        self.ffmpeg = ffmpeg
        self.source = source
        self.seq_files = list(seq_files or [])
        self.width, self.height = int(width), int(height)
        self.spec = ExportSpec.from_context(context, source_path=source)
        self.ctx = self.spec.to_context()
        self._proc = None
        self._decode_proc = None
        self._input_temp = None
        self._movie_audio_temp = None
        self._movie_audio_source_identity = None

    def stop_now(self):
        for proc in (self._proc, self._decode_proc):
            if proc is None:
                continue
            try:
                proc.kill()
            except OSError:
                pass

    # -- source / filters -------------------------------------------------
    def _source_args(self):
        c = self.ctx
        start_ss = ['-ss', '{:.8f}'.format(c['start'] / max(0.001, c['source_fps']))]
        if self.seq_files:
            if self._input_temp is None:
                self._input_temp = tempfile.mkdtemp(prefix='framereviewer_export_')
                playlist = os.path.join(self._input_temp, 'images.ffconcat')
                with open(playlist, 'w') as handle:
                    handle.write('ffconcat version 1.0\n')
                    for frame in self.seq_files:
                        escaped = os.path.abspath(frame).replace("'", "'\\''")
                        handle.write("file '{}'\n".format(escaped))
                        handle.write('duration {:.12f}\n'.format(1.0 / max(0.001, c['source_fps'])))
            return start_ss + ['-safe', '0', '-f', 'concat', '-i',
                               os.path.join(self._input_temp, 'images.ffconcat')]
        return start_ss + ['-i', self.source]

    def _filters(self, moving=False):
        c = self.ctx
        filters = []
        # Moving crops are flipped in OpenCV before perspective rectification.
        # Reapplying ffmpeg flips would cancel the requested orientation.
        if c['flip_h'] and not moving:
            filters.append('hflip')
        if c['flip_v'] and not moving:
            filters.append('vflip')
        if not moving and c['crop']:
            x, y, w, h = c['crop']
            filters.append('crop={}:{}:{}:{}'.format(w, h, x, y))
        if not moving:
            filters.append('scale={}:{}'.format(c['out_w'], c['out_h']))
        if c['speed'] != 1.0:
            filters.append('setpts=PTS/{:.8f}'.format(c['speed']))
        if c.get('burnin') and not moving:
            filters.extend(burnin.ffmpeg_filters(
                c['burnin'], c.get('burnin_context'), source_start=c['start']))
        return filters

    def _output_path(self):
        path = self.ctx['path']
        return path.replace('####', '%04d') if self.ctx['sequence'] else path

    def _final_args(self, src_args, moving=False):
        c = self.ctx
        args = [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y'] + src_args
        if not moving:
            args += ['-frames:v', str(c['count'])]
        filters = self._filters(moving=moving)
        if filters:
            args += ['-vf', ','.join(filters)]
        fps = c['fps']
        if c['sequence']:
            args += ['-vsync', '0', '-r', '{:.8f}'.format(fps), '-start_number', '1001',
                     '-q:v', '2', self._output_path()]
        else:
            args += ['-r', '{:.8f}'.format(fps), '-c:v', 'libx264', '-crf', '18',
                     '-preset', 'medium', '-pix_fmt', 'yuv420p', '-an', self._output_path()]
        return args

    def _execute(self, args):
        kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        kwargs.update(_no_window_kwargs())
        kwargs['env'] = media_environment()
        self._proc = subprocess.Popen(args, **kwargs)
        _, err = self._proc.communicate()
        code = self._proc.returncode
        self._proc = None
        if code:
            raise RuntimeError(err.decode('utf-8', 'replace')[-1500:])

    # -- precise source -> working -> display image exports ----------------
    def _managed_probe_frame(self, path, size=None):
        """Inspect each actual image before choosing output channel precision."""
        if self.isInterruptionRequested():
            raise RuntimeError('Managed source probe cancelled.')
        if not os.path.isfile(path):
            raise RuntimeError('Managed source image disappeared: {}'.format(path))
        probe = (os.path.join(os.path.dirname(self.ffmpeg), 'ffprobe')
                 if os.path.isfile(os.path.join(os.path.dirname(self.ffmpeg), 'ffprobe'))
                 else shutil.which('ffprobe'))
        if not probe:
            raise RuntimeError('Managed export requires ffprobe to validate source channels.')
        try:
            command = [
                probe, '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height,pix_fmt',
                '-of', 'json', path]
            kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            kwargs.update(_no_window_kwargs())
            kwargs['env'] = media_environment()
            self._decode_proc = subprocess.Popen(command, **kwargs)
            try:
                output, err = self._decode_proc.communicate(timeout=30)
                if self._decode_proc.returncode:
                    raise RuntimeError('Managed image channel probe failed: {}'.format(
                        err.decode('utf-8', 'replace')[-400:]))
                if len(output) > 65536:
                    raise RuntimeError('Managed image channel probe exceeded safe metadata size.')
            finally:
                proc = self._decode_proc
                self._decode_proc = None
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            if self.isInterruptionRequested():
                raise RuntimeError('Managed source probe cancelled.')
            info = json.loads(output.decode('utf-8'))['streams'][0]
            fmt = str(info.get('pix_fmt', '')).casefold()
            if (int(info['width']), int(info['height'])) != (size or
                                                              (self.width, self.height)):
                raise ValueError(
                    'Could not verify managed image channels: source dimensions '
                    'changed or mismatch frozen dimensions: {}'.format(path))
            if not fmt or fmt == 'none':
                raise ValueError('Managed source image has unknown pixel channels: {}'.format(path))
            return _has_alpha_format(fmt)
        except (IndexError, KeyError, TypeError, json.JSONDecodeError,
                subprocess.SubprocessError) as exc:
            raise RuntimeError('Could not verify managed image channels for {}: {}'.format(path, exc))

    def _managed_read_frame(self, path, np, channels=3, size=None):
        """Decode a single source image as planar float32 RGB or RGBA.

        FFmpeg decodes EXR HALF/FLOAT without PNG/JPEG/QPixmap quantization,
        and can decode standard PNG/TIFF as normalized float. Never decode via
        thumbnail cache or an unmanaged screen/display conversion.
        """
        width, height = size or (self.width, self.height)
        frame_bytes = width * height * channels * 4
        if frame_bytes > 256 * 1024 * 1024:
            raise ValueError('Managed frame exceeds the 256 MB per-frame limit.')
        if not os.path.isfile(path):
            raise RuntimeError('Managed source image disappeared: {}'.format(path))
        fmt = 'gbrapf32le' if channels == 4 else 'gbrpf32le'
        args = [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
                '-i', path, '-frames:v', '1', '-pix_fmt', fmt,
                '-f', 'rawvideo', 'pipe:1']
        kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        kwargs.update(_no_window_kwargs())
        kwargs['env'] = media_environment()
        self._decode_proc = subprocess.Popen(args, **kwargs)
        try:
            raw, err = self._decode_proc.communicate(timeout=120)
            if self._decode_proc.returncode or len(raw) != frame_bytes:
                raise RuntimeError('Cannot decode full precision image {}: {}'.format(
                    path, err.decode('utf-8', 'replace')[-500:]))
        finally:
            if self._decode_proc.poll() is None:
                self._decode_proc.kill()
                self._decode_proc.wait()
            self._decode_proc = None
        planes = np.frombuffer(raw, dtype='<f4').reshape(
            (channels, height, width))  # FFmpeg: green, blue, red, alpha
        order = (2, 0, 1, 3) if channels == 4 else (2, 0, 1)
        return np.stack(tuple(planes[index] for index in order), axis=-1)

    def _managed_geometry(self, pixels):
        c = self.ctx
        if c.get('flip_h'):
            pixels = pixels[:, ::-1, :]
        if c.get('flip_v'):
            pixels = pixels[::-1, :, :]
        if c.get('crop'):
            x, y, w, h = (int(v) for v in c['crop'])
            if min(x, y) < 0 or min(w, h) < 1 or x + w > self.width or y + h > self.height:
                raise ValueError('Managed export crop is outside the source image.')
            pixels = pixels[y:y + h, x:x + w, :]
        if (pixels.shape[1], pixels.shape[0]) != self.spec.output_size:
            if tracking.cv2 is None:
                raise RuntimeError('OpenCV is required for floating-point managed resizing.')
            pixels = tracking.cv2.resize(
                pixels, self.spec.output_size, interpolation=tracking.cv2.INTER_LINEAR)
        return pixels

    def _managed_geometry_frame(self, working, geometry, state, np):
        """Rectify WORKING-linear float RGB(A) before display transfer/encode.

        Source coordinates use the viewer's post-flip crop frame. Interpolate
        associated-alpha pixels in linear space to avoid colored fringes, then
        restore the frozen association before any Display transform.
        """
        from ..viewer.color import process_pixels
        if self.isInterruptionRequested():
            raise RuntimeError('Managed geometry export cancelled.')
        crop = geometry['crop']
        if geometry['flip_h']:
            working = working[:, ::-1, :]
        if geometry['flip_v']:
            working = working[::-1, :, :]
        x, y, w, h = crop
        working = working[y:y + h, x:x + w, :]
        alpha_straight = working.shape[2] == 4 and state.alpha_mode != 'premultiplied'
        if alpha_straight and geometry['square_pixel_size'][0] != w:
            working = np.ascontiguousarray(working.copy())
            working[:, :, :3] *= working[:, :, 3:4]
        scaled_w = geometry['square_pixel_size'][0]
        if scaled_w != w:
            if tracking.cv2 is None:
                raise RuntimeError('OpenCV is required for verified float linear PAR rectification.')
            working = tracking.cv2.resize(
                working, (scaled_w, h),
                interpolation=tracking.cv2.INTER_LINEAR)
            if working.ndim != 3:
                raise RuntimeError('Geometry interpolation lost source channels.')
        if alpha_straight and scaled_w != w:
            alpha = working[:, :, 3:4]
            working[:, :, :3] = np.divide(
                working[:, :, :3], alpha,
                out=np.zeros_like(working[:, :, :3]), where=alpha > 1e-8)
        turns = geometry['rotation'] // 90
        if turns:
            working = np.rot90(working, k=-turns, axes=(0, 1))
        if (working.shape[1], working.shape[0]) != tuple(geometry['output_size']):
            raise RuntimeError('Managed geometry produced unexpected oriented float raster.')
        if self.spec.stage == 'display':
            return process_pixels(
                working, state=state, stage='display',
                source_stage='working', alpha_mode=state.alpha_mode)
        return working

    def _managed_geometry_probe_exr(self, path, geometry):
        """Reject unrectifiable native EXR windows and implicit part/AOV loss."""
        if os.path.splitext(path)[1].lower() != '.exr':
            return
        from ..media import exr
        try:
            header = exr._raw_header(path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError('Managed EXR geometry cannot verify native raster: {}'.format(exc))
        if header is None:
            raise ValueError('Managed EXR geometry requires canonical single-part root RGB(A) HALF/FLOAT with a verified native header.')
        size = geometry['input_size']
        native = (0, 0, size[0], size[1])
        if (tuple(header['data_window']) != native or
                tuple(header['display_window']) != native or
                header['part_count'] != 1 or header.get('layer')):
            raise ValueError('Managed EXR geometry does not support offset/cropped display windows, multipart or named layers.')
        ratio = float(Fraction(geometry['pixel_aspect']))
        if abs(float(header['pixel_aspect_ratio']) - ratio) > max(1e-6, ratio * 1e-6):
            raise ValueError('Frozen pixel aspect contradicts native EXR header: {}'.format(path))

    def _managed_placeholder(self, frame, width, height, channels, np):
        """Convert the SAME opaque Qt display raster to float for PNG16/EXR.

        This is explicitly display-artifact u8 color, with native 8-bit
        palette expanded to 16-bit PNG samples. It never implies Source HDR.
        """
        policy = frame['hole_policy']
        raster = missing_placeholder.raster_placeholder(
            policy, width, height, channels=channels)
        # np.float32 and the existing PNG16 encoder's (+.5) round-to-nearest
        # map exact n/255 display values to integer n*257 (0..65535).
        return np.frombuffer(raster, dtype=np.uint8).reshape(
            height, width, channels).astype(np.float32) / 255.0

    def _managed_movie_probe(self):
        """Reject VFR, ambiguous color, changing dimensions and unknown frame count.

        The confirmed source color space lives in the frozen ExportSpec; media
        tags alone cannot establish scene-referred color provenance.
        """
        ffprobe = os.path.join(os.path.dirname(self.ffmpeg), 'ffprobe')
        if not os.path.isfile(ffprobe):
            ffprobe = shutil.which('ffprobe')
        if not ffprobe:
            raise RuntimeError('Managed movie requires ffprobe.')
        if not os.path.isfile(self.spec.source_path):
            raise RuntimeError('Managed movie source disappeared.')
        args = [ffprobe, '-v', 'error', '-count_frames',
                '-select_streams', 'v:0',
                '-show_entries',
                'stream=width,height,codec_name,pix_fmt,color_space,color_range,avg_frame_rate,r_frame_rate,nb_read_frames',
                '-of', 'json', self.spec.source_path]
        kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        kwargs.update(_no_window_kwargs())
        self._decode_proc = subprocess.Popen(args, **kwargs)
        try:
            output, err = self._decode_proc.communicate(timeout=30)
            if self._decode_proc.returncode:
                raise RuntimeError('Managed movie probe failed: {}'.format(
                    err.decode('utf-8', 'replace')[-400:]))
            info = json.loads(output.decode('utf-8'))['streams'][0]
        finally:
            if self._decode_proc.poll() is None:
                self._decode_proc.kill()
                self._decode_proc.wait()
            self._decode_proc = None
        fmt = str(info.get('pix_fmt', '')).casefold()
        if not fmt or fmt == 'none':
            raise ValueError('Managed movie requires known source pixel format.')
        if (int(info.get('width', 0)), int(info.get('height', 0))) != (
                self.width, self.height):
            raise ValueError('Managed movie dimensions differ from frozen export source.')
        try:
            total = int(info['nb_read_frames'])
            fps = Fraction(self.spec.source_fps)
            avg = Fraction(info['avg_frame_rate'])
            nominal = Fraction(info['r_frame_rate'])
        except (ValueError, ZeroDivisionError, TypeError, KeyError) as exc:
            raise ValueError('Managed movie requires a known constant source FPS and frame count: {}'.format(exc))
        if total < self.spec.end + 1 or total <= 0 or total > 50000:
            raise ValueError('Managed movie is short or exceeds the 50,000-frame precision limit.')
        if fps != avg or fps != nominal:
            raise ValueError('Managed movie source FPS disagrees with probed CFR rate ({} / {}).'.format(
                info['avg_frame_rate'], info['r_frame_rate']))
        # A source can report matching nominal/average rates and still have
        # irregular timestamps; check the complete bounded decoded PTS list.
        args = [ffprobe, '-v', 'error', '-select_streams', 'v:0',
                '-show_frames', '-show_entries',
                'frame=best_effort_timestamp_time', '-of', 'csv=p=0',
                self.spec.source_path]
        self._decode_proc = subprocess.Popen(args, **kwargs)
        try:
            raw, err = self._decode_proc.communicate(timeout=60)
            if self._decode_proc.returncode or len(raw) > 4 * 1024 * 1024:
                raise RuntimeError('Managed movie timestamp probe failed or exceeded bounded metadata.')
        finally:
            if self._decode_proc.poll() is None:
                self._decode_proc.kill()
                self._decode_proc.wait()
            self._decode_proc = None
        stamps = []
        try:
            for line in raw.decode('utf-8').splitlines():
                field = line.strip().split(',')[0]
                if not field:
                    continue
                stamps.append(Fraction(field))
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError('Managed movie has unparseable frame timestamps: {}'.format(exc))
        if len(stamps) != total:
            raise ValueError('Managed movie timestamp count differs from decoded frame count.')
        first = stamps[0]
        for n, stamp in enumerate(stamps):
            if abs(float(stamp - first - Fraction(n, 1) / fps)) > 0.001:
                raise ValueError('Managed movie has variable timestamps at frame {}; VFR unsupported.'.format(n))
        yuv = fmt.startswith(('yuv', 'yuva', 'nv', 'p010', 'p016'))
        matrix = None
        range_name = None
        if yuv:
            frozen = self.ctx['movie_color_provenance']
            matrix = frozen.get('yuv_matrix')
            range_name = frozen.get('yuv_range')
            if matrix not in ('bt601', 'bt709', 'bt2020') or range_name not in (
                    'limited', 'full'):
                raise ValueError('Managed YUV movie needs user-confirmed yuv_matrix and yuv_range to avoid ambiguous RGB decoding.')
            tags = {'bt709': 'bt709', 'bt470bg': 'bt601',
                    'smpte170m': 'bt601', 'bt2020nc': 'bt2020'}
            tagged_matrix = info.get('color_space')
            tagged_range = info.get('color_range')
            if tagged_matrix not in (None, 'unknown') and tags.get(
                    tagged_matrix) != matrix:
                raise ValueError('Frozen movie YUV matrix contradicts encoded stream metadata.')
            if tagged_range not in (None, 'unknown'):
                if {'tv': 'limited', 'pc': 'full'}.get(tagged_range) != range_name:
                    raise ValueError('Frozen movie YUV range contradicts encoded stream metadata.')
        return {'frame_count': total, 'codec': str(info.get('codec_name')),
                'source_pix_fmt': fmt, 'has_alpha': _has_alpha_format(fmt),
                'source_fps': self.spec.source_fps, 'pts_origin': str(first),
                'yuv_matrix': matrix, 'yuv_range': range_name}

    def _managed_movie_start(self, movie_info):
        """One bounded raw float stream; trim counts decoded frames, not seconds."""
        has_alpha = movie_info['has_alpha']
        fmt = 'gbrapf32le' if has_alpha else 'gbrpf32le'
        filters = []
        if movie_info['yuv_matrix']:
            scale_matrix = {'bt601': 'bt601', 'bt709': 'bt709',
                            'bt2020': 'bt2020'}[movie_info['yuv_matrix']]
            scale_range = ('full' if movie_info['yuv_range'] == 'full'
                           else 'limited')
            filters.append('scale=in_color_matrix={}:in_range={}'.format(
                scale_matrix, scale_range))
        filters.append('trim=start_frame={}:end_frame={}'.format(
            self.spec.start, self.spec.end + 1))
        args = [self.ffmpeg, '-hide_banner', '-loglevel', 'error',
                '-xerror', '-nostdin', '-i', self.spec.source_path,
                '-map', '0:v:0', '-an', '-sn', '-dn',
                '-vf', ','.join(filters),
                '-fps_mode', 'passthrough', '-frames:v', str(self.spec.count),
                '-pix_fmt', fmt, '-f', 'rawvideo', 'pipe:1']
        kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      bufsize=0)
        kwargs.update(_no_window_kwargs())
        kwargs['env'] = media_environment()
        self._decode_proc = subprocess.Popen(args, **kwargs)

    def _managed_movie_next(self, np, channels):
        """Read exactly one frame in fixed 1-MiB pieces; never buffer a movie."""
        required = self.width * self.height * channels * 4
        if required > 256 * 1024 * 1024:
            raise ValueError('Managed movie frame exceeds the 256 MB limit.')
        remaining = required
        chunks = []
        while remaining:
            if self.isInterruptionRequested():
                raise RuntimeError('Managed movie export cancelled.')
            piece = self._decode_proc.stdout.read(min(1024 * 1024, remaining))
            if not piece:
                raise RuntimeError('Managed movie decode failed or ended before requested frame range.')
            chunks.append(piece)
            remaining -= len(piece)
        planes = np.frombuffer(b''.join(chunks), dtype='<f4').reshape(
            channels, self.height, self.width)
        order = (2, 0, 1, 3) if channels == 4 else (2, 0, 1)
        return np.stack(tuple(planes[index] for index in order), axis=-1)

    def _managed_movie_finish(self):
        proc = self._decode_proc
        if proc is None:
            return
        try:
            if proc.stdout is not None and not proc.stdout.closed:
                extra = proc.stdout.read(1)
                if extra:
                    raise RuntimeError('Managed movie decoder emitted unexpected extra frames.')
            err = proc.stderr.read() if proc.stderr is not None else b''
            if proc.wait(timeout=30) != 0:
                raise RuntimeError('Managed movie decoder exited with error: {}'.format(
                    err.decode('utf-8', 'replace')[-500:]))
        finally:
            self._managed_movie_abort()

    def _managed_movie_abort(self):
        proc = self._decode_proc
        self._decode_proc = None
        if proc is None:
            return
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None and not pipe.closed:
                try:
                    pipe.close()
                except OSError:
                    pass

    def _managed_audio_probe_json(self, path, count_frames=False):
        """Bounded, cancellable FFprobe of both source and staged mux media."""
        probe = os.path.join(os.path.dirname(self.ffmpeg), 'ffprobe')
        if not os.path.isfile(probe):
            probe = shutil.which('ffprobe')
        if not probe:
            raise RuntimeError('Managed embedded audio requires ffprobe.')
        if self.isInterruptionRequested():
            raise RuntimeError('Managed movie audio cancelled.')
        args = [probe, '-v', 'error']
        if count_frames:
            args += ['-count_frames']
        args += ['-show_entries',
                 'stream=index,codec_type,codec_name,time_base,sample_rate,channels,channel_layout,start_time,duration,nb_read_frames,avg_frame_rate',
                 '-show_format', '-of', 'json', path]
        kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        kwargs.update(_no_window_kwargs())
        kwargs['env'] = media_environment()
        self._decode_proc = subprocess.Popen(args, **kwargs)
        try:
            if self.isInterruptionRequested():
                raise RuntimeError('Managed movie audio cancelled.')
            out, err = self._decode_proc.communicate(timeout=30)
            if self.isInterruptionRequested():
                raise RuntimeError('Managed movie audio cancelled.')
            if self._decode_proc.returncode or len(out) > 256 * 1024:
                raise RuntimeError('Cannot probe managed audio streams: {}'.format(
                    err.decode('utf-8', 'replace')[-500:]))
            return json.loads(out.decode('utf-8'))
        finally:
            proc = self._decode_proc
            self._decode_proc = None
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()

    def _managed_audio_stat(self):
        """Detect replacement or mutation of the source across the two passes.

        Video and audio are decoded from the same file in separate FFmpeg
        processes. Without an identity check, an editor replacing that path
        between passes could silently pair one video's frames with another
        file's soundtrack, even if both have valid matching stream metadata.
        """
        try:
            info = os.stat(self.spec.source_path)
        except OSError as exc:
            raise RuntimeError('Managed movie audio source disappeared or changed: {}'.format(exc))
        if not os.path.isfile(self.spec.source_path):
            raise RuntimeError('Managed movie audio source changed into a non-file.')
        return (info.st_dev, info.st_ino, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)

    def _managed_audio_assert_source(self):
        identity = self._movie_audio_source_identity
        if identity is not None and self._managed_audio_stat() != identity:
            raise RuntimeError(
                'Managed movie source changed during audio export; refusing '
                'to combine video and soundtrack from different source revisions.')

    def _managed_audio_source(self, movie_info):
        """Verify frozen track identity and a truly aligned zero-origin A/V clock."""
        self._managed_audio_assert_source()
        frozen = validate_managed_movie_audio(self.spec)
        streams = self._managed_audio_probe_json(self.spec.source_path).get('streams') or []
        selected = [stream for stream in streams if (
            stream.get('index') == frozen['stream_index'] and
            stream.get('codec_type') == 'audio')]
        videos = [stream for stream in streams if stream.get('codec_type') == 'video']
        if len(selected) != 1 or len(videos) != 1 or videos[0].get('index') != 0:
            raise ValueError('Frozen embedded audio stream_index is not an actual track in a single-video source.')
        audio = selected[0]
        try:
            if (audio.get('time_base') != frozen['source_time_base'] or
                    int(audio['sample_rate']) != frozen['source_sample_rate'] or
                    int(audio['channels']) != 2):
                raise ValueError('Frozen audio time_base/sample rate/channels disagree with source.')
            # Frame zero and the first decoded audio sample must align. A
            # nonzero track delay requires a separately tested offset model.
            first_video = Fraction(str(movie_info['pts_origin']))
            start_audio = Fraction(str(audio.get('start_time', '0')))
            start_video = Fraction(str(videos[0].get('start_time', '0')))
            if any(abs(float(t)) > .001 for t in (
                    first_video, start_audio, start_video)):
                raise ValueError('Embedded audio/video initial PTS offsets are unsupported; select Drop.')
        except (KeyError, TypeError, ZeroDivisionError) as exc:
            raise ValueError('Audio stream is missing exact timing metadata: {}'.format(exc))
        sample_rate = frozen['source_sample_rate']
        fps = Fraction(self.spec.source_fps)
        start_sample = round(Fraction(self.spec.start * sample_rate, 1) / fps)
        end_sample = round(Fraction((self.spec.end + 1) * sample_rate, 1) / fps)
        required_samples = end_sample - start_sample
        if required_samples <= 0:
            raise ValueError('Selected audio trim is empty.')
        # Do not silently pad a source shorter than the selected video range.
        try:
            duration = Fraction(str(audio['duration']))
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            raise ValueError('Selected embedded audio duration must be known.')
        if duration < Fraction(end_sample, sample_rate) - Fraction(1, 100):
            raise ValueError('Embedded audio ends before the selected video frame range.')
        # Sample-position trim is only correct for a gap-free source audio
        # clock. Audit decoded sample PTS, not merely avg stream duration.
        audio_ordinal = [s['index'] for s in streams
                         if s.get('codec_type') == 'audio'].index(
                             frozen['stream_index'])
        self._managed_audio_check_pts(audio_ordinal, sample_rate, end_sample)
        self._managed_audio_assert_source()
        return {'source_stream_index': frozen['stream_index'],
                'source_time_base': audio['time_base'],
                'source_sample_rate': sample_rate,
                'source_codec': audio.get('codec_name'),
                'source_start_time': str(start_audio),
                'source_video_start_time': str(start_video),
                'trim_start_samples': start_sample,
                'trim_end_samples': end_sample,
                'trim_start_time': str(Fraction(self.spec.start, 1) / fps),
                'trim_duration': str(Fraction(self.spec.count, 1) / fps)}

    def _managed_audio_check_pts(self, ordinal, sample_rate, end_sample):
        """Reject timestamp gaps/overlaps that cannot be aligned by sample index."""
        probe = os.path.join(os.path.dirname(self.ffmpeg), 'ffprobe')
        if not os.path.isfile(probe):
            probe = shutil.which('ffprobe')
        args = [probe, '-v', 'error', '-select_streams',
                'a:{}'.format(ordinal), '-show_frames', '-show_entries',
                'frame=best_effort_timestamp_time,nb_samples',
                '-of', 'json', self.spec.source_path]
        kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        kwargs.update(_no_window_kwargs())
        kwargs['env'] = media_environment()
        self._decode_proc = subprocess.Popen(args, **kwargs)
        try:
            if self.isInterruptionRequested():
                raise RuntimeError('Managed movie audio cancelled.')
            raw, err = self._decode_proc.communicate(timeout=60)
            if self.isInterruptionRequested():
                raise RuntimeError('Managed movie audio cancelled.')
            if self._decode_proc.returncode or len(raw) > 4 * 1024 * 1024:
                raise RuntimeError('Managed audio PTS validation failed or exceeded bounded metadata.')
            frames = json.loads(raw.decode('utf-8'))['frames']
            if not frames or len(frames) > 20000:
                raise ValueError('Audio source has no bounded decoded sample timeline.')
            elapsed_samples = 0
            for idx, frame in enumerate(frames):
                pts = Fraction(str(frame['best_effort_timestamp_time']))
                samples = int(frame['nb_samples'])
                if samples <= 0 or samples > 8192:
                    raise ValueError('Unexpected audio frame sample count at {}'.format(idx))
                expected = Fraction(elapsed_samples, sample_rate)
                if abs(float(pts - expected)) > .0015:
                    raise ValueError('Audio PTS discontinuity at decoded frame {}; alignment unverified.'.format(idx))
                elapsed_samples += samples
            if elapsed_samples + round(sample_rate / 100) < end_sample:
                raise ValueError('Decoded audio ends before selected movie range.')
        except (KeyError, IndexError, TypeError, ZeroDivisionError) as exc:
            raise ValueError('Audio source frame PTS metadata is incomplete: {}'.format(exc))
        finally:
            proc = self._decode_proc
            self._decode_proc = None
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()

    def _managed_movie_mux_audio(self, video_only, source_receipt):
        """Copy pixel-verified video, decode/trim one selected stream, encode AAC.

        Only the private stage output is touched; the queue owns commit,
        rollback and the public original. No audio is copied from a player
        device, and no external routing is implicitly adopted.
        """
        frozen = self.ctx['movie_audio_export']
        self._managed_audio_assert_source()
        duration = Fraction(self.spec.count, 1) / Fraction(self.spec.fps)
        seconds = '{:.12f}'.format(float(duration))
        input_audio = source_receipt['source_stream_index']
        audio_filter = ('[1:{}]atrim=start_sample={}:end_sample={},'
                        'asetpts=N/SR/TB,aresample=48000:async=0:first_pts=0,'
                        'apad=whole_dur={},atrim=duration={},'
                        'asetpts=N/SR/TB[aout]').format(
                            input_audio, source_receipt['trim_start_samples'],
                            source_receipt['trim_end_samples'], seconds, seconds)
        cmd = [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
               '-xerror', '-y', '-i', video_only, '-i', self.spec.source_path,
               '-filter_complex', audio_filter, '-map', '0:v:0',
               '-map', '[aout]', '-c:v', 'copy', '-c:a', frozen['codec'],
               '-b:a', str(frozen['bit_rate']), '-ar', str(frozen['sample_rate']),
               '-ac', str(frozen['channels']), '-map_metadata', '-1',
               '-movflags', '+faststart', self.spec.path]
        kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        kwargs.update(_no_window_kwargs())
        kwargs['env'] = media_environment()
        if self.isInterruptionRequested():
            raise RuntimeError('Managed movie audio cancelled.')
        self._proc = subprocess.Popen(cmd, **kwargs)
        try:
            # If cancellation arrived between the initial check and Popen
            # returning, stop this freshly spawned process immediately.
            if self.isInterruptionRequested():
                raise RuntimeError('Managed movie audio cancelled.')
            _, stderr = self._proc.communicate(timeout=120)
            if self.isInterruptionRequested():
                raise RuntimeError('Managed movie audio cancelled.')
            if self._proc.returncode:
                raise RuntimeError('Managed movie audio mux failed: {}'.format(
                    stderr.decode('utf-8', 'replace')[-1000:]))
        finally:
            proc = self._proc
            self._proc = None
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stderr is not None and not proc.stderr.closed:
                proc.stderr.close()
        data = self._managed_audio_probe_json(self.spec.path, count_frames=True)
        self._managed_audio_assert_source()
        streams = data.get('streams') or []
        audio = [s for s in streams if s.get('codec_type') == 'audio']
        videos = [s for s in streams if s.get('codec_type') == 'video']
        if len(audio) != 1 or len(videos) != 1:
            raise RuntimeError('Managed audio mux did not retain exactly one audio and video stream.')
        result = audio[0]
        try:
            start = Fraction(str(result['start_time']))
            length = Fraction(str(result['duration']))
            video_start = Fraction(str(videos[0]['start_time']))
            count = int(videos[0]['nb_read_frames'])
            sample_rate = int(result['sample_rate'])
            channels = int(result['channels'])
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            raise RuntimeError('Muxed audio is missing verifiable timing: {}'.format(exc))
        if (result.get('codec_name') != 'aac' or sample_rate != 48000 or
                channels != 2 or count != self.spec.count or
                abs(float(start)) > .025 or abs(float(video_start)) > .01 or
                abs(float(length - duration)) > .05 or
                not result.get('nb_read_frames') or
                result.get('nb_read_frames') in ('0', 'N/A')):
            raise RuntimeError('Muxed AAC stream failed timing, duration, codec or frame-count verification.')
        return dict(source_receipt,
                    output_stream_index=result['index'],
                    output_codec=result['codec_name'],
                    output_sample_rate=sample_rate, output_channels=channels,
                    output_start_time=str(start),
                    output_video_start_time=str(video_start),
                    output_duration=str(length),
                    output_audio_frames=int(result['nb_read_frames']),
                    expected_duration=str(duration))

    def _managed_composite_pixels(self, entry, members, parameters, mode,
                                  state, alpha_by_path, np):
        """The production float CPU compositing reference, never QPixmap SDR."""
        from ..core.frame_payload import FramePayload, FrameGeometry
        from ..viewer.compositor import composite_working
        from ..viewer.color import process_pixels
        width, height = self.spec.output_size
        physical = self.ctx['composite_export']['version'] == 2
        payloads = []
        states = []
        source_geometries = []
        for info, member in zip(entry['members'], members):
            if self.isInterruptionRequested():
                raise RuntimeError('Managed composite cancelled.')
            size = (member['width'], member['height'])
            path = info['path']
            member_color = member['color']
            if path is not None:
                channels = 4 if alpha_by_path[path] else 3
                if member.get('input_kind') == 'movie':
                    pixels = self._managed_composite_movie_frame(
                        member, info['input_frame_index'], np)
                else:
                    pixels = self._managed_read_frame(path, np, channels, size=size)
                alpha = member['alpha_mode'] if channels == 4 else 'opaque'
                metadata = {'input_space': member_color['input_space']}
            elif info['kind'] == 'outside_trim':
                pixels = np.zeros((size[1], size[0], 4), np.float32)
                alpha = 'straight'
                metadata = {'input_space': state.working_space}
            else:
                # Placeholder is an intentional source-frame hole, NOT a
                # decoder failure; a failed real FFmpeg decode aborts export.
                policy = info['missing_policy']
                if policy == 'hold':
                    policy = 'black'  # no previous frame to hold
                missing = {'hole_policy': policy}
                pixels = self._managed_placeholder(
                    missing, size[0], size[1], 4, np)
                alpha = 'straight'
                metadata = {'input_space': 'sRGB'}
            member_par = float(Fraction(member['pixel_aspect'])) if physical else 1.0
            source_geometry = FrameGeometry(size[0], size[1], member_par)
            payloads.append(FramePayload(
                pixels, source_id=member['source_id'],
                source_frame=info['source_frame'] or 0,
                geometry=source_geometry,
                color_metadata=metadata, alpha_mode=alpha, stage='source'))
            source_geometries.append((size[0], size[1], member_par))
            local = dict(member_color)
            if path is None:
                local['input_space'] = metadata['input_space']
                local['bypass_input'] = False
            states.append(local)
        if mode == 'flicker':
            # Flicker selection is on the authoritative session *record*
            # frame, not an encoded-output ordinal, source label, seek index,
            # or FFmpeg keyframe. No new transport clock is introduced.
            if self.ctx['composite_export']['version'] != 2:
                raise ValueError('Flicker delivery requires independently verified v2 native EXR geometry.')
            period = parameters.get('flicker_frames', 1)
            if type(period) is not int or not 1 <= period <= 120:
                raise ValueError('Invalid frozen Flicker session-frame period.')
            parameters = dict(parameters)
            parameters.pop('flicker_frames', None)
            parameters['active'] = (int(entry['display_index']) // period) % 2
        working = composite_working(
            mode, payloads, color_state=state, width=width, height=height,
            source_color_states=states,
            source_geometries=source_geometries if physical else None,
            **parameters)
        if self.spec.stage == 'working':
            return working
        return process_pixels(working, state=state, stage='display',
                              source_stage='working', alpha_mode='straight')

    def _managed_composite_movie_probe(self, member):
        """Re-probe frozen CFR stream and ALL presentation timestamps.

        The frozen UI proof is never treated as verification: this independent
        per-job probe detects changed frame count, color, codec, SAR, rotation,
        timestamp spacing, stream index and source timing before any encode.
        """
        ffprobe = os.path.join(os.path.dirname(self.ffmpeg), 'ffprobe')
        if not os.path.isfile(ffprobe):
            ffprobe = shutil.which('ffprobe')
        if not ffprobe:
            raise RuntimeError('Composite CFR movie requires ffprobe.')
        source = member['source_path']
        frozen = member['movie_proof']
        self._managed_composite_movie_check_hash(member)
        kwargs = {'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE,
                  'env': media_environment()}
        kwargs.update(_no_window_kwargs())

        def probe(args, max_bytes, timeout):
            if self.isInterruptionRequested():
                raise RuntimeError('Composite movie probe cancelled.')
            self._decode_proc = subprocess.Popen(args, **kwargs)
            try:
                output, err = self._decode_proc.communicate(timeout=timeout)
                if (self.isInterruptionRequested() or self._decode_proc.returncode or
                        len(output) > max_bytes):
                    raise RuntimeError('Composite movie probe failed/cancelled: {}'.format(
                        err.decode('utf-8', 'replace')[-350:]))
                return output
            finally:
                if self._decode_proc.poll() is None:
                    self._decode_proc.kill()
                    self._decode_proc.wait()
                self._decode_proc = None

        metadata = json.loads(probe([
            ffprobe, '-v', 'error', '-count_frames',
            '-show_entries',
            'stream=index,codec_type,codec_name,width,height,pix_fmt,avg_frame_rate,r_frame_rate,time_base,nb_read_frames,sample_aspect_ratio,color_space,color_range,start_time,duration,sample_rate,channels,channel_layout,side_data_list:stream_tags=rotate',
            '-of', 'json', source], 256 * 1024, 35).decode('utf-8'))
        streams = metadata.get('streams', [])
        # Legacy v3 still requires video-only. V4 PCM sidecar explicitly
        # freezes every audio stream in proof v2; hidden streams fail.
        if (not streams or streams[0].get('codec_type') not in (None, 'video') or
                (len(streams) != 1 and frozen.get('version') != 2)):
            raise ValueError('Composite movie streams disagree with frozen video/audio proof.')
        info = streams[0]
        actual = {
            'version': 1, 'stream_index': int(info.get('index', -1)),
            'codec': info.get('codec_name'), 'pix_fmt': info.get('pix_fmt'),
            'avg_frame_rate': info.get('avg_frame_rate'),
            'r_frame_rate': info.get('r_frame_rate'),
            'time_base': info.get('time_base'),
            'frame_count': int(info.get('nb_read_frames', -1)),
            'width': int(info.get('width', -1)),
            'height': int(info.get('height', -1)),
            'sample_aspect_ratio': info.get('sample_aspect_ratio'),
            'rotation': int(info.get('tags', {}).get('rotate', 0) or 0),
            'color_space': info.get('color_space'),
            'color_range': info.get('color_range'),
            'start_time': info.get('start_time'),
            'source_sha256': frozen['source_sha256'],
            'source_size_bytes': frozen['source_size_bytes'],
        }
        if frozen.get('version') == 2:
            actual['version'] = 2
            actual['audio_tracks'] = _record_audio_track_proofs(streams)
        side_data = info.get('side_data_list', [])
        if side_data and any(float(v.get('rotation', 0) or 0) for v in side_data):
            raise ValueError('Composite movie contains unsupported displaymatrix rotation.')
        if actual != frozen:
            raise ValueError('Composite movie ffprobe metadata changed or contradicts frozen stream proof.')
        stamps = probe([
            ffprobe, '-v', 'error', '-select_streams', 'v:0',
            '-show_frames', '-show_entries',
            'frame=best_effort_timestamp_time', '-of', 'csv=p=0', source],
            256 * 1024, 60)
        try:
            parsed = [Fraction(line.strip().split(',')[0]) for line in
                      stamps.decode('utf-8').splitlines() if line.strip()]
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            raise ValueError('Composite CFR movie has unreadable decoded timestamps: {}'.format(exc))
        if len(parsed) != frozen['frame_count']:
            raise ValueError('Composite CFR movie frame timestamp count changed.')
        fps = Fraction(member['source_fps'])
        for i, pts in enumerate(parsed):
            if abs(pts - Fraction(i, 1) / fps) > Fraction(1, 2000):
                raise ValueError('Composite movie frame {} has variable/nonzero timestamps (VFR unsupported).'.format(i))
        return actual

    def _managed_composite_movie_check_hash(self, member):
        """Bounded byte-identity proof, even if metadata/mtime was restored."""
        path = member['source_path']
        frozen = member['movie_proof']
        if os.path.islink(path):
            raise ValueError('Composite movie source symlink is not trusted.')
        info = os.stat(path)
        if (not 0 < info.st_size <= 512 * 1024 * 1024 or
                info.st_size != frozen['source_size_bytes']):
            raise ValueError('Composite movie source size differs from frozen SHA256 identity.')
        digest = hashlib.sha256()
        size = 0
        with open(path, 'rb') as stream:
            while True:
                if self.isInterruptionRequested():
                    raise RuntimeError('Composite movie hashing cancelled.')
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > frozen['source_size_bytes']:
                    raise ValueError('Composite movie source grew during hashing.')
                digest.update(chunk)
        after = os.stat(path)
        if (size != frozen['source_size_bytes'] or
                digest.hexdigest() != frozen['source_sha256'] or
                (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise ValueError('Composite movie changed since its frozen SHA256 proof.')

    def _managed_composite_movie_frame(self, member, index, np):
        """Deterministically decode one proven zero-based CFR index to float RGB.

        FFmpeg decodes from frame zero with `select=eq(n,index)`; no approximate
        seek, time rounding, proxy/thumb cache or duplicated FPS resampling.
        Each process is bounded and cancelled/terminated before the next one.
        """
        if type(index) is not int or not 0 <= index < member['movie_proof']['frame_count']:
            raise ValueError('Composite movie input index is outside its frozen source.')
        key = member['source_id']
        cached = self._composite_movie_last.get(key)
        if cached is not None and cached[0] == index:
            return cached[1]
        if self.isInterruptionRequested():
            raise RuntimeError('Composite movie frame selection cancelled.')
        provenance = member['movie_color_provenance']
        matrix = provenance['yuv_matrix']
        rng = provenance['yuv_range']
        width, height = member['width'], member['height']
        expected = width * height * 3 * 4
        if expected > 64 * 1024 * 1024:
            raise ValueError('Composite movie float frame exceeds bounded decode memory.')
        expression = 'select=eq(n\\,{})'.format(index)
        filters = 'scale=in_color_matrix={}:in_range={},{}'.format(
            matrix, 'full' if rng == 'full' else 'limited', expression)
        args = [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-xerror',
                '-nostdin', '-i', member['source_path'], '-map', '0:v:0',
                '-an', '-sn', '-dn', '-vf', filters, '-fps_mode', 'passthrough',
                '-frames:v', '1', '-pix_fmt', 'gbrpf32le',
                '-f', 'rawvideo', 'pipe:1']
        kwargs = {'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE,
                  'env': media_environment()}
        kwargs.update(_no_window_kwargs())
        self._decode_proc = subprocess.Popen(args, **kwargs)
        try:
            raw, err = self._decode_proc.communicate(timeout=90)
            if (self.isInterruptionRequested() or self._decode_proc.returncode or
                    len(raw) != expected):
                raise RuntimeError('Composite movie decode-index {} failed/cancelled: {}'.format(
                    index, err.decode('utf-8', 'replace')[-350:]))
        finally:
            if self._decode_proc.poll() is None:
                self._decode_proc.kill()
                self._decode_proc.wait()
            self._decode_proc = None
        planes = np.frombuffer(raw, dtype='<f4').reshape((3, height, width))
        pixels = np.stack((planes[2], planes[0], planes[1]), axis=-1)
        self._composite_movie_last[key] = (index, pixels)
        return pixels

    def _managed_export(self):
        """Managed RGB/RGBA image or confirmed CFR movie precision export.

        Float EXR sequences retain negatives and HDR highlights. Non-EXR
        display outputs explicitly quantize/clip only at the final encode step.
        Unsupported effects fail instead of being silently omitted.
        """
        import numpy as np
        from ..viewer.color import ColorPipelineState, process_pixels
        validate_managed_export(self.spec, self.seq_files,
                                (self.width, self.height))
        movie = bool(self.ctx.get('managed_movie'))
        composite = bool(self.ctx.get('composite_export'))
        output_dir = os.path.dirname(os.path.abspath(self.spec.path))
        if (not os.path.basename(output_dir).startswith('.frqueue-') or
                not os.path.isdir(output_dir) or
                os.path.islink(output_dir) or os.path.lexists(self.spec.path)):
            raise ValueError('Managed color export must render to a new private durable-queue stage; refusing in-place overwrite.')
        plan = (managed_composite_plan(self.spec) if composite else
                managed_movie_plan(self.spec) if movie else
                managed_input_plan(self.spec, self.seq_files))
        state = ColorPipelineState.from_dict(self.ctx['color'])
        geometry = (managed_geometry_plan(self.spec, (self.width, self.height))
                    if self.ctx.get('managed_geometry_export') is not None else None)
        audio_preserve = movie and self.ctx.get('movie_audio_policy') == 'preserve'
        source_audio_receipt = None
        composite_source_stats = None
        composite_movie_probes = None
        if composite:
            members = self.ctx['composite_export']['members']
            physical = self.ctx['composite_export']['version'] == 2
            record = self.ctx['composite_export']['version'] == 4
            mixed = self.ctx['composite_export']['version'] in (3, 4)
            self._composite_movie_last = {}
            if self.spec.count > 2000 or sum(len(m['seq_files']) for m in members) > 2000:
                raise ValueError('Composite export exceeds bounded 2,000-frame decode budget.')
            source_paths = dict.fromkeys(
                (p['path'], m['width'], m['height'],
                 m['pixel_aspect'] if physical else '1/1')
                for entry in plan for p in entry['members']
                for m in (members[p['member_index']],)
                if p['path'])
            if physical or mixed:
                composite_source_stats = {}
                for path, w, h, par in source_paths:
                    if self.isInterruptionRequested():
                        raise RuntimeError('Managed composite geometry probe cancelled.')
                    if os.path.islink(path):
                        raise ValueError('Composite native geometry cannot use a mutable source symlink.')
                    stat = os.stat(path)
                    if mixed and (not stat.st_size or stat.st_size > 512 * 1024 * 1024):
                        raise ValueError('Composite v3 source exceeds bounded movie/file size.')
                    composite_source_stats[path] = (stat.st_dev, stat.st_ino,
                                                    stat.st_size, stat.st_mtime_ns)
                    if not mixed or path.lower().endswith('.exr'):
                        self._managed_geometry_probe_exr(path, {
                            'input_size': [w, h], 'pixel_aspect': par})
            if mixed:
                composite_movie_probes = {}
                for member in members:
                    if member['input_kind'] == 'movie':
                        composite_movie_probes[(member['occurrence_id'] if record
                                                 else member['source_id'])] = (
                            self._managed_composite_movie_probe(member))
            record_audio_enabled = (record and
                self.ctx['composite_export']['audio_policy'] == 'wav_pcm_v1')
            source_alpha = {
                path: (False if mixed and os.path.splitext(path)[1].lower() in
                       MANAGED_MOVIE_EXTENSIONS else
                       self._managed_probe_frame(path, size=(w, h)))
                for path, w, h, _ in source_paths}
            for member in members:
                if member['alpha_mode'] == 'opaque' and any(
                        source_alpha.get(path) for path in member['seq_files']):
                    raise ValueError(
                        'Composite member {} declares opaque alpha but source '
                        'contains transparency channels; confirm straight or '
                        'premultiplied alpha instead.'.format(
                            member['source_id']))
            source_has_alpha = any(source_alpha.values())
            # The reference always emits RGBA. Work EXR and Display PNG retain
            # exact straight alpha; MP4 must be strictly opaque across frames.
            has_alpha = True
            movie_info = None
        elif movie:
            if audio_preserve:
                self._movie_audio_source_identity = self._managed_audio_stat()
            movie_info = self._managed_movie_probe()
            if audio_preserve:
                self._managed_audio_assert_source()
            has_alpha = movie_info['has_alpha']
            if has_alpha:
                confirmed_alpha = self.ctx['movie_color_provenance'].get('alpha_mode')
                if confirmed_alpha not in ('straight', 'premultiplied') or confirmed_alpha != state.alpha_mode:
                    raise ValueError('Managed movie with transparency requires explicit frozen movie_color_provenance.alpha_mode matching the pixel alpha association.')
            if audio_preserve:
                source_audio_receipt = self._managed_audio_source(movie_info)
        else:
            movie_info = None
            # A later RGBA frame must not lose alpha simply because frame one
            # is RGB. Probe before opening the fixed-format rawvideo encoder.
            sources = dict.fromkeys(frame['path'] for frame in plan if frame['path'])
            for path in sources:
                if self.isInterruptionRequested():
                    raise RuntimeError('Managed image export cancelled.')
                # Even the legacy no-geometry path must never flatten a real
                # non-square/windowed/AOV EXR under a square-pixel provenance
                # claim. This is a *probe* only; no new decoder or selection.
                self._managed_geometry_probe_exr(
                    path, geometry or {
                        'input_size': [self.width, self.height],
                        'pixel_aspect': '1/1'})
            has_alpha = any(self._managed_probe_frame(path) for path in sources)
        channels = 4 if has_alpha else 3
        suffix = os.path.splitext(self.spec.path)[1].lower()
        if has_alpha and suffix in ('.jpg', '.jpeg', '.mp4') and not composite:
            raise ValueError('Transparent source media cannot be encoded as {} without losing alpha; use EXR or PNG.'.format(suffix))
        if composite and suffix == '.mp4':
            if any(m['alpha_mode'] != 'opaque' for m in members):
                raise ValueError('Composite MP4 does not preserve member transparency; use EXR or PNG.')
            has_alpha = False
        float_output = suffix == '.exr'
        if float_output:
            fmt = 'gbrapf32le' if has_alpha else 'gbrpf32le'
            codec = ['-c:v', 'exr', '-pix_fmt', fmt,
                     '-compression', 'zip16', '-format', 'float']
        elif suffix == '.png':
            fmt = 'rgba64be' if has_alpha else 'rgb48le'
            codec = ['-c:v', 'png', '-pix_fmt', fmt]
        else:
            fmt = 'rgb24'
            codec = (['-c:v', 'mjpeg', '-q:v', '2', '-pix_fmt', 'yuvj420p']
                     if self.spec.sequence else
                     ['-c:v', 'libx264', '-crf', '18', '-preset', 'medium',
                      '-pix_fmt', 'yuv420p'])
        width, height = self.spec.output_size
        encode_target = self._output_path()
        if audio_preserve:
            # Video-only encoding and audio mux both stay inside the new
            # queue-private directory. Only the final validated MP4 is
            # submitted for durable publication; no public target is touched.
            fd, encode_target = tempfile.mkstemp(
                prefix='.frvideo-', suffix='.mp4', dir=output_dir)
            os.close(fd)
            os.unlink(encode_target)
            self._movie_audio_temp = encode_target
        args = [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
                '-y', '-f', 'rawvideo', '-pix_fmt', fmt,
                '-video_size', '{}x{}'.format(width, height),
                # Every plan item maps to exactly one output frame. Applying
                # source FPS here and re-timing with -r would duplicate/drop
                # mapped sparse frames when the reviewer selects another FPS.
                '-framerate', self.spec.fps, '-i', 'pipe:0',
                '-frames:v', str(self.spec.count), '-an'] + codec
        if self.spec.sequence:
            args += ['-f', 'image2', '-start_number', '1001', self._output_path()]
        else:
            args += ['-r', self.spec.fps, '-movflags', '+faststart',
                     encode_target]
        kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                      stderr=subprocess.PIPE, bufsize=1024 * 1024)
        kwargs.update(_no_window_kwargs())
        kwargs['env'] = media_environment()
        self._proc = subprocess.Popen(args, **kwargs)
        clipped = 0
        source_map = []
        try:
            if movie:
                self._managed_movie_start(movie_info)
            for i, frame in enumerate(plan):
                if self.isInterruptionRequested():
                    raise RuntimeError('Managed export cancelled.')
                if composite:
                    if record and frame['kind'] == 'record_gap':
                        # A record gap is genuinely no source and carries
                        # transparent black in both EXR and PNG, not an
                        # inferred clip frame or opaque missing-file slate.
                        pixels = np.zeros((height, width, 4), np.float32)
                    elif record:
                        origin = frame['members'][0]
                        pixels = self._managed_composite_pixels(
                            {'members': [origin]},
                            [members[origin['member_index']]], {},
                            'a_b', state, source_alpha, np)
                    else:
                        pixels = self._managed_composite_pixels(
                            frame, members, self.ctx['composite_export']['parameters'],
                            self.ctx['composite_export']['mode'], state, source_alpha,
                            np)
                    if suffix == '.mp4' and not np.all(
                            pixels[:, :, 3] >= 1 - 1e-6):
                        raise ValueError('Composite MP4 cannot flatten transparent pixels.')
                    if not has_alpha:
                        pixels = pixels[:, :, :3]
                elif movie:
                    raw_pixels = self._managed_movie_next(np, channels)
                    processed = process_pixels(raw_pixels, state=state,
                                               stage=self.spec.stage,
                                               source_stage='source',
                                               alpha_mode=state.alpha_mode)
                    pixels = self._managed_geometry(processed)
                elif frame['path'] is None:
                    pixels = self._managed_placeholder(
                        frame, self.width if geometry else width,
                        self.height if geometry else height, channels, np)
                    if geometry:
                        # Synthetic missing-frame slates have no input color
                        # interpretation. They are already in display-like
                        # float values; rectify only their pixel geometry.
                        from dataclasses import replace
                        pixels = self._managed_geometry_frame(
                            pixels, geometry,
                            replace(state, bypass_display=True), np)
                else:
                    raw_pixels = self._managed_read_frame(frame['path'], np, channels)
                    if geometry:
                        processed = process_pixels(raw_pixels, state=state,
                                                   stage='working',
                                                   source_stage='source',
                                                   alpha_mode=state.alpha_mode)
                        pixels = self._managed_geometry_frame(
                            processed, geometry, state, np)
                    else:
                        processed = process_pixels(raw_pixels, state=state,
                                                   stage=self.spec.stage,
                                                   source_stage='source',
                                                   alpha_mode=state.alpha_mode)
                        pixels = self._managed_geometry(processed)
                if (has_alpha and not composite and suffix == '.png' and
                        state.alpha_mode == 'premultiplied' and
                        (movie or frame.get('path') is not None)):
                    # PNG stores straight alpha; EXR retains the source alpha
                    # association. Process_pixels already re-premultiplies
                    # transformed RGB, so unassociate exactly once for PNG.
                    # Composite_working already returns straight alpha, so
                    # NEVER unpremultiply it a second time.
                    alpha = pixels[:, :, 3:4]
                    pixels = pixels.copy()
                    pixels[:, :, :3] = np.divide(
                        pixels[:, :, :3], alpha,
                        out=np.zeros_like(pixels[:, :, :3]), where=alpha > 1e-8)
                if not np.all(np.isfinite(pixels)):
                    raise RuntimeError('Managed transform yielded NaN or infinity at source frame {}.'.format(
                        frame['source_frame']))
                if float_output:
                    # FFmpeg planar float stores G, B, R, and optional A.
                    order = (1, 2, 0, 3) if has_alpha else (1, 2, 0)
                    payload = np.ascontiguousarray(
                        np.stack(tuple(pixels[:, :, index] for index in order),
                                 axis=0), dtype='<f4').tobytes()
                else:
                    clipped += int(np.count_nonzero((pixels < 0) | (pixels > 1)))
                    maximum = 65535 if fmt in ('rgb48le', 'rgba64be') else 255
                    dtype = '>u2' if fmt == 'rgba64be' else '<u2' if fmt == 'rgb48le' else 'u1'
                    payload = np.ascontiguousarray(
                        (np.clip(pixels, 0, 1) * maximum + 0.5).astype(dtype)).tobytes()
                self._proc.stdin.write(payload)
                if composite:
                    source_map.append(frame)
                elif movie:
                    source_map.append(dict(frame))
                else:
                    source_map.append({
                        'display_index': frame['display_index'],
                        'source_frame': frame['source_frame'],
                        'hole': frame['hole'], 'held_from': frame['held_from'],
                        'missing_policy': frame['hole_policy'],
                        'kind': 'intentional_hole' if frame['hole'] else 'decoded_source'})
                self.progress.emit(int(90 * (i + 1) / len(plan)))
            if movie:
                self._managed_movie_finish()
                if audio_preserve:
                    self._managed_audio_assert_source()
            self._proc.stdin.close()
            # Avoid communicate() flushing the already-closed stdin.
            stderr = self._proc.stderr.read()
            self._proc.stderr.close()
            code = self._proc.wait(timeout=120)
            if code:
                raise RuntimeError('Managed FFmpeg encode failed: {}'.format(
                    stderr.decode('utf-8', 'replace')[-800:]))
        finally:
            if movie:
                self._managed_movie_abort()
            if self._proc is not None:
                proc = self._proc
                self._proc = None  # Never retain a killed QThread subprocess.
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                for pipe in (proc.stdin, proc.stderr):
                    if pipe is not None and not pipe.closed:
                        try:
                            pipe.close()
                        except OSError:
                            # Closing a pipe after FFmpeg was killed by cancel
                            # can raise EPIPE; the process has already exited.
                            pass
        audio_mux_receipt = None
        if audio_preserve:
            if self.isInterruptionRequested():
                raise RuntimeError('Managed movie audio cancelled.')
            self._managed_audio_assert_source()
            audio_mux_receipt = self._managed_movie_mux_audio(
                encode_target, source_audio_receipt)
            self._managed_audio_assert_source()
            os.unlink(encode_target)
            self._movie_audio_temp = None
        if composite_source_stats is not None:
            # A frozen EXR geometry proof cannot silently outlive an in-flight
            # source replacement. This check is after every pixel was decoded
            # and before the stage may emit a verified receipt for queue commit.
            if self.isInterruptionRequested():
                raise RuntimeError('Managed composite geometry export cancelled.')
            for path, original in composite_source_stats.items():
                if os.path.islink(path):
                    raise RuntimeError('Composite native source changed to symlink during export.')
                stat = os.stat(path)
                if (stat.st_dev, stat.st_ino, stat.st_size,
                        stat.st_mtime_ns) != original:
                    raise RuntimeError('Composite native source changed during geometry-verified export: {}'.format(path))
            if composite_movie_probes is not None:
                for member in members:
                    if member['input_kind'] == 'movie':
                        self._managed_composite_movie_check_hash(member)
        record_audio_receipt = None
        if composite and record_audio_enabled:
            if self.isInterruptionRequested():
                raise RuntimeError('Managed record WAV cancelled before staging.')
            from .record_audio import render_record_audio
            record_audio_receipt = render_record_audio(
                self.spec, self.ffmpeg,
                interrupted=self.isInterruptionRequested,
                set_process=lambda process: setattr(self, '_decode_proc', process))
        record_av_receipt = None
        if composite and record and self.ctx['composite_export'].get('record_av') is not None:
            if self.isInterruptionRequested():
                raise RuntimeError('Managed record MOV cancelled before private stage mux.')
            from .record_av_mux import render_record_av
            record_av_receipt = render_record_av(
                self.spec, self.ffmpeg,
                interrupted=self.isInterruptionRequested,
                set_process=lambda process: setattr(self, '_decode_proc', process))
        record_rgba16_receipt = None
        if composite and record and self.ctx['composite_export'].get('record_rgba16') is not None:
            if self.isInterruptionRequested():
                raise RuntimeError('Managed record RGBA16 NUT cancelled before private stage.')
            from .record_rgba16_mux import render_record_rgba16
            record_rgba16_receipt = render_record_rgba16(
                self.spec, self.ffmpeg,
                interrupted=self.isInterruptionRequested,
                set_process=lambda process: setattr(self, '_decode_proc', process))
        return {'input_stage': 'source', 'stage': self.spec.stage,
                'pipeline': state.to_dict(),
                'input_kind': ('composite_image_sequence' if composite else
                               'movie' if movie else 'image_sequence'),
                'composite_export': self.ctx.get('composite_export') if composite else None,
                'composite_geometry_plan': (
                    managed_composite_geometry_plan(self.spec) if composite else None),
                'composite_movie_probes': composite_movie_probes,
                'record_audio_receipt': record_audio_receipt,
                'record_av_receipt': record_av_receipt,
                'record_rgba16_receipt': record_rgba16_receipt,
                'managed_geometry_export': self.ctx.get('managed_geometry_export') if geometry else None,
                'geometry_plan': geometry,
                'movie_color_provenance': self.ctx.get('movie_color_provenance') if movie else None,
                'movie_decode': movie_info,
                'audio_policy': self.ctx.get('movie_audio_policy') if movie else None,
                'audio_export': self.ctx.get('movie_audio_export') if audio_preserve else None,
                'audio_mux': audio_mux_receipt,
                'frame_map': source_map, 'output_sample_format': fmt,
                'source_has_alpha': (source_has_alpha if composite else has_alpha),
                'alpha_mode': ('straight' if composite and has_alpha else
                               state.alpha_mode if has_alpha else 'opaque'),
                'output_alpha_mode': ('straight' if composite and has_alpha else
                                       'straight' if suffix == '.png' else state.alpha_mode)
                                     if has_alpha else 'opaque',
                'precision': 'float32 scene-referred HDR' if float_output
                             else '16-bit normalized display' if fmt in ('rgb48le', 'rgba64be')
                             else '8-bit normalized display',
                'clipped_channel_samples': clipped}

    # -- tracked export ---------------------------------------------------
    def _nearest_crop(self, index):
        c = self.ctx
        original = c['crop'] or (0, 0, self.width, self.height)
        if not c['tracking_enabled'] or not c['centers']:
            return original
        centers = c['centers']
        nearest = min(centers, key=lambda f: abs(f - index))
        cx, cy = centers[nearest]
        dx, dy = c['tracking_offset']
        w, h = c['tracking_size'] or original[2:]
        w = int(utils.clamp(w, 2, self.width))
        h = int(utils.clamp(h, 2, self.height))
        x = int(utils.clamp(round(cx + dx - w / 2), 0, self.width - w))
        y = int(utils.clamp(round(cy + dy - h / 2), 0, self.height - h))
        return x, y, w, h

    def _nearest_quad(self, index):
        """Apply crop edits to the tracked corners in the displayed coordinate space."""
        c = self.ctx
        quads = c.get('tracking_quads') or {}
        if not quads:
            return None
        nearest = min(quads, key=lambda frame: abs(frame - index))
        corners = tracking.np.asarray(quads[nearest], dtype=tracking.np.float32)
        if corners.shape != (4, 2):
            return None
        centers = c['centers']
        nearest_center = min(centers, key=lambda frame: abs(frame - index))
        cx, cy = centers[nearest_center]
        base_w, base_h = c.get('tracking_base_size') or c.get('tracking_size') or (1, 1)
        current_w, current_h = c.get('tracking_size') or (base_w, base_h)
        offset_x, offset_y = c['tracking_offset']
        corners[:, 0] = cx + (corners[:, 0] - cx) * current_w / max(1.0, base_w) + offset_x
        corners[:, 1] = cy + (corners[:, 1] - cy) * current_h / max(1.0, base_h) + offset_y
        if not tracking.np.all(tracking.np.isfinite(corners)):
            return None
        return corners

    def _tracked(self):
        c = self.ctx
        cv2, np = tracking.cv2, tracking.np
        frame_bytes = self.width * self.height * 3
        cmd = ([self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin'] + self._source_args()
               + ['-frames:v', str(c['count']), '-an', '-sn', '-pix_fmt', 'bgr24', '-f', 'rawvideo', 'pipe:1'])
        tempdir = tempfile.mkdtemp(prefix='framereviewer_export_')
        proc = None
        try:
            kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1024 * 1024)
            kwargs.update(_no_window_kwargs())
            kwargs['env'] = media_environment()
            proc = subprocess.Popen(cmd, **kwargs)
            self._proc = proc
            for i in range(c['count']):
                if self.isInterruptionRequested():
                    raise RuntimeError('Export cancelled')
                data = bytearray()
                while len(data) < frame_bytes:
                    chunk = proc.stdout.read(min(1024 * 1024, frame_bytes - len(data)))
                    if not chunk:
                        raise RuntimeError('FFmpeg decode ended at frame {}'.format(i))
                    data.extend(chunk)
                image = np.frombuffer(data, dtype=np.uint8).reshape((self.height, self.width, 3))
                if c['flip_h']:
                    image = cv2.flip(image, 1)
                if c['flip_v']:
                    image = cv2.flip(image, 0)
                quad = (self._nearest_quad(c['start'] + i)
                        if c['tracking_enabled'] and c.get('tracking_mode', 1) != 1 else None)
                if quad is not None:
                    # Perspective rectification also works for similarity and
                    # affine tracks, preserving rotation, scale, and shear.
                    dest = np.float32([[0, 0], [c['out_w'] - 1, 0],
                                       [c['out_w'] - 1, c['out_h'] - 1],
                                       [0, c['out_h'] - 1]])
                    warp = cv2.getPerspectiveTransform(quad.astype(np.float32), dest)
                    cropped = cv2.warpPerspective(
                        image, warp, (c['out_w'], c['out_h']),
                        flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
                else:
                    x, y, w, h = self._nearest_crop(c['start'] + i)
                    cropped = cv2.resize(
                        image[y:y + h, x:x + w], (c['out_w'], c['out_h']),
                        interpolation=cv2.INTER_AREA)
                if c.get('burnin'):
                    rgb = cv2.cvtColor(cropped, cv2.COLOR_BGR2RGB)
                    context = dict(c.get('burnin_context') or {})
                    context.setdefault('fps', c['source_fps'])
                    rgb = burnin.render_frame(
                        rgb, c['burnin'], context, c['start'] + i)
                    cropped = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                extension = 'png' if c.get('burnin') else 'jpg'
                path = os.path.join(tempdir, 'crop_{:06d}.{}'.format(i, extension))
                options = [] if extension == 'png' else [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
                if not cv2.imwrite(path, cropped, options):
                    raise RuntimeError('Cannot write processing frame {}'.format(path))
                if i % 5 == 0:
                    self.progress.emit(int(75 * (i + 1) / c['count']))
            proc.stdout.close()
            if proc.wait() != 0:
                raise RuntimeError('FFmpeg raw frame decode failed.')
            self._proc = None
            self.progress.emit(80)
            self._execute(self._final_args(
                ['-framerate', '{:.8f}'.format(c['source_fps']), '-start_number', '0',
                 '-i', os.path.join(tempdir, 'crop_%06d.' + extension)], moving=True))
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()
            self._proc = None
            import shutil
            shutil.rmtree(tempdir, ignore_errors=True)

    def _managed_stabilized_export(self):
        """One stage-owned QThread renders only proven native Working EXR.

        All color, signed source labels, tracking, frame identity, pixels and
        per-frame SHA256 receipts come from the independently tested native
        stabilized_renderer. No screen capture or display-stage shortcut.
        """
        from .stabilized_renderer import (
            validate_stabilized_export, render_stabilized_frame)
        frozen = self.ctx['stabilization_export']
        validate_managed_export(self.spec, self.seq_files, (self.width, self.height))
        folder = os.path.dirname(os.path.abspath(self.spec.path))
        if (not os.path.isdir(folder) or os.path.islink(folder) or
                not os.path.basename(folder).startswith('.frqueue-')):
            raise RuntimeError('Stabilized export needs a new durable queue stage.')
        validate_stabilized_export(frozen, cancel=self.isInterruptionRequested,
                                   source_id=self.spec.source_id, verify_files=True)
        receipts, mapping = [], []
        for i, row in enumerate(frozen['frames']):
            if self.isInterruptionRequested():
                raise RuntimeError('Stabilization interrupted before native EXR frame.')
            name = os.path.basename(self.spec.path).replace(
                '####', '{:04d}'.format(1001 + i))
            output = os.path.join(folder, name)
            receipt = render_stabilized_frame(
                frozen, row['source_frame'], output, ffmpeg=self.ffmpeg,
                cancel=self.isInterruptionRequested)
            receipts.append(receipt)
            mapping.append({'display_index': self.spec.start + i,
                            'source_frame': row['source_frame'],
                            'source_id': frozen['source_id'],
                            'source_path': row['source_path'],
                            'offset': list(row['offset'])})
            self.progress.emit(int(90 * (i + 1) / self.spec.count))
        validate_stabilized_export(frozen, cancel=self.isInterruptionRequested,
                                   source_id=self.spec.source_id, verify_files=True)
        return {'input_stage': 'source', 'stage': 'working',
                'input_kind': 'stabilized_exr_sequence',
                'stabilization_export': frozen,
                'stabilization_receipts': receipts, 'frame_map': mapping,
                'output_sample_format': 'gbrapf32le',
                'pipeline': frozen['source_color'],
                'output_alpha_mode': ('premultiplied' if
                    frozen['alpha_mode'] == 'premultiplied' else 'straight'),
                'precision': 'native-linear float32 EXR with source-verified stabilization'}

    def run(self):
        try:
            managed = None
            if self.ctx.get('stabilization_export') is not None:
                managed = self._managed_stabilized_export()
            elif self.spec.stage != 'source' or self.ctx.get('managed_movie') or self.ctx.get('composite_export'):
                managed = self._managed_export()
            else:
                # Source stage retains its legacy native FFmpeg route, but
                # transparent image sequences may never be silently flattened
                # by the JPEG/H264 encoders used by that route.
                suffix = os.path.splitext(self.spec.path)[1].lower()
                if self.seq_files and suffix in ('.mp4', '.jpg', '.jpeg'):
                    for path in self.seq_files:
                        if self.isInterruptionRequested():
                            raise RuntimeError('Export cancelled')
                        if self._managed_probe_frame(path):
                            raise ValueError(
                                'Alpha image sequences cannot be exported to {} without discarding transparency; use an EXR/PNG image sequence.'.format(suffix))
            if managed is None and self.spec.stage == 'source' and (
                    (self.ctx['tracking_enabled'] and self.ctx['centers']) or
                    (self.ctx.get('burnin') and tracking.HAVE_CV2)):
                if not tracking.HAVE_CV2:
                    raise RuntimeError('OpenCV required for tracked/burned export')
                self._tracked()
            elif managed is None and self.spec.stage == 'source':
                self._execute(self._final_args(self._source_args()))
            if self.isInterruptionRequested():
                raise RuntimeError('Export cancelled')
            probe = os.path.join(os.path.dirname(self.ffmpeg), 'ffprobe')
            verification = verify_output(self.spec, ffprobe=probe if os.path.isfile(probe) else None)
            if managed is not None:
                verification['managed_color'] = managed
            if self._movie_audio_source_identity is not None:
                self._managed_audio_assert_source()
            self.spec.write_manifest(verification)
            self.progress.emit(100)
            self.result.emit()
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self._movie_audio_source_identity = None
            if self._movie_audio_temp is not None:
                try:
                    if os.path.isfile(self._movie_audio_temp):
                        os.unlink(self._movie_audio_temp)
                except OSError:
                    pass
                self._movie_audio_temp = None
            if self._input_temp:
                import shutil
                shutil.rmtree(self._input_temp, ignore_errors=True)
                self._input_temp = None
