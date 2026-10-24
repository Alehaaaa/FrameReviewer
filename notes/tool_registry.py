# -*- coding: utf-8 -*-
"""Single source of truth for expandable annotation-tool UI metadata."""
from __future__ import absolute_import

from collections import namedtuple


ToolSpec = namedtuple(
    'ToolSpec',
    'id label icon tooltip default_size settings_title size_label max_size')

TOOL_SPECS = (
    ToolSpec('select', 'Select', 'select',
             'Select, marquee, move, scale, rotate notes',
             3, 'Selection', 'Size', 100),
    ToolSpec('brush', 'Brush', 'brush', 'Brush',
             6, 'Brush Tip', 'Size', 100),
    ToolSpec('eraser', 'Eraser', 'eraser', 'Eraser',
             24, 'Eraser Tip', 'Diameter', 150),
    ToolSpec('rectangle', 'Rectangle', 'rectangle', 'Rectangle',
             3, 'Rectangle Outline', 'Width', 100),
    ToolSpec('ellipse', 'Ellipse', 'ellipse', 'Ellipse',
             3, 'Ellipse Outline', 'Width', 100),
    ToolSpec('arrow', 'Arrow', 'arrow', 'Arrow',
             3, 'Arrow Outline', 'Width', 100),
    ToolSpec('text', 'Text', 'text',
             'Text: click to type on the image; click existing text to edit',
             36, 'Text', 'Size', 100),
)

TOOL_BY_ID = {spec.id: spec for spec in TOOL_SPECS}


def tool_defaults(include_select=False):
    return {spec.id: spec.default_size for spec in TOOL_SPECS
            if include_select or spec.id != 'select'}
