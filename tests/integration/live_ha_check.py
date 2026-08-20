# Live network-layer check against the REAL Home Assistant instance, using
# the REAL config.py — run from the home LAN before first flashing a release:
#
#   make test-live
#
# Read-only throughout: API ping, speaker discovery, one template state
# fetch, and a WebSocket subscribe with one snapshot. No services are called,
# so no speaker makes a sound. Exercises app.httpc, app.ha and app.hapush
# under the real MicroPython interpreter against the real server.
import asyncio

from app.ha import HAClient
from app.hapush import HAPush
from app.settings import HA_URL


def fail(msg):
    print("FAILED:", msg)
    raise SystemExit(1)


async def main():
    print("Target:", HA_URL)

    ha = HAClient()
    if not await ha.ping():
        fail("cannot reach the HA REST API — are you on the home LAN?")
    print("REST ping: OK")

    speakers = await ha.discover_speakers()
    if not speakers:
        fail("no Sonos speakers discovered via the template API")
    print("discovery: %d speakers" % len(speakers))

    ha.set_entity(speakers[0]["entity_id"])
    state = await ha.get_state()
    if state is None:
        fail("template state poll returned nothing")
    print("template poll (%s): state=%s title=%s volume=%s"
          % (speakers[0]["entity_id"], state["state"], state["title"],
             state["volume"]))

    push = HAPush()
    try:
        await push.connect([s["entity_id"] for s in speakers])
        changed = await push.wait_update(lambda: False)
        if not changed:
            fail("websocket subscribe produced no snapshot")
        print("websocket snapshot: %d entities" % len(changed))
        for entity_id in sorted(push.states):
            s = push.states[entity_id]
            print("  %-40s %-8s %s" % (entity_id, s["state"] or "-",
                                       s["title"] or ""))
    finally:
        push.close()

    print()
    print("LIVE CHECK PASSED — REST + template + websocket verified against real HA")


asyncio.run(main())
