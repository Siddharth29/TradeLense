from __future__ import annotations

from pathlib import Path

from scripts.cleanup_git_refs import clean_invalid_remote_refs


def _write_ref(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def test_cleanup_removes_invalid_refs(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path
    git_dir = repo_root / ".git"
    refs_root = git_dir / "refs" / "remotes" / "origin"
    git_dir.mkdir()

    # Valid ref should remain untouched.
    valid_ref = refs_root / "main"
    _write_ref(valid_ref, "1234567890abcdef1234567890abcdef12345678")

    # Invalid ref should be deleted.
    bad_ref = refs_root / "codex" / "__pycache__" / "add-lru-cache-in-configs.cpython-312.pyc"
    _write_ref(bad_ref, "0000000")

    removed = clean_invalid_remote_refs(repo_root)

    assert bad_ref.relative_to(refs_root.parent) in removed
    assert not bad_ref.exists()

    # The valid ref should still exist.
    assert valid_ref.exists()

    # Empty parent directories were cleaned up (origin/codex removed but origin remains).
    assert not (refs_root / "codex").exists()
    assert refs_root.exists()


def test_cleanup_handles_missing_git_dir(tmp_path: Path, monkeypatch) -> None:
    # When .git does not exist, no refs should be removed and the call should not fail.
    removed = clean_invalid_remote_refs(tmp_path)
    assert removed == []
