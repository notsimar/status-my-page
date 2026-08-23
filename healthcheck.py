#!/usr/bin/env python3
"""Healthcheck module for status-my-page.

Compatibility entry point: the implementation lives in
``statuspage/_healthcheck_impl.py`` but is loaded under THIS module's name
(``healthcheck``) so there is exactly ONE module object. Tests and tools that
monkeypatch ``healthcheck._BASE_DIR`` (and friends) therefore patch the very
globals the implementation reads, and ``import healthcheck`` keeps working
everywhere. New code may equally use ``statuspage.healthcheck``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_IMPL_PATH = Path(__file__).resolve().parent / "statuspage" / "_healthcheck_impl.py"

_spec = importlib.util.spec_from_file_location(__name__, _IMPL_PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover - packaging error
    raise ImportError(f"cannot load healthcheck implementation from {_IMPL_PATH}")
_mod = importlib.util.module_from_spec(_spec)
sys.modules[__name__] = _mod
_spec.loader.exec_module(_mod)
