# lc7001-hue

Make Legrand RFLC wall dimmers and scene-controller buttons drive Philips Hue lights.

A small service that connects to a Legrand LC7001 hub and a Hue bridge on your LAN and
keeps them in step. Touch a paddle and the matching Hue room, zone, or bulb follows —
on/off and brightness. Scene-controller buttons work too, and can be repurposed even if
their Legrand scenes no longer control anything. It optionally follows in the other
direction as well, so dimming from the Hue app leaves the wall dimmer at the right level.

Everything is configured from a web page on your LAN, the same way Homebridge is.

No cloud, no account, no polling — it holds a live connection to both hubs.

## Why this exists

HomeKit can't use brightness as a trigger. In the Home app, "When an accessory is
controlled" for a light offers *Turns On* and *Turns Off* and nothing else. So Apple Home
can manage "paddle up = Hue on at 80%," but it can't follow a dimmer. Homebridge doesn't
change that — it's the HomeKit automation model itself — and there's no Homebridge plugin
that mirrors one accessory's brightness onto another.

So this talks to both hubs directly and skips HomeKit entirely. It doesn't depend on
Homebridge; it just coexists with it.

## What I learned about the LC7001

Documented here because I couldn't find it written down anywhere, and it shapes the
design:

- **The hub does not report a paddle ramp while it happens.** Press and hold, and it
  immediately reports the *endpoint* — `Power: false` going down, `PowerLevel: 100`
  going up — then reports the true level only when you release, often 2–3 seconds later.
  Naively following that makes the lights slam to black or full and then jump back.
- That endpoint is **indistinguishable from a deliberate tap** until the correction
  arrives, which is a real trade-off with no universally right answer. Hence the three
  selectable **paddle hold** modes below.
- Every `ZonePropertiesChanged` carries **both** `Power` and `PowerLevel`, and an "off"
  message reports the *remembered* level, not zero.
- **Every change is sent twice**, typically 100–300 ms apart.
- Some dimmers also emit a momentary `Power: false` mid-sequence with `Power: true`
  following a fraction of a second later.
- **A fast double tap never reaches the hub.** The paddle handles it internally as
  go-to-full and sends one `Power: true, PowerLevel: 100`, identical to any other
  turn-on at full. There is no way to tell a double tap from a single tap, which
  is why the scene gesture is off-then-on instead.
- **A press has to be held for around a second before the paddle registers it.**
  Quick taps produce nothing at all -- no message reaches the hub. So two
  deliberate presses land 3-5 seconds apart, not under one, and any gesture built
  on "quickly" has to be generous about what that means.
- **Some presses arrive collapsed.** On at least one of my dimmers, an off
  followed by an on can turn up as a single `Power: true, PowerLevel: 100` with
  no off at all. When that happens there is nothing in the stream to detect.
- The hub accepts only a **handful of simultaneous connections**. Homebridge, the Legrand
  app, and this service all consume one.
- Auth is a challenge/response: the hub sends `Hello V1 \x00<hex challenge> <MAC>`, and
  you reply with AES-128-ECB over the challenge, keyed by the MD5 of your password, hex
  encoded. A hub with no password set just sends `{"MAC":"..."}` instead.

## The web UI

Open **http://&lt;host&gt;:8582**.

**Live switch activity** — walk over, tap a switch or a scene button, and it appears in
the log with its name and ID. This is how you find out which zone is which without
guessing from names.

**Dimmers** — map one Legrand dimmer to one Hue target. Pick a Hue *zone* when you want
several bulbs to move together; one command to the bridge keeps them in step instead of
stair-stepping. **flash** blinks a target so you can confirm you picked the right one.
**Now** shows live state.

**Paddle hold** — pick how the ramp endpoints described above get rendered:

| Mode | Behaviour |
| --- | --- |
| **Snappy** | Taps act instantly. Holding the paddle bounces to black or full first, then settles. |
| **Soft fade** | Acts immediately but eases in over ~2s, so the bounce becomes a swell and the correction blends into the glide. |
| **No bounce** | Waits ~2.5s for the endpoint to be confirmed. Holds are perfectly clean; a deliberate tap takes a couple of seconds. |

**Scenes** *(experimental — see the caveat below)* — tick this and flicking the
switch off and then back on within `flick_seconds` (default 5) steps to the next
Hue scene in that room. Only works on a Hue room or zone; scenes belong to
groups, not to individual bulbs.

A *double tap* can't be used for this: the Legrand paddle implements
double-tap-to-full in its own firmware and reports a single
`Power: true, PowerLevel: 100` to the hub, so the second press never arrives. An
off and an on are normally two separate messages, which is unambiguous — hence
the flick.

**The caveat.** How well this works depends on the individual dimmer. RFLC
paddles need roughly a second of press before they register anything, and on at
least one of mine an off-then-on still sometimes arrives as a single collapsed
`on at 100` with no off in it. When the hub doesn't report two presses there is
nothing to detect, and the flick simply won't fire. Watch **Live switch
activity** in the web UI while you press: if you see a distinct `off` line and a
distinct `on` line, this will work on that dimmer. If every attempt shows up as
one line, it won't, and the setting is best left off.

Two things worth knowing even if you never use it, because both were real bugs:

- A scene sets every bulb differently, so while it lands the bridge reports the
  group's aggregate brightness repeatedly. With **Follow Hue** on, writing each
  of those to the wall dimmer and pushing the echoes back flattened the scene to
  one uniform brightness and saturated it at 100%. Both directions now hold
  still for `SCENE_SETTLE_SECONDS` while a scene lands, then sync once.
- After a scene landed, the link still held the *paddle's* remembered level —
  usually 100 — as its idea of the room, so the next update repainted everything
  at full. It now reads the group back from the bridge and adopts what the scene
  actually set.

**Scene-controller buttons** — map an LC7001 scene to a Hue action. The scene doesn't
need to do anything on the Legrand side; the press itself is the signal, so a scene whose
zones are all dead makes a perfectly good free button.

**Save & apply** writes the config and reloads it in place — no restart, no dropped
connections.

## Install

Needs Python 3.9+, and a machine that stays awake on the same network as both hubs.

```bash
git clone git@github.com:YOURNAME/lc7001-hue.git ~/lc7001-hue
cd ~/lc7001-hue
chmod +x install.sh service.sh
./install.sh          # private virtualenv + dependencies, then first-time setup
./service.sh install  # run in the background, start at login (macOS launchd)
./service.sh ui       # print the web UI address
```

`install.sh` builds its own virtualenv inside the folder and touches nothing else.

First-time setup needs the Hue bridge's link button pressed, and your LC7001 password if
you set one. After that, everything is done in the web UI.

| Command | |
| --- | --- |
| `./service.sh status` | Is it running? |
| `./service.sh logs` | Live log tail |
| `./service.sh test` | One-shot connectivity check against both hubs |
| `./service.sh update` | `git pull`, reinstall deps, restart |
| `./service.sh restart` | Restart |
| `./service.sh uninstall` | Stop and remove |
| `./service.sh run` | Foreground, debug logging |

macOS: if the machine sleeps, this sleeps. `sudo pmset -a sleep 0` on an always-on Mac.

## Per-dimmer settings

| Setting | Meaning |
| --- | --- |
| **Min %** | What the bottom of the paddle's travel means in Hue. Raise to 5–10 if the lowest setting is uselessly dim. |
| **Max %** | The top of travel — lower it to cap the room's brightness. |
| **Follow Hue** | On: changes from the Hue app or Apple Home are pushed back to the wall dimmer so the paddle stays in sync. Off: the wall switch is the only source of truth. |
| **Paddle hold** | See the table above. |

`config.json` also accepts `throttle` (seconds between Hue writes while a dimmer moves),
`transition_ms` (normal glide), `endpoint_hold` and `ramp_transition_ms` (override the
paddle-hold mode), and `jump_threshold` (how big a level change counts as a swing). The
web UI preserves these when you save.

## Troubleshooting

**"LC7001 offline".** The hub allows few simultaneous connections and Homebridge holds
one — quit the Legrand phone app. Give the hub a DHCP reservation so its address is
stable.

**A dimmer stops working.** Check the log for `pusher stopped unexpectedly`. Each dimmer
runs its own worker; a crashed one is reported loudly rather than failing silently.

**The lights fight each other.** Turn off **Follow Hue** for that dimmer.

**A scene button does nothing.** Watch the live log while pressing. No `BUTTON` line means
the controller isn't reaching the LC7001 at all — a Legrand pairing problem, not a
mapping one.

## Testing

```bash
python3 test_harness.py
```

Runs the real service against a simulated LC7001 and Hue bridge on localhost — no
hardware needed. Covers dimmer sweeps and coalescing, the momentary-off and
ramp-endpoint artefacts, all three paddle-hold modes, the reverse Hue→LC7001 path,
feedback-loop suppression, reconnection, scene triggers, worker liveness, and a live
remap through the web API.

## Layout

| File | |
| --- | --- |
| `lc7001_hue.py` | The service |
| `webui.py` | Web UI — one page, standard library only, no framework |
| `hue_client.py` | Minimal Hue CLIP v2 client |
| `setup_wizard.py` | Terminal first-time setup (pairing + credentials) |
| `test_harness.py` | Offline self-test with simulated hubs |
| `install.sh` / `service.sh` | Install and lifecycle |

## License

MIT.

Built with [Claude](https://claude.com/claude-code).
