"""Redis broadcasts with a fake Redis: publish, dispatch, untrusted messages, reconnect, Redis being down."""
import json
import queue
import time

import redis

from monetize360.security.keys import KeyRecord, KeyVerifier, generate, parse
from monetize360.store.cache import Events


class FakePubSub:
    def __init__(self, bus: "FakeRedis"):
        self.bus, self.inbox, self.closed = bus, queue.Queue(), False

    def subscribe(self, channel):
        if self.bus.down:
            raise redis.ConnectionError("down")
        self.bus.subscribers.setdefault(channel, []).append(self)

    def get_message(self, timeout=1.0):
        if self.bus.down:
            raise redis.ConnectionError("down")
        try:
            return self.inbox.get(timeout=0.02)
        except queue.Empty:
            return None

    def close(self):
        self.closed = True


class FakeRedis:
    def __init__(self):
        self.subscribers: dict[str, list[FakePubSub]] = {}
        self.down = False

    def pubsub(self, ignore_subscribe_messages=True):
        return FakePubSub(self)

    def publish(self, channel, data):
        if self.down:
            raise redis.ConnectionError("down")
        for sub in self.subscribers.get(channel, []):
            sub.inbox.put({"type": "message", "data": data})
        return 1


def wait_for(condition, seconds=2.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_a_published_event_reaches_every_listening_replica():
    bus, got_a, got_b = FakeRedis(), [], []
    a, b = Events("", "db1", client=bus, retry_seconds=0.05), Events("", "db1", client=bus, retry_seconds=0.05)
    a.on("key_revoked", got_a.append)
    b.on("key_revoked", got_b.append)
    a.start(), b.start()
    assert wait_for(lambda: a.connected and b.connected)
    assert a.publish("key_revoked", key_id="ABCDEFGHJK") is True
    assert wait_for(lambda: got_a and got_b)
    assert got_a == got_b == [{"type": "key_revoked", "key_id": "ABCDEFGHJK"}]
    a.stop(), b.stop()


def test_two_databases_on_one_redis_do_not_hear_each_other():
    bus, got = FakeRedis(), []
    prod, test = Events("", "prod-db", client=bus, retry_seconds=0.05), Events("", "test-db", client=bus)
    prod.on("key_revoked", got.append)
    prod.start()
    assert wait_for(lambda: prod.connected)
    test.publish("key_revoked", key_id="ABCDEFGHJK")
    time.sleep(0.1)
    assert got == [] and prod.channel != test.channel
    prod.stop()


def test_untrusted_messages_are_ignored_and_a_failing_handler_does_not_stop_the_listener():
    events, got = Events("", "db", client=FakeRedis()), []

    def handler(message):
        if message.get("boom"):
            raise RuntimeError("handler bug")
        got.append(message["key_id"])

    events.on("key_revoked", handler)
    for raw in ("not json", "[1, 2]", "null", json.dumps({"type": "grant_admin", "key_id": "X"}),
                json.dumps({"no_type": True}), None, 42, json.dumps({"type": "key_revoked", "boom": True})):
        events.dispatch(raw)                                   # none of these may raise
    events.dispatch(json.dumps({"type": "key_revoked", "key_id": "ABCDEFGHJK"}))
    assert got == ["ABCDEFGHJK"]


def test_publish_reports_false_when_redis_is_down_instead_of_raising():
    bus = FakeRedis()
    bus.down = True
    assert Events("", "db", client=bus).publish("key_revoked", key_id="ABCDEFGHJK") is False


def test_after_a_connection_gap_the_listener_reconnects_and_the_connect_hook_runs_again():
    bus, connects = FakeRedis(), []
    events = Events("", "db", client=bus, retry_seconds=0.05)
    events.on_connect(lambda: connects.append(1))
    events.start()
    assert wait_for(lambda: events.connected and len(connects) == 1)
    bus.down = True
    assert wait_for(lambda: not events.connected)
    bus.down = False
    assert wait_for(lambda: events.connected and len(connects) == 2)
    events.stop()


def test_a_revocation_event_and_a_reconnect_both_drop_cached_keys():
    rows, lookups = {}, []
    key_id, api_key, secret_hash = generate("test")
    rows[key_id] = KeyRecord(key_id, "hotel_001", secret_hash, "pricing", ("hotel",))

    def lookup(k):
        lookups.append(k)
        return rows.get(k)

    verifier = KeyVerifier(lookup, ttl=3600)
    bus = FakeRedis()
    events = Events("", "db", client=bus, retry_seconds=0.05)
    events.on("key_revoked", lambda m: verifier.evict(m["key_id"]))
    events.on_connect(verifier.clear)
    events.start()
    assert wait_for(lambda: events.connected)

    assert verifier.verify(api_key) is not None and len(lookups) == 1
    rows[key_id] = KeyRecord(**{**rows[key_id].__dict__, "revoked_at": "now"})
    assert verifier.verify(api_key) is not None                # still cached: this replica hasn't heard yet
    events.publish("key_revoked", key_id=parse(api_key)[0])
    assert wait_for(lambda: verifier.verify(api_key) is None)   # heard: refused at once, not after the TTL

    # a revocation broadcast while disconnected is caught by clearing the cache on reconnect
    other_id, other_key, other_hash = generate("test")
    rows[other_id] = KeyRecord(other_id, "hotel_001", other_hash, "pricing", ("hotel",))
    assert verifier.verify(other_key) is not None
    bus.down = True
    assert wait_for(lambda: not events.connected)
    rows[other_id] = KeyRecord(**{**rows[other_id].__dict__, "revoked_at": "now"})      # revoked, broadcast lost
    bus.down = False
    assert wait_for(lambda: events.connected)
    assert wait_for(lambda: verifier.verify(other_key) is None)
    events.stop()
