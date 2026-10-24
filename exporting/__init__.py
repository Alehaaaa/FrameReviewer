# -*- coding: utf-8 -*-
"""Media export UI and jobs.

The Qt-bound controller is imported lazily so Qt-free submodules (such as
``burnin``) can be imported and unit tested without a Qt binding present.
"""

__all__ = ['ExportJob', 'ExportOptionsDialog']


def __getattr__(name):  # lazy, so importing the package never forces Qt early
    if name in ('ExportJob', 'ExportOptionsDialog'):
        from .controller import ExportJob, ExportOptionsDialog
        return {'ExportJob': ExportJob,
                'ExportOptionsDialog': ExportOptionsDialog}[name]
    raise AttributeError(name)
