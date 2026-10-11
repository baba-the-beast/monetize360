"""Generate keys (secrets.token_urlsafe), store SHA-256 of secret, constant-time verify, 60s in-memory cache, evict on
key_revoked event.

Key format:  m360_<env>_<key_id>_<secret>      e.g.  m360_live_K7Q2M9XB4T_8fJ2...
  key_id  10 chars A-Z2-9, public: it is how the key is looked up and what logs show
  secret  43 chars (256 random bits), shown ONCE at creation; only its SHA-256 is stored

SHA-256 (not bcrypt) is right here: the secret is 256 random bits, so there is nothing to brute-force, and
verification stays fast enough for every request. Slow hashes exist for weak human passwords."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from monetize360.security.principal import ROLES, Principal

PREFIX = "m360"
ENVS = ("live", "test")
_KEY_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no 0/O/1/I: read aloud without mistakes
_KEY = re.compile(r"m360_(live|test)_([A-Z2-9]{10})_([A-Za-z0-9_-]{43})")
MAX_CACHE = 10_000


class KeyStoreUnavailable(Exception):
    """The key store can't be reached and this key has never been seen by this replica."""


@dataclass(frozen=True)
class KeyRecord:
    """One row of api_keys. The secret itself is never here, only its hash."""
    key_id: str
    tenant_id: str
    secret_sha256: bytes
    role: str
    domains: tuple[str, ...]
    rate_limit_rps: int = 200
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


def hash_secret(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("ascii")).digest()


def generate(env: str = "live") -> tuple[str, str, bytes]:
    """Returns (key_id, api_key, secret_sha256). Show api_key to the user once; store only the other two."""
    if env not in ENVS:
        raise ValueError(f"env must be one of {ENVS}")
    key_id = "".join(secrets.choice(_KEY_ID_ALPHABET) for _ in range(10))
    secret = secrets.token_urlsafe(32)
    return key_id, f"{PREFIX}_{env}_{key_id}_{secret}", hash_secret(secret)


def parse(api_key: str) -> tuple[str, str] | None:
    """(key_id, secret), or None when the text isn't shaped like one of our keys."""
    m = _KEY.fullmatch(api_key) if isinstance(api_key, str) else None
    return (m.group(2), m.group(3)) if m else None


class KeyVerifier:
    """Turns an API key into a Principal. Records are cached for `ttl` seconds so the hot path doesn't hit the
    database on every request; `evict` drops one immediately (called when a key is revoked). Without a shared
    revocation event (Redis, M5), another replica keeps a revoked key for at most `ttl` seconds.
    If the store is down, keys already seen keep working (stale) and unseen keys raise KeyStoreUnavailable."""

    def __init__(self, lookup: Callable[[str], KeyRecord | None], *, ttl: float = 60.0,
                 monotonic: Callable[[], float] = time.monotonic):
        self._lookup, self._ttl, self._monotonic = lookup, ttl, monotonic
        self._cache: dict[str, tuple[float, KeyRecord | None]] = {}

    def evict(self, key_id: str) -> None:
        self._cache.pop(key_id, None)

    def _record(self, key_id: str) -> KeyRecord | None:
        now = self._monotonic()
        hit = self._cache.get(key_id)
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        try:
            record = self._lookup(key_id)
        except Exception as e:                   # noqa: BLE001 - any store failure: database down, pool timeout...
            if hit is not None:
                return hit[1]                    # keep pricing for keys we already know (ARCHITECTURE §12)
            raise KeyStoreUnavailable(str(e)) from None
        if len(self._cache) >= MAX_CACHE:        # bounded: a flood of made-up key ids can't grow memory forever
            self._cache.clear()
        self._cache[key_id] = (now, record)      # unknown ids are cached too, so they don't hammer the database
        return record

    def verify(self, api_key: str, now: datetime | None = None) -> Principal | None:
        parsed = parse(api_key)
        if parsed is None:
            return None
        key_id, secret = parsed
        record = self._record(key_id)
        # always compare, even for an unknown id, so a wrong id and a wrong secret take the same path
        stored = record.secret_sha256 if record is not None else b"\x00" * 32
        if not hmac.compare_digest(hash_secret(secret), bytes(stored)) or record is None:
            return None
        now = now or datetime.now(timezone.utc)
        if record.revoked_at is not None or (record.expires_at is not None and record.expires_at <= now):
            return None
        if record.role not in ROLES:
            return None
        return Principal(tenant_id=record.tenant_id, key_id=record.key_id, role=record.role,
                         domains=frozenset(record.domains))

    def rate_limit_rps(self, key_id: str) -> int | None:
        hit = self._cache.get(key_id)
        return hit[1].rate_limit_rps if hit is not None and hit[1] is not None else None
