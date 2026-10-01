"""On-disk cache for transcriptions and judgments.

Keys are hashes of whatever determines the answer (stroke IDs, model, prompt
version), so a line is only sent again when its strokes or the request change.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class AICache:
    def __init__(self, root: Path):
        self.root = root

    @staticmethod
    def key(*parts: Any) -> str:
        blob = json.dumps(parts, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:32]

    def _path(self, kind: str, key: str) -> Path:
        return self.root / kind / f"{key}.json"

    def get(self, kind: str, key: str) -> Any | None:
        p = self._path(kind, key)
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return None

    def put(self, kind: str, key: str, value: Any) -> None:
        p = self._path(kind, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value, indent=2))
