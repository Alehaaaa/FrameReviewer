# -*- coding: utf-8 -*-
"""Sequential export orchestration for existing QThread ExportJob instances.

No Qt dependency is imported here. The caller supplies an unstarted ExportJob
factory; signals are connected before start and the *finished* callback (not
the result signal) releases the slot, avoiding overlapping FFmpeg exports.

Completed media is verified in a same-filesystem private staging directory
before it is promoted. Cancelled/failed stages never touch final outputs.
"""
from __future__ import absolute_import

import glob
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid

from .spec import (ExportSpec, verify_output, managed_movie_plan,
                   managed_composite_plan, managed_composite_geometry_plan,
                   managed_geometry_plan,
                   validate_managed_movie_audio)
from .queue_store import (ExportQueueStore, PENDING, RUNNING, COMMITTING,
                          SUCCEEDED, FAILED, CRASHED, CANCELLING, CANCELLED,
                          _owner_alive, _unsafe_output_parent)


class QueueBusyError(RuntimeError):
    """A live Maya/FrameReviewer instance already owns this export queue."""


def _files(spec):
    if spec.sequence:
        name = os.path.basename(spec.path)
        found = re.search(r'\#{4}', name)
        if found is None:
            raise ValueError('Invalid image sequence pattern.')
        return sorted(path for path in glob.glob(os.path.join(
            os.path.dirname(spec.path), name[:found.start()] + '*' +
            name[found.end():])) if os.path.isfile(path))
    return [spec.path] if os.path.isfile(spec.path) else []


def _signature(spec):
    """Inode-independent rename receipt, not a security hash or media check."""
    files = _files(spec)
    if not files:
        return None
    if any(os.path.islink(path) for path in files):
        raise ValueError('Export frames must not be symlinked.')
    signature = [[os.path.basename(path), os.stat(path).st_size,
                  os.stat(path).st_mtime_ns] for path in files]
    record = spec.to_context().get('composite_export') or {}
    if record.get('version') == 4 and record.get('audio_policy') == 'wav_pcm_v1':
        from .record_audio import audio_sidecar_path
        wav = audio_sidecar_path(spec)
        if not os.path.isfile(wav) or os.path.islink(wav):
            raise RuntimeError('Managed record audio sidecar is missing or symlinked.')
        digest = hashlib.sha256()
        with open(wav, 'rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        stat = os.stat(wav)
        signature.append([os.path.basename(wav), stat.st_size,
                          stat.st_mtime_ns, digest.hexdigest()])
    if record.get('version') == 4 and record.get('record_av') is not None:
        from .record_av_mux import record_av_path
        mov = record_av_path(spec)
        if not os.path.isfile(mov) or os.path.islink(mov):
            raise RuntimeError('Record A/V MOV is absent or symlinked.')
        digest = hashlib.sha256()
        with open(mov,'rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        stat = os.stat(mov)
        signature.append([os.path.basename(mov), stat.st_size,
                          stat.st_mtime_ns, digest.hexdigest()])
    if record.get('version') == 4 and record.get('record_rgba16') is not None:
        from .record_rgba16_mux import record_rgba16_path
        nut = record_rgba16_path(spec)
        if not os.path.isfile(nut) or os.path.islink(nut):
            raise RuntimeError('Record RGBA16 NUT is absent or symlinked.')
        digest = hashlib.sha256()
        with open(nut, 'rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        stat = os.stat(nut)
        signature.append([os.path.basename(nut), stat.st_size,
                          stat.st_mtime_ns, digest.hexdigest()])
    return signature


def _staging_parent(spec):
    # Image-sequence output is a dedicated folder, movie is a single filename.
    return (os.path.dirname(os.path.dirname(spec.path)) if spec.sequence
            else os.path.dirname(spec.path))


def _safe_stage(stage, spec, task_id):
    """Never recursively remove a path from an untrusted recovery document."""
    if not stage or not isinstance(stage, str):
        return False
    if not os.path.basename(stage).startswith('.frqueue-{}-'.format(task_id[:12])):
        return False
    parent = os.path.realpath(_staging_parent(spec))
    return (os.path.normpath(stage) == stage and
            os.path.dirname(stage) == os.path.abspath(_staging_parent(spec)) and
            os.path.dirname(os.path.realpath(stage)) == parent and
            not os.path.islink(stage))


def _target_safe(spec):
    """Recheck path components immediately before rename/overwrite."""
    target = os.path.dirname(spec.path) if spec.sequence else spec.path
    for path in (target, spec.path + '.frmanifest.json'):
        if os.path.islink(path):
            raise ValueError('Refusing to export over a symlink.')
    if _unsafe_output_parent(spec.path):
        raise ValueError('Refusing to export through a symlink parent.')
    if os.path.normpath(spec.path) != spec.path:
        raise ValueError('Refusing noncanonical output path.')


def _backup_path(spec, task):
    parent = _staging_parent(spec)
    name = '.frqueue-backup-{}-{}'.format(task.id[:12], task.attempt)
    return os.path.join(parent, name if spec.sequence else name + '.media')


def _valid_managed_color(managed, spec):
    """Check provenance against the frozen timeline, including signed holes."""
    ctx = spec.to_context()
    if ctx.get('stabilization_export') is not None:
        try:
            from .stabilized_renderer import (
                validate_stabilized_export, stabilized_linear_pixels,
                _decode_float, _hash_file, _header)
            import numpy as np
            frozen = ctx['stabilization_export']
            validate_stabilized_export(frozen, source_id=spec.source_id,
                                       verify_files=True)
            from .spec import validate_managed_export
            validate_managed_export(spec, (), spec.output_size)
            if (not isinstance(managed, dict) or
                    managed.get('input_stage') != 'source' or
                    managed.get('stage') != 'working' or
                    managed.get('input_kind') != 'stabilized_exr_sequence' or
                    json.dumps(managed.get('stabilization_export'), sort_keys=True) !=
                    json.dumps(frozen, sort_keys=True) or
                    managed.get('output_sample_format') != 'gbrapf32le' or
                    json.dumps(managed.get('pipeline'), sort_keys=True) !=
                    json.dumps(frozen['source_color'], sort_keys=True) or
                    managed.get('output_alpha_mode') != ('premultiplied' if
                        frozen['alpha_mode'] == 'premultiplied' else 'straight') or
                    not isinstance(managed.get('frame_map'), list) or
                    not isinstance(managed.get('stabilization_receipts'), list) or
                    len(managed['frame_map']) != spec.count or
                    len(managed['stabilization_receipts']) != spec.count):
                return False
            ffmpeg = shutil.which('ffmpeg')
            if not ffmpeg:
                return False
            # Neither a foreign frame nor an unreceipted extra media sidecar
            # may silently accompany a supposedly immutable native delivery.
            allowed = {os.path.basename(spec.path).replace(
                '####', '{:04d}'.format(1001 + i)) for i in range(spec.count)}
            allowed.add(os.path.basename(spec.path) + '.frmanifest.json')
            if set(os.listdir(os.path.dirname(spec.path))) - allowed:
                return False
            for index, (row, receipt) in enumerate(zip(
                    frozen['frames'], managed['stabilization_receipts'])):
                expected_map = {'display_index': spec.start + index,
                                'source_frame': row['source_frame'],
                                'source_id': frozen['source_id'],
                                'source_path': row['source_path'],
                                'offset': row['offset']}
                if (managed['frame_map'][index] != expected_map or
                        not isinstance(receipt, dict) or
                        receipt.get('frozen_sha256') != frozen['frozen_sha256'] or
                        receipt.get('source_frame') != row['source_frame'] or
                        receipt.get('source_sha256') != row['source_sha256'] or
                        receipt.get('tracking_sha256') != frozen['tracking_sha256'] or
                        receipt.get('offset') != row['offset'] or
                        receipt.get('width') != frozen['width'] or
                        receipt.get('height') != frozen['height'] or
                        receipt.get('channels') != 4 or
                        receipt.get('pixel_format') != 'float32_rgba' or
                        receipt.get('pixel_truth_verified') is not True):
                    return False
                name = os.path.basename(spec.path).replace(
                    '####', '{:04d}'.format(1001 + index))
                target = os.path.join(os.path.dirname(spec.path), name)
                if (_hash_file(target, None) != (
                        receipt.get('output_sha256'),
                        receipt.get('output_size_bytes')) or
                        _header(target, frozen['width'], frozen['height'],
                                'straight') != ['R', 'G', 'B', 'A']):
                    return False
                # A replaced output and JSON can carry *matching forged*
                # SHA256. Independently recompute actual working pixel truth
                # from the original signed EXR frame and tracked translation.
                original = _decode_float(
                    row['source_path'], frozen['width'], frozen['height'],
                    len(row['source_channels']), ffmpeg, None)
                truth = stabilized_linear_pixels(
                    original, row['offset'], flip_h=frozen['flip_h'],
                    flip_v=frozen['flip_v'], alpha_mode=frozen['alpha_mode'])
                actual = _decode_float(
                    target, frozen['width'], frozen['height'], 4, ffmpeg, None)
                if (not np.allclose(actual, truth, atol=2e-5, rtol=3e-6) or
                        not np.all(actual[truth[:, :, 3] == 0] == 0)):
                    return False
            return True
        except (ValueError, RuntimeError, KeyError, TypeError, OSError,
                OverflowError, IndexError, ImportError):
            return False
    movie = bool(ctx.get('managed_movie'))
    composite = ctx.get('composite_export') is not None
    if (not isinstance(managed, dict) or
            managed.get('stage') != spec.stage or
            managed.get('input_stage') != 'source' or
            managed.get('input_kind') != (
                'composite_image_sequence' if composite else
                'movie' if movie else 'image_sequence') or
            not isinstance(managed.get('frame_map'), list) or
            len(managed['frame_map']) != spec.count or
            not managed.get('output_sample_format') or
            not isinstance(managed.get('pipeline'), dict)):
        return False
    # A render cannot replace the frozen input/grade/OCIO configuration with a
    # different pipeline in its staged sidecar or crash-recovery receipt.
    frozen = ctx.get('color') or {}
    if any(json.dumps(managed['pipeline'].get(k), sort_keys=True) !=
           json.dumps(v, sort_keys=True) for k, v in frozen.items()):
        return False
    if composite:
        try:
            if (movie or spec.stage == 'source' or
                    json.dumps(managed.get('composite_export'), sort_keys=True) !=
                    json.dumps(ctx['composite_export'], sort_keys=True) or
                    managed.get('composite_geometry_plan') !=
                    managed_composite_geometry_plan(spec) or
                    json.dumps(managed['frame_map'], sort_keys=True) !=
                    json.dumps(managed_composite_plan(spec), sort_keys=True)):
                return False
            frozen_members = ctx['composite_export']['members']
            version = ctx['composite_export']['version']
            if version in (3, 4):
                probes = {(m['occurrence_id'] if version == 4 else m['source_id']):
                          m['movie_proof']
                          for m in frozen_members if m['input_kind'] == 'movie'}
                if managed.get('composite_movie_probes') != probes:
                    return False
            elif managed.get('composite_movie_probes') is not None:
                return False
            if version == 4 and ctx['composite_export'].get('audio_policy') == 'wav_pcm_v1':
                from .record_audio import verify_record_audio
                receipt = managed.get('record_audio_receipt')
                if not isinstance(receipt, dict) or \
                        receipt.get('selected_sources') is None:
                    return False
                # Reconstruct selected PCM against the frozen source bytes.
                # A forged JSON receipt/hash alone can never publish WAV.
                verify_record_audio(spec, receipt, complete=True)
            elif managed.get('record_audio_receipt') is not None:
                return False
            if version == 4 and ctx['composite_export'].get('record_av') is not None:
                from .record_av_mux import verify_record_av
                if not isinstance(managed.get('record_av_receipt'), dict):
                    return False
                verify_record_av(spec, managed['record_av_receipt'])
            elif managed.get('record_av_receipt') is not None:
                return False
            if version == 4 and ctx['composite_export'].get('record_rgba16') is not None:
                from .record_rgba16_mux import verify_record_rgba16
                if not isinstance(managed.get('record_rgba16_receipt'), dict):
                    return False
                verify_record_rgba16(spec, managed['record_rgba16_receipt'])
            elif managed.get('record_rgba16_receipt') is not None:
                return False
        except (ValueError, TypeError, OverflowError, KeyError):
            return False
        except (RuntimeError, OSError, EOFError):
            return False
        return True
    if movie:
        if managed.get('managed_geometry_export') or managed.get('geometry_plan'):
            return False
        audio_policy = ctx.get('movie_audio_policy')
        if (managed.get('movie_color_provenance') != ctx.get('movie_color_provenance') or
                audio_policy not in ('drop', 'preserve') or
                managed.get('audio_policy') != audio_policy or
                not isinstance(managed.get('movie_decode'), dict)):
            return False
        if audio_policy == 'preserve':
            # A recovered queue may only publish a staged movie whose chosen
            # soundtrack matches the user's frozen explicit consent. A generic
            # successful MP4 probe is insufficient: a wrong stream or late
            # offset would otherwise be accepted as color/audio-managed.
            try:
                from fractions import Fraction
                frozen_audio = validate_managed_movie_audio(spec)
                receipt = managed.get('audio_mux')
                if (managed.get('audio_export') != frozen_audio or
                        not isinstance(receipt, dict) or
                        type(receipt.get('output_audio_frames')) is not int or
                        receipt['output_audio_frames'] < 1):
                    return False
                fps = Fraction(spec.fps)
                sr = frozen_audio['source_sample_rate']
                start_samples = round(Fraction(spec.start, 1) / fps * sr)
                end_samples = round(Fraction(spec.end + 1, 1) / fps * sr)
                expected_duration = Fraction(spec.count, 1) / fps
                expected = {
                    'source_stream_index': frozen_audio['stream_index'],
                    'source_time_base': frozen_audio['source_time_base'],
                    'source_sample_rate': sr,
                    'trim_start_samples': start_samples,
                    'trim_end_samples': end_samples,
                    'trim_start_time': str(Fraction(spec.start, 1) / fps),
                    'trim_duration': str(expected_duration),
                    'output_codec': 'aac', 'output_sample_rate': 48000,
                    'output_channels': 2,
                    'expected_duration': str(expected_duration),
                }
                if any(receipt.get(key) != value for key, value in expected.items()):
                    return False
                if (abs(Fraction(str(receipt.get('source_start_time')))) >
                        Fraction(1, 1000) or
                        abs(Fraction(str(receipt.get('source_video_start_time')))) >
                        Fraction(1, 1000) or
                        abs(Fraction(str(receipt.get('output_start_time')))) >
                        Fraction(25, 1000) or
                        abs(Fraction(str(receipt.get('output_video_start_time')))) >
                        Fraction(1, 100) or
                        abs(Fraction(str(receipt.get('output_duration'))) -
                            expected_duration) > Fraction(5, 100)):
                    return False
            except (ValueError, TypeError, KeyError, ZeroDivisionError,
                    OverflowError, RuntimeError):
                return False
        elif (managed.get('audio_export') or managed.get('audio_mux')):
            return False
        decoded = managed['movie_decode']
        confirmed = ctx['movie_color_provenance']
        try:
            if (int(decoded.get('frame_count', -1)) <= spec.end or
                    decoded.get('source_fps') != spec.source_fps or
                    not decoded.get('source_pix_fmt') or
                    not decoded.get('pts_origin') or
                    decoded.get('yuv_matrix') != confirmed.get('yuv_matrix') or
                    decoded.get('yuv_range') != confirmed.get('yuv_range')):
                return False
            expected = managed_movie_plan(spec)
        except (ValueError, TypeError, OverflowError, KeyError):
            return False
        # Strict, zero-based decode lineage prevents a stale or fabricated
        # movie receipt from being promoted/recovered as color-managed media.
        return all(isinstance(row, dict) and all(
            row.get(key) == value for key, value in canonical.items())
            for row, canonical in zip(managed['frame_map'], expected))
    timeline = ctx.get('frame_numbers')
    frozen_geometry = ctx.get('managed_geometry_export')
    if frozen_geometry is not None:
        try:
            expected_geometry = managed_geometry_plan(
                spec, frozen_geometry['input_size'])
        except (KeyError, ValueError, TypeError, OverflowError):
            return False
        if (managed.get('managed_geometry_export') != frozen_geometry or
                managed.get('geometry_plan') != expected_geometry):
            return False
    elif (managed.get('managed_geometry_export') is not None or
          managed.get('geometry_plan') is not None):
        return False
    existing = ctx.get('existing_frame_numbers')
    real = set(existing) if isinstance(existing, (list, tuple)) else None
    previous = None
    if isinstance(timeline, (list, tuple)) and real is not None:
        for source in timeline[:spec.start]:
            if source in real:
                previous = source
    for offset, row in enumerate(managed['frame_map']):
        index = spec.start + offset
        if not isinstance(row, dict) or row.get('display_index') != index:
            return False
        if isinstance(timeline, (list, tuple)):
            if index >= len(timeline) or row.get('source_frame') != timeline[index]:
                return False
            if real is not None:
                source_frame = timeline[index]
                missing = source_frame not in real
                if row.get('hole') is not missing:
                    return False
                if missing:
                    expected = (previous if ctx.get('missing_frame_policy') == 'hold'
                                else None)
                    if row.get('held_from') != expected:
                        return False
                else:
                    if row.get('held_from') is not None:
                        return False
                    previous = source_frame
    return True


def _staged_spec(spec, stage):
    ctx = spec.to_context()
    ctx['path'] = os.path.join(stage, os.path.basename(spec.path))
    return ExportSpec.from_context(ctx, source_path=spec.source_path,
                                   source_id=spec.source_id)


class ExportQueueRunner(object):
    """Drive one existing ExportJob at a time via its usual Qt signals.

    ``create_job(task, staged_context)`` must return an **unstarted** ExportJob
    exposing ``progress``, ``result``, ``error``, ``finished`` and ``start``.
    ``on_change(task)`` and ``on_idle()`` are optional UI hooks. The queue never
    creates its own playhead, QThread, QApplication, nor secondary worker pool.

    Restart policy is deliberately conservative: interrupted jobs become
    CRASHED. Call ``retry(id)`` explicitly after reviewing their failure. A
    crash during output commit is reconciled using the stage receipt so rerun
    does not produce a duplicate output.
    """

    def __init__(self, store, create_job, on_change=None, on_idle=None,
                 ffprobe=None, verifier=None):
        if not isinstance(store, ExportQueueStore):
            raise TypeError('Expected an ExportQueueStore instance.')
        if not callable(create_job):
            raise TypeError('create_job must be callable.')
        self.store = store
        self.create_job = create_job
        self.on_change = on_change
        self.on_idle = on_idle
        self.ffprobe = ffprobe
        self.verifier = verifier or verify_output
        self.active_id = None
        self.active_job = None
        self._got_result = False
        self._error = None
        self._stage = None
        self._shutdown_requested = False
        self._lease_path = store.path + '.runlock'
        self._lease_nonce = None
        self._reconcile_commits()

    def _acquire_lease(self):
        if self._lease_nonce:
            return
        nonce = uuid.uuid4().hex
        for _attempt in range(2):
            try:
                fd = os.open(self._lease_path,
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if _owner_alive(self._lease_path):
                    raise QueueBusyError('Another process owns the export queue.')
                # Dead-process lease; OS lockfile is the only disposable item.
                try:
                    os.unlink(self._lease_path)
                except FileNotFoundError:
                    pass
                continue
            else:
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                        json.dump({'pid': os.getpid(), 'nonce': nonce}, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                except Exception:
                    os.unlink(self._lease_path)
                    raise
                self._lease_nonce = nonce
                self.store._lease_nonce = nonce
                return
        raise QueueBusyError('Cannot acquire the export queue lease.')

    def _release_lease(self):
        nonce = self._lease_nonce
        self._lease_nonce = None
        self.store._lease_nonce = None
        if nonce is None:
            return
        try:
            with open(self._lease_path, encoding='utf-8') as stream:
                owner = json.load(stream)
            if owner.get('nonce') == nonce:
                os.unlink(self._lease_path)
        except (FileNotFoundError, OSError, ValueError):
            pass

    def _notify(self, task_id):
        if self.on_change is not None:
            self.on_change(self.store.get(task_id))

    def enqueue(self, spec, seq_files=(), width=None, height=None,
                overwrite=False, start=False):
        task = self.store.enqueue(spec, seq_files=seq_files, width=width,
                                  height=height, overwrite=overwrite)
        self._notify(task.id)
        if start:
            self.start_next()
        return self.store.get(task.id)

    def start_next(self):
        """Start oldest pending entry if the previous job has fully finished."""
        if self.active_id is not None:
            return None
        if self._shutdown_requested:
            self._release_lease()
            return None
        task = self.store.next_pending()
        if task is None:
            self._release_lease()
            if self.on_idle is not None:
                self.on_idle()
            return None
        self._acquire_lease()
        try:
            self._prepare(task)
        except Exception as exc:
            # Failed preflight must not block the remaining queue.
            if self.active_id is None:
                self.store.fail_pending(task.id, str(exc))
                self._notify(task.id)
                return self.start_next()
            self._finish_with_error(str(exc))
            return self.active_id
        return task.id

    def _prepare(self, task):
        spec = task.spec
        parent = _staging_parent(spec)
        _target_safe(spec)
        os.makedirs(parent, exist_ok=True)
        target = os.path.dirname(spec.path) if spec.sequence else spec.path
        if os.path.islink(target):
            raise ValueError('Refusing to export over a symlink.')
        if os.path.lexists(target) and not task.overwrite:
            raise FileExistsError('Export output appeared after enqueue: {}'.format(target))
        # Dispose of *only* a previous private temp path on explicit retry.
        recovery = task.recovery or {}
        old_stage = recovery.get('stage')
        if _safe_stage(old_stage, spec, task.id) and os.path.isdir(old_stage):
            shutil.rmtree(old_stage)
        stage = tempfile.mkdtemp(prefix='.frqueue-{}-'.format(task.id[:12]),
                                  dir=parent)
        recovery = {'phase': 'rendering', 'stage': stage,
                    'output': spec.path, 'backup': None,
                    'signature': None}
        self.store.begin(task.id, recovery)
        self.active_id = task.id
        self._stage = stage
        self._got_result = False
        self._error = None
        self._notify(task.id)
        staged = _staged_spec(spec, stage)
        # A staged context differs solely in output path; the original frozen
        # spec stored in the journal never changes during this attempt.
        job = self.create_job(self.store.get(task.id), staged.to_context())
        if job is None:
            raise ValueError('Export job factory returned no job.')
        self.active_job = job
        job.progress.connect(lambda value, tid=task.id: self._progress(tid, value))
        job.result.connect(lambda tid=task.id: self._result(tid))
        job.error.connect(lambda message, tid=task.id: self._failed(tid, message))
        job.finished.connect(lambda tid=task.id: self._finished(tid))
        if hasattr(job, 'deleteLater'):
            job.finished.connect(job.deleteLater)
        job.start()

    def _progress(self, task_id, progress):
        if self.active_id != task_id:
            return
        self.store.progress(task_id, progress)
        self._notify(task_id)

    def _result(self, task_id):
        if self.active_id == task_id:
            self._got_result = True

    def _failed(self, task_id, message):
        if self.active_id == task_id:
            self._error = str(message)

    def _finish_with_error(self, message):
        task_id = self.active_id
        if task_id is None:
            return
        try:
            state = self.store.get(task_id).state
            if state == CANCELLING:
                self.store.cancelled(task_id)
            elif state in (RUNNING, COMMITTING):
                self.store.fail(task_id, message)
            self._notify(task_id)
        finally:
            self._release()
        self.start_next()

    def _finished(self, task_id):
        if self.active_id != task_id:
            return  # stale QThread notification from an earlier export
        task = self.store.get(task_id)
        if task.state == CANCELLING:
            self._finish_with_error('Export cancelled.')
            return
        if self._error or not self._got_result:
            self._finish_with_error(self._error or
                                    'Export worker exited without a result.')
            return
        try:
            stage = self._stage
            staged = _staged_spec(task.spec, stage)
            verification = self.verifier(staged, ffprobe=self.ffprobe)
            if staged.stage != 'source' or staged.to_context().get('managed_movie'):
                # The QThread writes this sidecar only *after* running the
                # precision stage. Do not label an existing source-stage
                # FFmpeg render as color managed, even after a queue restart.
                sidecar = staged.path + '.frmanifest.json'
                with open(sidecar, 'r', encoding='utf-8') as stream:
                    manifest = json.load(stream)
                rendered = (manifest.get('verification') or {}).get('managed_color')
                if (not _valid_managed_color(rendered, staged) or
                        manifest.get('output', {}).get('stage') != staged.stage or
                        manifest.get('source', {}).get('path') != staged.source_path):
                    raise RuntimeError('Managed render is missing matching precision-color evidence.')
                verification['managed_color'] = rendered
            recovery = task.recovery
            recovery['phase'] = 'committing'
            recovery['signature'] = _signature(staged)
            if staged.stage != 'source' or staged.to_context().get('managed_movie'):
                recovery['managed_color'] = verification['managed_color']
            if recovery['signature'] is None:
                raise RuntimeError('Staged output is missing.')
            target = os.path.dirname(task.spec.path) if task.spec.sequence else task.spec.path
            if task.overwrite and os.path.lexists(target):
                recovery['backup'] = _backup_path(task.spec, task)
            self.store.committing(task_id, recovery)
            self._promote(self.store.get(task_id))
            self.store.succeed(task_id, verification)
            self._notify(task_id)
        except Exception as exc:
            self._finish_with_error(str(exc))
            return
        self._cleanup_backup(self.store.get(task_id))
        self._release()
        self.start_next()

    def _cleanup_backup(self, task):
        backup = (task.recovery or {}).get('backup')
        if not _safe_backup(backup, task.spec, task.id, task.attempt):
            return
        try:
            if os.path.isdir(backup):
                shutil.rmtree(backup)
            elif os.path.isfile(backup):
                os.unlink(backup)
        except OSError:
            pass

    def _promote(self, task):
        spec = task.spec
        recovery = task.recovery
        stage = recovery['stage']
        if not _safe_stage(stage, spec, task.id):
            raise ValueError('Unsafe staging path in queue recovery journal.')
        _target_safe(spec)
        stage_spec = _staged_spec(spec, stage)
        if _signature(stage_spec) != recovery['signature']:
            raise RuntimeError('Staged output changed during export commit.')
        target = os.path.dirname(spec.path) if spec.sequence else spec.path
        if os.path.lexists(target) and not task.overwrite:
            raise FileExistsError('Output path appeared while exporting: {}'.format(target))
        backup = recovery.get('backup')
        if backup is not None and not _safe_backup(backup, spec, task.id, task.attempt):
            raise ValueError('Unsafe rollback location in export journal.')
        moved_old = False
        promoted = False
        try:
            if os.path.lexists(target):
                if not task.overwrite or not backup:
                    raise FileExistsError('No safe overwrite backup was reserved.')
                if os.path.lexists(backup):
                    raise FileExistsError('Rollback location already exists.')
                os.replace(target, backup)
                moved_old = True
            if spec.sequence:
                os.replace(stage, target)
            else:
                os.replace(stage_spec.path, target)
            promoted = True
            # Verify after promotion while preserving the original output for
            # rollback. Journal success is persisted before backup cleanup.
            probe = self.verifier(spec, ffprobe=self.ffprobe)
            if spec.stage != 'source' or spec.to_context().get('managed_movie'):
                receipt = recovery.get('managed_color')
                if not _valid_managed_color(receipt, spec):
                    raise RuntimeError('Missing managed color provenance at commit.')
                probe['managed_color'] = receipt
            spec.write_manifest(probe)
        except Exception:
            # Return newly promoted media to private staging, then restore the
            # original output. Never delete an existing public output folder.
            if promoted and os.path.lexists(target):
                private_output = stage if spec.sequence else stage_spec.path
                if not os.path.lexists(private_output):
                    os.replace(target, private_output)
            if moved_old and not os.path.lexists(target) and os.path.lexists(backup):
                os.replace(backup, target)
            raise

    def _release(self):
        task_id = self.active_id
        stage = self._stage
        self.active_job = None
        self.active_id = None
        self._stage = None
        self._got_result = False
        self._error = None
        if task_id:
            task = self.store.get(task_id)
            if _safe_stage(stage, task.spec, task_id) and os.path.isdir(stage):
                shutil.rmtree(stage)

    def cancel(self, task_id):
        """Pending cancellation is immediate; active cancellation waits for finished."""
        before = self.store.get(task_id)
        if before.state in (SUCCEEDED, FAILED, CRASHED, CANCELLED):
            return before
        result = self.store.cancel(task_id)
        self._notify(task_id)
        if task_id == self.active_id and self.active_job is not None:
            job = self.active_job
            if hasattr(job, 'requestInterruption'):
                job.requestInterruption()
            if hasattr(job, 'stop_now'):
                job.stop_now()
        return result

    def retry(self, task_id, start=False):
        # A codec/manifest error may have happened *after* media promotion.
        # Reconcile first so retry cannot launch a second export onto a valid
        # finished output after a late commit failure.
        self._reconcile_commits()
        if self.store.get(task_id).state == SUCCEEDED:
            return self.store.get(task_id)
        task = self.store.retry(task_id)
        self._notify(task_id)
        if start:
            self.start_next()
        return task

    def shutdown(self, cancel_active=False, wait=False, timeout_ms=3000):
        """Stop dispatching new work; optionally request/await active shutdown.

        With cancel_active=True, request interruption but retain references
        until the job emits finished. ``wait=True`` is for explicit shutdown
        paths, never normal UI use: it waits at most ``timeout_ms`` and drains
        queued Qt signals once. Returns False when a thread is still alive.
        Do not destroy a live Qt worker from Maya.
        """
        self._shutdown_requested = True
        if cancel_active and self.active_id is not None:
            self.cancel(self.active_id)
        job = self.active_job
        if wait and job is not None and hasattr(job, 'wait'):
            job.wait(max(0, int(timeout_ms)))
            try:
                from ..core.qtcompat import QtCore
                app = QtCore.QCoreApplication.instance()
                if app is not None:
                    app.processEvents()
            except ImportError:
                pass
        if self.active_id is None:
            self._release_lease()
        return self.active_id is None

    def _reconcile_commits(self):
        """Recover successful promotion or restore an interrupted overwrite.

        Only a task *persisted as committing before the crash* can be inferred
        successful by comparing its staged stat receipt and re-verifying media.
        Failed/crashed rendering is never re-run without an explicit retry.
        """
        for task in self.store.tasks():
            recovery = task.recovery or {}
            if task.state not in (CRASHED, FAILED) or recovery.get('phase') != 'committing':
                continue
            spec = task.spec
            target = os.path.dirname(spec.path) if spec.sequence else spec.path
            receipt = recovery.get('signature')
            stage = recovery.get('stage')
            if not _safe_stage(stage, spec, task.id):
                continue  # Untrusted journal paths can never move or delete files.
            backup = recovery.get('backup')
            if backup and not _safe_backup(backup, spec, task.id, task.attempt):
                continue
            try:
                _target_safe(spec)
                matching = receipt is not None and _signature(spec) == receipt
            except (OSError, ValueError):
                continue
            if matching:
                try:
                    check = self.verifier(spec, ffprobe=self.ffprobe)
                    if spec.stage != 'source' or spec.to_context().get('managed_movie'):
                        managed = recovery.get('managed_color')
                        if not _valid_managed_color(managed, spec):
                            raise RuntimeError('Cannot recover managed export without color provenance.')
                        check['managed_color'] = managed
                    spec.write_manifest(check)
                    self.store.recover_succeeded(task.id, check)
                    self._cleanup_backup(self.store.get(task.id))
                    self._notify(task.id)
                except Exception:
                    pass  # fall through: invalid final should restore backup
                else:
                    continue
            if backup and os.path.lexists(backup):
                try:
                    if os.path.lexists(target):
                        # A failed post-rename verification must not keep a
                        # corrupt replacement in front of the original media.
                        # Quarantine only in the task's own private stage.
                        private_output = (stage if spec.sequence else
                                          _staged_spec(spec, stage).path)
                        if os.path.lexists(private_output):
                            continue  # ambiguous state: leave both untouched
                        os.replace(target, private_output)
                    os.replace(backup, target)
                except OSError:
                    pass  # Leave original backup intact for manual recovery.


def existing_export_job_factory(ffmpeg, parent=None):
    """Adapter for the shipping controller.ExportJob, loaded lazily for Maya.

    The caller owns the QApplication and the UI progress presentation. Queue
    jobs remain ordinary ExportJob QThreads with familiar progress/result/error
    signals; only their context output path is pointed at private staging.
    """
    def make_job(task, staged_context):
        from .controller import ExportJob
        inputs = task.inputs
        return ExportJob(ffmpeg, task.spec.source_path, inputs['seq_files'],
                         inputs['width'], inputs['height'], staged_context,
                         parent=parent)
    return make_job


def _safe_backup(backup, spec, task_id, attempt=None):
    if not backup or not isinstance(backup, str):
        return False
    expected = '.frqueue-backup-{}-{}'.format(task_id[:12], attempt)
    if not spec.sequence:
        expected += '.media'
    return (os.path.normpath(backup) == backup and
            (os.path.basename(backup) == expected if attempt is not None
             else os.path.basename(backup).startswith('.frqueue-backup-{}-'.format(task_id[:12]))) and
            os.path.dirname(backup) == os.path.abspath(_staging_parent(spec)) and
            os.path.dirname(os.path.realpath(backup)) ==
            os.path.realpath(_staging_parent(spec)) and
            not os.path.islink(backup))


__all__ = ['ExportQueueRunner', 'QueueBusyError',
           'existing_export_job_factory']
