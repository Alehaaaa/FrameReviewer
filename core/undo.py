# -*- coding: utf-8 -*-
"""A plain undo/redo stack of paired callables.

Each step is an (undo, redo) pair plus an optional frame to jump to first, so a
drawing edit can return to the frame it happened on before it is reverted. The
window builds the closures; this class only sequences them and caps history.
"""
from __future__ import absolute_import


class UndoStack(object):
    def __init__(self, limit=100):
        self._done = []
        self._undone = []
        self._limit = int(limit)

    def push(self, undo, redo, frame=None):
        self._done.append((undo, redo, frame))
        if len(self._done) > self._limit:
            self._done = self._done[-self._limit:]
        self._undone = []

    def undo(self):
        if not self._done:
            return False
        undo, redo, frame = self._done.pop()
        undo()
        self._undone.append((undo, redo, frame))
        return True

    def redo(self):
        if not self._undone:
            return False
        undo, redo, frame = self._undone.pop()
        redo()
        self._done.append((undo, redo, frame))
        return True

    def clear(self):
        self._done = []
        self._undone = []

    @property
    def can_undo(self):
        return bool(self._done)

    @property
    def can_redo(self):
        return bool(self._undone)
