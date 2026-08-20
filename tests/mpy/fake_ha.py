# Loopback fake of Home Assistant's WebSocket API for protocol tests.
# Speaks real RFC 6455 server-side framing (unmasked sends, unmasks client
# frames) so app.wsclient and app.hapush are exercised end-to-end over TCP.
import binascii
import json

import asyncio

try:
    import hashlib
except ImportError:
    hashlib = None

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

PORT = 18123


class FakeHAServer:
    """Runs one scripted HA session: auth, subscribe, snapshot, diffs,
    ws-ping (expects a pong back), then close."""

    def __init__(self):
        self.received = []       # client JSON messages, in order
        self.pong_payload = None  # payload echoed back to our ws-level ping
        self.done = False
        self._server = None

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", PORT)

    def close(self):
        if self._server:
            self._server.close()

    # ---- server-side framing ----------------------------------------------

    async def _read_exact(self, reader, n):
        buf = b""
        while len(buf) < n:
            chunk = await reader.read(n - len(buf))
            if not chunk:
                raise OSError("client closed")
            buf += chunk
        return buf

    async def _read_frame(self, reader):
        b1, b2 = await self._read_exact(reader, 2)
        opcode = b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            length = int.from_bytes(await self._read_exact(reader, 2), "big")
        elif length == 127:
            length = int.from_bytes(await self._read_exact(reader, 8), "big")
        payload = b""
        if b2 & 0x80:  # client frames must be masked
            mask = await self._read_exact(reader, 4)
            data = bytearray(await self._read_exact(reader, length))
            for i in range(length):
                data[i] ^= mask[i & 3]
            payload = bytes(data)
        elif length:
            payload = await self._read_exact(reader, length)
        return opcode, payload

    async def _recv_json(self, reader):
        while True:
            opcode, payload = await self._read_frame(reader)
            if opcode == 0xA:  # pong
                self.pong_payload = payload
                continue
            if opcode == 0x1:
                msg = json.loads(payload)
                self.received.append(msg)
                return msg

    async def _send_frame(self, writer, opcode, payload):
        header = bytearray([0x80 | opcode])
        if len(payload) < 126:
            header.append(len(payload))
        else:
            header.append(126)
            header += len(payload).to_bytes(2, "big")
        writer.write(bytes(header) + payload)
        await writer.drain()

    async def _send_json(self, writer, obj):
        await self._send_frame(writer, 0x1, json.dumps(obj).encode())

    # ---- scripted session ---------------------------------------------------

    async def _handle(self, reader, writer):
        # HTTP upgrade handshake
        key = None
        await reader.readline()  # request line
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            if line.lower().startswith(b"sec-websocket-key:"):
                key = line.split(b":", 1)[1].strip().decode()
        accept = ""
        if hashlib and key:
            digest = hashlib.sha1((key + _GUID).encode()).digest()
            accept = binascii.b2a_base64(digest).strip().decode()
        writer.write((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: %s\r\n"
            "\r\n" % accept).encode())
        await writer.drain()

        await self._send_json(writer, {"type": "auth_required"})
        await self._recv_json(reader)  # auth
        await self._send_json(writer, {"type": "auth_ok"})
        await self._recv_json(reader)  # subscribe_entities
        await self._send_json(writer, {"id": 1, "type": "result", "success": True})

        # Full snapshot for two speakers
        await self._send_json(writer, {"id": 1, "type": "event", "event": {"a": {
            "media_player.kitchen": {"s": "playing", "a": {
                "media_artist": "Artist A", "media_title": "Song One",
                "media_album_name": "Album X", "volume_level": 0.4,
                "friendly_name": "Kitchen", "entity_picture": "/img/1.png",
                "group_members": ["a", "b"]}},
            "media_player.den": {"s": "idle", "a": {
                "friendly_name": "Den", "volume_level": 0.2}},
        }}})
        # Diff: kitchen track change
        await self._send_json(writer, {"id": 1, "type": "event", "event": {"c": {
            "media_player.kitchen": {"+": {"a": {
                "media_artist": "Artist B", "media_title": "Song Two"}}},
        }}})
        # Diff: state change plus attribute removal
        await self._send_json(writer, {"id": 1, "type": "event", "event": {"c": {
            "media_player.kitchen": {"+": {"s": "paused"},
                                     "-": {"a": ["entity_picture"]}},
        }}})
        # WS-level ping — the client must answer with a pong carrying the payload
        await self._send_frame(writer, 0x9, b"keepalive")
        while self.pong_payload is None:
            opcode, payload = await self._read_frame(reader)
            if opcode == 0xA:
                self.pong_payload = payload
        # Close handshake
        await self._send_frame(writer, 0x8, b"")
        self.done = True
        writer.close()
