# SPDX-FileCopyrightText: 2026 madan6557
# SPDX-License-Identifier: GPL-3.0-or-later

import importlib as _importlib
import sys as _sys


_TOOLKIT_MODULE_NAME = __name__ + ".curve_toolkit"
_EXCLUDED_EXPORTS = {"register", "unregister"}


def _load_toolkit_module(force_reload=False):
    module = _sys.modules.get(_TOOLKIT_MODULE_NAME)
    if module is None:
        module = _importlib.import_module(_TOOLKIT_MODULE_NAME)
    elif force_reload:
        module = _importlib.reload(module)
    return module


def _sync_public_exports(module):
    for name in list(globals()):
        if name.startswith("_") or name in _EXCLUDED_EXPORTS:
            continue
        globals().pop(name, None)

    for name in dir(module):
        if name.startswith("_") or name in _EXCLUDED_EXPORTS:
            continue
        globals()[name] = getattr(module, name)


_toolkit_module = _load_toolkit_module(force_reload=True)
_sync_public_exports(_toolkit_module)


def register():
    module = _load_toolkit_module(force_reload=True)
    _sync_public_exports(module)
    module.register()


def unregister():
    module = _sys.modules.get(_TOOLKIT_MODULE_NAME)
    if module is not None:
        module.unregister()


def __getattr__(name):
    module = _sys.modules.get(_TOOLKIT_MODULE_NAME)
    if module is None:
        module = _load_toolkit_module()
    return getattr(module, name)
