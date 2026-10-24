# -*- coding: utf-8 -*-
"""Top-level workspace tool registry."""
from __future__ import absolute_import


WORKSPACE_TOOLS = (
    ('media', 'Media', 'Media and source information (V)'),
    ('playlist', 'Playlist', 'Playlist and multi-clip composition (P)'),
    ('draw', 'Draw', 'Draw review annotations (D)'),
    ('review', 'Review', 'Review overlays and display tools (R)'),
    ('crop', 'Crop', 'Crop tool (C)'),
    ('track', 'Track', 'Track the crop forward (T)'),
    ('export', 'Export', 'Export and output settings (X)'),
    ('shotgrid', 'Flow / ShotGrid', 'Flow / ShotGrid review versions (G)'),
)

__all__ = ['WORKSPACE_TOOLS']
