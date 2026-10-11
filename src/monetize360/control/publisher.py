"""Draft -> approve (different user) -> publish / rollback. Moves pack_pointer and publishes pack_published event.
MUST run control.schema.validate before engine.compile_pack (the engine trusts a schema-valid dict) - add a test
that proves publishing always validates first (engine review #12)."""
