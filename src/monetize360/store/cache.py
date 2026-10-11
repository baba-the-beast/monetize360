"""Redis client: signals, max_change anchor (anchor_price, anchor_time) + last issued price per (tenant, domain,
entity), idempotency results (24h), pub/sub events (pack_published, key_revoked, kill), audit stream XADD to
audit:{hash(tenant, domain) % 16}."""
