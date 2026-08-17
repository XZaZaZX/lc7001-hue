"""Minimal async client for the Philips Hue bridge local CLIP v2 API.

Only the pieces this project needs: pairing, listing resources, setting
on/off + brightness, and streaming events back from the bridge.

The bridge uses a self-signed certificate whose CN is the bridge id, so TLS
verification is disabled by default (traffic never leaves the LAN).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Mapping

import httpx

_log = logging.getLogger(__name__)

DISCOVERY_URL = "https://discovery.meethue.com/"


class HueError(RuntimeError):
    """Something went wrong talking to the bridge."""


class LinkButtonNotPressed(HueError):
    """The bridge wants the physical link button pressed first."""


async def discover_bridges(timeout: float = 10.0) -> list[dict[str, Any]]:
    """Ask Signify's discovery service which bridges live on this network."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(DISCOVERY_URL)
            response.raise_for_status()
            return response.json()
    except Exception as error:  # noqa: BLE001 - discovery is best effort
        _log.debug("bridge discovery failed: %s", error)
        return []


async def create_app_key(
    ip: str, app_name: str = "lc7001-hue#bridge", verify: bool = False
) -> tuple[str, str]:
    """Register with the bridge. The link button must be pressed first.

    Returns (application_key, client_key).
    """
    payload = {"devicetype": app_name, "generateclientkey": True}
    async with httpx.AsyncClient(verify=verify, timeout=10.0) as client:
        response = await client.post(f"https://{ip}/api", json=payload)
        response.raise_for_status()
        body = response.json()

    if not isinstance(body, list) or not body:
        raise HueError(f"unexpected pairing response: {body!r}")

    entry = body[0]
    if "error" in entry:
        description = entry["error"].get("description", "")
        if "link button" in description:
            raise LinkButtonNotPressed(description)
        raise HueError(description or str(entry["error"]))

    success = entry.get("success", {})
    return success["username"], success.get("clientkey", "")


class HueBridge:
    """Talks CLIP v2 to one bridge."""

    def __init__(
        self,
        ip: str,
        app_key: str,
        verify: bool = False,
        scheme: str = "https",
    ) -> None:
        self.ip = ip
        self.app_key = app_key
        self._verify = verify
        self._scheme = scheme  # only overridden by the test harness
        self._client: httpx.AsyncClient | None = None

    @property
    def base_url(self) -> str:
        return f"{self._scheme}://{self.ip}"

    def _headers(self) -> dict[str, str]:
        return {"hue-application-key": self.app_key}

    async def __aenter__(self) -> "HueBridge":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers(),
            verify=self._verify,
            timeout=10.0,
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise HueError("HueBridge used outside of its async context")
        return self._client

    async def get(self, resource: str) -> list[dict[str, Any]]:
        """GET /clip/v2/resource/<resource> and return the data array."""
        response = await self.client.get(f"/clip/v2/resource/{resource}")
        response.raise_for_status()
        body = response.json()
        errors = body.get("errors") or []
        if errors:
            raise HueError("; ".join(e.get("description", str(e)) for e in errors))
        return body.get("data", [])

    async def put(self, resource: str, rid: str, body: Mapping[str, Any]) -> None:
        """PUT a state change to one resource."""
        response = await self.client.put(
            f"/clip/v2/resource/{resource}/{rid}", json=dict(body)
        )
        response.raise_for_status()
        payload = response.json()
        errors = payload.get("errors") or []
        if errors:
            raise HueError("; ".join(e.get("description", str(e)) for e in errors))

    async def set_state(
        self,
        resource: str,
        rid: str,
        on: bool,
        brightness: float | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Set on/off and (when on) brightness, optionally ramped."""
        body: dict[str, Any] = {"on": {"on": on}}
        if on and brightness is not None:
            body["dimming"] = {"brightness": round(max(0.0, min(100.0, brightness)), 1)}
        if duration_ms:
            body["dynamics"] = {"duration": int(duration_ms)}
        await self.put(resource, rid, body)

    async def events(self) -> AsyncIterator[list[dict[str, Any]]]:
        """Yield decoded batches from the bridge's SSE event stream."""
        headers = dict(self._headers())
        headers["Accept"] = "text/event-stream"
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            verify=self._verify,
            timeout=httpx.Timeout(None, connect=10.0),
        ) as client:
            async with client.stream("GET", "/eventstream/clip/v2") as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[len("data:"):].strip()
                    if not raw:
                        continue
                    try:
                        yield json.loads(raw)
                    except json.JSONDecodeError:
                        _log.debug("unparsable event payload: %s", raw)


async def scenes_for_target(
    bridge: HueBridge, resource: str, rid: str
) -> list[dict[str, str]]:
    """Every Hue scene belonging to the room or zone a switch points at.

    Scenes are owned by a room or zone, never by a bare light, so a switch
    mapped to a single bulb has nothing to cycle through and gets an empty list.
    Returned in the bridge's own order, which is stable between calls.
    """
    if resource != "grouped_light":
        return []

    groups = await bridge.get(f"grouped_light/{rid}")
    if not groups:
        return []
    owner = (groups[0].get("owner") or {}).get("rid")
    if not owner:
        return []

    scenes = []
    for scene in await bridge.get("scene"):
        if (scene.get("group") or {}).get("rid") != owner:
            continue
        scenes.append(
            {
                "id": scene["id"],
                "name": (scene.get("metadata") or {}).get("name", "(unnamed)"),
            }
        )
    return scenes


async def recall_scene(bridge: HueBridge, scene_id: str) -> None:
    """Activate a scene."""
    await bridge.put("scene", scene_id, {"recall": {"action": "active"}})


async def summarize_targets(bridge: HueBridge) -> list[dict[str, str]]:
    """List the things worth pointing a wall switch at: rooms, zones, lights."""
    targets: list[dict[str, str]] = []

    for group_kind in ("room", "zone"):
        try:
            groups = await bridge.get(group_kind)
        except HueError:
            continue
        for group in groups:
            name = (group.get("metadata") or {}).get("name", "(unnamed)")
            for service in group.get("services", []):
                if service.get("rtype") == "grouped_light":
                    targets.append(
                        {
                            "label": f"{name} ({group_kind})",
                            "resource": "grouped_light",
                            "id": service["rid"],
                        }
                    )

    for light in await bridge.get("light"):
        name = (light.get("metadata") or {}).get("name", "(unnamed)")
        targets.append(
            {"label": f"{name} (single bulb)", "resource": "light", "id": light["id"]}
        )

    return targets


if __name__ == "__main__":  # pragma: no cover - convenience probe
    async def _main() -> None:
        logging.basicConfig(level=logging.INFO)
        found = await discover_bridges()
        print(json.dumps(found, indent=2))

    asyncio.run(_main())
