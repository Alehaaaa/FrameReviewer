# -*- coding: utf-8 -*-
"""ID-based merge of local and incoming notes.

Pure Python. A remote (ShotGrid) or session refresh must not replace unsynced
local edits wholesale, so merging is keyed on the stable note ``id`` rather than
on list position or frame. The caller passes the set of locally *dirty* ids
(notes created or edited since the last sync); those are protected:

* A dirty note the remote also changed is kept in its local form and reported as
  a conflict, so the UI can let the reviewer choose.
* A dirty note the remote does not have is a new/unsynced local note and is
  kept.
* Everything else follows the incoming set, which is treated as the shared
  truth (including remote deletions of non-dirty notes).

Incoming order is preserved; surviving local-only notes are appended.
"""
from __future__ import absolute_import


# Fields that change on every save and must not, by themselves, count as a
# conflicting edit.
_VOLATILE = ('modified',)


def _stable(note):
    return {k: v for k, v in note.items() if k not in _VOLATILE}


def notes_differ(a, b):
    """True if two notes differ in anything but volatile timestamp fields."""
    return _stable(a) != _stable(b)


def merge_notes(local, incoming, dirty_ids=None):
    """Merge ``local`` and ``incoming`` note lists by id.

    Returns ``(merged, conflicts)``. ``conflicts`` is a list of
    ``{'id', 'local', 'remote'}`` for dirty notes the remote also changed.
    """
    dirty = set(dirty_ids or [])
    local_by = {n['id']: n for n in local if 'id' in n}

    merged = []
    conflicts = []
    seen = set()

    for remote in incoming:
        nid = remote.get('id')
        seen.add(nid)
        mine = local_by.get(nid)
        if nid in dirty and mine is not None:
            merged.append(mine)                 # protect the local edit
            if notes_differ(mine, remote):
                conflicts.append({'id': nid, 'local': mine, 'remote': remote})
        else:
            merged.append(remote)               # incoming is the shared truth

    for mine in local:
        nid = mine.get('id')
        if nid in seen:
            continue
        if nid in dirty:
            merged.append(mine)                 # new, unsynced local note
        # Otherwise the remote dropped it and it was not locally edited: let
        # the deletion stand.

    return merged, conflicts
