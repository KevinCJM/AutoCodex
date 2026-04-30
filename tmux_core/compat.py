from __future__ import annotations

import sys
from importlib import import_module
from types import ModuleType


def alias_module(current_name: str, target_name: str) -> ModuleType:
    module = import_module(target_name)
    sys.modules[current_name] = module
    return module
