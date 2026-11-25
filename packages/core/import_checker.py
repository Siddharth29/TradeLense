"""Import whitelist utility.

This module provides :func:`check_import_whitelist` which parses a source
string and ensures that only modules from a small, safe set are imported.
It raises :class:`ImportError` if any other module is imported.
"""

from __future__ import annotations

import ast
from typing import Set

# Modules that user provided strategy code is allowed to import.  Only the
# top-level package name is considered.
# SECURITY: "os" must never be whitelisted — generated strategy code runs
# with this gate as its first line of defense (2026-06-11).
_ALLOWED_MODULES: Set[str] = {"math", "statistics", "json", "datetime", "random", "numpy", "re"}


def check_import_whitelist(source: str) -> None:
    """Validate imports in *source* against a whitelist.

    Parameters
    ----------
    source:
        Python source code as a string.

    Raises
    ------
    ImportError
        If *source* contains an import statement for a module outside the
        whitelist.
    """

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name.split(".")[0]
                if module not in _ALLOWED_MODULES:
                    raise ImportError(f"import of module '{module}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            # Disallow relative imports (level > 0) and modules not in the list.
            if node.level != 0:
                raise ImportError("relative imports are not allowed")
            module = (node.module or "").split(".")[0]
            if module not in _ALLOWED_MODULES:
                raise ImportError(
                    f"import from module '{module or '<unknown>'}' is not allowed"
                )
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "now"
                and (
                    (isinstance(func.value, ast.Name) and func.value.id == "datetime")
                    or (
                        isinstance(func.value, ast.Attribute)
                        and isinstance(func.value.value, ast.Name)
                        and func.value.attr == "datetime"
                        and func.value.value.id == "datetime"
                    )
                )
            ):
                raise ImportError(
                    "use of datetime.now() is not allowed; use provided 'now'"
                )


__all__ = ["check_import_whitelist"]
