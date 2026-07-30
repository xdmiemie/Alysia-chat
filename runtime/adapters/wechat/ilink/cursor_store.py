from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile


class CursorStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> str:
        if not self.path.exists():
            return ""
        return self.path.read_text(encoding="utf-8-sig").strip()

    def commit(self, cursor: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.path.parent,
            delete=False,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        ) as temp_file:
            temp_file.write(cursor)
            temp_name = temp_file.name
        os.replace(temp_name, self.path)

    def reset(self) -> None:
        if self.path.exists():
            self.path.unlink()
