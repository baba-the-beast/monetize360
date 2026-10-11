"""Generate keys (secrets.token_urlsafe), store SHA-256 of secret, constant-time verify, 60s in-memory cache, evict
on key_revoked event."""
