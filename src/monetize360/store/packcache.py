"""In-memory {(tenant, domain): CompiledPack}; atomic swap on publish event, 10s poll backstop, keep last-known-
good on load failure."""
