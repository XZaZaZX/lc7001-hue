#!/usr/bin/env python3
"""Offline test: fake LC7001 hub + fake Hue bridge, real bridge code in between.

Run with:  python3 test_harness.py
No hardware, no network beyond localhost.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import lc7001_hue
from lc7001_hue import Bridge, Config, Link, SceneLink

# --------------------------------------------------------------------------
# fake Hue bridge (plain HTTP; the real one is HTTPS with a self-signed cert)
# --------------------------------------------------------------------------

HUE_PUTS: list[tuple[str, dict]] = []
HUE_EVENTS: "asyncio.Queue[str]" = asyncio.Queue()
_events_thread_queue: list[str] = []
_events_lock = threading.Lock()


def push_hue_event(payload: list[dict[str, Any]]) -> None:
    with _events_lock:
        _events_thread_queue.append(json.dumps(payload))


class FakeHue(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:  # quiet
        pass

    def _json(self, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/eventstream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            while True:
                with _events_lock:
                    pending = list(_events_thread_queue)
                    _events_thread_queue.clear()
                for payload in pending:
                    try:
                        self.wfile.write(f"data: {payload}\n\n".encode())
                        self.wfile.flush()
                    except OSError:
                        return
                threading.Event().wait(0.05)
        elif self.path.endswith("/light"):
            self._json({"errors": [], "data": [{"id": "light-1"}]})
        elif "/grouped_light/" in self.path:
            self._json({"errors": [], "data": [{"owner": {"rid": "room-1"}}]})
        elif self.path.endswith("/scene"):
            self._json({"errors": [], "data": [
                {"id": "scene-a", "group": {"rid": "room-1"},
                 "metadata": {"name": "Relax"}},
                {"id": "scene-b", "group": {"rid": "room-1"},
                 "metadata": {"name": "Concentrate"}},
                {"id": "scene-z", "group": {"rid": "other-room"},
                 "metadata": {"name": "Somewhere else"}},
            ]})
        else:
            self._json({"errors": [], "data": []})

    def do_PUT(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        HUE_PUTS.append((self.path, body))
        self._json({"errors": [], "data": [{"rid": "x"}]})


# --------------------------------------------------------------------------
# fake LC7001 hub
# --------------------------------------------------------------------------

ZONES = {
    3: {"Name": "Bedroom Main", "DeviceType": "Dimmer", "Power": False, "PowerLevel": 50},
    4: {"Name": "Bedroom Lamps", "DeviceType": "Dimmer", "Power": False, "PowerLevel": 50},
}


SCENES = {5: "Scene Controller 2", 6: "Scene Controller 3"}


class FakeLC7001:
    def __init__(self) -> None:
        self.server: asyncio.AbstractServer | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.sets: list[dict] = []
        self.port = 0
        # The real hub echoes every write back as a ZonePropertiesChanged.
        self.echo_writes = False

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    def _send(self, message: dict) -> None:
        assert self.writer is not None
        self.writer.write(json.dumps(message).encode() + b"\x00")

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.writer = writer
        # unauthenticated hubs greet with their MAC
        writer.write(b'{"MAC":"0026EC010203"}\x00')
        await writer.drain()
        while True:
            try:
                frame = await reader.readuntil(b"\x00")
            except (asyncio.IncompleteReadError, ConnectionResetError):
                return
            message = json.loads(frame[:-1])
            service = message.get("Service")
            _id = message.get("ID", 0)
            if service == "ReportZoneProperties":
                zid = message["ZID"]
                self._send(
                    {
                        "ID": _id,
                        "Service": "ReportZoneProperties",
                        "ZID": zid,
                        "PropertyList": dict(ZONES[zid]),
                        "Status": "Success",
                    }
                )
            elif service == "ListZones":
                self._send(
                    {
                        "ID": _id,
                        "Service": "ListZones",
                        "ZoneList": [{"ZID": z} for z in ZONES],
                        "Status": "Success",
                    }
                )
            elif service == "ListScenes":
                self._send(
                    {
                        "ID": _id,
                        "Service": "ListScenes",
                        "SceneList": [{"SID": sid} for sid in SCENES],
                        "Status": "Success",
                    }
                )
            elif service == "ReportSceneProperties":
                sid = message["SID"]
                self._send(
                    {
                        "ID": _id,
                        "Service": "ReportSceneProperties",
                        "SID": sid,
                        "PropertyList": {"Name": SCENES[sid], "ZoneList": []},
                        "Status": "Success",
                    }
                )
            elif service == "SetZoneProperties":
                self.sets.append(message)
                zid = message["ZID"]
                ZONES[zid].update(message.get("PropertyList", {}))
                self._send(
                    {
                        "ID": _id,
                        "Service": "SetZoneProperties",
                        "ZID": zid,
                        "Status": "Success",
                    }
                )
                if self.echo_writes:
                    self.wall_change(zid, **message.get("PropertyList", {}))
            await writer.drain()

    def last_set_level(self, zid: int) -> "int | None":
        """The PowerLevel of the most recent write this hub received."""
        for message in reversed(self.sets):
            if message.get("ZID") != zid:
                continue
            level = (message.get("PropertyList") or {}).get("PowerLevel")
            if level is not None:
                return int(level)
        return None

    def press_scene_button(self, sid: int) -> None:
        """Simulate a scene-controller button press."""
        self._send({"ID": 0, "Service": "RunScene", "SID": sid, "Status": "Success"})

    def wall_change(self, zid: int, **properties: Any) -> None:
        """Simulate someone touching the physical paddle."""
        ZONES[zid].update(properties)
        self._send(
            {
                "ID": 0,
                "Service": "ZonePropertiesChanged",
                "ZID": zid,
                "PropertyList": dict(properties),
                "Status": "Success",
            }
        )


# --------------------------------------------------------------------------

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


async def main() -> int:
    lc7001_hue.ECHO_SUPPRESSION_SECONDS = 1.0

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeHue)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    hue_port = httpd.server_address[1]

    hub = FakeLC7001()
    await hub.start()

    config = Config(
        lc7001_host="127.0.0.1",
        lc7001_password=None,
        hue_ip=f"127.0.0.1:{hue_port}",
        hue_app_key="test-key",
        hue_scheme="http",
        links=[
            Link(
                name="Bedroom",
                zid=3,
                hue_resource="grouped_light",
                hue_id="group-abc",
                min_brightness=1.0,
                throttle=0.15,
                ramp_mode="fade",
                jump_threshold=30,
            )
        ],
        scenes=[
            SceneLink(
                name="Sunroom on",
                sid=5,
                hue_resource="grouped_light",
                hue_id="sunroom-group",
                on=True,
                brightness=60.0,
            ),
            SceneLink(
                name="Sunroom off",
                sid=6,
                hue_resource="grouped_light",
                hue_id="sunroom-group",
                on=False,
            ),
        ],
    )
    cfg_path = pathlib.Path(tempfile.mkdtemp()) / "config.json"
    cfg_path.write_text(json.dumps({
        "lc7001": {"host": "127.0.0.1"},
        "hue": {"ip": config.hue_ip, "app_key": "test-key", "scheme": "http"},
        "links": [{"name": "Bedroom", "lc7001_zid": 3,
                   "hue_resource": "grouped_light", "hue_id": "group-abc",
                   "min_brightness": 1.0, "throttle": 0.15}],
        "scenes": [{"name": "Sunroom on", "lc7001_sid": 5,
                    "hue_resource": "grouped_light", "hue_id": "sunroom-group",
                    "on": True, "brightness": 60},
                   {"name": "Sunroom off", "lc7001_sid": 6,
                    "hue_resource": "grouped_light", "hue_id": "sunroom-group",
                    "on": False}],
    }))
    bridge = Bridge(config, config_path=cfg_path)
    bridge.hub._port = hub.port  # point the client at the fake hub's port

    web_port = 18582
    task = asyncio.create_task(bridge.run(web_port=web_port))
    await asyncio.sleep(1.0)

    print("\n1. initial state pulled on connect")
    check("connected and read zone 3", bridge.links[0].desired.get("level") == 50,
          str(bridge.links[0].desired))

    print("\n2. wall switch on at 40%")
    HUE_PUTS.clear()
    hub.wall_change(3, Power=True, PowerLevel=40)
    await asyncio.sleep(0.6)
    got = [body for _path, body in HUE_PUTS]
    check("hue told to turn on", any(b.get("on", {}).get("on") for b in got), str(got))
    check(
        "hue brightness is 40",
        any(b.get("dimming", {}).get("brightness") == 40.0 for b in got),
        str(got),
    )

    print("\n3. fast dimmer sweep is coalesced, not spammed")
    HUE_PUTS.clear()
    for level in range(41, 81):
        hub.wall_change(3, PowerLevel=level)
        await asyncio.sleep(0.005)
    await asyncio.sleep(1.0)
    sent = [b.get("dimming", {}).get("brightness") for _p, b in HUE_PUTS]
    check("far fewer hue writes than switch events", len(HUE_PUTS) < 15,
          f"{len(HUE_PUTS)} writes for 40 events")
    check("last hue write is the final level (80)", sent and sent[-1] == 80.0, str(sent))

    print("\n4. wall switch off")
    HUE_PUTS.clear()
    hub.wall_change(3, Power=False)
    await asyncio.sleep(1.2)
    check(
        "hue told to turn off",
        any(b.get("on", {}).get("on") is False for _p, b in HUE_PUTS),
        str(HUE_PUTS),
    )

    print("\n5. change made in the Hue app flows back to the wall dimmer")
    await asyncio.sleep(2.0)  # let echo suppression lapse
    hub.sets.clear()
    push_hue_event(
        [
            {
                "type": "update",
                "data": [
                    {
                        "id": "group-abc",
                        "type": "grouped_light",
                        "on": {"on": True},
                        "dimming": {"brightness": 25.0},
                    }
                ],
            }
        ]
    )
    await asyncio.sleep(1.0)
    levels = [
        s.get("PropertyList", {}).get("PowerLevel")
        for s in hub.sets
        if s.get("ZID") == 3
    ]
    check("lc7001 zone set to 25", 25 in levels, str(hub.sets))

    print("\n6. no feedback loop (hue echo of our own write is ignored)")
    HUE_PUTS.clear()
    hub.sets.clear()
    hub.wall_change(3, Power=True, PowerLevel=60)
    await asyncio.sleep(0.4)
    push_hue_event(
        [
            {
                "type": "update",
                "data": [
                    {
                        "id": "group-abc",
                        "type": "grouped_light",
                        "on": {"on": True},
                        "dimming": {"brightness": 60.0},
                    }
                ],
            }
        ]
    )
    await asyncio.sleep(0.8)
    check("echo did not bounce back to lc7001", len(hub.sets) == 0, str(hub.sets))

    print("\n7. paddle press right after a Hue app change still wins")
    push_hue_event(
        [
            {
                "type": "update",
                "data": [
                    {
                        "id": "group-abc",
                        "type": "grouped_light",
                        "on": {"on": True},
                        "dimming": {"brightness": 10.0},
                    }
                ],
            }
        ]
    )
    await asyncio.sleep(0.3)
    HUE_PUTS.clear()
    hub.wall_change(3, Power=True, PowerLevel=85)  # user grabs the paddle
    await asyncio.sleep(0.8)
    sent = [b.get("dimming", {}).get("brightness") for _p, b in HUE_PUTS]
    check("wall change reached hue at 85", 85.0 in sent, str(sent))

    print("\n8. reconnects after the hub drops the connection")
    HUE_PUTS.clear()
    if hub.writer is not None:
        hub.writer.close()
    await asyncio.sleep(3.0)
    hub.wall_change(3, Power=True, PowerLevel=30)
    await asyncio.sleep(0.8)
    sent = [b.get("dimming", {}).get("brightness") for _p, b in HUE_PUTS]
    check("still working after a dropped connection", 30.0 in sent, str(sent))

    print("\n9. scene-controller button drives a separate Hue target")
    HUE_PUTS.clear()
    hub.press_scene_button(5)
    await asyncio.sleep(0.8)
    hit = [(p, b) for p, b in HUE_PUTS if "sunroom-group" in p]
    check("scene 5 set the sunroom group on at 60",
          any(b.get("dimming", {}).get("brightness") == 60.0 for _p, b in hit), str(HUE_PUTS))
    HUE_PUTS.clear()
    hub.press_scene_button(6)
    await asyncio.sleep(0.8)
    check("scene 6 turned the sunroom group off",
          any(b.get("on", {}).get("on") is False for _p, b in HUE_PUTS), str(HUE_PUTS))
    HUE_PUTS.clear()
    hub.press_scene_button(2)  # unmapped
    await asyncio.sleep(0.6)
    check("unmapped scene is ignored", len(HUE_PUTS) == 0, str(HUE_PUTS))

    print("\n9a. every pusher task is alive")
    # A pusher that dies takes its dimmer with it, silently. This caught a real
    # bug: Config.load() runs before asyncio.run(), and on Python 3.9 the
    # asyncio.Event built there belongs to a different loop, so the very first
    # wait() blew up and the dimmer went dead.
    dead = [
        (link.name, task.exception())
        for link, task in zip(bridge.links, bridge._pushers)
        if task.done()
    ]
    check("no pusher has exited", not dead, str(dead))
    check("every link has a waker bound in-loop",
          all(link.wake is not None for link in bridge.links))

    print("\n9aa. the three paddle-hold modes behave differently")
    from lc7001_hue import RAMP_MODES
    link0 = bridge.links[0]
    for mode, (hold, glide) in RAMP_MODES.items():
        link0.ramp_mode = mode
        check(f"mode '{mode}' hold={hold}s glide={glide}ms",
              link0.hold_seconds() == hold and link0.ramp_ms() == glide,
              f"{link0.hold_seconds()} {link0.ramp_ms()}")
    link0.ramp_mode = "steady"
    hub.wall_change(3, Power=True, PowerLevel=40)
    await asyncio.sleep(0.8)
    HUE_PUTS.clear()
    hub.wall_change(3, PowerLevel=100)      # phantom endpoint
    await asyncio.sleep(0.5)
    hub.wall_change(3, PowerLevel=55)       # the truth, before the hold expires
    await asyncio.sleep(3.5)
    sent = [b.get("dimming", {}).get("brightness") for _p, b in HUE_PUTS if "dimming" in b]
    check("'no bounce' mode never shows the phantom 100", 100.0 not in sent, str(sent))
    check("'no bounce' mode lands on the real level", sent and sent[-1] == 55.0, str(sent))
    link0.ramp_mode = "fade"

    print("\n9b. momentary off mid-ramp does not blink the lights")
    # Reproduces what the real LC7001 does: partway through a ramp it reports
    # Power=False, then Power=True at a lower level a fraction of a second later.
    hub.wall_change(3, Power=True, PowerLevel=80)
    await asyncio.sleep(0.5)
    HUE_PUTS.clear()
    for level in (75, 70, 65):
        hub.wall_change(3, PowerLevel=level)
        await asyncio.sleep(0.15)
        hub.wall_change(3, Power=False)          # the spurious off
        await asyncio.sleep(0.3)
        hub.wall_change(3, Power=True, PowerLevel=level - 2)
        await asyncio.sleep(0.15)
    await asyncio.sleep(1.0)
    offs = [b for _p, b in HUE_PUTS if b.get("on", {}).get("on") is False]
    check("no off was ever sent to hue during the ramp", not offs, str(HUE_PUTS)[:300])
    brightnesses = [
        b.get("dimming", {}).get("brightness") for _p, b in HUE_PUTS if "dimming" in b
    ]
    check("the ramp still reached the final level (63)",
          brightnesses and brightnesses[-1] == 63.0, str(brightnesses))

    print("\n9c. big swings glide slowly, small nudges stay brisk")
    await asyncio.sleep(1.0)
    HUE_PUTS.clear()
    hub.wall_change(3, Power=True, PowerLevel=60)   # settle somewhere mid-range
    await asyncio.sleep(0.6)
    HUE_PUTS.clear()
    hub.wall_change(3, PowerLevel=62)               # a nudge
    await asyncio.sleep(0.6)
    nudge = [b.get("dynamics", {}).get("duration") for _p, b in HUE_PUTS]
    check("a small step uses the brisk glide", nudge and nudge[-1] == 400, str(nudge))

    HUE_PUTS.clear()
    hub.wall_change(3, PowerLevel=100)              # the phantom ramp endpoint
    await asyncio.sleep(0.6)
    jump = [b.get("dynamics", {}).get("duration") for _p, b in HUE_PUTS]
    check("a jump to the endpoint eases in slowly", jump and jump[-1] == 2000, str(jump))

    HUE_PUTS.clear()
    hub.wall_change(3, Power=False)                 # off is always a big swing
    await asyncio.sleep(1.2)
    offs = [b.get("dynamics", {}).get("duration") for _p, b in HUE_PUTS
            if b.get("on", {}).get("on") is False]
    check("turning off fades rather than snaps", offs and offs[-1] == 2000, str(HUE_PUTS))

    print("\n9e. flicking the switch off and back on cycles Hue scenes")
    hub.wall_change(3, Power=True, PowerLevel=50)
    await asyncio.sleep(1.2)
    link0.scene_cycle = True          # arm it only once the zone has settled on
    link0.scene_list = []
    link0.scene_index = -1
    link0.last_off_at = 0.0

    # A deliberate flick: off, then straight back on inside the window.
    HUE_PUTS.clear()
    hub.wall_change(3, Power=False)
    await asyncio.sleep(0.4)
    hub.wall_change(3, Power=True, PowerLevel=50)
    await asyncio.sleep(1.2)
    recalls = [pth for pth, b in HUE_PUTS if "recall" in b]
    check("a flick recalls a scene", len(recalls) == 1, str(HUE_PUTS)[:300])
    check("it recalls the room's first scene",
          recalls and recalls[0].endswith("/scene-a"), str(recalls))
    # The off half of the flick reaches the lights -- that really happened at the
    # wall. What must not happen is a brightness write after the recall, undoing
    # the scene we just set.
    after = [b for pth, b in HUE_PUTS[[p for p, _ in HUE_PUTS].index(recalls[0]) + 1:]
             if "dimming" in b] if recalls else []
    check("nothing overwrites the scene afterwards", not after, str(HUE_PUTS)[:400])

    # A second flick steps on, and wraps past the end of the list.
    HUE_PUTS.clear()
    for expected in ("/scene-b", "/scene-a"):
        hub.wall_change(3, Power=False)
        await asyncio.sleep(0.4)
        hub.wall_change(3, Power=True, PowerLevel=50)
        await asyncio.sleep(1.2)
    recalls = [pth for pth, b in HUE_PUTS if "recall" in b]
    check("further flicks step through and wrap around",
          [r.rsplit("/", 1)[-1] for r in recalls] == ["scene-b", "scene-a"], str(recalls))
    check("scenes from other rooms are not in the rotation",
          not any("scene-z" in r for r in recalls), str(recalls))

    # Off, wait, on -- ordinary use, must NOT be read as a gesture.
    HUE_PUTS.clear()
    hub.wall_change(3, Power=False)
    await asyncio.sleep(2.0)
    hub.wall_change(3, Power=True, PowerLevel=50)
    await asyncio.sleep(1.2)
    check("a slow off-then-on is left alone",
          not [b for _p, b in HUE_PUTS if "recall" in b], str(HUE_PUTS)[:300])
    check("a slow off-then-on still sets brightness normally",
          [b for _p, b in HUE_PUTS if "dimming" in b], str(HUE_PUTS)[:300])

    # And with the toggle off, a flick is just a flick.
    link0.scene_cycle = False
    HUE_PUTS.clear()
    hub.wall_change(3, Power=False)
    await asyncio.sleep(0.4)
    hub.wall_change(3, Power=True, PowerLevel=50)
    await asyncio.sleep(1.2)
    check("scene cycling stays off when the box is unticked",
          not [b for _p, b in HUE_PUTS if "recall" in b], str(HUE_PUTS)[:300])
    link0.scene_list = []
    link0.scene_index = -1

    print("\n9f. a scene is not flattened by Follow Hue chasing it")

    def hue_event(payload: dict) -> None:
        push_hue_event([{"type": "update", "data": [payload]}])

    hub.echo_writes = True          # model the real hub echoing our own writes
    # Straight from a real log: after a recall the bridge reports the group's
    # brightness several times as the individual bulbs arrive (51, 54, 70, 39).
    # Writing each to the wall, then pushing the wall's echo back to Hue as one
    # uniform brightness, used to flatten the scene and saturate it at 100%.
    link0.scene_cycle = True
    link0.last_off_at = 0.0
    hub.wall_change(3, Power=True, PowerLevel=50)
    await asyncio.sleep(1.2)

    HUE_PUTS.clear()
    hub.wall_change(3, Power=False)
    await asyncio.sleep(0.4)
    hub.wall_change(3, Power=True, PowerLevel=50)
    await asyncio.sleep(0.8)

    # The bulbs arrive one by one; the group's aggregate is reported each time.
    for brightness in (51.0, 54.0, 70.0, 39.0):
        hue_event({"type": "grouped_light", "id": "group-abc",
                   "on": {"on": True}, "dimming": {"brightness": brightness}})
        await asyncio.sleep(0.05)
    await asyncio.sleep(1.5)

    writes = [b for _p, b in HUE_PUTS if "dimming" in b]
    check("the settling scene is never written back to Hue", not writes,
          str(HUE_PUTS)[:400])
    check("and it never saturates at full",
          100.0 not in [b["dimming"]["brightness"] for b in writes], str(writes))

    # The wall dimmer still ends up in step once everything has settled.
    await asyncio.sleep(4.0)
    check("the wall dimmer is synced to where the scene landed",
          hub.last_set_level(3) == link0.unscale(39.0),
          f"wall={hub.last_set_level(3)} want={link0.unscale(39.0)}")

    # And an off during all this is still obeyed instantly -- a settling scene
    # must never make the switch feel dead.
    HUE_PUTS.clear()
    hub.wall_change(3, Power=True, PowerLevel=50)
    await asyncio.sleep(0.4)
    hub.wall_change(3, Power=False)
    await asyncio.sleep(0.4)
    link0.scene_until = time.monotonic() + 5.0     # pretend a scene is landing
    hub.wall_change(3, Power=True, PowerLevel=50)
    await asyncio.sleep(0.4)
    HUE_PUTS.clear()
    hub.wall_change(3, Power=False)
    await asyncio.sleep(1.5)
    check("turning the switch off mid-scene still reaches the lights",
          [b for _p, b in HUE_PUTS if b.get("on", {}).get("on") is False],
          str(HUE_PUTS)[:300])

    hub.echo_writes = False
    link0.scene_cycle = False
    link0.scene_until = 0.0
    link0.scene_list = []
    link0.scene_index = -1
    link0.wall_writes = []

    print("\n9d. UI save keeps hand-tuned fields it doesn't display")
    import json as _json
    cfg = _json.loads(cfg_path.read_text())
    cfg["links"][0]["throttle"] = 0.42
    cfg["links"][0]["transition_ms"] = 777
    cfg_path.write_text(_json.dumps(cfg))
    await bridge.ui_save({
        "links": [{"name": "Bedroom", "lc7001_zid": 3,
                   "hue_resource": "grouped_light", "hue_id": "group-abc",
                   "min_brightness": 9, "max_brightness": 100, "follow_hue": True}],
        "scenes": [],
    })
    saved = _json.loads(cfg_path.read_text())["links"][0]
    check("throttle survived the save", saved.get("throttle") == 0.42, str(saved))
    check("transition survived the save", saved.get("transition_ms") == 777, str(saved))
    check("the edited field was applied", saved.get("min_brightness") == 9, str(saved))

    print("\n10. web UI")

    def get(path: str) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{web_port}{path}", timeout=5) as r:
            return json.loads(r.read())

    def post(path: str, payload: dict) -> int:
        req = urllib.request.Request(
            f"http://127.0.0.1:{web_port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status

    def fetch_page() -> bytes:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{web_port}/", timeout=5
        ) as response:
            return response.read()

    # every HTTP call goes through a thread: a blocking urlopen on the event
    # loop would deadlock against the server running on that same loop
    page = await asyncio.to_thread(fetch_page)
    check("page loads", b"lc7001-hue" in page and b"Live switch activity" in page)

    state = await asyncio.to_thread(get, "/api/state")
    check("state reports LC7001 connected", state["lc7001"] is True, str(state))
    check("state lists the mapped dimmer", state["links"][0]["lc7001_zid"] == 3, str(state))

    devices = await asyncio.to_thread(get, "/api/devices")
    zids = [z["zid"] for z in devices["zones"]]
    sids = [s["sid"] for s in devices["scenes"]]
    check("device picker lists every LC7001 zone", 3 in zids and 4 in zids, str(zids))
    check("device picker lists scenes with names", 5 in sids and 6 in sids, str(sids))

    events = await asyncio.to_thread(get, "/api/events?since=0")
    check("event log captured switch activity", len(events["events"]) > 0,
          str(events)[:200])
    check("event log names the zone",
          any(e["name"] == "Bedroom Main" for e in events["events"]), str(events)[:300])

    # remap the dimmer onto the second zone and confirm it takes effect live
    status = await asyncio.to_thread(post, "/api/config", {
        "links": [{"name": "Remapped", "lc7001_zid": 4,
                   "hue_resource": "grouped_light", "hue_id": "group-xyz",
                   "min_brightness": 1, "max_brightness": 100,
                   "follow_hue": True}],
        "scenes": [],
    })
    check("save returns ok", status == 200, str(status))
    await asyncio.sleep(0.5)
    HUE_PUTS.clear()
    hub.wall_change(4, Power=True, PowerLevel=70)
    await asyncio.sleep(0.8)
    hit = [b for p, b in HUE_PUTS if "group-xyz" in p]
    check("remapped dimmer drives the new Hue target",
          any(b.get("dimming", {}).get("brightness") == 70.0 for b in hit), str(HUE_PUTS))
    HUE_PUTS.clear()
    hub.wall_change(3, Power=True, PowerLevel=20)
    await asyncio.sleep(0.6)
    check("the removed mapping no longer fires", len(HUE_PUTS) == 0, str(HUE_PUTS))
    check("config file was rewritten",
          json.loads(cfg_path.read_text())["links"][0]["lc7001_zid"] == 4)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    httpd.shutdown()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
