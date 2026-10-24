# -*- coding: utf-8 -*-
"""Maya Camera Sequencer to stable, portable FrameReviewer sources.

Maya commands are dependency-injected to keep this module testable without
Maya. Playblast stays on Maya's main/UI thread. Cached results are versioned by
scene/shot signature, so stale output cannot replace a reviewed version.
"""
from __future__ import absolute_import

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from decimal import Decimal

from ..app.session import Source, Representation, ROLE_PLAYBLAST, KIND_SEQUENCE
from ..app.session_controller import atomic_json_write


# Hard ceilings prevent a corrupt manifest or injected frame directory from
# monopolizing Maya's UI thread with unbounded JSON parsing or file hashing.
MAX_PLAYBLAST_FRAMES = 10000
MAX_PLAYBLAST_FRAME_BYTES = 128 * 1024 * 1024
MAX_PLAYBLAST_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_PLAYBLAST_MANIFEST_BYTES = 3 * 1024 * 1024
_FRAME_NAME = re.compile(r'^shot\.(-?\d{1,12}(?:\.\d{1,6})?)\.jpe?g$', re.I)
_SHA256 = re.compile(r'^[0-9a-f]{64}$')


def _safe(text):
    return re.sub(r'[^a-zA-Z0-9_-]+', '_', str(text or 'unnamed')).strip('_')[:70]


def _hash(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:16]


class MayaShot(object):
    def __init__(self, node, uuid, camera, start, end, scene_path='',
                 scene_mtime=0, sequence_start=None, sequence_end=None,
                 scene_dirty=False):
        self.node = str(node)
        self.uuid = str(uuid)
        self.camera = str(camera or '')
        self.start = float(start)
        self.end = float(end)
        self.sequence_start = float(sequence_start if sequence_start is not None
                                    else start)
        self.sequence_end = float(sequence_end if sequence_end is not None
                                  else end)
        self.scene_path = str(scene_path or '')
        self.scene_mtime = float(scene_mtime or 0)
        # Snapshot metadata, never a hash of Maya node content. An unsaved
        # scene does not have a durable revision for cache reuse.
        self.scene_dirty = bool(scene_dirty or not self.scene_path)
        if self.end < self.start:
            raise ValueError('Maya shot ends before it begins.')

    @property
    def source_id(self):
        return 'maya-shot-' + self.uuid

    @property
    def signature(self):
        return _hash(json.dumps(self.to_dict(), sort_keys=True))

    def to_dict(self):
        data = dict(node=self.node, uuid=self.uuid, camera=self.camera,
                    start=self.start, end=self.end,
                    sequence_start=self.sequence_start,
                    sequence_end=self.sequence_end,
                    scene_path=self.scene_path, scene_mtime=self.scene_mtime)
        # Keep the historical signature stable for genuinely clean scenes.
        if self.scene_dirty:
            data['scene_dirty'] = True
        return data


def _scene_modified(cmds, scene_path):
    """Get Maya's unsaved-change bit; None means freshness is not provable.

    Maya normally returns a boolean. A legacy minimal fake command that
    ignores flags returns sceneName for *every* file query; tolerate that
    specific stub without permitting arbitrary unknown responses to reuse.
    """
    try:
        value = cmds.file(query=True, modified=True)
    except (RuntimeError, TypeError, ValueError, AttributeError):
        return None
    if isinstance(value, bool):
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if scene_path and value == scene_path:
        return False  # backwards-compatible file() stub, not a real Maya reply
    return None


def _live_freshness(cmds, shot):
    """Recheck after inventory: Maya edits may occur before render is called."""
    try:
        current_scene = str(cmds.file(query=True, sceneName=True) or '')
    except (RuntimeError, TypeError, ValueError, AttributeError):
        current_scene = ''
    if current_scene != shot.scene_path:
        raise RuntimeError('Maya scene changed; refresh Sequencer shots before playblast.')
    # The shot object may be from an earlier inventory even when Maya now
    # reports a clean scene. A new on-disk save changes its authoritative
    # signature; ask for a fresh shot inventory instead of reusing an old one.
    if current_scene and os.path.isfile(current_scene):
        current_mtime = os.path.getmtime(current_scene)
        if current_mtime != shot.scene_mtime:
            raise RuntimeError('Maya scene was saved since inventory; '
                               'refresh Sequencer shots before playblast.')
    modified = _scene_modified(cmds, current_scene)
    return bool(shot.scene_dirty or not current_scene or modified is not False)


def _query(cmds, node, name, fallback=None):
    try:
        return cmds.shot(node, query=True, **{name: True})
    except (RuntimeError, ValueError, TypeError, AttributeError):
        return fallback


def inventory(cmds):
    """Return sorted sequencer shots with stable Maya UUID identities."""
    try:
        scene = str(cmds.file(query=True, sceneName=True) or '')
    except (RuntimeError, AttributeError, TypeError):
        scene = ''
    modified = os.path.getmtime(scene) if scene and os.path.isfile(scene) else 0
    dirty = _scene_modified(cmds, scene)
    dirty_scene = bool(not scene or dirty is not False)
    shots = []
    for node in cmds.ls(type='shot') or []:
        try:
            ids = cmds.ls(node, uuid=True) or []
            sid = str(ids[0]) if ids else _hash(scene + '/' + node)
            start, end = _query(cmds, node, 'startTime'), _query(cmds, node, 'endTime')
            if start is None or end is None:
                continue
            camera = _query(cmds, node, 'currentCamera', '')
            shots.append(MayaShot(
                node, sid, camera, start, end, scene_path=scene,
                scene_mtime=modified, scene_dirty=dirty_scene,
                sequence_start=_query(cmds, node, 'sequenceStartTime', start),
                sequence_end=_query(cmds, node, 'sequenceEndTime', end)))
        except (RuntimeError, ValueError, TypeError):
            continue
    return sorted(shots, key=lambda s: (s.sequence_start, s.sequence_end, s.node))


def add_to_session(controller, shots):
    """Create/update logical sources while retaining annotation IDs and versions."""
    output = []
    for shot in shots:
        source = controller.session.source(shot.source_id)
        if source is None:
            source = Source(name=shot.node, id=shot.source_id)
            controller.session.add_source(source)
            controller.dirty = True
        source.name = shot.node
        data = shot.to_dict()
        if source.metadata.get('maya_shot') != data:
            source.metadata['maya_shot'] = data
            controller.dirty = True
        source.timing.update({'start_frame': int(round(shot.start)),
                              'frame_count': max(1, int(round(shot.end-shot.start))+1)})
        output.append(source)
    if shots and controller.current is None:
        controller.set_current(shots[0].source_id)
    return output


def _safe_folder_fd(folder, root):
    """Open a cache folder without following symlinks within the cache root."""
    absolute_root = os.path.abspath(root)
    folder = os.path.abspath(folder)
    relative = os.path.relpath(folder, absolute_root)
    if relative == '..' or relative.startswith('..' + os.sep):
        raise ValueError('Cached playblast escaped its root.')
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(absolute_root, flags)
    try:
        for part in relative.split(os.sep):
            if part in ('', '.'):
                continue
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise ValueError('Cache revision is not a real directory.')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _scan_frame_names(folder_fd, manifest_required=False, shot=None):
    """Reject any unexpected frame filename, injected file or symlink."""
    names = []
    frame_numbers = set()
    manifest_seen = False
    with os.scandir(folder_fd) as entries:
        for entry in entries:
            name = entry.name
            if name == 'manifest.json':
                if not entry.is_file(follow_symlinks=False):
                    raise ValueError('Cached manifest is not a regular file.')
                manifest_seen = True
                continue
            match = _FRAME_NAME.fullmatch(name)
            if match is None or not entry.is_file(follow_symlinks=False):
                raise ValueError('Unexpected or symlinked playblast entry.')
            number = Decimal(match.group(1))
            if (shot is not None and
                    not Decimal(str(shot.start)) <= number <= Decimal(str(shot.end))):
                raise ValueError('Unexpected frame outside the Maya shot range.')
            if number in frame_numbers:
                raise ValueError('Duplicate source frame names in playblast.')
            frame_numbers.add(number)
            names.append(name)
            if len(names) > MAX_PLAYBLAST_FRAMES:
                raise ValueError('Playblast exceeds maximum frame count.')
    if not names or (manifest_required and not manifest_seen):
        raise ValueError('Playblast has no valid frames or manifest.')
    return sorted(names)


def _read_manifest(folder_fd):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open('manifest.json', flags, dir_fd=folder_fd)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or
                st.st_size <= 0 or st.st_size > MAX_PLAYBLAST_MANIFEST_BYTES):
            raise ValueError('Cached manifest has an unsafe size or type.')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_PLAYBLAST_MANIFEST_BYTES + 1)
        if len(raw) != st.st_size:
            raise ValueError('Cached manifest changed during read.')
        # Reject ambiguous duplicate JSON keys instead of allowing the last
        # field to silently override the expected signature or frame list.
        def unique_object(pairs):
            result = {}
            for name, value in pairs:
                if name in result:
                    raise ValueError('Duplicate cached manifest JSON key.')
                result[name] = value
            return result
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
    finally:
        os.close(fd)


def _frame_integrity(folder_fd, name, expected=None):
    """Read one stable regular frame exactly once; never follow symlinks."""
    fd = os.open(name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=folder_fd)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_size <= 0 or
                st.st_size > MAX_PLAYBLAST_FRAME_BYTES):
            raise ValueError('Invalid playblast frame size or file type.')
        if expected is not None and st.st_size != expected['size']:
            raise ValueError('Cached playblast frame changed size.')
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, st.st_size + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > st.st_size:
                raise ValueError('Playblast frame grew while hashing.')
            digest.update(chunk)
        end = os.fstat(fd)
        current = os.stat(name, dir_fd=folder_fd, follow_symlinks=False)
        if (total != st.st_size or
                (end.st_size, end.st_mtime_ns, end.st_ctime_ns) !=
                (st.st_size, st.st_mtime_ns, st.st_ctime_ns) or
                (current.st_dev, current.st_ino, current.st_size,
                 current.st_mtime_ns, current.st_ctime_ns) !=
                (st.st_dev, st.st_ino, st.st_size,
                 st.st_mtime_ns, st.st_ctime_ns)):
            raise ValueError('Playblast frame changed during verification.')
        result = {'name': name, 'size': total, 'sha256': digest.hexdigest()}
        if expected is not None and result != expected:
            raise ValueError('Cached playblast frame digest does not match.')
        return result
    finally:
        os.close(fd)


def _all_frame_integrity(folder_fd, names, expected=None):
    records = []
    total = 0
    for index, name in enumerate(names):
        frame = _frame_integrity(
            folder_fd, name, expected[index] if expected is not None else None)
        total += frame['size']
        if total > MAX_PLAYBLAST_TOTAL_BYTES:
            raise ValueError('Playblast exceeds bounded total frame size.')
        records.append(frame)
    return records, total


def _verified_cached_folder(folder, shot, root):
    """Return the first frame only after verifying the *entire* immutable set."""
    folder_fd = _safe_folder_fd(folder, root)
    try:
        payload = _read_manifest(folder_fd)
        if type(payload) is not dict or payload.get('signature') != shot.signature:
            raise ValueError('Cached playblast signature mismatch.')
        if payload.get('shot') != shot.to_dict():
            raise ValueError('Cached playblast shot metadata mismatch.')
        names = _scan_frame_names(folder_fd, manifest_required=True, shot=shot)
        # Legacy filename-only manifests cannot prove content identity.
        # Prefer a fresh private render while retaining their old revisions.
        if type(payload.get('integrity_version')) is not int or payload['integrity_version'] != 1:
            raise ValueError('Unverifiable legacy playblast manifest.')
        files = payload.get('files')
        frames = payload.get('frames')
        if (type(files) is not list or files != names or
                type(frames) is not list or len(frames) != len(names) or
                type(payload.get('frame_count')) is not int or
                payload['frame_count'] != len(names) or
                type(payload.get('total_bytes')) is not int or
                not 0 < payload['total_bytes'] <= MAX_PLAYBLAST_TOTAL_BYTES):
            raise ValueError('Cached playblast manifest frame set is invalid.')
        for name, item in zip(names, frames):
            if (type(item) is not dict or set(item) != {'name', 'size', 'sha256'} or
                    item['name'] != name or type(item['size']) is not int or
                    not 0 < item['size'] <= MAX_PLAYBLAST_FRAME_BYTES or
                    type(item['sha256']) is not str or
                    _SHA256.fullmatch(item['sha256']) is None):
                raise ValueError('Cached playblast manifest frame identity is invalid.')
        if sum(f['size'] for f in frames) != payload['total_bytes']:
            raise ValueError('Cached manifest has an incorrect total size.')
        actual, actual_total = _all_frame_integrity(folder_fd, names, expected=frames)
        if actual_total != payload['total_bytes'] or actual != frames:
            raise ValueError('Cached playblast frames failed integrity verification.')
        return os.path.join(folder, names[0])
    finally:
        os.close(folder_fd)


def cached_playblast(shot, root=None):
    """Return an entire integrity-verified scene+shot revision, or None."""
    if shot.scene_dirty or not shot.scene_path:
        return None
    folder = cache_folder(shot, root)
    cache_root = root or os.path.join(os.path.expanduser('~'), '.framereviewer',
                                      'playblasts')
    try:
        return _verified_cached_folder(folder, shot, cache_root)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return None


def cache_folder(shot, root=None):
    root = root or os.path.join(os.path.expanduser('~'), '.framereviewer',
                                'playblasts')
    scene_key = _hash(os.path.normpath(shot.scene_path or '<untitled>'))
    return os.path.join(root, scene_key, _safe(shot.uuid), shot.signature)


def render(shot, cmds, root=None, percent=100, quality=90,
           show_ornaments=False, off_screen=True, reuse=True):
    """Make a complete playblast revision; leave old review output untouched."""
    # Read Maya's live modified bit *before* cache selection: the shot was
    # inventoried earlier and may still have identical camera/range/mtime.
    fresh_revision = _live_freshness(cmds, shot)
    if reuse and not fresh_revision:
        cached = cached_playblast(shot, root)
        if cached:
            return cached
    base = cache_folder(shot, root)
    # A same-signature directory may be old, partial or tampered. Even when
    # clean, keep it intact and publish this replacement to a private revision.
    corrupt_or_legacy = os.path.lexists(base)
    private_revision = bool(fresh_revision or not reuse or corrupt_or_legacy)
    # Never reuse or overwrite a prior signed/complete revision just because
    # a dirty scene's node properties and on-disk mtime are unchanged. Force
    # render is also its own revision even for a saved scene.
    destination = (base + '-fresh-' + uuid.uuid4().hex
                   if private_revision else base)
    if not os.path.isdir(os.path.dirname(destination)):
        os.makedirs(os.path.dirname(destination))
    cache_root = root or os.path.join(os.path.expanduser('~'), '.framereviewer',
                                      'playblasts')
    parent_fd = _safe_folder_fd(os.path.dirname(destination), cache_root)
    os.close(parent_fd)
    stage = tempfile.mkdtemp(prefix='.shot_stage_', dir=os.path.dirname(destination))
    previous_time = None
    active_panel = None
    previous_camera = None
    try:
        previous_time = cmds.currentTime(query=True)
        cmds.currentTime(shot.start)
        try:
            active_panel = cmds.getPanel(withFocus=True)
            if cmds.getPanel(typeOf=active_panel) == 'modelPanel' and shot.camera:
                previous_camera = cmds.modelEditor(active_panel, query=True, camera=True)
                cmds.modelEditor(active_panel, edit=True, camera=shot.camera)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            active_panel, previous_camera = None, None
        cmds.playblast(format='image', compression='jpg',
                       filename=os.path.join(stage, 'shot'),
                       forceOverwrite=True, viewer=False,
                       showOrnaments=bool(show_ornaments),
                       offScreen=bool(off_screen),
                       percent=max(10, min(100, int(percent))),
                       quality=max(1, min(100, int(quality))),
                       clearCache=True, sequenceTime=False,
                       startTime=shot.start, endTime=shot.end)
        stage_fd = _safe_folder_fd(stage, os.path.dirname(stage))
        try:
            files = _scan_frame_names(stage_fd, shot=shot)
            records, total_bytes = _all_frame_integrity(stage_fd, files)
        finally:
            os.close(stage_fd)
        # A scriptJob may have changed Maya during playblast. A clean output
        # may no longer be safely labeled as matching the saved scene.
        if not fresh_revision and _live_freshness(cmds, shot):
            fresh_revision = True
            private_revision = True
            destination = base + '-fresh-' + uuid.uuid4().hex
        manifest_data = {'signature': shot.signature, 'shot': shot.to_dict(),
                         'files': files, 'fresh_revision': private_revision,
                         'reused_saved_scene': False,
                         'integrity_version': 1, 'frames': records,
                         'frame_count': len(files), 'total_bytes': total_bytes}
        atomic_json_write(os.path.join(stage, 'manifest.json'), manifest_data)
        if _verified_cached_folder(stage, shot, os.path.dirname(stage)) is None:
            raise RuntimeError('Staged playblast integrity verification failed.')
        # Acquire the name with exclusive mkdir before a directory rename.
        # Without this reservation, POSIX rename can silently replace another
        # process's *empty* cache folder between lexists() and publication.
        # Only our own temporary empty reservation can be displaced here.
        reserved = False
        try:
            try:
                os.mkdir(destination, 0o700)
                reserved = True
            except FileExistsError:
                # A concurrent complete, verified result is safe to reuse.
                existing = cached_playblast(shot, root) if not fresh_revision else None
                if existing and reuse:
                    return existing
                if destination != base:
                    raise RuntimeError('An incomplete cached revision exists at ' + destination)
                # The base appeared while we were rendering, and it cannot
                # be trusted. Leave it untouched; publish a private revision.
                destination = base + '-fresh-' + uuid.uuid4().hex
                private_revision = True
                manifest_data['fresh_revision'] = True
                atomic_json_write(os.path.join(stage, 'manifest.json'), manifest_data)
                os.mkdir(destination, 0o700)
                reserved = True
            os.rename(stage, destination)
            reserved = False
        finally:
            if reserved:
                try:
                    os.rmdir(destination)  # exclusively created empty placeholder only
                except OSError:
                    pass
        stage = None
        return os.path.join(destination, files[0])
    finally:
        if previous_camera is not None and active_panel:
            try:
                cmds.modelEditor(active_panel, edit=True, camera=previous_camera)
            except (RuntimeError, ValueError, TypeError):
                pass
        if previous_time is not None:
            try:
                cmds.currentTime(previous_time)
            except (RuntimeError, ValueError, TypeError):
                pass
        if stage and os.path.isdir(stage):
            shutil.rmtree(stage)


def attach_playblast(source, media_path):
    """Update the playblast representation while preserving review identity."""
    source.relink(ROLE_PLAYBLAST, media_path, KIND_SEQUENCE)
    source.set_active_role(ROLE_PLAYBLAST)
    return source
