# Minimal WebSocket client (RFC 6455) over asyncio streams — ws:// only,
# built for Home Assistant's /api/websocket endpoint on the LAN.
#
# Scope: text messages, ping/pong, close, and continuation frames. Client
# frames are masked as the RFC requires; server frames are accepted masked or
# not. Every read is bounded by a timeout — a dead peer can never wedge the
# reader task. Like httpc, close() must always run so the CYW43 socket slot
# is freed.
import binascii
import os

import asyncio

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def _random_bytes(n):
    try:
        return os.urandom(n)
    except AttributeError:  # port without urandom — non-crypto fallback is fine
        import time
        seed = time.ticks_us()
        out = bytearray(n)
        for i in range(n):
            seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
            out[i] = seed & 0xFF
        return bytes(out)


def _accept_hash(key):
    """Expected Sec-WebSocket-Accept for a key, or None if sha1 unavailable."""
    try:
        import hashlib
        digest = hashlib.sha1((key + _GUID).encode()).digest()
        return binascii.b2a_base64(digest).strip().decode()
    except (ImportError, AttributeError):
        return None


class WebSocket:
    def __init__(self, reader, writer):
        self._reader = reader
        self._writer = writer

    @classmethod
    async def connect(cls, host, port, path, timeout=10):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout)
        try:
            key = binascii.b2a_base64(_random_bytes(16)).strip().decode()
            writer.write((
                "GET %s HTTP/1.1\r\n"
                "Host: %s:%d\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Key: %s\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n" % (path, host, port, key)).encode())
            await asyncio.wait_for(writer.drain(), timeout)

            status = await asyncio.wait_for(reader.readline(), timeout)
            if b" 101 " not in status:
                raise OSError("websocket handshake refused: %s" % status)
            accept = None
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout)
                if line in (b"\r\n", b"\n", b""):
                    break
                if line.lower().startswith(b"sec-websocket-accept:"):
                    accept = line.split(b":", 1)[1].strip().decode()
            expected = _accept_hash(key)
            if expected is not None and accept != expected:
                raise OSError("websocket accept-key mismatch")
        except BaseException:
            writer.close()
            raise
        return cls(reader, writer)

    async def _read_exact(self, n, timeout):
        buf = b""
        while len(buf) < n:
            chunk = await asyncio.wait_for(self._reader.read(n - len(buf)), timeout)
            if not chunk:
                raise OSError("websocket connection closed mid-frame")
            buf += chunk
        return buf

    async def _send_frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)          # 0x80: client frames are masked
        elif length < 65536:
            header.append(0x80 | 126)
            header += length.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += length.to_bytes(8, "big")
        mask = _random_bytes(4)
        header += mask
        masked = bytearray(payload)
        for i in range(length):
            masked[i] ^= mask[i & 3]
        self._writer.write(bytes(header) + bytes(masked))
        await self._writer.drain()

    async def send(self, text):
        await self._send_frame(OP_TEXT, text.encode())

    async def recv(self, timeout):
        """Return the next text/binary message as bytes, or None if the peer
        closed. Handles ping/pong and continuation frames internally.
        Raises asyncio.TimeoutError if nothing arrives in time."""
        message = b""
        while True:
            b1, b2 = await self._read_exact(2, timeout)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = int.from_bytes(await self._read_exact(2, timeout), "big")
            elif length == 127:
                length = int.from_bytes(await self._read_exact(8, timeout), "big")
            if b2 & 0x80:  # masked (unusual from a server, but legal to handle)
                mask = await self._read_exact(4, timeout)
                payload = bytearray(await self._read_exact(length, timeout))
                for i in range(length):
                    payload[i] ^= mask[i & 3]
                payload = bytes(payload)
            else:
                payload = await self._read_exact(length, timeout) if length else b""

            if opcode == OP_PING:
                await self._send_frame(OP_PONG, payload)
            elif opcode == OP_PONG:
                pass  # liveness only
            elif opcode == OP_CLOSE:
                try:
                    await self._send_frame(OP_CLOSE, b"")
                except OSError:
                    pass
                return None
            else:  # TEXT / BINARY / CONT
                message += payload
                if fin:
                    return message

    def close(self):
        try:
            self._writer.close()
        except OSError:
            pass
