"""Utility to clean invalid remote-tracking references.

This repository has previously encountered situations where corrupted
remote-tracking branches (for example entries created under
``.git/refs/remotes/origin`` that do not contain a valid 40 character
SHA-1) break Git commands with errors such as::

    fatal: bad object refs/remotes/origin/...

Running this script removes those corrupted references and prunes any
now-empty directories so Git commands succeed again.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Iterable

_HEX_RE = re.compile(r"^[0-9a-f]{40}$")


def _iter_ref_files(root: Path) -> Iterable[Path]:
    """Yield every file below *root* that could contain a ref."""

    if not root.exists():
        return []

    return (p for p in root.rglob("*") if p.is_file())


def _is_valid_ref_contents(contents: str) -> bool:
    """Return ``True`` if *contents* look like a valid Git reference."""

    text = contents.strip()
    return bool(text) and _HEX_RE.match(text) is not None


def _cleanup_empty_dirs(root: Path) -> None:
    """Remove empty directories under *root* (bottom-up)."""

    for path in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        try:
            path.rmdir()
        except OSError:
            # Directory still contains files or is otherwise in use; leave it.
            pass


def clean_invalid_remote_refs(repo_root: Path) -> list[Path]:
    """Delete remote-tracking refs with invalid object ids.

    Returns a list of paths (relative to ``.git/refs/remotes``) that were
    removed.
    """

    git_dir = repo_root / ".git"
    refs_root = git_dir / "refs" / "remotes"
    removed: list[Path] = []

    for ref_file in _iter_ref_files(refs_root):
        try:
            contents = ref_file.read_text(encoding="utf-8")
        except OSError:
            # If we cannot read the file, err on the side of removing it so it
            # does not keep breaking Git commands.
            contents = ""

        if not _is_valid_ref_contents(contents):
            rel_path = ref_file.relative_to(refs_root)
            try:
                ref_file.unlink()
            except OSError as exc:  # pragma: no cover - defensive logging
                print(f"Failed to remove invalid ref {rel_path}: {exc}", file=sys.stderr)
            else:
                removed.append(rel_path)

    _cleanup_empty_dirs(refs_root)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Remove invalid remote Git refs")
    parser.add_argument(
        "repo",
        nargs="?",
        default=".",
        help="Path to the repository root (defaults to current directory)",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo).resolve()
    git_dir = repo_root / ".git"

    if not git_dir.exists():
        print(f"No .git directory found at {git_dir}", file=sys.stderr)
        return os.EX_USAGE

    removed = clean_invalid_remote_refs(repo_root)

    if not removed:
        print("No invalid remote refs found.")
        return os.EX_OK

    print("Removed invalid remote refs:")
    for path in removed:
        print(f"  {path}")

    return os.EX_OK


if __name__ == "__main__":
    sys.exit(main())
