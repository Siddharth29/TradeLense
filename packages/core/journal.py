from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


class AssetJournal:
    """Append-only JSONL journal for an individual asset.

    Entries are buffered in-memory and flushed to ``{asset}_{YYYYMMDD}.jsonl``
    files.  A new file is created whenever the UTC date changes.
    """

    def __init__(
        self,
        root: str | Path,
        asset_id: str,
        agent_name: str,
        buffer_size: int = 1,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.asset_id = asset_id
        self.agent_name = agent_name
        self.buffer_size = int(buffer_size)
        self._buffer: list[dict[str, Any]] = []
        self._date: str | None = None

    # internal -------------------------------------------------------------
    def _serialize(self, obj: Any) -> Mapping[str, Any]:
        if is_dataclass(obj):
            return asdict(obj)
        if isinstance(obj, Mapping):
            return dict(obj)
        raise TypeError("unsupported type for serialization")

    def _ensure_date(self, date_str: str) -> None:
        if self._date != date_str:
            self.flush()
            self._date = date_str

    def _append(
        self,
        kind: str,
        payload: Any,
        prompt_hash: str,
        gpt_mode: str,
        latency_ms: float,
    ) -> None:
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y%m%d")
        self._ensure_date(date_str)

        rec: dict[str, Any] = {
            "timestamp": now.isoformat(),
            "type": kind,
            "asset_id": self.asset_id,
            "agent_name": self.agent_name,
            "prompt_hash": prompt_hash,
            "gpt_mode": gpt_mode,
            "latency_ms": latency_ms,
        }
        rec.update(self._serialize(payload))
        self._buffer.append(rec)
        if len(self._buffer) >= self.buffer_size:
            self.flush()

    # public ---------------------------------------------------------------
    def log_decision(
        self,
        decision: Any,
        prompt_hash: str,
        gpt_mode: str,
        latency_ms: float,
    ) -> None:
        self._append("decision", decision, prompt_hash, gpt_mode, latency_ms)

    def log_order(
        self,
        order: Any,
        prompt_hash: str,
        gpt_mode: str,
        latency_ms: float,
    ) -> None:
        self._append("order", order, prompt_hash, gpt_mode, latency_ms)

    def log_fill(
        self,
        fill: Any,
        prompt_hash: str,
        gpt_mode: str,
        latency_ms: float,
    ) -> None:
        self._append("fill", fill, prompt_hash, gpt_mode, latency_ms)

    def log_position(
        self,
        position: Any,
        prompt_hash: str,
        gpt_mode: str,
        latency_ms: float,
    ) -> None:
        self._append("position", position, prompt_hash, gpt_mode, latency_ms)

    def log_action(
        self,
        action: Any,
        prompt_hash: str,
        gpt_mode: str,
        latency_ms: float,
    ) -> None:
        self._append("action", action, prompt_hash, gpt_mode, latency_ms)

    def log_gpt_response(
        self,
        gpt_response: Any,
        prompt_hash: str,
        gpt_mode: str,
        latency_ms: float,
    ) -> None:
        self._append("gpt_response", gpt_response, prompt_hash, gpt_mode, latency_ms)

    def log_trigger_check(
        self,
        trigger_check: Any,
        prompt_hash: str,
        gpt_mode: str,
        latency_ms: float,
    ) -> None:
        self._append("trigger_check", trigger_check, prompt_hash, gpt_mode, latency_ms)

    def flush(self) -> None:
        if not self._buffer or self._date is None:
            return
        path = self.root / f"{self.asset_id}_{self._date}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            for rec in self._buffer:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._buffer.clear()


__all__ = ["AssetJournal"]
