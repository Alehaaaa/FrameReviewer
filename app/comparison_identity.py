# -*- coding: utf-8 -*-
"""Qt-independent logical Source resolution for review/edit compositions.

Never equate media filename with reviewed Source identity: independent edit
events may share a movie path but carry different source-frame trims, color
interpretation, annotations, and ShotGrid IDs.
"""
from __future__ import absolute_import

import os


def resolve_comparison_ids(paths, mode, panel, controller):
    """Return one immutable identity per playlist row in the supplied order.

    The panel supplies selected row IDs for comparisons, or all row IDs for
    sequence/record timelines. Fallback matching consumes duplicate paths in
    order; an unseen path remains unresolved, never aliases a prior ID.
    """
    if panel is not None:
        ids = (panel.source_ids() if mode == 'sequence'
               else panel.effective_source_ids())
        if len(ids) == len(paths):
            matched = True
            for source_id, path in zip(ids, paths):
                source = controller.session.source(source_id)
                resolved = controller.resolved_path(source) if source else None
                if not resolved or os.path.normcase(os.path.normpath(resolved)) != \
                        os.path.normcase(os.path.normpath(path)):
                    matched = False
                    break
            if matched:
                return list(ids)
    candidates = list(controller.session.sources)
    result = []
    for path in paths:
        key = os.path.normcase(os.path.normpath(path))
        match = next((source for source in candidates
                      if controller.resolved_path(source) and
                      os.path.normcase(os.path.normpath(
                          controller.resolved_path(source))) == key), None)
        if match is None:
            result.append(path)
        else:
            candidates.remove(match)
            result.append(match.id)
    return result
