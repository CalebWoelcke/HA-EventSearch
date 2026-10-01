"""Make the HA-free ``scout`` package importable as ``scout`` without Home Assistant.

The integration folder is deliberately *not* put on sys.path: it contains
``calendar.py``, which would shadow the standard library module.
"""

import importlib.util
from pathlib import Path
import sys

_PKG = Path(__file__).resolve().parents[1] / "custom_components" / "local_event_scout" / "scout"
_spec = importlib.util.spec_from_file_location("scout", _PKG / "__init__.py", submodule_search_locations=[str(_PKG)])
_module = importlib.util.module_from_spec(_spec)
sys.modules["scout"] = _module
_spec.loader.exec_module(_module)
