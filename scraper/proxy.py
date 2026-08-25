"""Simple proxy pool: reads a file (host:port or host:port:user:pass, one per line)
and hands them out round-robin."""
from __future__ import annotations

from itertools import cycle
from pathlib import Path


class ProxyPool:
    def __init__(self, proxies: list[str]):
        self._proxies = [p.strip() for p in proxies if p.strip() and not p.strip().startswith("#")]
        self._cycle = cycle(self._proxies) if self._proxies else None

    @classmethod
    def from_file(cls, path: str | Path) -> "ProxyPool":
        p = Path(path)
        if not p.exists():
            return cls([])
        return cls(p.read_text(encoding="utf-8").splitlines())

    def __bool__(self) -> bool:
        return bool(self._proxies)

    def __len__(self) -> int:
        return len(self._proxies)

    def next(self) -> str | None:
        if not self._cycle:
            return None
        return next(self._cycle)
