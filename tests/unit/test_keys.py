"""Hashing, constant-time verify, scope checks, revocation evicts cache."""
from datetime import datetime, timedelta, timezone

import pytest

from monetize360.security import keys
from monetize360.security.keys import KeyRecord, KeyStoreUnavailable, KeyVerifier
from monetize360.security.ratelimit import RateLimiter

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


class Store:
    """A fake api_keys table that counts lookups and can be switched off."""

    def __init__(self):
        self.rows: dict[str, KeyRecord] = {}
        self.lookups = 0
        self.down = False

    def issue(self, tenant="hotel_001", role="pricing", domains=("hotel",), **extra) -> str:
        key_id, api_key, secret_hash = keys.generate("test")
        self.rows[key_id] = KeyRecord(key_id, tenant, secret_hash, role, tuple(domains), **extra)
        return api_key

    def lookup(self, key_id):
        self.lookups += 1
        if self.down:
            raise ConnectionError("database is down")
        return self.rows.get(key_id)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def store():
    return Store()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def verifier(store, clock):
    return KeyVerifier(store.lookup, ttl=60, monotonic=clock)


# ---------------------------------------------------------------- format and hashing

def test_generated_key_shape_and_only_the_hash_is_kept():
    key_id, api_key, secret_hash = keys.generate("live")
    prefix, env, kid, secret = api_key.split("_", 3)
    assert (prefix, env, kid) == ("m360", "live", key_id)
    assert len(key_id) == 10 and len(secret) == 43 and len(secret_hash) == 32
    assert secret_hash == keys.hash_secret(secret)
    assert secret.encode() not in secret_hash and keys.parse(api_key) == (key_id, secret)


def test_keys_are_unique_and_unguessable():
    made = {keys.generate()[1] for _ in range(200)}
    assert len(made) == 200


@pytest.mark.parametrize("bad", [
    "", "Bearer x", "m360_live_SHORT_secret", "m360_prod_ABCDEFGHJK_" + "a" * 43, "m360_live_ABCDEFGHJK_" + "a" * 42,
    "m360_live_abcdefghjk_" + "a" * 43, "m360_live_ABCDEFGHJK_" + "a" * 42 + "!", "x" * 5000,
    "m360_live_ABCDEFGHJK_" + "a" * 43 + "\n", None, 12345,
])
def test_malformed_keys_are_rejected_without_touching_the_store(store, verifier, bad):
    assert keys.parse(bad) is None
    assert verifier.verify(bad, NOW) is None
    assert store.lookups == 0


def test_env_must_be_live_or_test():
    with pytest.raises(ValueError, match="env"):
        keys.generate("prod")


# ---------------------------------------------------------------- verify

def test_valid_key_becomes_a_principal_built_only_from_the_record(store, verifier):
    p = verifier.verify(store.issue(tenant="hotel_001", role="editor", domains=("hotel", "spa")), NOW)
    assert (p.tenant_id, p.role, p.domains) == ("hotel_001", "editor", frozenset({"hotel", "spa"}))
    assert p.allows("hotel") and not p.allows("airline")


def test_wrong_secret_unknown_id_and_swapped_secret_all_fail(store, verifier):
    a, b = store.issue(), store.issue()
    id_a, secret_a = keys.parse(a)
    _, secret_b = keys.parse(b)
    assert verifier.verify(f"m360_test_{id_a}_{secret_b}", NOW) is None          # right id, another key's secret
    assert verifier.verify(f"m360_test_ZZZZZZZZZZ_{secret_a}", NOW) is None      # unknown id
    assert verifier.verify(a[:-1] + ("A" if a[-1] != "A" else "B"), NOW) is None  # one character off
    assert verifier.verify(a, NOW) is not None


def test_env_in_the_key_text_does_not_matter_only_the_secret_does(store, verifier):
    key_id, secret = keys.parse(store.issue())
    assert verifier.verify(f"m360_live_{key_id}_{secret}", NOW) is not None


def test_expired_and_revoked_keys_fail(store, verifier):
    expired = store.issue(expires_at=NOW - timedelta(seconds=1))
    not_yet = store.issue(expires_at=NOW + timedelta(seconds=1))
    revoked = store.issue(revoked_at=NOW - timedelta(days=1))
    assert verifier.verify(expired, NOW) is None
    assert verifier.verify(not_yet, NOW) is not None
    assert verifier.verify(not_yet, NOW + timedelta(seconds=1)) is None          # the instant it expires
    assert verifier.verify(revoked, NOW) is None


def test_an_unknown_role_in_the_table_is_not_trusted(store, verifier):
    assert verifier.verify(store.issue(role="superuser"), NOW) is None


def test_all_domains_scope(store, verifier):
    p = verifier.verify(store.issue(domains=("*",)), NOW)
    assert p.allows("hotel") and p.allows("anything")


# ---------------------------------------------------------------- cache, revocation, store outage

def test_records_are_cached_for_the_ttl(store, verifier, clock):
    key = store.issue()
    for _ in range(50):
        assert verifier.verify(key, NOW) is not None
    assert store.lookups == 1
    clock.t += 61
    verifier.verify(key, NOW)
    assert store.lookups == 2


def test_unknown_ids_are_cached_too_so_they_cannot_hammer_the_store(store, verifier):
    fake = "m360_test_ZZZZZZZZZZ_" + "a" * 43
    for _ in range(50):
        assert verifier.verify(fake, NOW) is None
    assert store.lookups == 1


def test_revocation_takes_effect_on_evict_and_at_the_latest_after_the_ttl(store, verifier, clock):
    key = store.issue()
    key_id, _ = keys.parse(key)
    assert verifier.verify(key, NOW) is not None
    store.rows[key_id] = KeyRecord(**{**store.rows[key_id].__dict__, "revoked_at": NOW})

    assert verifier.verify(key, NOW) is not None          # another replica: still cached ...
    clock.t += 61
    assert verifier.verify(key, NOW) is None              # ... for at most the TTL

    key2 = store.issue()
    id2, _ = keys.parse(key2)
    verifier.verify(key2, NOW)
    store.rows[id2] = KeyRecord(**{**store.rows[id2].__dict__, "revoked_at": NOW})
    verifier.evict(id2)                                   # this replica handled the revoke: immediate
    assert verifier.verify(key2, NOW) is None


def test_store_outage_known_keys_keep_working_unknown_ones_are_unavailable(store, verifier, clock):
    known, unseen = store.issue(), store.issue()
    assert verifier.verify(known, NOW) is not None
    store.down = True
    clock.t += 3600                                       # long after the TTL
    assert verifier.verify(known, NOW) is not None        # pricing continues when Postgres is down
    with pytest.raises(KeyStoreUnavailable):
        verifier.verify(unseen, NOW)
    store.down = False
    assert verifier.verify(unseen, NOW) is not None


def test_cache_is_bounded(store, clock, monkeypatch):
    monkeypatch.setattr(keys, "MAX_CACHE", 100)
    v = KeyVerifier(store.lookup, monotonic=clock)
    for i in range(500):
        v.verify("m360_test_" + f"{i:010d}".translate(str.maketrans("01", "AB")) + "_" + "a" * 43, NOW)
    assert len(v._cache) <= 100


# ---------------------------------------------------------------- rate limit

def test_rate_limit_allows_a_burst_then_refills_over_time(clock):
    limiter = RateLimiter(monotonic=clock)
    assert all(limiter.allow("k1", 5) == 0 for _ in range(5))
    wait = limiter.allow("k1", 5)
    assert 0 < wait <= 0.2                                # one token comes back in 1/5 s
    assert limiter.allow("k2", 5) == 0                    # another key has its own bucket
    clock.t += 0.2
    assert limiter.allow("k1", 5) == 0
    clock.t += 100
    assert all(limiter.allow("k1", 5) == 0 for _ in range(5)) and limiter.allow("k1", 5) > 0     # capped at 1 s worth


def test_rate_limit_of_zero_blocks():
    assert RateLimiter().allow("k", 0) > 0
