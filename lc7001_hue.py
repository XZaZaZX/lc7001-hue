#!/usr/bin/env python3
"""lc7001-hue: make Legrand RFLC wall dimmers control Philips Hue lights.

Listens to a Legrand LC7001 hub over its local API. When a mapped RFLC
dimmer or switch changes -- at the wall, in the Legrand app, or from
HomeKit via Homebridge -- the matching Hue room, zone, or bulb is set to
the same on/off state and brightness.

Optionally follows in the other direction too, so changing the Hue lights
from the Hue app or Apple Home keeps the wall dimmer's level in sync.

Usage:
    python3 lc7001_hue.py --config config.json
    python3 lc7001_hue.py --config config.json --check    # one-shot test
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import lc7001.aio

from hue_client import HueBridge, HueError, recall_scene, scenes_for_target

_log = logging.getLogger("lc7001-hue")

DEFAULT_MIN_BRIGHTNESS = 1.0
# The LC7001 reports a moving paddle roughly 2-3 times a second, so there is no
# point throttling harder than that -- doing so only adds lag. Letting Hue glide
# for about one reporting interval turns those steps into a smooth ramp.
DEFAULT_THROTTLE_SECONDS = 0.12
DEFAULT_TRANSITION_MS = 400
# Some RFLC dimmers report a momentary Power=False partway through a ramp, with
# Power=True following a fraction of a second later at the new level. Acting on
# that makes the Hue lights blink, so an off is held back briefly to see whether
# it was real -- but only while the paddle is actively moving, so a deliberate
# tap-off stays instant.
DEFAULT_JUMP_THRESHOLD = 30

# The LC7001 does not report a paddle ramp as it happens. The instant you press
# and hold, it snaps its idea of the zone to the endpoint -- off going down, 100
# going up -- and only reports the true level when you let go, seconds later.
# That endpoint is indistinguishable from a deliberate tap until the correction
# arrives, so there is a genuine trade-off and no universally right answer:
#
#   snappy  taps act instantly; holding the paddle visibly bounces first
#   fade    act at once but ease in slowly, turning the bounce into a swell
#   steady  hold the endpoint back until it is confirmed; no bounce, slower taps
#
# (hold seconds, glide milliseconds for a big swing)
RAMP_MODES: dict[str, tuple[float, int]] = {
    "snappy": (0.0, 400),
    "fade": (0.4, 2000),
    "steady": (2.5, 600),
}
DEFAULT_RAMP_MODE = "fade"
ECHO_SUPPRESSION_SECONDS = 2.5
LEVEL_TOLERANCE = 1  # LC7001 steps we treat as "same" when Hue reports back

# Flick a dimmer off and straight back on to step to the next Hue scene in the
# room. Off-then-on is used rather than a double tap because the Legrand paddle
# handles a double tap in its own firmware -- it goes to full brightness and
# reports a single "on at 100" to the hub, so the second press never reaches us.
# An off and an on arrive as two separate messages, which is unambiguous.
DEFAULT_FLICK_SECONDS = 1.5


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


@dataclass
class Link:
    """One wall dimmer wired (in software) to one Hue target."""

    name: str
    zid: int
    hue_resource: str  # "grouped_light" or "light"
    hue_id: str
    min_brightness: float = DEFAULT_MIN_BRIGHTNESS
    max_brightness: float = 100.0
    throttle: float = DEFAULT_THROTTLE_SECONDS
    transition_ms: int = DEFAULT_TRANSITION_MS
    follow_hue: bool = True
    ramp_mode: str = DEFAULT_RAMP_MODE
    jump_threshold: int = DEFAULT_JUMP_THRESHOLD
    scene_cycle: bool = False
    flick_seconds: float = DEFAULT_FLICK_SECONDS
    # left as None unless hand-tuned in config.json, in which case they win
    endpoint_hold: float | None = None
    ramp_transition_ms: int | None = None

    # runtime state
    desired: dict[str, Any] = field(default_factory=dict, repr=False)
    last_sent: dict[str, Any] = field(default_factory=dict, repr=False)
    wake: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    last_write_to_hue: float = field(default=0.0, repr=False)
    last_off_at: float = field(default=0.0, repr=False)
    scene_list: list[dict[str, str]] = field(default_factory=list, repr=False)
    scene_index: int = field(default=-1, repr=False)
    skip_push: bool = field(default=False, repr=False)

    def is_flick(self, now: float) -> bool:
        """Did this switch go off and back on quickly enough to mean 'next scene'?"""
        return (
            self.scene_cycle
            and self.last_off_at > 0.0
            and (now - self.last_off_at) <= self.flick_seconds
        )

    def _mode(self) -> tuple[float, int]:
        return RAMP_MODES.get(self.ramp_mode, RAMP_MODES[DEFAULT_RAMP_MODE])

    def hold_seconds(self) -> float:
        """How long to sit on a suspected ramp endpoint before believing it."""
        return self._mode()[0] if self.endpoint_hold is None else self.endpoint_hold

    def ramp_ms(self) -> int:
        """Glide length for a big swing."""
        return self._mode()[1] if self.ramp_transition_ms is None else self.ramp_transition_ms

    def is_big_swing(self, previous: Mapping[str, Any], on: bool,
                     level: int | None) -> bool:
        """An on/off flip or a large jump -- the shape a ramp endpoint takes."""
        if previous.get("on") != on:
            return True
        old, new = previous.get("level"), level
        return (
            old is not None
            and new is not None
            and abs(int(new) - int(old)) >= self.jump_threshold
        )

    def scale(self, power_level: int) -> float:
        """Map an LC7001 level (1-100) onto the configured Hue range."""
        span = max(0.0, self.max_brightness - self.min_brightness)
        return self.min_brightness + span * (max(1, min(100, power_level)) - 1) / 99.0

    def unscale(self, brightness: float) -> int:
        """Map a Hue brightness (0-100) back onto an LC7001 level (1-100)."""
        span = max(0.001, self.max_brightness - self.min_brightness)
        level = 1 + (brightness - self.min_brightness) * 99.0 / span
        return int(max(1, min(100, round(level))))


@dataclass
class SceneLink:
    """One LC7001 scene (a scene-controller button) wired to a Hue action."""

    name: str
    sid: int
    hue_resource: str = ""  # "grouped_light" / "light"; empty when recalling
    hue_id: str = ""
    on: bool = True
    brightness: float | None = None
    transition_ms: int = DEFAULT_TRANSITION_MS
    hue_scene_id: str = ""  # recall this Hue scene instead of setting a level


@dataclass
class Config:
    lc7001_host: str
    lc7001_password: str | None
    hue_ip: str
    hue_app_key: str
    links: list[Link]
    scenes: list[SceneLink] = field(default_factory=list)
    log_level: str = "INFO"
    web_port: int = 8582
    hue_scheme: str = "https"  # only changed by the test harness

    @classmethod
    def load(cls, path: Path) -> "Config":
        raw = json.loads(path.read_text())
        lc = raw.get("lc7001", {})
        hue = raw.get("hue", {})
        links = []
        for item in raw.get("links", []):
            links.append(
                Link(
                    name=item.get("name", f"zone {item['lc7001_zid']}"),
                    zid=int(item["lc7001_zid"]),
                    hue_resource=item["hue_resource"],
                    hue_id=item["hue_id"],
                    min_brightness=float(
                        item.get("min_brightness", DEFAULT_MIN_BRIGHTNESS)
                    ),
                    max_brightness=float(item.get("max_brightness", 100.0)),
                    throttle=float(item.get("throttle", DEFAULT_THROTTLE_SECONDS)),
                    transition_ms=int(
                        item.get("transition_ms", DEFAULT_TRANSITION_MS)
                    ),
                    follow_hue=bool(item.get("follow_hue", True)),
                    ramp_mode=str(item.get("ramp_mode", DEFAULT_RAMP_MODE)),
                    jump_threshold=int(
                        item.get("jump_threshold", DEFAULT_JUMP_THRESHOLD)
                    ),
                    scene_cycle=bool(item.get("scene_cycle", False)),
                    flick_seconds=float(
                        item.get("flick_seconds", DEFAULT_FLICK_SECONDS)
                    ),
                    endpoint_hold=(
                        float(item["endpoint_hold"])
                        if item.get("endpoint_hold") is not None
                        else None
                    ),
                    ramp_transition_ms=(
                        int(item["ramp_transition_ms"])
                        if item.get("ramp_transition_ms") is not None
                        else None
                    ),
                )
            )
        scenes = []
        for item in raw.get("scenes", []):
            scenes.append(
                SceneLink(
                    name=item.get("name", f"scene {item['lc7001_sid']}"),
                    sid=int(item["lc7001_sid"]),
                    hue_resource=item.get("hue_resource", ""),
                    hue_id=item.get("hue_id", ""),
                    on=bool(item.get("on", True)),
                    brightness=(
                        float(item["brightness"])
                        if item.get("brightness") is not None
                        else None
                    ),
                    transition_ms=int(item.get("transition_ms", DEFAULT_TRANSITION_MS)),
                    hue_scene_id=item.get("hue_scene_id", ""),
                )
            )
        if not links and not scenes:
            # Perfectly normal on first boot: the service comes up, connects to
            # both hubs, and waits for mappings to be made in the web UI.
            _log.info("nothing mapped yet - open the web UI to set it up")
        return cls(
            lc7001_host=lc.get("host", "LCM1.local"),
            lc7001_password=lc.get("password") or None,
            hue_ip=hue["ip"],
            hue_app_key=hue["app_key"],
            links=links,
            scenes=scenes,
            web_port=int(raw.get("web_port", 8582)),
            log_level=raw.get("log_level", "INFO"),
            hue_scheme=hue.get("scheme", "https"),
        )


# --------------------------------------------------------------------------
# LC7001 side
# --------------------------------------------------------------------------


class Hub(lc7001.aio.Hub):
    """LC7001 hub that reports mapped zones on connect and on every change."""

    def __init__(self, host: str, key: bytes | None, bridge: "Bridge") -> None:
        super().__init__(host, key=key)
        self._bridge = bridge
        self.on(self.EVENT_AUTHENTICATED, self._on_authenticated)
        self.on(self.EVENT_ZONE_PROPERTIES_CHANGED, self._on_zone_message)
        self.on(self.EVENT_REPORT_ZONE_PROPERTIES, self._on_zone_message)
        self.on(self.EVENT_SET_ZONE_PROPERTIES, self._on_set_ack)
        self.on(self.EVENT_RUN_SCENE, self._on_run_scene)
        self.on(self.EVENT_LIST_ZONES, self._on_catalog_zones)
        self.on(self.EVENT_LIST_SCENES, self._on_catalog_scenes)
        self.on(self.EVENT_REPORT_SCENE_PROPERTIES, self._on_scene_report)
        self.on(self.EVENT_DISCONNECTED, self._on_disconnected)

    async def _on_authenticated(self, address: str | None = None) -> None:
        _log.info("connected to LC7001 at %s (%s)", self.host(), address or "?")
        self._bridge.lc7001_up = True
        # Pull the whole catalog so the web UI can offer every zone and scene.
        await self.send(self.compose_list_zones())
        await self.send(self.compose_list_scenes())

    async def _on_disconnected(self) -> None:
        self._bridge.lc7001_up = False

    async def _on_set_ack(self, message: Mapping) -> None:
        error = self.StatusError(message)
        if error:
            _log.warning("LC7001 rejected a zone write: %s", error.args)

    async def _on_zone_message(self, message: Mapping) -> None:
        if self.StatusError(message):
            return
        zid = message.get(self.ZID)
        properties = message.get(self.PROPERTY_LIST) or {}
        if zid is None or not properties:
            return
        zid = int(zid)
        self._bridge.note_zone(zid, properties)
        if message.get(self.SERVICE) == "ZonePropertiesChanged":
            self._bridge.log_event("zone", zid, properties)
        await self._bridge.on_lc7001_zone(zid, properties)

    async def _on_catalog_zones(self, message: Mapping) -> None:
        """Learn every zone the hub knows about, for the web UI's pickers."""
        if self.StatusError(message):
            return
        for item in message.get(self.ZONE_LIST, []) or []:
            await self.send(self.compose_report_zone_properties(int(item[self.ZID])))

    async def _on_catalog_scenes(self, message: Mapping) -> None:
        if self.StatusError(message):
            return
        for item in message.get(self.SCENE_LIST, []) or []:
            await self.send(self.compose_report_scene_properties(int(item[self.SID])))

    async def _on_scene_report(self, message: Mapping) -> None:
        if self.StatusError(message):
            return
        sid = message.get(self.SID)
        properties = message.get(self.PROPERTY_LIST) or {}
        if sid is not None:
            self._bridge.scene_catalog[int(sid)] = properties.get(self.NAME, "?")

    async def _on_run_scene(self, message: Mapping) -> None:
        if self.StatusError(message):
            return
        sid = message.get(self.SID)
        if sid is None:
            return
        self._bridge.log_event("scene", int(sid), {})
        await self._bridge.on_lc7001_scene(int(sid))

    async def apply_zone(self, zid: int, power: bool, level: int | None) -> None:
        await self.send(
            self.compose_set_zone_properties(zid, power=power, power_level=level)
        )


# --------------------------------------------------------------------------
# the bridge itself
# --------------------------------------------------------------------------


class Bridge:
    def __init__(self, config: Config, config_path: Path | None = None) -> None:
        self.config = config
        self.config_path = config_path
        self.links = config.links
        self._by_zid: dict[int, Link] = {}
        self._by_hue: dict[tuple[str, str], Link] = {}
        self._index_links()
        self.hue = HueBridge(
            config.hue_ip, config.hue_app_key, scheme=config.hue_scheme
        )
        key = (
            lc7001.aio.hash_password(config.lc7001_password.encode())
            if config.lc7001_password
            else None
        )
        self.hub = Hub(config.lc7001_host, key, self)

        # web UI state
        self.zone_catalog: dict[int, dict[str, Any]] = {}
        self.scene_catalog: dict[int, str] = {}
        self.events: list[dict[str, Any]] = []
        self.event_cursor = 0
        self.lc7001_up = False
        self.hue_up = False
        self._pushers: list[asyncio.Task] = []

    def _index_links(self) -> None:
        self._by_zid = {link.zid: link for link in self.links}
        self._by_hue = {(link.hue_resource, link.hue_id): link for link in self.links}

    def _reset_wakers(self) -> None:
        """Rebuild each link's wake Event on the loop that will await it.

        On Python 3.9 an asyncio.Event binds to whatever loop exists when it is
        constructed. Config.load() runs before asyncio.run(), so the Events
        would otherwise belong to a different loop and every wait() would raise
        the moment a switch moved -- killing the pusher silently. Constructing
        them here, inside the running loop, keeps that from happening.
        """
        for link in self.links:
            link.wake = asyncio.Event()
            if link.desired:
                link.wake.set()  # don't lose state learned before we started

    async def _run_pusher(self, link: Link) -> None:
        """Wrapper so a dying pusher is never silent."""
        try:
            await self._pusher(link)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _log.exception(
                "[%s] pusher stopped unexpectedly - this dimmer is now dead "
                "until the service restarts",
                link.name,
            )
            raise

    # ---- web UI support --------------------------------------------------

    def note_zone(self, zid: int, properties: Mapping[str, Any]) -> None:
        """Remember a zone's name/type so the UI can offer it in a picker."""
        entry = self.zone_catalog.setdefault(zid, {})
        for key in ("Name", "DeviceType", "Power", "PowerLevel"):
            if key in properties:
                entry[key] = properties[key]

    def log_event(self, kind: str, ident: int, properties: Mapping[str, Any]) -> None:
        if kind == "scene":
            name = self.scene_catalog.get(ident, "?")
            detail = ""
        elif kind == "flick":
            name = (self.zone_catalog.get(ident) or {}).get("Name", "?")
            detail = str(properties.get("scene", ""))
        else:
            name = (self.zone_catalog.get(ident) or {}).get("Name", "?")
            bits = []
            if lc7001.aio.Composer.POWER in properties:
                bits.append("on" if properties[lc7001.aio.Composer.POWER] else "off")
            if lc7001.aio.Composer.POWER_LEVEL in properties:
                bits.append(f"{properties[lc7001.aio.Composer.POWER_LEVEL]}%")
            detail = "  ".join(bits)

        self.event_cursor += 1
        self.events.append(
            {
                "seq": self.event_cursor,
                "time": time.strftime("%H:%M:%S"),
                "kind": kind,
                "id": ident,
                "name": name,
                "detail": detail,
            }
        )
        del self.events[:-400]

    def ui_events(self, since: int) -> dict[str, Any]:
        fresh = [e for e in self.events if e["seq"] > since]
        return {"cursor": self.event_cursor, "events": fresh}

    def ui_state(self) -> dict[str, Any]:
        return {
            "lc7001": self.lc7001_up,
            "hue": self.hue_up,
            "links": [
                {
                    "name": link.name,
                    "lc7001_zid": link.zid,
                    "hue_resource": link.hue_resource,
                    "hue_id": link.hue_id,
                    "min_brightness": link.min_brightness,
                    "max_brightness": link.max_brightness,
                    "follow_hue": link.follow_hue,
                    "ramp_mode": link.ramp_mode,
                    "scene_cycle": link.scene_cycle,
                    "state": {
                        "on": link.desired.get("on"),
                        "level": link.desired.get("level"),
                    }
                    if link.desired
                    else None,
                }
                for link in self.links
            ],
            "scenes": [
                {
                    "name": scene.name,
                    "lc7001_sid": scene.sid,
                    "hue_resource": scene.hue_resource,
                    "hue_id": scene.hue_id,
                    "on": scene.on,
                    "brightness": scene.brightness,
                }
                for scene in self.config.scenes
            ],
        }

    async def ui_devices(self) -> dict[str, Any]:
        from hue_client import summarize_targets

        try:
            targets = await summarize_targets(self.hue)
            self.hue_up = True
        except Exception as error:  # noqa: BLE001
            _log.warning("could not list Hue targets: %s", error)
            self.hue_up = False
            targets = []

        return {
            "zones": [
                {
                    "zid": zid,
                    "name": entry.get("Name", "?"),
                    "type": entry.get("DeviceType", ""),
                }
                for zid, entry in sorted(self.zone_catalog.items())
            ],
            "scenes": [
                {"sid": sid, "name": name}
                for sid, name in sorted(self.scene_catalog.items())
            ],
            "hue": targets,
        }

    async def ui_identify(self, resource: str, rid: str) -> None:
        """Blink a Hue target so the user can see which one it is."""
        if not resource or not rid:
            return
        # Mark it as our own write so the follower doesn't mistake the flashing
        # for a real Hue change and push it back onto the wall dimmer.
        link = self._by_hue.get((resource, rid))
        if link is not None:
            link.last_write_to_hue = time.monotonic() + 3.0
        try:
            for _ in range(3):
                await self.hue.set_state(resource, rid, on=True, brightness=100)
                await asyncio.sleep(0.45)
                await self.hue.set_state(resource, rid, on=False)
                await asyncio.sleep(0.45)
        except Exception as error:  # noqa: BLE001
            _log.warning("identify failed: %s", error)

    async def ui_save(self, payload: Mapping[str, Any]) -> None:
        """Persist new mappings from the web UI and apply them live."""
        raw: dict[str, Any] = {}
        if self.config_path is not None and self.config_path.exists():
            raw = json.loads(self.config_path.read_text())
        # The web UI only edits a subset of each row's fields. Carry the rest
        # (throttle, transition, debounce, Hue scene recall) across from the
        # existing config so saving from the browser never silently discards
        # hand-tuning done in the file.
        keep_links = (
            "throttle", "transition_ms", "jump_threshold",
            "endpoint_hold", "ramp_transition_ms", "flick_seconds",
        )
        keep_scenes = ("transition_ms", "hue_scene_id")

        def merge(incoming: list, existing: list, key: str, keep: tuple) -> list:
            previous = {row.get(key): row for row in existing if row.get(key) is not None}
            merged = []
            for row in incoming:
                row = dict(row)
                old = previous.get(row.get(key), {})
                for field_name in keep:
                    if field_name not in row and field_name in old:
                        row[field_name] = old[field_name]
                merged.append(row)
            return merged

        raw["links"] = merge(
            list(payload.get("links", [])),
            raw.get("links", []),
            "lc7001_zid",
            keep_links,
        )
        raw["scenes"] = merge(
            list(payload.get("scenes", [])),
            raw.get("scenes", []),
            "lc7001_sid",
            keep_scenes,
        )

        if self.config_path is not None:
            self.config_path.write_text(json.dumps(raw, indent=2) + "\n")
            self.config_path.chmod(0o600)
            new_config = Config.load(self.config_path)
        else:  # pragma: no cover - only when run without a config file
            return

        for task in self._pushers:
            task.cancel()
        await asyncio.gather(*self._pushers, return_exceptions=True)

        self.config = new_config
        self.links = new_config.links
        self._index_links()
        self._reset_wakers()
        self._pushers = [
            asyncio.create_task(self._run_pusher(link)) for link in self.links
        ]
        for link in self.links:
            entry = self.zone_catalog.get(link.zid) or {}
            if lc7001.aio.Composer.POWER in entry:
                await self.on_lc7001_zone(link.zid, entry)
        _log.info(
            "config reloaded: %d dimmer(s), %d button(s)",
            len(self.links),
            len(self.config.scenes),
        )

    # ---- LC7001 -> Hue ---------------------------------------------------

    async def on_lc7001_zone(self, zid: int, properties: Mapping[str, Any]) -> None:
        link = self._by_zid.get(zid)
        if link is None:
            return

        # No time-based echo filter is needed in this direction: when we write
        # to the LC7001 ourselves we also record the values in link.desired, so
        # the hub's echo compares equal below and changes nothing. A genuine
        # paddle press always differs, and is never swallowed.
        changed = False
        flicked = False
        if lc7001.aio.Composer.POWER in properties:
            power = bool(properties[lc7001.aio.Composer.POWER])
            if link.desired.get("on") != power:
                was_on = link.desired.get("on")
                link.desired["on"] = power
                changed = True
                if not power:
                    link.last_off_at = time.monotonic()
                elif was_on is False:
                    flicked = link.is_flick(time.monotonic())
        if lc7001.aio.Composer.POWER_LEVEL in properties:
            level = int(properties[lc7001.aio.Composer.POWER_LEVEL])
            if link.desired.get("level") != level:
                link.desired["level"] = level
                changed = True

        if flicked:
            # Off and straight back on means "next scene", not "restore the old
            # level". Mark the state as already delivered so the pusher doesn't
            # race the scene recall with a brightness write; Follow Hue brings
            # the wall dimmer back into step once the scene lands.
            link.last_off_at = 0.0
            link.skip_push = True
            await self.cycle_scene(link)
            return

        if changed:
            _log.info(
                "[%s] wall switch -> power=%s level=%s",
                link.name,
                link.desired.get("on"),
                link.desired.get("level"),
            )
            link.wake.set()

    async def _pusher(self, link: Link) -> None:
        """Coalesce rapid dimmer movement into a paced stream of Hue writes."""
        while True:
            await link.wake.wait()
            link.wake.clear()

            snapshot = self._current_snapshot(link)
            if snapshot == link.last_sent:
                continue

            # A scene recall is on its way for this switch. Writing a brightness
            # now would land on top of it and undo the scene, so adopt the state
            # as already delivered and stay out of the way.
            if link.skip_push:
                link.skip_push = False
                link.last_sent = snapshot
                continue

            big = link.is_big_swing(link.last_sent, snapshot["on"], snapshot["level"])
            hold = link.hold_seconds()

            # A big swing might be a real command, or it might be the hub
            # announcing where a ramp is headed before telling us where the
            # paddle actually stopped. Sit on it briefly; if a different value
            # arrives meanwhile, the first one was a phantom and never reaches
            # the lights.
            if big and hold > 0:
                await asyncio.sleep(hold)
                fresh = self._current_snapshot(link)
                if fresh != snapshot:
                    _log.debug("[%s] discarded an unconfirmed ramp endpoint", link.name)
                    link.wake.set()
                    continue

            on = snapshot["on"]
            level = snapshot["level"]

            brightness = link.scale(level) if (on and level is not None) else None
            duration = link.ramp_ms() if big else link.transition_ms
            try:
                await self.hue.set_state(
                    link.hue_resource,
                    link.hue_id,
                    on=on,
                    brightness=brightness,
                    duration_ms=duration,
                )
            except Exception as error:  # noqa: BLE001
                _log.warning("[%s] Hue write failed: %s", link.name, error)
                await asyncio.sleep(1.0)
                link.wake.set()  # retry
                continue

            link.last_sent = snapshot
            link.last_write_to_hue = time.monotonic()
            _log.debug("[%s] hue <- on=%s brightness=%s", link.name, on, brightness)

            # Pace the writes, then pick up anything that moved while we slept.
            await asyncio.sleep(link.throttle)
            if self._current_snapshot(link) != link.last_sent:
                link.wake.set()

    @staticmethod
    def _current_snapshot(link: Link) -> dict[str, Any]:
        on = bool(link.desired.get("on", False))
        return {"on": on, "level": link.desired.get("level") if on else None}

    async def cycle_scene(self, link: Link) -> None:
        """Step this switch's Hue room on to its next scene."""
        try:
            if not link.scene_list:
                link.scene_list = await scenes_for_target(
                    self.hue, link.hue_resource, link.hue_id
                )
            if not link.scene_list:
                _log.info(
                    "[%s] flick ignored: no Hue scenes on this target "
                    "(scenes belong to rooms and zones, not single bulbs)",
                    link.name,
                )
                return
            link.scene_index = (link.scene_index + 1) % len(link.scene_list)
            scene = link.scene_list[link.scene_index]
            _log.info(
                "[%s] flick -> scene %d/%d: %s",
                link.name,
                link.scene_index + 1,
                len(link.scene_list),
                scene["name"],
            )
            self.log_event("flick", link.zid, {"scene": scene["name"]})
            await recall_scene(self.hue, scene["id"])
        except HueError as error:
            _log.warning("[%s] could not recall a scene: %s", link.name, error)
        except Exception:  # noqa: BLE001
            _log.exception("[%s] scene cycling failed", link.name)

    # ---- LC7001 scenes (scene-controller buttons) -> Hue -----------------

    async def on_lc7001_scene(self, sid: int) -> None:
        targets = [s for s in self.config.scenes if s.sid == sid]
        if not targets:
            _log.debug("scene %s fired, nothing mapped to it", sid)
            return
        for scene in targets:
            try:
                if scene.hue_scene_id:
                    await self.hue.put(
                        "scene", scene.hue_scene_id, {"recall": {"action": "active"}}
                    )
                    _log.info("[%s] scene button -> recalled Hue scene", scene.name)
                else:
                    await self.hue.set_state(
                        scene.hue_resource,
                        scene.hue_id,
                        on=scene.on,
                        brightness=scene.brightness,
                        duration_ms=scene.transition_ms,
                    )
                    _log.info(
                        "[%s] scene button -> hue on=%s brightness=%s",
                        scene.name,
                        scene.on,
                        scene.brightness,
                    )
            except Exception as error:  # noqa: BLE001
                _log.warning("[%s] Hue scene write failed: %s", scene.name, error)

    # ---- Hue -> LC7001 ---------------------------------------------------

    async def _follower(self) -> None:
        """Keep the wall dimmer's level in step with changes made in Hue."""
        if not any(link.follow_hue for link in self.links):
            return
        while True:
            try:
                async for batch in self.hue.events():
                    for event in batch:
                        for item in event.get("data", []):
                            await self._on_hue_event(item)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                _log.warning("Hue event stream dropped (%s); retrying", error)
                await asyncio.sleep(5.0)

    async def _on_hue_event(self, item: Mapping[str, Any]) -> None:
        link = self._by_hue.get((item.get("type", ""), item.get("id", "")))
        if link is None or not link.follow_hue:
            return
        if time.monotonic() - link.last_write_to_hue < ECHO_SUPPRESSION_SECONDS:
            return

        power = link.desired.get("on")
        level = link.desired.get("level")
        if "on" in item:
            power = bool(item["on"].get("on", power))
        if "dimming" in item and item["dimming"].get("brightness") is not None:
            level = link.unscale(float(item["dimming"]["brightness"]))

        # Ignore the bridge reporting back roughly what we just told it. Hue
        # rounds brightness internally, so allow a point of slack rather than
        # letting a 39.8-vs-40 difference ping-pong between the two hubs.
        current_level = link.desired.get("level")
        same_level = (
            level is not None
            and current_level is not None
            and abs(int(level) - int(current_level)) <= LEVEL_TOLERANCE
        )
        if power == link.desired.get("on") and (same_level or level == current_level):
            return

        link.desired["on"] = power
        link.desired["level"] = level
        link.last_sent = {"on": bool(power), "level": level if power else None}
        _log.info("[%s] hue -> wall switch power=%s level=%s", link.name, power, level)
        await self.hub.apply_zone(link.zid, bool(power), level if power else None)

    # ---- lifecycle -------------------------------------------------------

    async def run(self, web_port: int = 0) -> None:
        async with self.hue:
            try:
                await self.hue.get("light")
                self.hue_up = True
            except Exception as error:  # noqa: BLE001
                _log.warning("Hue bridge not reachable yet: %s", error)

            server = None
            if web_port:
                from webui import WebServer

                server = WebServer(self, "", web_port)
                await server.start()

            self._reset_wakers()
            self._pushers = [
                asyncio.create_task(self._run_pusher(link)) for link in self.links
            ]
            tasks = [
                asyncio.create_task(self.hub.loop()),
                asyncio.create_task(self._follower()),
            ]
            try:
                # _pushers is rebuilt on reload, so wait on the stable tasks
                # and let the pushers live alongside them.
                await asyncio.gather(*tasks)
            finally:
                for task in tasks + self._pushers:
                    task.cancel()
                await asyncio.gather(
                    *tasks, *self._pushers, return_exceptions=True
                )
                if server is not None:
                    await server.close()


# --------------------------------------------------------------------------
# live event listener (identify which switch is which)
# --------------------------------------------------------------------------


async def listen(config: Config, seconds: float) -> int:
    """Print every zone change and scene trigger as it happens.

    Walk over, tap a switch, and its name and ZID appear here.
    """
    key = (
        lc7001.aio.hash_password(config.lc7001_password.encode())
        if config.lc7001_password
        else None
    )
    hub = lc7001.aio.Hub(config.lc7001_host, key=key, loop_timeout=-1)
    zone_names: dict[int, str] = {}
    scene_names: dict[int, str] = {}

    async def on_auth(_address: str | None = None) -> None:
        await hub.send(hub.compose_list_zones())
        await hub.send(hub.compose_list_scenes())

    async def on_list_zones(message: Mapping) -> None:
        for item in message.get(hub.ZONE_LIST, []) or []:
            await hub.send(hub.compose_report_zone_properties(int(item[hub.ZID])))

    async def on_list_scenes(message: Mapping) -> None:
        for item in message.get(hub.SCENE_LIST, []) or []:
            await hub.send(hub.compose_report_scene_properties(int(item[hub.SID])))

    async def on_zone_report(message: Mapping) -> None:
        zid = message.get(hub.ZID)
        properties = message.get(hub.PROPERTY_LIST) or {}
        if zid is not None:
            zone_names[int(zid)] = properties.get(hub.NAME, "?")

    async def on_scene_report(message: Mapping) -> None:
        sid = message.get(hub.SID)
        properties = message.get(hub.PROPERTY_LIST) or {}
        if sid is not None:
            scene_names[int(sid)] = properties.get(hub.NAME, "?")

    async def on_zone_changed(message: Mapping) -> None:
        zid = message.get(hub.ZID)
        properties = message.get(hub.PROPERTY_LIST) or {}
        if zid is None:
            return
        bits = []
        if hub.POWER in properties:
            bits.append("on" if properties[hub.POWER] else "off")
        if hub.POWER_LEVEL in properties:
            bits.append(f"level {properties[hub.POWER_LEVEL]}")
        print(
            f"  {time.strftime('%H:%M:%S')}  ZONE  ZID {zid:<3} "
            f"{zone_names.get(int(zid), '?'):<24} {'  '.join(bits)}",
            flush=True,
        )

    async def on_scene_run(message: Mapping) -> None:
        sid = message.get(hub.SID)
        if sid is None:
            return
        print(
            f"  {time.strftime('%H:%M:%S')}  SCENE SID {sid:<3} "
            f"{scene_names.get(int(sid), '?')}",
            flush=True,
        )

    hub.on(hub.EVENT_AUTHENTICATED, on_auth)
    hub.on(hub.EVENT_LIST_ZONES, on_list_zones)
    hub.on(hub.EVENT_LIST_SCENES, on_list_scenes)
    hub.on(hub.EVENT_REPORT_ZONE_PROPERTIES, on_zone_report)
    hub.on(hub.EVENT_REPORT_SCENE_PROPERTIES, on_scene_report)
    hub.on(hub.EVENT_ZONE_PROPERTIES_CHANGED, on_zone_changed)
    hub.on(hub.EVENT_RUN_SCENE, on_scene_run)
    hub.on(hub.EVENT_SCENE_PROPERTIES_CHANGED, on_scene_run)

    task = asyncio.create_task(hub.loop())
    await asyncio.sleep(3.0)  # let the name tables fill in
    print(
        f"\nListening for {int(seconds)}s -- go tap your switches and scene "
        f"controller buttons.\n"
    )
    try:
        await asyncio.sleep(seconds)
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
    print("\nDone listening.")
    return 0


# --------------------------------------------------------------------------
# one-shot connectivity check
# --------------------------------------------------------------------------


async def check(config: Config) -> int:
    problems = 0

    print(f"Hue bridge  {config.hue_ip}")
    try:
        async with HueBridge(config.hue_ip, config.hue_app_key) as hue:
            lights = await hue.get("light")
            print(f"  ok - {len(lights)} light(s) visible")
    except Exception as error:  # noqa: BLE001
        print(f"  FAILED - {error}")
        problems += 1

    print(f"LC7001      {config.lc7001_host}")
    key = (
        lc7001.aio.hash_password(config.lc7001_password.encode())
        if config.lc7001_password
        else None
    )
    zones: dict[int, dict] = {}
    hub = lc7001.aio.Hub(config.lc7001_host, key=key, loop_timeout=-1)

    async def collect(message: Mapping) -> None:
        if hub.StatusError(message):
            return
        zid = message.get(hub.ZID)
        if zid is not None:
            zones[int(zid)] = dict(message.get(hub.PROPERTY_LIST) or {})

    async def on_auth(_address: str | None = None) -> None:
        for link in config.links:
            await hub.send(hub.compose_report_zone_properties(link.zid))

    hub.on(hub.EVENT_REPORT_ZONE_PROPERTIES, collect)
    hub.on(hub.EVENT_AUTHENTICATED, on_auth)

    task = asyncio.create_task(hub.loop())
    try:
        failure: BaseException | None = None
        for _ in range(100):  # up to ~10 seconds
            if len(zones) >= len(config.links):
                break
            if task.done():
                failure = task.exception()
                break
            await asyncio.sleep(0.1)
        if failure is not None:
            print(f"  FAILED - {failure}")
            problems += 1
        else:
            print(f"  ok - reported {len(zones)} of {len(config.links)} mapped zone(s)")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    for link in config.links:
        properties = zones.get(link.zid)
        if properties is None:
            print(f"  ! {link.name}: zone {link.zid} did not report")
            problems += 1
        else:
            print(
                f"  - {link.name}: zone {link.zid} "
                f"'{properties.get('Name', '?')}' "
                f"power={properties.get('Power')} level={properties.get('PowerLevel')} "
                f"-> hue {link.hue_resource} {link.hue_id[:8]}"
            )

    print("\nAll good." if problems == 0 else f"\n{problems} problem(s) found.")
    return 1 if problems else 0


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("config.json"),
        help="path to config.json",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="test connectivity and print the mapped zones, then exit",
    )
    parser.add_argument(
        "--listen",
        nargs="?",
        type=float,
        const=60.0,
        default=None,
        metavar="SECONDS",
        help="print every switch/scene event as it happens, to identify devices",
    )
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    if not args.config.exists():
        print(f"no config at {args.config} - run setup_wizard.py first", file=sys.stderr)
        return 2

    config = Config.load(args.config)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else config.log_level,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    logging.getLogger("lc7001.aio").setLevel(
        logging.DEBUG if args.verbose else logging.WARNING
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.listen is not None:
        return asyncio.run(listen(config, args.listen))

    if args.check:
        return asyncio.run(check(config))

    bridge = Bridge(config, config_path=args.config)

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        task = asyncio.create_task(bridge.run(web_port=config.web_port))
        await asyncio.wait({task, asyncio.create_task(stop.wait())},
                           return_when=asyncio.FIRST_COMPLETED)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    _log.info(
        "lc7001-hue starting: %d dimmer(s), %d button(s), web UI on port %d",
        len(config.links), len(config.scenes), config.web_port,
    )
    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        pass
    _log.info("lc7001-hue stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
