"""Own audit:{shard} streams via a Redis lease (one worker per shard), build the per-(tenant, domain) hash chain,
batch INSERT ... ON CONFLICT DO NOTHING (created_at = event issued_at), ACK. On start: resume (seq, hash) from
Postgres."""
