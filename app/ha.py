# Home Assistant REST client.
#
# State polling uses the /api/template endpoint to fetch ONLY the fields the
# display shows (~200 bytes/poll) instead of the full media_player state —
# Sonos entities carry large attribute sets (source lists, group members)
# that would otherwise be downloaded and JSON-parsed every second.
import asyncio

from app import httpc, log
from app.settings import HA_URL, HA_TOKEN, HTTP_TIMEOUT

# Keys of the compact state dict returned by get_state().
STATE_KEYS = ("state", "artist", "title", "album", "volume", "name", "picture")

_SPEAKER_TEMPLATE = """
    {% set devices = states | map(attribute='entity_id') | map('device_id') | unique | reject('eq', None) | list %}
    {%- set ns = namespace(sonos_devices=[]) %}
    {%- for device in devices %}
        {%- if 'sonos' in device_attr(device, 'identifiers') | join %}
            {%- set entities = device_entities(device) | list %}
            {%- set ns.sonos_devices = ns.sonos_devices + [{'device_id': device, 'device_name': device_attr(device, 'name'), 'entities': entities}] %}
        {%- endif %}
    {%- endfor %}
    {{ ns.sonos_devices | tojson }}
"""


def _state_template(entity_id):
    return (
        "{% set e = '" + entity_id + "' %}"
        '{{ {"state": states(e),'
        ' "artist": state_attr(e, "media_artist"),'
        ' "title": state_attr(e, "media_title"),'
        ' "album": state_attr(e, "media_album_name"),'
        ' "volume": state_attr(e, "volume_level"),'
        ' "name": state_attr(e, "friendly_name"),'
        ' "picture": state_attr(e, "entity_picture")} | tojson }}'
    )


class HAClient:
    def __init__(self):
        self._headers = {
            "Authorization": "Bearer " + HA_TOKEN,
            "Content-Type": "application/json",
        }
        self.connected = False
        self._state_tpl = None  # prebuilt per speaker — no per-poll garbage

    def set_entity(self, entity_id):
        """Select the speaker whose state get_state() polls."""
        self._state_tpl = {"template": _state_template(entity_id)}

    async def _post_template(self, payload):
        status, data = await httpc.request(
            "POST", HA_URL + "/api/template", self._headers, payload,
            timeout=HTTP_TIMEOUT)
        if status != 200:
            raise OSError("template API returned %s" % status)
        return data

    async def ping(self):
        """Cheap reachability check of the HA API."""
        try:
            status, _ = await httpc.request(
                "GET", HA_URL + "/api", self._headers, timeout=HTTP_TIMEOUT)
            return status is not None
        except (OSError, ValueError, asyncio.TimeoutError):
            return False

    async def get_state(self):
        """Poll the selected speaker. Returns a compact dict (STATE_KEYS) or None."""
        if self._state_tpl is None:
            return None
        try:
            data = await self._post_template(self._state_tpl)
            if isinstance(data, dict):
                self.connected = True
                return data
        except (OSError, ValueError, asyncio.TimeoutError) as e:
            log.debug("state poll failed: %r" % e)
        self.connected = False
        return None

    async def call_service(self, service, entity_id, retries=1):
        """Call a media_player service. Returns True on success."""
        for attempt in range(retries + 1):
            try:
                status, _ = await httpc.request(
                    "POST", HA_URL + "/api/services/media_player/" + service,
                    self._headers, {"entity_id": entity_id},
                    timeout=HTTP_TIMEOUT)
                if status == 200:
                    self.connected = True
                    return True
            except (OSError, ValueError, asyncio.TimeoutError) as e:
                log.error("service %s failed: %r" % (service, e))
            if attempt < retries:
                await asyncio.sleep(0.5)
        self.connected = False
        return False

    async def discover_speakers(self):
        """Find Sonos speakers. Returns [{'entity_id':…, 'name':…}, …] or []."""
        try:
            result = await self._post_template({"template": _SPEAKER_TEMPLATE})
        except (OSError, ValueError, asyncio.TimeoutError) as e:
            log.error("speaker discovery failed: %r" % e)
            return []
        speakers = []
        for device in result or []:
            for entity in device["entities"]:
                if entity.startswith("media_player."):
                    speakers.append({"entity_id": entity, "name": device["device_name"]})
                    log.info("Found speaker: %s (%s)" % (device["device_name"], entity))
                    break
        return speakers

    def art_url(self, picture):
        """Absolute album-art URL from a state dict's 'picture' field."""
        if not picture:
            return None
        return HA_URL + picture if picture.startswith("/") else picture

    @property
    def headers(self):
        return self._headers
