#!/usr/bin/env python3
"""Interactive setup for lc7001-hue.

Finds the Hue bridge, pairs with it, reads the zone list off the LC7001,
lets you match each wall dimmer to a Hue room / zone / bulb, and writes
config.json.

    python3 setup_wizard.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

import lc7001.aio

from hue_client import (
    HueBridge,
    LinkButtonNotPressed,
    create_app_key,
    discover_bridges,
    summarize_targets,
)

CONFIG_PATH = Path(__file__).with_name("config.json")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def ask_yes(prompt: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    answer = input(f"{prompt} ({suffix}): ").strip().lower()
    if not answer:
        return default
    return answer.startswith("y")


def rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n" + "-" * max(8, len(title)))


# --------------------------------------------------------------------------
# Hue
# --------------------------------------------------------------------------


async def setup_hue(existing: Mapping[str, Any]) -> dict[str, Any]:
    rule("Step 1 - Philips Hue bridge")

    ip = existing.get("ip", "")
    if not ip:
        print("Looking for your Hue bridge...")
        bridges = await discover_bridges()
        if len(bridges) == 1:
            ip = bridges[0].get("internalipaddress", "")
            print(f"  found one at {ip}")
        elif len(bridges) > 1:
            for index, bridge in enumerate(bridges, 1):
                print(f"  {index}) {bridge.get('internalipaddress')}")
            choice = int(ask("Which bridge", "1"))
            ip = bridges[choice - 1].get("internalipaddress", "")
        else:
            print("  couldn't auto-discover (that's common on some networks)")
    ip = ask("Hue bridge IP address", ip)

    app_key = existing.get("app_key", "")
    if app_key and ask_yes("Reuse the saved Hue application key?", True):
        return {"ip": ip, "app_key": app_key}

    print("\nPress the round button on top of the Hue bridge now.")
    input("Then press Return here... ")
    for attempt in range(4):
        try:
            app_key, _client_key = await create_app_key(ip)
            print("  paired with the bridge")
            return {"ip": ip, "app_key": app_key}
        except LinkButtonNotPressed:
            if attempt == 3:
                break
            input("  bridge says the button wasn't pressed - press it, then Return... ")
        except Exception as error:  # noqa: BLE001
            raise SystemExit(f"  could not pair with the Hue bridge: {error}")
    raise SystemExit("  pairing failed - the link button was never registered")


# --------------------------------------------------------------------------
# LC7001
# --------------------------------------------------------------------------


async def read_lc7001_zones(host: str, password: str | None) -> dict[int, dict]:
    """Connect once, list every zone and its properties, then disconnect."""
    key = lc7001.aio.hash_password(password.encode()) if password else None
    hub = lc7001.aio.Hub(host, key=key, loop_timeout=-1)
    zones: dict[int, dict] = {}
    expected: set[int] = set()
    done = asyncio.Event()

    async def on_list(message: Mapping) -> None:
        if hub.StatusError(message):
            return
        for item in message.get(hub.ZONE_LIST, []):
            zid = int(item[hub.ZID])
            expected.add(zid)
            await hub.send(hub.compose_report_zone_properties(zid))
        if not expected:
            done.set()

    async def on_report(message: Mapping) -> None:
        if hub.StatusError(message):
            return
        zid = message.get(hub.ZID)
        if zid is None:
            return
        zones[int(zid)] = dict(message.get(hub.PROPERTY_LIST) or {})
        if expected and expected <= zones.keys():
            done.set()

    async def on_auth(_address: str | None = None) -> None:
        await hub.send(hub.compose_list_zones())

    hub.on(hub.EVENT_AUTHENTICATED, on_auth)
    hub.on(hub.EVENT_LIST_ZONES, on_list)
    hub.on(hub.EVENT_REPORT_ZONE_PROPERTIES, on_report)

    task = asyncio.create_task(hub.loop())
    try:
        waiter = asyncio.create_task(done.wait())
        finished, _ = await asyncio.wait(
            {task, waiter}, timeout=20, return_when=asyncio.FIRST_COMPLETED
        )
        waiter.cancel()
        if task in finished and task.exception() is not None:
            raise SystemExit(f"  could not reach the LC7001: {task.exception()}")
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
    return zones


async def setup_lc7001(existing: Mapping[str, Any]) -> tuple[dict[str, Any], dict[int, dict]]:
    rule("Step 2 - Legrand LC7001 hub")
    print("Use the hub's IP address if 'LCM1.local' doesn't resolve.")
    host = ask("LC7001 host", existing.get("host") or "LCM1.local")
    print("Leave the password blank if the Legrand app never asked you to set one.")
    password = ask("LC7001 password", existing.get("password") or "")

    print("\nReading the zone list...")
    zones = await read_lc7001_zones(host, password or None)
    if not zones:
        raise SystemExit(
            "  no zones came back. Check the host/password, and note the LC7001 "
            "only allows a few simultaneous connections - if Homebridge and the "
            "Legrand app are both connected, close the app and try again."
        )
    print(f"  found {len(zones)} zone(s)")
    return {"host": host, "password": password}, zones


# --------------------------------------------------------------------------
# mapping
# --------------------------------------------------------------------------


def choose_links(zones: dict[int, dict], targets: list[dict[str, str]]) -> list[dict]:
    rule("Step 3 - match your wall dimmers to your Hue lights")

    zone_items = sorted(zones.items())
    print("Legrand devices the LC7001 knows about:\n")
    for index, (zid, properties) in enumerate(zone_items, 1):
        print(
            f"  {index:>2}) {properties.get('Name', '(unnamed)'):<24}"
            f" zone {zid:<3} {properties.get('DeviceType', '?')}"
        )

    print("\nHue targets:\n")
    for index, target in enumerate(targets, 1):
        print(f"  {index:>2}) {target['label']}")

    links: list[dict] = []
    print(
        "\nNow pair them up. A room or zone target is usually what you want - "
        "one command moves every bulb in it together."
    )
    while True:
        raw_zone = ask("\nWall dimmer number (blank when finished)")
        if not raw_zone:
            break
        try:
            zid, properties = zone_items[int(raw_zone) - 1]
        except (ValueError, IndexError):
            print("  that isn't one of the numbers above")
            continue

        raw_target = ask("Hue target number")
        try:
            target = targets[int(raw_target) - 1]
        except (ValueError, IndexError):
            print("  that isn't one of the numbers above")
            continue

        name = ask("Name for this pairing", properties.get("Name", f"zone {zid}"))
        floor = ask(
            "Lowest Hue brightness the dimmer's bottom of travel should give (1-100)",
            "1",
        )

        links.append(
            {
                "name": name,
                "lc7001_zid": zid,
                "hue_resource": target["resource"],
                "hue_id": target["id"],
                "min_brightness": float(floor),
                "max_brightness": 100.0,
                "throttle": 0.3,
                "transition_ms": 300,
                "follow_hue": ask_yes(
                    "Also push Hue app / Apple Home changes back to the wall dimmer?",
                    True,
                ),
            }
        )
        print(f"  linked '{name}' -> {target['label']}")

    return links


# --------------------------------------------------------------------------


async def main() -> int:
    print("\n\033[1mlc7001-hue setup\033[0m")
    print("Legrand RFLC wall dimmers -> Philips Hue lights")

    existing: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        with contextlib.suppress(Exception):
            existing = json.loads(CONFIG_PATH.read_text())
        if existing:
            print(f"\nFound an existing config at {CONFIG_PATH}; reusing what I can.")

    hue_config = await setup_hue(existing.get("hue", {}))
    lc_config, zones = await setup_lc7001(existing.get("lc7001", {}))

    async with HueBridge(hue_config["ip"], hue_config["app_key"]) as bridge:
        targets = await summarize_targets(bridge)
    if not targets:
        raise SystemExit("The Hue bridge reported no lights.")

    links = choose_links(zones, targets)
    if not links:
        print("\nNothing linked - not writing a config.")
        return 1

    config = {
        "lc7001": lc_config,
        "hue": hue_config,
        "links": links,
        "log_level": "INFO",
    }
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
    CONFIG_PATH.chmod(0o600)

    rule("Done")
    print(f"Wrote {CONFIG_PATH}")
    print("\nTest it with:")
    print(f"  python3 {Path(__file__).with_name('lc7001_hue.py')} --check")
    print("\nThen run it for real with:")
    print(f"  python3 {Path(__file__).with_name('lc7001_hue.py')} --verbose")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print()
        sys.exit(130)
