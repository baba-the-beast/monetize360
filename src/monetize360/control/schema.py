"""Gate 1: validate a pack dict against packs/schema/pack.schema.json (path from config.settings()); return every
error with its path. Used by PUT /v1/packs, the linter and tests. The engine never reads the schema."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from monetize360.config import settings


@lru_cache(maxsize=4)
def _validator(path: str) -> Draft202012Validator:
    schema = json.loads(Path(path).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate(pack: Any, schema_path: str | Path | None = None) -> list[dict[str, str]]:
    """All schema errors as [{path, message}], sorted by path. Empty list = valid shape."""
    validator = _validator(str(schema_path or settings().pack_schema_path))
    errors = [{"path": "/".join(str(p) for p in e.absolute_path) or "(root)", "message": e.message}
              for e in validator.iter_errors(pack)]
    return sorted(errors, key=lambda e: e["path"])
