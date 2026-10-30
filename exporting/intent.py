# -*- coding: utf-8 -*-
"""Immutable export request construction shared by UI delivery paths."""
from __future__ import absolute_import

from dataclasses import dataclass
import os

from .spec import ExportSpec


@dataclass(frozen=True)
class ExportIntent(object):
    context: dict
    spec: ExportSpec


def build_export_intents(snapshot, paths, stage, source_path, source_id=''):
    """Freeze validated output contexts before any job is started."""
    intents = []
    for path in paths:
        context = dict(
            snapshot, path=os.path.abspath(path), source_id=source_id,
            source_path=source_path, sequence=('.####.' in path),
            color_stage=stage)
        spec = ExportSpec.from_context(
            context, source_path=source_path, source_id=source_id)
        intents.append(ExportIntent(context, spec))
    if not intents:
        raise ValueError('Select at least one export output.')
    return tuple(intents)


__all__ = ['ExportIntent', 'build_export_intents']
