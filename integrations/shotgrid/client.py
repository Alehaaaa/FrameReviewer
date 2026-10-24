# -*- coding: utf-8 -*-
"""Flow Production Tracking / ShotGrid data layer.

Pure data: a thin, Qt-free wrapper over the public ``shotgun_api3`` client so
the UI (shotgrid_ui) and any future headless caller share one definition of the
schema. Authentication prefers a Toolkit/Desktop user session or environment
credentials, then accepts a session-only script key without storing secrets.

Nothing here touches Qt or Maya, and ``shotgun_api3`` is imported defensively so
importing the package never fails on a workstation that lacks it -- callers
branch on :data:`AVAILABLE` and surface :data:`IMPORT_ERROR`.

Entity/field names mirror the standard ShotGrid schema::

    Project                                  id, name
    Sequence   project                       id, code, description, sg_status_list
    Shot       project, sg_sequence          id, code, description, image, sg_status_list
    Version    project, entity(=Shot)        id, code, created_at, description, image,
                                             sg_path_to_movie, sg_path_to_frames,
                                             sg_uploaded_movie, sg_status_list, user
    Note       note_links in [Version, Shot] id, subject, content, created_at, user
"""
from __future__ import absolute_import

import hashlib
import os
import re
import tempfile

try:
    import shotgun_api3
    AVAILABLE = True
    IMPORT_ERROR = ''
except Exception as exc:  # pragma: no cover - environment dependent
    shotgun_api3 = None
    AVAILABLE = False
    IMPORT_ERROR = str(exc)

USER_AGENT = 'FrameReviewer/3.0'

# Fields requested per entity, in one place so queries and the UI agree.
PROJECT_FIELDS = ['id', 'name']
SEQUENCE_FIELDS = ['id', 'code', 'description', 'sg_status_list']
SHOT_FIELDS = ['id', 'code', 'description', 'image', 'sg_status_list', 'sg_sequence']
VERSION_FIELDS = ['id', 'code', 'created_at', 'description', 'entity', 'image',
                  'sg_path_to_frames', 'sg_path_to_movie', 'sg_status_list',
                  'sg_uploaded_movie', 'user']
NOTE_FIELDS = ['id', 'subject', 'content', 'created_at', 'user', 'note_links']


class DuplicateReviewTag(ValueError):
    """More than one remote Note has this exact FrameReviewer publication tag."""


class RemoteIdentityMismatch(ValueError):
    """Remote Version identity does not match the explicitly queued context."""


def _entity_id(value, entity_type):
    if not isinstance(value, dict) or value.get('type') != entity_type:
        raise ValueError('Expected a {} entity reference.'.format(entity_type))
    number = value.get('id')
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError('Expected a positive {} ID.'.format(entity_type))
    return number


def version_details(sg, version_id, project_id=None, entity=None):
    """Read the authoritative Version and verify optional project/entity IDs.

    A session Source ID and media filename are *not* ShotGrid Version IDs.
    Callers must resolve and explicitly provide the actual selected Version id.
    """
    if isinstance(version_id, bool) or not isinstance(version_id, int) or version_id < 1:
        raise ValueError('A positive ShotGrid Version ID is required.')
    details = sg.find_one('Version', [['id', 'is', version_id]],
                          ['id', 'project', 'entity', 'sg_status_list'])
    if not details or details.get('id') != version_id:
        raise RemoteIdentityMismatch('ShotGrid Version is no longer available.')
    if project_id is not None:
        if _entity_id(details.get('project'), 'Project') != project_id:
            raise RemoteIdentityMismatch('ShotGrid project does not match the queued Version.')
    if entity is not None:
        if not isinstance(entity, dict) or details.get('entity') is None:
            raise RemoteIdentityMismatch('ShotGrid linked entity is missing.')
        actual = details['entity']
        if (str(actual.get('type')) != str(entity.get('type')) or
                actual.get('id') != entity.get('id')):
            raise RemoteIdentityMismatch('ShotGrid linked entity does not match the queued Version.')
    return details


def find_review_note_by_tag(sg, version_id, tag, page_size=100, max_pages=8):
    """Find a Note by an exact, durable FrameReviewer tag and Version link.

    Uses standard SG filters and bounded pagination; failure to verify uniqueness
    must stop publication, never permit an unchecked duplicate create. Return a
    minimal record, or ``None`` when all pages were checked with no match.
    """
    if not isinstance(tag, str) or not tag.startswith('[FrameReviewer-Review:'):
        raise ValueError('Expected a FrameReviewer review marker.')
    version = {'type': 'Version', 'id': int(version_id)}
    if version['id'] < 1 or page_size < 1 or max_pages < 1:
        raise ValueError('Invalid bounded ShotGrid search parameters.')
    found = None
    filters = [['note_links', 'in', [version]], ['content', 'contains', tag]]
    fields = ['id', 'subject', 'content', 'note_links']
    for page in range(1, max_pages + 1):
        rows = sg.find('Note', filters, fields, limit=page_size, page=page)
        if not isinstance(rows, (list, tuple)):
            raise RuntimeError('ShotGrid note lookup returned an invalid page.')
        for entry in rows:
            if (tag not in str(entry.get('content') or '') or not
                    any(link.get('type') == 'Version' and link.get('id') == version_id
                        for link in entry.get('note_links') or [])):
                continue
            if not isinstance(entry.get('id'), int) or entry['id'] < 1:
                raise RuntimeError('ShotGrid returned a Note without a valid ID.')
            if found is not None and found['id'] != entry['id']:
                raise DuplicateReviewTag('Multiple Notes share a review marker.')
            found = {'id': entry['id'], 'subject': entry.get('subject') or '',
                     'content': entry.get('content') or ''}
        if len(rows) < page_size:
            return found
    # A completely full last page is ambiguous: a matching Note may be beyond
    # the search budget, so it must not trigger a new create.
    raise RuntimeError('ShotGrid Note lookup reached its safety page limit.')


def create_review_note(sg, version, subject, content):
    """Create one Note linked to the verified Version and its project."""
    version_id = version['id']
    data = {'subject': str(subject), 'content': str(content),
            'note_links': [{'type': 'Version', 'id': version_id}]}
    project = version.get('project')
    if isinstance(project, dict) and project.get('type') == 'Project' and project.get('id'):
        data['project'] = {'type': 'Project', 'id': project['id']}
    created = sg.create('Note', data)
    if not isinstance(created, dict) or not isinstance(created.get('id'), int) or created['id'] < 1:
        raise RuntimeError('ShotGrid did not confirm the created Note ID.')
    return created['id']


def update_version_review_status(sg, version_id, status):
    """Set only Version.sg_status_list; never update unrelated status fields."""
    result = sg.update('Version', int(version_id), {'sg_status_list': str(status)})
    if not isinstance(result, dict) or result.get('id') != version_id:
        raise RuntimeError('ShotGrid did not confirm the Version status write.')
    return result


def _order(field, direction='asc'):
    return [{'field_name': field, 'direction': direction}]


def normalize_site(site_url):
    """Return a canonical ``https://site`` form, or raise if empty."""
    site = (site_url or '').strip().rstrip('/')
    if not site:
        raise ValueError('A ShotGrid / Flow site URL is required.')
    if '://' not in site:
        site = 'https://' + site
    return site


def toolkit_client():
    """Reuse the active Flow Toolkit/Desktop user without saving credentials."""
    try:
        import sgtk
        engine = sgtk.platform.current_engine()
        return getattr(engine, 'shotgun', None) if engine is not None else None
    except (ImportError, AttributeError, RuntimeError):
        return None


def is_available():
    return bool(AVAILABLE or toolkit_client() is not None)


def connect(site_url=None, script_name=None, api_key=None, timeout=30,
            provider=None):
    """Open and validate a script-authenticated connection.

    Validation is a single cheap authenticated call, so a bad site, script name
    or key fails here (with the server's own message) rather than later mid-UI.
    """
    credentials = (script_name or '').strip(), (api_key or '').strip()
    supplied = any(credentials)
    toolkit = (provider() if callable(provider) else provider)
    if toolkit is None and not supplied:
        toolkit = toolkit_client()
    if toolkit is not None:
        wanted = (site_url or '').strip()
        actual = str(getattr(toolkit, 'base_url', '') or '').strip()
        if wanted and actual and normalize_site(wanted) != normalize_site(actual):
            raise ValueError('Toolkit is authenticated to a different ShotGrid site.')
        return toolkit
    if not AVAILABLE:
        raise RuntimeError('shotgun_api3 is unavailable. Use the active Toolkit '
                           'user session or install the ShotGrid API in Maya.')
    site = normalize_site(site_url or os.environ.get('FRAMEREVIEWER_SHOTGRID_SITE'))
    script_name = (script_name or os.environ.get('FRAMEREVIEWER_SHOTGRID_SCRIPT_NAME')
                   or '').strip()
    api_key = (api_key or os.environ.get('FRAMEREVIEWER_SHOTGRID_API_KEY') or '').strip()
    if not script_name or not api_key:
        raise ValueError('Provide a session-only script key, or set studio '
                         'ShotGrid credentials in your environment.')
    sg = shotgun_api3.Shotgun(site, script_name=script_name, api_key=api_key,
                              connect=False)
    try:
        sg.config.timeout_secs = timeout
    except Exception:
        pass
    try:
        sg.add_user_agent(USER_AGENT)
    except Exception:
        pass
    sg.find_one('Project', [], ['id'])  # raises on bad credentials / site
    return sg


# --------------------------------------------------------------- queries
def projects(sg):
    return sg.find('Project', [], PROJECT_FIELDS, _order('name'))


def sequences(sg, project):
    return sg.find('Sequence', [['project', 'is', project]],
                   SEQUENCE_FIELDS, _order('code'))


def shots(sg, project, sequence=None):
    filters = [['project', 'is', project]]
    if sequence:
        filters.append(['sg_sequence', 'is', sequence])
    return sg.find('Shot', filters, SHOT_FIELDS, _order('code'))


def versions(sg, project, shot):
    return sg.find('Version',
                   [['project', 'is', project], ['entity', 'is', shot]],
                   VERSION_FIELDS, _order('created_at', 'desc'))


def notes(sg, entities):
    """Notes whose ``note_links`` point at any of ``entities`` (Version/Shot)."""
    refs = [{'type': e['type'], 'id': e['id']}
            for e in entities if e and e.get('id') and e.get('type')]
    if not refs:
        return []
    return sg.find('Note', [['note_links', 'in', refs]],
                   NOTE_FIELDS, _order('created_at', 'desc'))


# ---------------------------------------------------------- movie resolve
def cache_dir():
    base = os.path.join(tempfile.gettempdir(), 'FrameReview_ShotGrid_Cache')
    os.makedirs(base, exist_ok=True)
    return base


def _slug(text):
    return re.sub(r'[^A-Za-z0-9._-]+', '_', (text or '').strip()) or 'version'


def _cache_name(version, ext):
    # Hash the id so the cache folder never leaks which show/shot was reviewed,
    # while staying stable so a re-load reuses the already-downloaded file.
    digest = hashlib.sha1(str(version.get('id')).encode('utf-8')).hexdigest()[:16]
    return '%s_%s%s' % (_slug(version.get('code')), digest, ext or '.mov')


def version_movie_path(sg, version):
    """Resolve a Version to a playable local file.

    Prefers an on-disk ``sg_path_to_movie``; otherwise downloads the uploaded
    movie into the cache folder (reusing it if already present). Raises with a
    clear message when a Version carries no playable movie.
    """
    local = (version.get('sg_path_to_movie') or '').strip()
    if local and os.path.isfile(local):
        return local

    attachment = version.get('sg_uploaded_movie')
    if isinstance(attachment, dict) and attachment.get('id'):
        ext = os.path.splitext(attachment.get('name') or '')[1]
        dest = os.path.join(cache_dir(), _cache_name(version, ext))
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            return dest
        partial = dest + '.part'
        try:
            sg.download_attachment({'type': 'Attachment', 'id': attachment['id']},
                                   file_path=partial)
            os.replace(partial, dest)
        except Exception:
            if os.path.isfile(partial):
                try:
                    os.remove(partial)
                except OSError:
                    pass
            raise
        return dest

    if local:
        raise RuntimeError(
            'This Version points at a movie that is not reachable from this '
            'machine:\n%s' % local)
    raise RuntimeError('This Version has no movie (no sg_path_to_movie and no '
                       'uploaded movie).')


def thumbnail_bytes(url, timeout=20):
    """Fetch a thumbnail URL's bytes (ShotGrid serves pre-signed URLs)."""
    if not url:
        return None
    import urllib.request
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()
