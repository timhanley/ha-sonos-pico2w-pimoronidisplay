# Minimal async HTTP/1.0 client (plain http:// only — HA lives on the LAN).
#
# Every network operation is wrapped in asyncio.wait_for so a hung server or
# stalled CYW43 transport can never wedge a task forever.
#
# INVARIANT: the writer is always closed — including on cancellation and
# timeout — to free the CYW43 socket slot. A leaked TCP connection blocks
# subsequent open_connection() calls for several seconds.
import asyncio
import gc
import json


def parse_url(url):
    """Split an http:// URL into (host, port, path)."""
    if not url.startswith("http://"):
        raise ValueError("only http:// URLs are supported")
    rest = url[7:]
    slash_pos = rest.find("/")
    if slash_pos == -1:
        host_port, path = rest, "/"
    else:
        host_port, path = rest[:slash_pos], rest[slash_pos:]
    if ":" in host_port:
        host, port = host_port.rsplit(":", 1)
        return host, int(port), path
    return host_port, 80, path


def build_request(method, host, port, path, headers, body_len):
    """Build the request head (bytes, ends with the blank line)."""
    lines = [
        "%s %s HTTP/1.0" % (method, path),
        "Host: %s:%d" % (host, port),
    ]
    if headers:
        for key in headers:
            lines.append("%s: %s" % (key, headers[key]))
    if body_len:
        lines.append("Content-Length: %d" % body_len)
    lines.append("\r\n")
    return "\r\n".join(lines).encode()


async def _read_head(reader, timeout):
    """Read the status line and skip headers. Returns the status code."""
    status_line = await asyncio.wait_for(reader.readline(), timeout)
    status = int(status_line.split(b" ")[1])
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout)
        if line in (b"\r\n", b"\n", b""):
            return status


async def request(method, url, headers=None, json_data=None, timeout=10):
    """Send a request; return (status_code, parsed_json_or_None)."""
    gc.collect()
    host, port, path = parse_url(url)
    body = json.dumps(json_data).encode() if json_data is not None else b""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout)
    try:
        writer.write(build_request(method, host, port, path, headers, len(body)) + body)
        await asyncio.wait_for(writer.drain(), timeout)
        status = await _read_head(reader, timeout)
        chunks = []
        while True:
            chunk = await asyncio.wait_for(reader.read(512), timeout)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
    finally:
        writer.close()
    gc.collect()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


async def request_to_file(url, headers, filename, timeout=15):
    """GET a URL, streaming the body to a file. Returns the status code."""
    gc.collect()
    host, port, path = parse_url(url)
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout)
    try:
        writer.write(build_request("GET", host, port, path, headers, 0))
        await asyncio.wait_for(writer.drain(), timeout)
        status = await _read_head(reader, timeout)
        with open(filename, "wb") as f:
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), timeout)
                if not chunk:
                    break
                f.write(chunk)
                await asyncio.sleep(0)
        gc.collect()
        return status
    finally:
        writer.close()
