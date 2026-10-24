# -*- coding: utf-8 -*-
"""Durable, Qt-free export-intent journal. One process owns a queue file.

The journal is **not** a substitute for media verification: a task becomes
successful only after the runner has verified and committed staged output.
Every state transition uses fsync + atomic os.replace; a torn save preserves
the previous valid snapshot. No pending/running tasks are pruned to fit history.
"""
from __future__ import absolute_import

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
import re
import tempfile
import uuid

from .spec import ExportSpec, validate_managed_export


SCHEMA_VERSION = 1
PENDING = 'pending'
RUNNING = 'running'
COMMITTING = 'committing'
SUCCEEDED = 'succeeded'
FAILED = 'failed'
CRASHED = 'crashed'
CANCELLING = 'cancelling'
CANCELLED = 'cancelled'
TERMINAL = frozenset((SUCCEEDED, FAILED, CRASHED, CANCELLED))
ACTIVE = frozenset((PENDING, RUNNING, COMMITTING, CANCELLING))
STATES = TERMINAL | ACTIVE
_ID = re.compile(r'^[a-f0-9]{32}$')
_MAX_BYTES = 16 * 1024 * 1024


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def _owner_alive(lock_path):
    """Conservative process-liveness check for an existing queue lease."""
    if not os.path.lexists(lock_path):
        return False
    try:
        with open(lock_path, 'r', encoding='utf-8') as stream:
            record = json.load(stream)
        pid = int(record['pid'])
        if pid <= 0:
            return True
        os.kill(pid, 0)
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return True  # Never steal a partially initialized lock.


def _encode(value):
    """Losslessly serialize spec context, including integer keys and tuples."""
    if isinstance(value, dict):
        return {'type': 'map', 'items': [[_encode(k), _encode(v)]
                                        for k, v in sorted(value.items(),
                                                           key=lambda p: str(p[0]))]}
    if isinstance(value, tuple):
        return {'type': 'tuple', 'items': [_encode(v) for v in value]}
    if isinstance(value, list):
        return {'type': 'list', 'items': [_encode(v) for v in value]}
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not (float('-inf') < value < float('inf')):
            raise ValueError('NaN or infinite export values are not supported.')
        return value
    raise TypeError('Unsupported export context type: {}'.format(type(value).__name__))


def _decode(value, depth=0):
    if depth > 64:
        raise ValueError('Queue context nesting exceeds safety limit.')
    if isinstance(value, dict):
        kind = value.get('type')
        parts = value.get('items')
        if not isinstance(parts, list):
            raise ValueError('Invalid queue context encoding.')
        if kind == 'map':
            pairs = []
            for pair in parts:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise ValueError('Invalid queue dictionary entry.')
                pairs.append((_decode(pair[0], depth + 1),
                              _decode(pair[1], depth + 1)))
            return dict(pairs)
        if kind in ('tuple', 'list'):
            values = [_decode(item, depth + 1) for item in parts]
            return tuple(values) if kind == 'tuple' else values
        raise ValueError('Unknown queue context encoding.')
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError('Invalid queue context value.')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def _spec_record(spec):
    return {'path': spec.path, 'source_path': spec.source_path,
            'source_id': spec.source_id, 'start': spec.start, 'end': spec.end,
            'count': spec.count, 'output_size': list(spec.output_size),
            'fps': spec.fps, 'source_fps': spec.source_fps,
            'stage': spec.stage, 'sequence': spec.sequence,
            'context': _encode(spec.to_context()),
            'schema_version': spec.schema_version}


def _spec_from_record(record):
    ctx = _decode(record['context'])
    spec = ExportSpec.from_context(ctx, source_path=record['source_path'],
                                   source_id=record['source_id'])
    if _spec_record(spec) != record:
        raise ValueError('Queue export spec integrity check failed.')
    return spec


def _output_key(spec):
    return os.path.normcase(os.path.abspath(spec.path))


def _unsafe_output_parent(path):
    """Block redirecting symlinks but tolerate macOS /var and /tmp aliases."""
    parent = os.path.dirname(path)
    while parent and parent != os.path.dirname(parent):
        if os.path.islink(parent) and not (
                (parent == '/var' and os.path.realpath(parent) == '/private/var') or
                (parent == '/tmp' and os.path.realpath(parent) == '/private/tmp')):
            return True
        parent = os.path.dirname(parent)
    return False


def _validate_spec(spec):
    if not isinstance(spec, ExportSpec):
        raise TypeError('enqueue expects a frozen ExportSpec.')
    if not os.path.isabs(spec.path) or not os.path.isabs(spec.source_path):
        raise ValueError('Queued exports require absolute source/output paths.')
    if os.path.normpath(spec.path) != spec.path:
        raise ValueError('Queue output path may not contain traversal or noncanonical components.')
    # A symlink in an output parent can redirect atomic promotion or backup
    # operations outside the selected directory after a restart.
    if _unsafe_output_parent(spec.path):
        raise ValueError('Queue output parent must not be a symlink.')
    if spec.sequence:
        if not re.search(r'\#{4}', os.path.basename(spec.path)):
            raise ValueError('Existing ExportJob requires a #### image sequence pattern.')
        output_dir = os.path.dirname(spec.path)
        if output_dir == os.path.dirname(output_dir):
            raise ValueError('Sequence output needs a dedicated output folder.')
    else:
        if '#' in spec.path:
            raise ValueError('Movie output may not contain sequence tokens.')
    for component in (spec.path, spec.source_path):
        if '\x00' in component:
            raise ValueError('NUL in export path.')
    target = os.path.dirname(spec.path) if spec.sequence else spec.path
    if os.path.islink(target):
        raise ValueError('Queue output must not be a symlink.')


@dataclass(frozen=True)
class QueueTask:
    """Stable read-only view. Mutable JSON fields are returned as fresh copies."""
    id: str
    state: str
    spec: ExportSpec
    progress: int
    attempt: int
    overwrite: bool
    created_at: str
    updated_at: str
    error: str
    _inputs_json: str
    _result_json: str
    _recovery_json: str
    _logs_json: str

    @property
    def inputs(self):
        return json.loads(self._inputs_json)

    @property
    def result(self):
        return json.loads(self._result_json)

    @property
    def recovery(self):
        return json.loads(self._recovery_json)

    @property
    def logs(self):
        return tuple(json.loads(self._logs_json))


class ExportQueueStore(object):
    """Atomic queue persistence and guarded state transitions (UI-neutral)."""

    def __init__(self, path, history_limit=100, log_limit=40):
        self.path = os.path.abspath(os.fspath(path))
        self.history_limit = max(1, int(history_limit))
        self.log_limit = max(4, int(log_limit))
        self._lease_nonce = None  # set by the runner owning this store
        self._data = {'schema_version': SCHEMA_VERSION, 'tasks': []}
        if os.path.isfile(self.path):
            with open(self.path, 'rb') as stream:
                raw = stream.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise ValueError('Export queue is too large.')
            data = json.loads(raw.decode('utf-8'))
            self._validate_data(data)
            self._data = data
            interrupted = False
            if not _owner_alive(self.path + '.runlock'):
                for task in self._data['tasks']:
                    if task['state'] in (RUNNING, COMMITTING, CANCELLING):
                        task['state'] = CRASHED
                        task['error'] = 'Export interrupted by application shutdown.'
                        self._event(task, 'warning', task['error'])
                        interrupted = True
                if interrupted:
                    self._save()

    def _validate_data(self, data):
        if not isinstance(data, dict) or data.get('schema_version') != SCHEMA_VERSION:
            raise ValueError('Unsupported export queue schema version.')
        if not isinstance(data.get('tasks'), list) or len(data['tasks']) > 3000:
            raise ValueError('Invalid number of queued exports.')
        seen = set()
        for task in data['tasks']:
            if (not isinstance(task, dict) or
                    not _ID.fullmatch(str(task.get('id', ''))) or
                    task['id'] in seen or task.get('state') not in STATES):
                raise ValueError('Malformed queue task.')
            seen.add(task['id'])
            spec = _spec_from_record(task['spec'])
            _validate_spec(spec)
            if task['output_key'] != _output_key(spec):
                raise ValueError('Queue output identity mismatch.')
            if not 0 <= int(task['progress']) <= 100:
                raise ValueError('Invalid export progress.')
            if (not isinstance(task.get('logs'), list) or
                    not isinstance(task.get('inputs'), dict)):
                raise ValueError('Invalid task logs or inputs.')
            if not isinstance(task.get('attempt'), int) or task['attempt'] < 0:
                raise ValueError('Invalid export attempt count.')
            if not isinstance(task.get('recovery'), (dict, type(None))):
                raise ValueError('Malformed export recovery journal.')
            if not isinstance(task.get('result'), (dict, type(None))):
                raise ValueError('Malformed export verification journal.')

    def _save(self):
        lease = self.path + '.runlock'
        if _owner_alive(lease):
            try:
                with open(lease, encoding='utf-8') as stream:
                    nonce = json.load(stream).get('nonce')
            except (OSError, ValueError, TypeError):
                nonce = None
            if not nonce or nonce != self._lease_nonce:
                raise RuntimeError('A different process owns this export queue.')
        folder = os.path.dirname(self.path)
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.frqueue-', suffix='.tmp', dir=folder)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(self._data, stream, ensure_ascii=False,
                          allow_nan=False, sort_keys=True, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
            # Persist the directory entry across power failure where supported.
            if hasattr(os, 'O_DIRECTORY'):
                try:
                    dfd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(dfd)
                    finally:
                        os.close(dfd)
                except OSError:
                    # Directory fsync is not available on some network filesystems;
                    # the already-fsynced temp + atomic rename still stands.
                    pass
        finally:
            if os.path.lexists(tmp):
                os.unlink(tmp)

    def _find(self, task_id):
        for record in self._data['tasks']:
            if record['id'] == task_id:
                return record
        raise KeyError('Unknown export queue task: {}'.format(task_id))

    def _snapshot(self, record):
        return QueueTask(record['id'], record['state'],
                         _spec_from_record(record['spec']),
                         record['progress'], record['attempt'],
                         record['overwrite'], record['created_at'],
                         record['updated_at'], record.get('error') or '',
                         _canonical(record['inputs']), _canonical(record['result']),
                         _canonical(record['recovery']), _canonical(record['logs']))

    def get(self, task_id):
        return self._snapshot(self._find(task_id))

    def tasks(self):
        return tuple(self._snapshot(record) for record in self._data['tasks'])

    def next_pending(self):
        for record in self._data['tasks']:
            if record['state'] == PENDING:
                return self._snapshot(record)
        return None

    def _event(self, record, level, message):
        record['logs'].append({'at': _timestamp(), 'level': str(level),
                               'message': str(message)[:1800]})
        del record['logs'][:-self.log_limit]
        record['updated_at'] = _timestamp()

    def log(self, task_id, level, message):
        """Append a bounded, durable user/codec diagnostic to one task."""
        record = self._find(task_id)
        self._event(record, level, message)
        self._save()
        return self._snapshot(record)

    def _prune(self):
        completed = [r for r in self._data['tasks'] if r['state'] in TERMINAL]
        removed = max(0, len(completed) - self.history_limit)
        if removed:
            ids = {r['id'] for r in completed[:removed]}
            self._data['tasks'] = [r for r in self._data['tasks']
                                   if r['id'] not in ids]

    def enqueue(self, spec, seq_files=(), width=None, height=None,
                overwrite=False):
        _validate_spec(spec)
        output = _output_key(spec)
        inputs = {'seq_files': list(seq_files or ()),
                  'width': int(width or spec.output_size[0]),
                  'height': int(height or spec.output_size[1])}
        if inputs['width'] <= 0 or inputs['height'] <= 0:
            raise ValueError('Invalid input media dimensions.')
        for source in inputs['seq_files']:
            if not isinstance(source, str) or not os.path.isabs(source):
                raise ValueError('Sequence input paths must be absolute.')
        if spec.stage != 'source':
            validate_managed_export(spec, inputs['seq_files'],
                                    (inputs['width'], inputs['height']))
        intent = {'spec': _spec_record(spec), 'inputs': inputs}
        digest = hashlib.sha256(_canonical(intent).encode('utf-8')).hexdigest()
        owners = [previous for previous in self._data['tasks']
                  if previous['output_key'] == output]
        active_owners = [owner for owner in owners if owner['state'] in ACTIVE]
        if active_owners:
            active = active_owners[-1]
            if active['fingerprint'] == digest:
                return self._snapshot(active)  # idempotent pending/running add
            raise ValueError('Another active export task owns this output path.')
        if owners:
            latest = owners[-1]
            if latest['fingerprint'] == digest:
                return self._snapshot(latest)  # retry failures; no repeat output
            if not overwrite:
                raise ValueError('Another queued/exported task owns this output path.')
            # Explicit overwrite may supersede old *terminal* output history.
            # Keep the old entry immutable for audit until bounded history
            # prunes it. Never overwrite an active export or silently rerender.
        target = os.path.dirname(spec.path) if spec.sequence else spec.path
        if os.path.lexists(target) and not overwrite:
            raise FileExistsError('Export output exists: {}'.format(target))
        if os.path.islink(target):
            raise ValueError('Refusing to export over a symlink.')
        now = _timestamp()
        record = {'id': uuid.uuid4().hex, 'state': PENDING, 'fingerprint': digest,
                  'output_key': output, 'spec': intent['spec'], 'inputs': inputs,
                  'overwrite': bool(overwrite), 'progress': 0, 'attempt': 0,
                  'created_at': now, 'updated_at': now, 'error': None,
                  'result': None, 'recovery': None, 'logs': []}
        self._event(record, 'info', 'Export added to queue.')
        self._data['tasks'].append(record)
        self._prune()
        self._save()
        return self._snapshot(record)

    def _transition(self, task_id, expected, next_state, message, error=None,
                    result=None, recovery=None):
        record = self._find(task_id)
        if record['state'] not in expected:
            raise ValueError('Invalid export state transition: {} -> {}.'.format(
                record['state'], next_state))
        record['state'] = next_state
        record['error'] = str(error)[:1800] if error else None
        if result is not None:
            record['result'] = copy.deepcopy(result)
        if recovery is not None:
            record['recovery'] = copy.deepcopy(recovery)
        if next_state == SUCCEEDED:
            record['progress'] = 100
        self._event(record, 'error' if error else 'info', message)
        self._prune()
        self._save()
        return self._snapshot(record)

    def begin(self, task_id, recovery):
        record = self._find(task_id)
        if record['state'] != PENDING:
            raise ValueError('Only pending exports can start.')
        record['attempt'] += 1
        record['progress'] = 0
        return self._transition(task_id, (PENDING,), RUNNING, 'Export started.',
                                recovery=recovery)

    def progress(self, task_id, percent):
        record = self._find(task_id)
        if record['state'] not in (RUNNING, CANCELLING):
            return self._snapshot(record)
        percent = max(0, min(99, int(percent)))
        previous = record['progress']
        if percent > previous:
            record['progress'] = percent
            # Persist checkpoints, not every FFmpeg progress callback.
            if percent // 10 > previous // 10:
                self._save()
        return self._snapshot(record)

    def committing(self, task_id, recovery):
        return self._transition(task_id, (RUNNING,), COMMITTING,
                                'Output verified; committing.', recovery=recovery)

    def succeed(self, task_id, verification, recovery=None):
        return self._transition(task_id, (COMMITTING,), SUCCEEDED,
                                'Export verified and committed.',
                                result={'verification': verification},
                                recovery=recovery)

    def recover_succeeded(self, task_id, verification):
        """Reconcile a crash *after* an independently verified media commit."""
        return self._transition(task_id, (CRASHED, FAILED), SUCCEEDED,
                                'Recovered verified output after interrupted commit.',
                                result={'verification': verification})

    def fail(self, task_id, message):
        return self._transition(task_id, (RUNNING, COMMITTING), FAILED,
                                'Export failed: {}'.format(message), error=message)

    def fail_pending(self, task_id, message):
        return self._transition(task_id, (PENDING,), FAILED,
                                'Export could not start: {}'.format(message),
                                error=message)

    def cancel(self, task_id):
        record = self._find(task_id)
        if record['state'] == PENDING:
            return self._transition(task_id, (PENDING,), CANCELLED,
                                    'Pending export cancelled.')
        if record['state'] in (RUNNING, COMMITTING):
            return self._transition(task_id, (RUNNING, COMMITTING), CANCELLING,
                                    'Cancellation requested.')
        return self._snapshot(record)

    def cancelled(self, task_id):
        return self._transition(task_id, (CANCELLING,), CANCELLED,
                                'Export cancelled; staged output will be removed.')

    def retry(self, task_id):
        record = self._find(task_id)
        if record['state'] not in (FAILED, CRASHED, CANCELLED):
            raise ValueError('Only failed, crashed or cancelled exports can retry.')
        index = self._data['tasks'].index(record)
        if any(later['output_key'] == record['output_key']
               for later in self._data['tasks'][index + 1:]):
            raise ValueError('Cannot retry an export superseded by a newer task.')
        record['progress'] = 0
        return self._transition(task_id, (FAILED, CRASHED, CANCELLED), PENDING,
                                'Export queued for retry.')


__all__ = ['ExportQueueStore', 'QueueTask', 'PENDING', 'RUNNING', 'COMMITTING',
           'SUCCEEDED', 'FAILED', 'CRASHED', 'CANCELLING', 'CANCELLED']
