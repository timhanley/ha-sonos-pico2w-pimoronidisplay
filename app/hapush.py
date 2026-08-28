# Push state updates from Home Assistant's WebSocket API.
#
# One connection subscribes to ALL discovered speakers at once
# (subscribe_entities), so switching speakers is instant — the state is
# already known — and no resubscribe is needed. HA sends a full snapshot on
# subscribe, then tiny diffs; between updates the link is idle, so idle
# traffic is near zero versus 1 Hz REST polling.
#
# Media commands also go over this socket (call_service): no TCP setup per
# press — roughly half the latency of a REST call, and none of the lwIP
# connection churn. The concurrent wait_update() loop receives the result
# messages and routes each to the awaiting call_service() by id.
#
# Memory: only the seven display-relevant fields are kept per speaker
# (compact dicts, same shape as ha.get_state()); the full attribute payloads
# are translated on arrival and discarded.
import json

import asyncio

from app import log
from app.httpc import parse_url
from app.settings import HA_TOKEN, HA_URL
from app.wsclient import WebSocket

# HA attribute name → compact state key (shared shape with app/ha.py)
_ATTR_MAP = {
    "media_artist": "artist",
    "media_title": "title",
    "media_album_name": "album",
    "volume_level": "volume",
    "friendly_name": "name",
    "entity_picture": "picture",
}

_RECV_SLICE_S = 1     # recv granularity — bounds how fast should_stop reacts
_IDLE_PING_S = 30     # send an app-level ping after this much silence
_PING_GRACE_S = 10    # traffic must arrive within this after a ping


def _empty_state():
    return {"state": None, "artist": None, "title": None, "album": None,
            "volume": None, "name": None, "picture": None}


class HAPush:
    def __init__(self):
        self._host, self._port, _ = parse_url(HA_URL)
        self._ws = None
        self._msg_id = 0    # HA requires strictly increasing ids per connection
        self._pending = {}  # msg id -> [Event, success] awaiting a result
        self.states = {}    # entity_id -> compact state dict
        self.subscribed = ()

    def _next_id(self):
        self._msg_id += 1
        return self._msg_id

    async def connect(self, entity_ids, timeout=10):
        """Open, authenticate, and subscribe. Raises OSError on failure."""
        self._ws = await WebSocket.connect(
            self._host, self._port, "/api/websocket", timeout)
        self._msg_id = 0  # fresh connection, fresh id space
        self._pending = {}
        try:
            msg = await self._recv_json(timeout)
            if msg.get("type") != "auth_required":
                raise OSError("unexpected websocket greeting")
            await self._ws.send(json.dumps(
                {"type": "auth", "access_token": HA_TOKEN}))
            msg = await self._recv_json(timeout)
            if msg.get("type") != "auth_ok":
                raise OSError("websocket auth failed: %s" % msg.get("type"))
            await self._ws.send(json.dumps(
                {"id": self._next_id(), "type": "subscribe_entities",
                 "entity_ids": list(entity_ids)}))
            self.subscribed = tuple(entity_ids)
            self.states = {e: _empty_state() for e in entity_ids}
            log.info("push: subscribed to %d speakers" % len(entity_ids))
        except BaseException:
            self.close()
            raise

    async def _recv_json(self, timeout):
        raw = await self._ws.recv(timeout)
        if raw is None:
            raise OSError("websocket closed")
        return json.loads(raw)

    def _apply_full(self, entity_id, data):
        """Initial snapshot: {"s": state, "a": {full attributes}}."""
        state = self.states.setdefault(entity_id, _empty_state())
        state["state"] = data.get("s")
        attrs = data.get("a", {})
        for ha_key, key in _ATTR_MAP.items():
            state[key] = attrs.get(ha_key)

    def _apply_diff(self, entity_id, diff):
        """Change event: {"+": {"s":…, "a": {changed}}, "-": {"a": [removed]}}."""
        state = self.states.setdefault(entity_id, _empty_state())
        plus = diff.get("+", {})
        if "s" in plus:
            state["state"] = plus["s"]
        attrs = plus.get("a", {})
        for ha_key, key in _ATTR_MAP.items():
            if ha_key in attrs:
                state[key] = attrs[ha_key]
        removed = diff.get("-", {}).get("a", [])
        for ha_key in removed:
            key = _ATTR_MAP.get(ha_key)
            if key:
                self.states[entity_id][key] = None

    def _handle_event(self, event):
        """Apply one subscribe_entities event; return the set of changed ids."""
        changed = set()
        for entity_id, data in event.get("a", {}).items():
            self._apply_full(entity_id, data)
            changed.add(entity_id)
        for entity_id, diff in event.get("c", {}).items():
            self._apply_diff(entity_id, diff)
            changed.add(entity_id)
        return changed

    async def call_service(self, service, entity_id, timeout=5):
        """Call a media_player service over the push socket. Returns HA's
        success flag. Raises OSError/asyncio.TimeoutError when the socket is
        unusable — caller falls back to REST. Requires a concurrent
        wait_update() loop: that is what receives the result message."""
        msg_id = self._next_id()
        slot = [asyncio.Event(), False]
        self._pending[msg_id] = slot
        try:
            await self._ws.send(json.dumps(
                {"id": msg_id, "type": "call_service",
                 "domain": "media_player", "service": service,
                 "target": {"entity_id": entity_id}}))
            await asyncio.wait_for(slot[0].wait(), timeout)
            return slot[1]
        finally:
            self._pending.pop(msg_id, None)

    async def wait_update(self, should_stop):
        """Block until entity states change; return the set of changed
        entity_ids, or None when should_stop() turned true. Raises OSError
        (or ValueError on protocol garbage) when the connection dies."""
        idle = 0
        awaiting_pong = 0
        while True:
            if should_stop():
                return None
            try:
                raw = await self._ws.recv(_RECV_SLICE_S)
            except asyncio.TimeoutError:
                idle += _RECV_SLICE_S
                if awaiting_pong:
                    awaiting_pong += _RECV_SLICE_S
                    if awaiting_pong > _PING_GRACE_S:
                        raise OSError("websocket ping timeout")
                elif idle >= _IDLE_PING_S:
                    await self._ws.send(json.dumps(
                        {"id": self._next_id(), "type": "ping"}))
                    awaiting_pong = 1
                continue
            if raw is None:
                raise OSError("websocket closed")
            idle = 0
            awaiting_pong = 0  # any traffic proves liveness
            msg = json.loads(raw)
            msg_type = msg.get("type")
            if msg_type == "event":
                changed = self._handle_event(msg.get("event", {}))
                if changed:
                    return changed
            elif msg_type == "result":
                slot = self._pending.pop(msg.get("id"), None)
                if slot is not None:
                    # a call_service() is awaiting this — its failure is its
                    # own problem, not the connection's
                    slot[1] = bool(msg.get("success"))
                    slot[0].set()
                elif not msg.get("success", True):
                    # subscribe/ping failures are connection-fatal
                    raise OSError("ws command failed: %s" % msg.get("error"))
            # pong / result-ok / anything else: liveness only

    def close(self):
        if self._ws is not None:
            self._ws.close()
            self._ws = None
