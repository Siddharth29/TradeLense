"""Basic file-template prompt builder used by legacy integrations."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, Tuple

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_prompt(asset: str, agent_config: Dict[str, str]) -> Tuple[str, str, Dict[str, str]]:
    """Build a minimal public prompt from safe, editable templates."""
    base = (TEMPLATES_DIR / "base.txt").read_text(encoding="utf-8")
    fragment_path = TEMPLATES_DIR / "instrument.txt"
    fragment = fragment_path.read_text(encoding="utf-8")
    values = {"asset": asset, **agent_config}
    prompt = f"{base.format(**values)}\n{fragment.format(**values)}".strip()
    return prompt, _hash(prompt), {"asset": asset, "template": "public-basic"}


__all__ = ["build_prompt"]

