# -*- coding: utf-8 -*-
"""Review-note data, persistence, and tool definitions.

This package deliberately has no Qt dependency.  The viewport owns rendering
and interaction; notes owns the portable data contract used by the viewport,
undo stack, import/export, and future integrations.
"""
from __future__ import absolute_import

from .model import normalize_drawings, copy_drawings, copy_strokes
from .serialization import build_payload, parse_payload, read_payload, write_payload

__all__ = [
    'build_payload', 'copy_drawings', 'copy_strokes', 'normalize_drawings',
    'parse_payload', 'read_payload', 'write_payload',
]
