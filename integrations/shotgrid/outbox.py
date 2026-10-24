# -*- coding: utf-8 -*-
"""Durable, Qt-free Flow/ShotGrid Note and Version-status publication outbox.

This module does not connect, authenticate, open UI dialogs or write remotely
when a job is enqueued. The UI must obtain the selected *remote* Version ID and
explicit reviewer's consent before calling ``publish(sync_client)`` from its
own worker thread. ``sync_client`` is the authenticated Shotgun/Toolkit client
from ``shotgrid.client.connect``; it is NEVER kept on disk or cached here.

Note publishing is idempotent even if the process crashes after the remote
create but before local acknowledgement: every Note includes a deterministic
source/version/note ID tag. A bounded, authoritative remote lookup must succeed
before each create; duplicate tags become conflicts rather than duplicate posts.
Status publication compares the server's Version status against an explicit
expected value. ShotGrid offers no compare-and-set mutation transaction, so an
external change between the precondition read and update remains possible.

The private, atomic JSON state contains only allowlisted review contents,
nonsecret entity IDs, hashes, bounded retry metadata and server-created IDs.
No API key, environment credential, authenticated client or exception message
is serialized. A process-level filesystem lock guards updates across instances.
"""
from __future__ import absolute_import

from contextlib import contextmanager
import copy
from dataclasses import dataclass
import hashlib
import json
import os
import re
import tempfile
import time

from . import client as sg_client


FORMAT = 'frame-review-shotgrid-outbox'
VERSION = 1
MAX_BYTES = 5 * 1024 * 1024
MAX_ENTRIES = 1000
STATUS_VALUES = frozenset(('queued', 'retry', 'done', 'conflict', 'failed'))
_STATUSES = re.compile(r'^[a-zA-Z0-9_]{1,32}$')
_TAG = '[FrameReviewer-Review:{}]'
_HEX = re.compile(r'^[0-9a-f]{64}$')


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _json_canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _key(site, source_id, version_id, note_id):
    return _hash(_json_canonical([site, source_id, version_id, note_id]))


def _string(value, label, limit, allow_empty=False):
    if not isinstance(value, str) or len(value) > limit or ('\x00' in value):
        raise ValueError('Invalid {}.'.format(label))
    if not allow_empty and not value.strip():
        raise ValueError('A nonblank {} is required.'.format(label))
    return value


def _positive(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError('A positive {} is required.'.format(label))
    return value


def _site(value):
    # Never accept an embedded auth secret, username, query or fragment.
    from urllib.parse import urlsplit
    url = sg_client.normalize_site(value)
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.netloc or parsed.username or
            parsed.password or parsed.path not in ('', '/') or parsed.query or
            parsed.fragment or not parsed.hostname):
        raise ValueError('Use a credential-free HTTPS ShotGrid site origin.')
    return 'https://' + parsed.netloc.lower()


def _entity(value, kind):
    if value is None:
        return None
    if not isinstance(value, dict) or value.get('type') != kind:
        raise ValueError('Expected a {} entity reference.'.format(kind))
    return {'type': kind, 'id': _positive(value.get('id'), kind + ' ID')}


def _now(value=None):
    return float(time.time() if value is None else value)


class OutboxBusy(RuntimeError):
    """A second process is already modifying or publishing this outbox."""


class OutboxConflict(ValueError):
    """A remote or locally queued value requires explicit conflict resolution."""


@dataclass(frozen=True)
class Publication:
    id: str
    kind: str
    state: str
    source_id: str
    version_id: int
    note_id: str = ''
    remote_id: int = 0
    attempts: int = 0
    next_attempt_at: float = 0.0
    code: str = ''
    content_hash: str = ''


@dataclass(frozen=True)
class PublishReport:
    attempted: int
    succeeded: int
    retried: int
    conflicts: int
    failed: int
    skipped: int
    results: tuple


@dataclass(frozen=True)
class ConflictInspection:
    """Ephemeral, read-only server evidence, never saved to the outbox.

    ``revision`` fences a concurrently changed local job. ``remote_status`` and
    ``note_relation`` are verified against the actual Version/linked Note, not
    a cached UI selection. Resolve re-reads both under the outbox lock; a stale
    inspection can never authorize a changed remote precondition.
    """
    job_id: str
    revision: str
    kind: str
    version_id: int
    source_id: str
    remote_status: object
    local_status: object
    expected_status: object
    note_relation: str
    remote_note_id: int
    local_subject: str
    local_content: str
    remote_subject: str
    remote_content: str
    actions: tuple


@contextmanager
def _file_lock(path):
    """Cross-platform nonblocking advisory lock, held throughout publication.

    This intentionally refuses concurrent publishers rather than racing an
    ambiguous in-flight remote write. The lock file contains no data/secrets.
    Its file descriptor release recovers automatically after a process crash.
    """
    fd = os.open(path + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == 'nt':
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            if os.fstat(fd).st_size == 0:
                os.write(fd, b'\0')
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise OutboxBusy('ShotGrid outbox is already in use.')
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise OutboxBusy('ShotGrid outbox is already in use.')
        try:
            yield
        finally:
            if os.name == 'nt':
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class ShotGridOutbox(object):
    """One site-bound durable publication queue.

    Narrow UI integration::

        box = ShotGridOutbox(path, site='https://studio.shotgrid.autodesk.com')
        box.enqueue_note(source_id, version_id, note_id, body, subject='Review')
        box.enqueue_status(source_id, version_id, 'rev', expected_status='wip')
        # Obtain explicit initial-write consent and an authenticated sg client.
        report = box.publish(sync_client)  # run off Maya/Qt GUI thread

    ``pending``/``entries`` expose only nonsecret operational metadata; use
    ``box.get(job_id)`` to inspect individual status. ``done`` Note entries
    expose ``remote_id``, which the host can copy into ReviewNote.remote_ref
    *after* checking that the note still matches the queued content hash.
    """

    def __init__(self, path, site, max_attempts=5, backoff_base=15.0,
                 backoff_cap=1800.0, max_pages=8, page_size=100):
        self.path = os.path.abspath(os.path.expanduser(str(path)))
        self.site = _site(site)
        if (not 1 <= int(max_attempts) <= 20 or
                not 0.1 <= float(backoff_base) <= 3600 or
                not float(backoff_base) <= float(backoff_cap) <= 86400 or
                not 1 <= int(max_pages) <= 50 or
                not 1 <= int(page_size) <= 200):
            raise ValueError('Invalid bounded retry or remote lookup parameters.')
        self.max_attempts = int(max_attempts)
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        self.max_pages = int(max_pages)
        self.page_size = int(page_size)
        directory = os.path.dirname(self.path)
        if not os.path.isdir(directory):
            os.makedirs(directory, mode=0o700, exist_ok=True)
        if os.path.islink(self.path):
            raise ValueError('Outbox file cannot be a symbolic link.')

    def _empty(self):
        return {'format': FORMAT, 'version': VERSION,
                'site': self.site, 'entries': []}

    def _read(self):
        if not os.path.exists(self.path):
            return self._empty()
        if os.path.islink(self.path):
            raise ValueError('Outbox file cannot be a symbolic link.')
        if os.path.getsize(self.path) > MAX_BYTES:
            raise ValueError('ShotGrid outbox exceeds the 5MB safety limit.')
        with open(self.path, 'r', encoding='utf-8') as stream:
            data = json.load(stream)
        if (not isinstance(data, dict) or data.get('format') != FORMAT or
                data.get('version') != VERSION or data.get('site') != self.site or
                not isinstance(data.get('entries'), list)):
            raise ValueError('Invalid or mismatched ShotGrid outbox state.')
        if len(data['entries']) > MAX_ENTRIES:
            raise ValueError('ShotGrid outbox exceeds its entry safety limit.')
        seen = set()
        for entry in data['entries']:
            if (not isinstance(entry, dict) or entry.get('kind') not in ('note', 'status') or
                    entry.get('state') not in STATUS_VALUES or
                    entry.get('id') in seen or not _HEX.fullmatch(str(entry.get('id', ''))) or
                    not isinstance(entry.get('data'), dict)):
                raise ValueError('Invalid ShotGrid outbox entry.')
            seen.add(entry['id'])
        return data

    def _write(self, data):
        serialized = (_json_canonical(data) + '\n').encode('utf-8')
        if len(serialized) > MAX_BYTES:
            raise ValueError('Outbox is larger than the 5MB limit.')
        directory = os.path.dirname(self.path)
        fd, name = tempfile.mkstemp(prefix='.shotgrid-outbox-', suffix='.tmp',
                                    dir=directory)
        try:
            if os.name != 'nt':
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            if os.name != 'nt':
                directory_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @staticmethod
    def _view(entry):
        return Publication(
            entry['id'], entry['kind'], entry['state'],
            entry['source_id'], entry['version_id'],
            entry.get('note_id') or '', entry.get('remote_id') or 0,
            entry['attempts'], entry.get('next_attempt_at', 0.0),
            entry.get('code') or '', entry['data'].get('content_hash') or '')

    def entries(self):
        with _file_lock(self.path):
            return tuple(self._view(e) for e in self._read()['entries'])

    def pending(self):
        return tuple(e for e in self.entries() if e.state in ('queued', 'retry'))

    def get(self, job_id):
        for entry in self.entries():
            if entry.id == job_id:
                return entry
        return None

    def retry_now(self, job_id):
        """Explicitly retry a transient failed/queued job without remote writes.

        A remote conflict cannot be retried this way: the reviewer must first
        resolve the disagreement outside the outbox. ``done`` remains final.
        No discarded or conflicted Note is blindly overwritten remotely.
        """
        with _file_lock(self.path):
            state = self._read()
            for entry in state['entries']:
                if entry['id'] != job_id:
                    continue
                if entry['state'] not in ('queued', 'retry', 'failed'):
                    raise OutboxConflict('Completed and conflicting jobs cannot be reset.')
                entry.update(state='queued', attempts=0, next_attempt_at=0.0, code='')
                self._write(state)
                return self._view(entry)
            raise KeyError(job_id)

    @staticmethod
    def _revision(entry):
        # No credentials or remote session data enter this fingerprint.
        return _hash(_json_canonical([entry['id'], entry['kind'], entry['state'],
                                      entry['data'], entry['attempts'],
                                      entry.get('code', '')]))

    def _inspect_entry(self, sg, entry):
        data = entry['data']
        version = sg_client.version_details(
            sg, entry['version_id'], project_id=data.get('project_id'),
            entity=data.get('entity'))
        remote_status = version.get('sg_status_list')
        relation, found = '', None
        actions = ['discard']
        if entry['kind'] == 'status':
            desired = data['status']
            expected = data['expected_status']
            if remote_status == desired:
                actions.insert(0, 'acknowledge')
            elif remote_status == expected:
                actions.insert(0, 'requeue')
            elif remote_status is None or (isinstance(remote_status, str) and
                                           _STATUSES.fullmatch(remote_status)):
                actions.insert(0, 'rebase')
        else:
            try:
                found = sg_client.find_review_note_by_tag(
                    sg, entry['version_id'], data['marker'],
                    page_size=self.page_size, max_pages=self.max_pages)
            except sg_client.DuplicateReviewTag:
                relation = 'multiple_matching_tags'
            if found is None and not relation:
                relation = 'absent'
                actions.insert(0, 'requeue')
            elif found is not None:
                relation = ('identical' if _hash(_json_canonical([
                    found['subject'], found['content']])) == data['content_hash']
                    else 'different_content')
                if relation == 'identical':
                    actions.insert(0, 'acknowledge')
        return ConflictInspection(
            job_id=entry['id'], revision=self._revision(entry),
            kind=entry['kind'], version_id=entry['version_id'],
            source_id=entry['source_id'], remote_status=remote_status,
            local_status=data.get('status'), expected_status=data.get('expected_status'),
            note_relation=relation, remote_note_id=found['id'] if found else 0,
            local_subject=data.get('subject') or '',
            local_content=(data.get('content') or '').removesuffix('\n\n' + data.get('marker', '')),
            remote_subject=found['subject'] if found else '',
            remote_content=(found['content'] if found else '').replace(
                '\n\n' + data.get('marker', ''), ''),
            actions=tuple(actions))

    def inspect_conflict(self, sync_client, job_id):
        """Fetch *current* remote Version/Note for a conflicted local job.

        Read-only ShotGrid calls are allowed; no queued/durable mutation occurs.
        Network/permission errors propagate; they never silently mean absence.
        """
        self._check_client(sync_client)
        with _file_lock(self.path):
            entry = next((copy.deepcopy(e) for e in self._read()['entries']
                          if e['id'] == job_id), None)
            if entry is None:
                raise KeyError(job_id)
            if entry['state'] != 'conflict':
                raise OutboxConflict('Only conflicted publications can be inspected here.')
        # Never hold the filesystem lock while a potentially slow network read
        # is pending: reconnecting/refreshing the panel must remain responsive.
        inspection = self._inspect_entry(sync_client, entry)
        with _file_lock(self.path):
            current = next((e for e in self._read()['entries']
                            if e['id'] == job_id), None)
            if current is None or self._revision(current) != inspection.revision:
                raise OutboxConflict('Local conflict changed during inspection.')
        return inspection

    def resolve_conflict(self, sync_client, inspection, action):
        """Apply an explicitly selected *local* resolution, never a remote write.

        Only accepted options from a fresh inspection are valid. An independent
        authoritative read inside the process lock confirms Version scope,
        status, Note marker and the original local revision. A rebase generates
        a NEW idempotent status operation with the observed remote status as
        precondition; publish() independently rechecks before any write.
        """
        if not isinstance(inspection, ConflictInspection):
            raise TypeError('Inspect the remote conflict before choosing a resolution.')
        if action not in ('acknowledge', 'requeue', 'rebase'):
            raise ValueError('Choose an explicit supported conflict resolution.')
        self._check_client(sync_client)
        with _file_lock(self.path):
            state = self._read()
            match = next((e for e in state['entries']
                          if e['id'] == inspection.job_id), None)
            if match is None or match['state'] != 'conflict' or (
                    self._revision(match) != inspection.revision):
                raise OutboxConflict('Local publication changed; inspect again.')
            fresh = self._inspect_entry(sync_client, match)
            if (fresh.remote_status != inspection.remote_status or
                    fresh.note_relation != inspection.note_relation or
                    fresh.remote_note_id != inspection.remote_note_id or
                    fresh.remote_subject != inspection.remote_subject or
                    fresh.remote_content != inspection.remote_content):
                raise OutboxConflict('Remote Version/Note changed; inspect again.')
            if action not in fresh.actions:
                raise OutboxConflict('This resolution is unsafe for the remote state.')
            if action == 'acknowledge':
                match.update(state='done', code='', next_attempt_at=0.0,
                             remote_id=(fresh.remote_note_id if match['kind'] == 'note'
                                        else match['version_id']))
                result = self._view(match)
            elif action == 'requeue':
                match.update(state='queued', attempts=0, code='',
                             next_attempt_at=0.0)
                result = self._view(match)
            else:
                old = match['data']
                desired, observed = old['status'], fresh.remote_status
                new_id = _hash(_json_canonical([
                    self.site, match['source_id'], match['version_id'],
                    'status', observed, desired]))
                replacement = copy.deepcopy(match)
                replacement.update(id=new_id, state='queued', attempts=0,
                                   next_attempt_at=0.0, code='', remote_id=None)
                replacement['data']['expected_status'] = observed
                preexisting = next((e for e in state['entries']
                                    if e['id'] == new_id and e is not match), None)
                if preexisting is not None:
                    if (preexisting['kind'] != 'status' or
                            preexisting['data'] != replacement['data'] or
                            preexisting['state'] not in ('queued', 'retry')):
                        raise OutboxConflict('A different status operation already exists.')
                    result = self._view(preexisting)
                else:
                    state['entries'].append(replacement)
                    result = self._view(replacement)
                state['entries'].remove(match)  # local-only, superseded conflict
            self._write(state)
            return result

    def discard(self, job_id):
        """Explicitly discard one local job; NEVER delete its remote entity.

        Discarding an ambiguous failed Note does not guarantee it was not
        published. A future enqueue of the same logical note still uses the
        deterministic tag and checks ShotGrid before creating a new Note.
        """
        with _file_lock(self.path):
            state = self._read()
            for index, entry in enumerate(state['entries']):
                if entry['id'] == job_id:
                    if entry['state'] == 'done':
                        raise OutboxConflict('Published records must remain as acknowledgements.')
                    state['entries'].pop(index)
                    self._write(state)
                    return True
            raise KeyError(job_id)

    @staticmethod
    def _check_scope(source_id, version_id, project_id=None, entity=None):
        _string(source_id, 'Source ID', 256)
        _positive(version_id, 'Version ID')
        if project_id is not None:
            _positive(project_id, 'Project ID')
        if entity is not None:
            if not isinstance(entity, dict):
                raise ValueError('Expected an entity reference.')
            kind = _string(entity.get('type'), 'linked entity type', 48)
            if kind not in ('Shot', 'Asset'):
                raise ValueError('Supported Version links must be Shot or Asset.')
            return _entity(entity, kind)
        return None

    def enqueue_note(self, source_id, version_id, note_id, body, subject='Review',
                     project_id=None, entity=None):
        """Queue a new Note, never silently update an existing remote Note.

        Pending local edits replace the existing job payload while retaining its
        stable idempotency tag. If the note was already published with different
        content, raise ``OutboxConflict``. Pass explicit Version ID from the
        selected ShotGrid Version, not from the local Source/Representation.
        """
        entity = self._check_scope(source_id, version_id, project_id, entity)
        _string(note_id, 'review Note ID', 256)
        _string(body, 'review Note body', 100000)
        _string(subject, 'review Note subject', 256)
        tag_hash = _key(self.site, source_id, version_id, note_id)
        marker = _TAG.format(tag_hash)
        if '[FrameReviewer-Review:' in body:
            raise ValueError('Review body cannot contain publication markers.')
        content = body.rstrip() + '\n\n' + marker
        digest = _hash(_json_canonical([subject, content]))
        data = {'subject': subject, 'content': content,
                'content_hash': digest, 'marker': marker,
                'project_id': project_id, 'entity': entity}
        return self._enqueue(tag_hash, 'note', source_id, version_id, data,
                             note_id=note_id)

    def enqueue_review_note(self, version_id, note, subject='Review',
                            project_id=None, entity=None):
        """Queue typed ReviewNote without claiming an existing remote Note.

        A Note carrying ``remote_ref`` must be reconciled/updated with explicit
        remote-state checks rather than being mistakenly re-created.
        """
        if getattr(note, 'remote_ref', None):
            raise OutboxConflict('This note already has a remote reference.')
        return self.enqueue_note(note.source_id, version_id, note.id, note.body,
                                 subject=subject, project_id=project_id, entity=entity)

    def enqueue_status(self, source_id, version_id, status, expected_status,
                       project_id=None, entity=None):
        """Queue a Version status change with an explicit remote precondition.

        ``expected_status=None`` intentionally means the *server* status is
        empty. Never take a default expected status from a local UI label.
        """
        entity = self._check_scope(source_id, version_id, project_id, entity)
        if not isinstance(status, str) or not _STATUSES.fullmatch(status):
            raise ValueError('Invalid ShotGrid status code.')
        if expected_status is not None and (not isinstance(expected_status, str) or
                                             not _STATUSES.fullmatch(expected_status)):
            raise ValueError('Invalid expected ShotGrid status code.')
        job_id = _hash(_json_canonical([self.site, source_id, version_id,
                                        'status', expected_status, status]))
        data = {'status': status, 'expected_status': expected_status,
                'project_id': project_id, 'entity': entity}
        return self._enqueue(job_id, 'status', source_id, version_id, data)

    def _enqueue(self, job_id, kind, source_id, version_id, data, note_id=''):
        with _file_lock(self.path):
            state = self._read()
            for entry in state['entries']:
                if entry['id'] != job_id:
                    continue
                if entry['kind'] != kind or entry.get('note_id', '') != note_id:
                    raise OutboxConflict('Publication ID collides with another task.')
                if entry['state'] in ('conflict', 'failed'):
                    raise OutboxConflict('Resolve existing publication before retrying.')
                if entry['data'] == data:
                    return self._view(entry)
                if entry['state'] == 'done':
                    raise OutboxConflict('Published note differs from the new local edit.')
                if kind != 'note':
                    raise OutboxConflict('Cannot replace an unrelated status operation.')
                if (entry['data'].get('project_id') != data.get('project_id') or
                        entry['data'].get('entity') != data.get('entity')):
                    raise OutboxConflict('Remote context changed for a pending Note.')
                entry['data'] = data
                entry['state'] = 'queued'
                entry['attempts'] = 0
                entry['next_attempt_at'] = 0.0
                entry['code'] = ''
                self._write(state)
                return self._view(entry)
            if len(state['entries']) >= MAX_ENTRIES:
                raise ValueError('Outbox is full; archive published entries first.')
            entry = {'id': job_id, 'kind': kind, 'state': 'queued',
                     'source_id': source_id, 'version_id': version_id,
                     'note_id': note_id, 'data': data, 'remote_id': None,
                     'attempts': 0, 'next_attempt_at': 0.0, 'code': '',
                     'created_at': _now()}
            state['entries'].append(entry)
            self._write(state)
            return self._view(entry)

    def _check_client(self, sync_client):
        if sync_client is None or not all(callable(getattr(sync_client, attr, None))
                                           for attr in ('find', 'find_one', 'create', 'update')):
            raise TypeError('Expected an authenticated ShotGrid API client.')
        actual = getattr(sync_client, 'base_url', None) or getattr(sync_client, 'url', None)
        if not actual or _site(actual) != self.site:
            raise ValueError('Authenticated ShotGrid site does not match outbox.')

    def _publish_one(self, sg, entry):
        data = entry['data']
        version_id = entry['version_id']
        version = sg_client.version_details(sg, version_id,
                                             project_id=data.get('project_id'),
                                             entity=data.get('entity'))
        if entry['kind'] == 'note':
            found = sg_client.find_review_note_by_tag(
                sg, version_id, data['marker'],
                page_size=self.page_size, max_pages=self.max_pages)
            if found is not None:
                # Same logical ID with another revision is a true conflict,
                # not a reason to produce a duplicate Note.
                if (_hash(_json_canonical([found['subject'], found['content']])) !=
                        data['content_hash']):
                    raise OutboxConflict('An already-published Note has changed.')
                return found['id']
            return sg_client.create_review_note(
                sg, version, data['subject'], data['content'])
        existing = version.get('sg_status_list')
        desired = data['status']
        if existing == desired:
            return version_id  # Ack after crash or already applied elsewhere.
        if existing != data['expected_status']:
            raise OutboxConflict('ShotGrid Version status changed remotely.')
        sg_client.update_version_review_status(sg, version_id, desired)
        return version_id

    def publish(self, sync_client, now=None, limit=10):
        """Attempt due queued jobs once, with durable per-job acknowledgements.

        Calls the synchronous authenticated ShotGrid API. Host should run this
        on a background thread and display ``PublishReport.results``. Neither
        queue nor authentication creates remote writes until explicitly called.
        All network calls happen *under the outbox process lock* and jobs are
        immediately flushed after each result. Ambiguous create failures are
        retried with the same tag and a remote lookup before another create.
        """
        self._check_client(sync_client)
        timestamp = _now(now)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('Publish limit must be 1..100.')
        events = []
        counters = {'done': 0, 'retry': 0, 'conflict': 0, 'failed': 0}
        skipped = 0
        with _file_lock(self.path):
            state = self._read()
            for entry in state['entries']:
                if entry['state'] not in ('queued', 'retry'):
                    skipped += 1
                    continue
                if entry.get('next_attempt_at', 0.0) > timestamp or len(events) >= limit:
                    skipped += 1
                    continue
                entry['attempts'] += 1
                # Persist in-flight claim before the network call. On crash, a
                # restart reruns the same idempotent marker/precondition lookup.
                entry['state'] = 'retry'
                entry['next_attempt_at'] = timestamp + self.backoff_cap
                self._write(state)
                try:
                    remote_id = self._publish_one(sync_client, entry)
                except (sg_client.DuplicateReviewTag, sg_client.RemoteIdentityMismatch,
                        OutboxConflict):
                    entry['state'] = 'conflict'
                    entry['code'] = 'remote_conflict'
                except (ValueError, PermissionError, TypeError):
                    entry['state'] = 'failed'
                    entry['code'] = 'invalid_or_denied'
                except Exception:
                    # Never persist exception messages; SG exceptions may embed
                    # credentials, request headers or private studio data.
                    if entry['attempts'] >= self.max_attempts:
                        entry['state'] = 'failed'
                        entry['code'] = 'retry_exhausted'
                    else:
                        entry['state'] = 'retry'
                        delay = min(self.backoff_cap, self.backoff_base *
                                    (2 ** (entry['attempts'] - 1)))
                        entry['next_attempt_at'] = timestamp + delay
                        entry['code'] = 'retryable_remote_error'
                else:
                    entry['state'] = 'done'
                    entry['remote_id'] = remote_id
                    entry['next_attempt_at'] = 0.0
                    entry['code'] = ''
                self._write(state)
                counters[entry['state']] += 1
                events.append(self._view(entry))
        return PublishReport(len(events), counters['done'], counters['retry'],
                             counters['conflict'], counters['failed'], skipped,
                             tuple(events))


__all__ = ['ShotGridOutbox', 'Publication', 'PublishReport', 'ConflictInspection', 'OutboxBusy',
           'OutboxConflict', 'FORMAT', 'VERSION']
