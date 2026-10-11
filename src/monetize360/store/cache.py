"""Redis, used for ONE thing: telling every replica that something changed (key revoked; later: pack published,
kill switch). Nothing is stored in Redis, and nothing on a price request waits for it.

Why so little: audit rows, max_change anchors and idempotency keys must never be lost and are read on every
price, so they live in Postgres (ARCHITECTURE 11.1). Redis may be remote, slow or without persistence; for a
broadcast none of that matters:

  - a message that arrives late only delays the news by that much
  - a message that never arrives is covered by the backstop (60 s key cache, 10 s pack poll)
  - after a connection gap every replica forgets its cached keys, so missed revocations still apply at once

Messages can only INVALIDATE, never grant: "key X revoked" makes a replica drop its cached copy and ask the
database again. Someone able to publish to this Redis can therefore cause extra lookups, but not access.
Channels are named per database, so two environments sharing one Redis don't hear each other."""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

import redis

log = logging.getLogger("monetize360.events")

Handler = Callable[[dict[str, Any]], None]


class Events:
    def __init__(self, url: str, namespace: str, *, client: Any = None, retry_seconds: float = 2.0):
        # short timeouts: a publish must never hang an admin request, and never touches a price request
        self._client = client or redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=3,
                                                      socket_timeout=3, health_check_interval=30)
        self.channel = f"m360:events:{namespace}"
        self._handlers: dict[str, Handler] = {}
        self._on_connect: list[Callable[[], None]] = []
        self._retry = retry_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False

    def on(self, event_type: str, handler: Handler) -> None:
        self._handlers[event_type] = handler

    def on_connect(self, callback: Callable[[], None]) -> None:
        """Called on every (re)subscription: whatever was broadcast while we weren't listening is unknown."""
        self._on_connect.append(callback)

    def publish(self, event_type: str, **payload: Any) -> bool:
        """Tell every replica. False when Redis can't be reached; the caller's change still stands, replicas
        just learn of it by their backstop instead."""
        try:
            self._client.publish(self.channel, json.dumps({"type": event_type, **payload}, sort_keys=True))
            return True
        except (redis.RedisError, OSError) as e:
            log.error("events: could not publish %s (%s)", event_type, type(e).__name__)
            return False

    def dispatch(self, raw: Any) -> None:
        """Handle one received message. Anything malformed or unknown is ignored: the channel is not trusted."""
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return
        handler = self._handlers.get(message.get("type")) if isinstance(message, dict) else None
        if handler is None:
            return
        try:
            handler(message)
        except Exception:      # noqa: BLE001 - one bad message must not stop the listener
            log.exception("events: handler for %s failed", message.get("type"))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._listen, name="events", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _listen(self) -> None:
        while not self._stop.is_set():
            pubsub = None
            try:
                pubsub = self._client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(self.channel)
                self.connected = True
                for callback in self._on_connect:
                    callback()
                while not self._stop.is_set():
                    message = pubsub.get_message(timeout=1.0)
                    if message and message.get("type") == "message":
                        self.dispatch(message.get("data"))
            except (redis.RedisError, OSError) as e:
                log.warning("events: connection lost (%s), retrying in %.0f s", type(e).__name__, self._retry)
            finally:
                self.connected = False
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except (redis.RedisError, OSError):
                        pass
            self._stop.wait(self._retry)
