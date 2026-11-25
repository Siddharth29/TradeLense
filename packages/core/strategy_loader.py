"""Utilities for loading and executing strategy code snippets."""

from __future__ import annotations

import builtins
import random
from functools import lru_cache
from hashlib import sha256
from types import CodeType
from typing import Dict, List, cast

import numpy as _np

from .strategy_protocol import DecideFunc
from .decision_normalizer import Decision, normalize_decision
# Import checker removed - allow all imports

__all__ = ["Decision", "run_strategy_code", "compile_counter"]


# Compilation cache ---------------------------------------------------------
compile_counter = 0


def _forbidden_open(*args, **kwargs):  # pragma: no cover - trivial
    """Replacement for :func:`open` that forbids file I/O."""
    raise PermissionError("file I/O disabled")


_SAFE_BUILTINS = {**builtins.__dict__, "open": _forbidden_open}


@lru_cache(maxsize=128)
def _get_code(src_hash: str, source: str) -> CodeType:
    """Compile *source* and cache the resulting code object."""
    global compile_counter
    compile_counter += 1
    return compile(source, "<strategy>", "exec")


# Public API ---------------------------------------------------------------

def run_strategy_code(source: str, candles_5: List[Dict], now: str) -> Decision:
    """Execute strategy ``source`` and return a :class:`Decision`.

    The ``source`` must define a ``decide`` function matching
    :data:`~packages.core.strategy_protocol.DecideFunc`.

    To ensure deterministic behaviour across runs, calls to ``random`` and
    ``numpy.random`` are only allowed after seeding.  Both modules are seeded
    with ``0`` before the strategy code executes and any subsequent attempt to
    use the generators without seeding will raise :class:`RuntimeError`.
    """

    # Enforce the same import whitelist as the production executor path.
    from .import_checker import check_import_whitelist
    check_import_whitelist(source)
    src_hash = sha256(source.encode("utf-8")).hexdigest()
    code = _get_code(src_hash, source)

    # ------------------------------------------------------------------
    # Patch ``random`` and ``numpy.random`` to forbid unseeded calls.
    rand_orig_random = random.random
    rand_orig_seed = random.seed
    rand_seeded = {"flag": False}

    def _rand_seed(value=None, *a, **k):
        if value is None:
            raise RuntimeError("random.seed() requires an explicit value")
        rand_seeded["flag"] = True
        return rand_orig_seed(value, *a, **k)

    def _rand_random(*a, **k):
        if not rand_seeded["flag"]:
            raise RuntimeError("random generator used before seeding")
        return rand_orig_random(*a, **k)

    np_rand = _np.random
    np_orig_seed = np_rand.seed
    np_seeded = {"flag": False}
    np_wrapped_fns = {}

    def _np_seed(value=None, *a, **k):
        if value is None:
            raise RuntimeError("numpy.random.seed() requires a value")
        np_seeded["flag"] = True
        return np_orig_seed(value, *a, **k)

    def _wrap_np_fn(fn_name):
        fn = getattr(np_rand, fn_name)

        def wrapper(*a, **k):
            if not np_seeded["flag"]:
                raise RuntimeError("numpy.random generator used before seeding")
            return fn(*a, **k)

        np_wrapped_fns[fn_name] = fn
        setattr(np_rand, fn_name, wrapper)

    for _fn in ["rand", "random", "randn", "integers"]:
        if hasattr(np_rand, _fn):
            _wrap_np_fn(_fn)

    random.seed = _rand_seed
    random.random = _rand_random
    np_rand.seed = _np_seed

    # Seed deterministically before executing strategy code.
    random.seed(0)
    np_rand.seed(0)

    env: Dict[str, object] = {"__builtins__": _SAFE_BUILTINS}
    try:
        exec(code, env, env)

        decide = env.get("decide")
        if not callable(decide):  # pragma: no cover - defensive
            raise AttributeError("Strategy source missing 'decide' callable")

        decide_func = cast(DecideFunc, decide)
        result = decide_func(candles_5, now)
        return normalize_decision(result)
    finally:  # restore patched functions
        random.random = rand_orig_random
        random.seed = rand_orig_seed
        np_rand.seed = np_orig_seed
        for name, fn in np_wrapped_fns.items():
            setattr(np_rand, name, fn)
