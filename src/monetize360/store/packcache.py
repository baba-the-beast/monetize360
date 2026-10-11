"""In-memory {(tenant, domain): CompiledPack}; atomic swap on publish event, 10s poll backstop, keep last-known-
good on load failure.

Sources: a directory of YAML files (`load_directory`, dev tenant only) or the live packs in Postgres
(`load_rows`). M5 adds the publish event and the poll; they call the same `put`, so `get` doesn't change."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from monetize360.control.schema import validate
from monetize360.engine import PackInvalid, compile_pack
from monetize360.engine.models import CompiledPack


@dataclass(frozen=True)
class LivePack:
    compiled: CompiledPack
    version: int


class PackCache:
    def __init__(self) -> None:
        self._packs: dict[tuple[str, str], LivePack] = {}
        self.load_errors: dict[tuple[str, str], str] = {}

    def get(self, tenant_id: str, domain: str) -> LivePack | None:
        return self._packs.get((tenant_id, domain))

    def __len__(self) -> int:
        return len(self._packs)

    def put(self, tenant_id: str, pack: Any, version: int, *, name: str | None = None) -> bool:
        """Validate (schema, then compile) and swap in. On failure the previous version stays live
        (last known good) and the reason is kept in `load_errors`. Returns True when the new pack is live."""
        label = name or (pack.get("domain") if isinstance(pack, dict) else None) or "?"
        errors = validate(pack)
        if not errors:
            try:
                compiled = compile_pack(pack)
            except PackInvalid as e:
                errors = e.details or [{"path": "(pack)", "message": e.message}]
        if errors:
            first = errors[0]
            self.load_errors[(tenant_id, label)] = f"{first['path']}: {first['message']}"
            return False
        key = (tenant_id, compiled.domain)
        self._packs[key] = LivePack(compiled, version)       # one assignment: readers see old or new, never half
        self.load_errors.pop(key, None)
        self.load_errors.pop((tenant_id, label), None)
        return True

    def load_directory(self, directory: Path, tenant_id: str, version: int = 1) -> int:
        """Load every *.yaml in `directory` as a live pack of `tenant_id`. Returns how many went live."""
        loaded = 0
        for path in sorted(Path(directory).glob("*.yaml")):
            try:
                pack = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as e:
                self.load_errors[(tenant_id, path.stem)] = f"can't read {path.name}: {e}"
                continue
            loaded += self.put(tenant_id, pack, version, name=path.stem)
        return loaded

    def load_rows(self, rows: list[dict]) -> int:
        """Load live packs read from Postgres ([{tenant_id, domain, version, yaml}]). Returns how many went live."""
        loaded = 0
        for row in rows:
            try:
                pack = yaml.safe_load(row["yaml"])
            except yaml.YAMLError as e:
                self.load_errors[(row["tenant_id"], row["domain"])] = f"stored YAML can't be parsed: {e}"
                continue
            if not isinstance(pack, dict) or pack.get("domain") != row["domain"]:
                # never serve a pack under a different name than the one it was stored and approved as
                self.load_errors[(row["tenant_id"], row["domain"])] = "stored pack's domain doesn't match its row"
                continue
            loaded += self.put(row["tenant_id"], pack, row["version"], name=row["domain"])
        return loaded

    def sync_rows(self, rows: list[dict]) -> int:
        """Bring the cache in line with the live packs read from Postgres: load only what is new or changed.
        A version that fails to load leaves the previous one in place. Returns how many were swapped in."""
        changed = [r for r in rows
                   if (current := self.get(r["tenant_id"], r["domain"])) is None or current.version != r["version"]]
        return self.load_rows(changed)
