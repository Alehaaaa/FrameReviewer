# -*- coding: utf-8 -*-
"""Flow Production Tracking / ShotGrid data layer.

Pure data: a thin, Qt-free wrapper over the public ``shotgun_api3`` client so
the UI (shotgrid_ui) and any future headless caller share one definition of the
schema. Authentication is script-key based (a registered Script entity's name
and API key), which suits a pipeline install with no per-user password prompt.

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


def connect(site_url, script_name, api_key, timeout=30):
    """Open and validate a script-authenticated connection.

    Validation is a single cheap authenticated call, so a bad site, script name
    or key fails here (with the server's own message) rather than later mid-UI.
    """
    if not AVAILABLE:
        raise RuntimeError(
            'shotgun_api3 is not importable: %s\n\nInstall it into Maya '
            "(e.g. mayapy -m pip install shotgun_api3) or add it to sys.path."
            % (IMPORT_ERROR or 'module not found'))
    site = normalize_site(site_url)
    script_name = (script_name or '').strip()
    api_key = (api_key or '').strip()
    if not script_name or not api_key:
        raise ValueError('Both a script name and an API key are required.')
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
