"""Small dependency-free web UI for lc7001-hue.

Serves one self-contained page plus a JSON API on the LAN, so mappings can be
built and changed from a browser instead of a config file. Deliberately uses
only the standard library -- this runs next to Homebridge and shouldn't drag
in a web framework to do it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from typing import Any, Awaitable, Callable

_log = logging.getLogger("lc7001-hue.web")

Handler = Callable[[str, dict[str, str], bytes], Awaitable[tuple[int, str, bytes]]]

_STATUS_TEXT = {
    200: "OK",
    204: "No Content",
    400: "Bad Request",
    404: "Not Found",
    500: "Internal Server Error",
}


class WebServer:
    """A very small HTTP/1.1 server: enough for one page and a JSON API."""

    def __init__(self, bridge: Any, host: str, port: int) -> None:
        self.bridge = bridge
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        _log.info("web UI on http://%s:%d", self.host or "0.0.0.0", self.port)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with_wait = getattr(self._server, "wait_closed", None)
            if with_wait:
                await self._server.wait_closed()

    # ---- plumbing --------------------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                request_line = await reader.readline()
                if not request_line:
                    return
                try:
                    method, raw_path, _version = request_line.decode().split()
                except ValueError:
                    return

                headers: dict[str, str] = {}
                while True:
                    line = await reader.readline()
                    if line in (b"\r\n", b"\n", b""):
                        break
                    name, _, value = line.decode().partition(":")
                    headers[name.strip().lower()] = value.strip()

                body = b""
                length = int(headers.get("content-length", "0") or 0)
                if length:
                    body = await reader.readexactly(length)

                parsed = urllib.parse.urlparse(raw_path)
                query = dict(urllib.parse.parse_qsl(parsed.query))

                try:
                    status, content_type, payload = await self._route(
                        method, parsed.path, query, body
                    )
                except Exception as error:  # noqa: BLE001
                    _log.exception("web request failed")
                    status, content_type = 500, "application/json"
                    payload = json.dumps({"error": str(error)}).encode()

                reason = _STATUS_TEXT.get(status, "OK")
                writer.write(
                    f"HTTP/1.1 {status} {reason}\r\n"
                    f"Content-Type: {content_type}\r\n"
                    f"Content-Length: {len(payload)}\r\n"
                    f"Cache-Control: no-store\r\n"
                    f"Connection: keep-alive\r\n\r\n".encode()
                    + payload
                )
                await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError, BrokenPipeError):
            pass
        finally:
            writer.close()

    @staticmethod
    def _json(data: Any, status: int = 200) -> tuple[int, str, bytes]:
        return status, "application/json", json.dumps(data).encode()

    # ---- routes ----------------------------------------------------------

    async def _route(
        self, method: str, path: str, query: dict[str, str], body: bytes
    ) -> tuple[int, str, bytes]:
        if method == "GET" and path in ("/", "/index.html"):
            return 200, "text/html; charset=utf-8", PAGE.encode()

        if method == "GET" and path == "/api/state":
            return self._json(self.bridge.ui_state())

        if method == "GET" and path == "/api/devices":
            return self._json(await self.bridge.ui_devices())

        if method == "GET" and path == "/api/events":
            since = int(query.get("since", "0") or 0)
            return self._json(self.bridge.ui_events(since))

        if method == "POST" and path == "/api/config":
            payload = json.loads(body or b"{}")
            await self.bridge.ui_save(payload)
            return self._json({"ok": True})

        if method == "POST" and path == "/api/identify":
            payload = json.loads(body or b"{}")
            await self.bridge.ui_identify(
                payload.get("resource", ""), payload.get("id", "")
            )
            return self._json({"ok": True})

        return self._json({"error": "not found"}, 404)


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>lc7001-hue</title>
<style>
  :root {
    --bg: #10131a; --panel: #171b24; --line: #262c38; --ink: #e6e9ef;
    --dim: #8d97a8; --accent: #5aa9ff; --good: #46c08a; --bad: #e5654b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  header {
    padding: 18px 24px; border-bottom: 1px solid var(--line);
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
  }
  h1 { font-size: 17px; margin: 0; font-weight: 600; letter-spacing: .2px; }
  .pills { display: flex; gap: 8px; margin-left: auto; flex-wrap: wrap; }
  .pill {
    font-size: 12px; padding: 4px 10px; border-radius: 999px;
    border: 1px solid var(--line); color: var(--dim);
  }
  .pill.up { color: var(--good); border-color: #24503f; }
  .pill.down { color: var(--bad); border-color: #5c2f26; }
  main { padding: 24px; max-width: 1100px; margin: 0 auto; }
  section {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 10px; padding: 18px; margin-bottom: 20px;
  }
  h2 { font-size: 14px; margin: 0 0 4px; font-weight: 600; }
  .hint { color: var(--dim); font-size: 13px; margin: 0 0 14px; }
  table { width: 100%; border-collapse: collapse; }
  th {
    text-align: left; font-size: 11px; text-transform: uppercase;
    letter-spacing: .08em; color: var(--dim); font-weight: 600;
    padding: 0 8px 8px 0; border-bottom: 1px solid var(--line);
  }
  td { padding: 9px 8px 9px 0; border-bottom: 1px solid var(--line); vertical-align: middle; }
  tr:last-child td { border-bottom: 0; }
  select, input[type=text], input[type=number] {
    background: #0d1017; color: var(--ink); border: 1px solid var(--line);
    border-radius: 6px; padding: 6px 8px; font: inherit; font-size: 13px; width: 100%;
  }
  input[type=number] { width: 74px; }
  button {
    background: #222836; color: var(--ink); border: 1px solid var(--line);
    border-radius: 6px; padding: 7px 13px; font: inherit; font-size: 13px; cursor: pointer;
  }
  button:hover { border-color: #39435a; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #06121f; font-weight: 600; }
  button.link { background: none; border: 0; color: var(--dim); padding: 4px 6px; }
  button.link:hover { color: var(--bad); }
  .row-actions { display: flex; gap: 6px; justify-content: flex-end; }
  .toolbar { display: flex; gap: 10px; align-items: center; margin-top: 14px; }
  .state { font-size: 12px; color: var(--dim); font-variant-numeric: tabular-nums; }
  .state b { color: var(--ink); font-weight: 600; }
  #log {
    background: #0d1017; border: 1px solid var(--line); border-radius: 8px;
    padding: 12px; height: 230px; overflow-y: auto;
    font: 12px/1.7 ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  #log .z { color: var(--accent); }
  #log .s { color: #d6a44c; }
  #log .t { color: var(--dim); }
  .empty { color: var(--dim); font-size: 13px; padding: 14px 0; }
  .saved { color: var(--good); font-size: 13px; }
  .saved.warn { color: #ffb454; }
  tr.bad td { background: #2b1512; }
  tr.bad select { border-color: var(--bad); }

  /* Phones and small tablets: a table with eight columns is unreadable at
     360px, so each row becomes a labelled card instead. Same markup, same
     JS -- only the presentation changes. */
  @media (max-width: 820px) {
    header { padding: 14px 16px; }
    main { padding: 16px 12px; }
    section { padding: 14px; }
    table, tbody, tr, td { display: block; width: 100%; }
    thead { display: none; }
    tr {
      border: 1px solid var(--line); border-radius: 10px;
      padding: 12px; margin-bottom: 12px; background: #0f131b;
    }
    td {
      border: 0; padding: 5px 0;
      display: grid; grid-template-columns: 104px minmax(0, 1fr);
      align-items: center; gap: 10px;
    }
    td::before {
      content: attr(data-label);
      color: var(--dim); font-size: 11px; font-weight: 600;
      text-transform: uppercase; letter-spacing: .06em;
    }
    td:empty { display: none; }
    input[type=number] { width: 100%; }
    input[type=checkbox] { justify-self: start; width: 22px; height: 22px; }
    .row-actions { justify-content: flex-start; gap: 12px; }
    .row-actions button { padding: 8px 4px; }
    #log { height: 180px; font-size: 11px; }
    .toolbar { flex-wrap: wrap; }
    button { padding: 10px 14px; }   /* comfortable tap targets */
  }
  @media (max-width: 420px) {
    td { grid-template-columns: 1fr; gap: 3px; }
    td::before { padding-top: 4px; }
  }
</style>
</head>
<body>
<header>
  <h1>lc7001-hue</h1>
  <div class="pills">
    <span class="pill" id="p-lc">LC7001</span>
    <span class="pill" id="p-hue">Hue</span>
  </div>
</header>
<main>

<section>
  <h2>Live switch activity</h2>
  <p class="hint">Walk over and tap a switch or a scene-controller button &mdash; it shows up here with its name and ID. This is the easy way to find out which zone is which.</p>
  <div id="log"></div>
</section>

<section>
  <h2>Dimmers</h2>
  <p class="hint">Each row points one Legrand wall dimmer at one Hue room, zone, or bulb. Pick a Hue <em>zone</em> when you want a couple of bulbs to move together.</p>
  <p class="hint"><b>Paddle hold</b> decides what happens when you press and hold. The LC7001 doesn&rsquo;t report a ramp as it happens &mdash; it announces the endpoint (off, or 100%) the moment you press, and only reports where the paddle really stopped a few seconds later. <b>Snappy</b>: taps are instant, holding visibly bounces first. <b>Soft fade</b>: acts at once but eases in, so the bounce becomes a swell. <b>No bounce</b>: waits for confirmation &mdash; clean holds, but a tap takes a couple of seconds to act. Change it, save, and try it at the wall.</p>
  <table>
    <thead>
      <tr>
        <th style="width:17%">Name</th>
        <th style="width:20%">Legrand dimmer</th>
        <th style="width:22%">Hue target</th>
        <th style="width:7%">Min %</th>
        <th style="width:7%">Max %</th>
        <th style="width:8%">Follow Hue</th>
        <th style="width:13%">Paddle hold</th>
        <th style="width:7%">Now</th>
        <th></th>
      </tr>
    </thead>
    <tbody id="links"></tbody>
  </table>
  <div id="links-empty" class="empty" hidden>No dimmers mapped yet.</div>
  <div class="toolbar">
    <button onclick="addLink()">Add dimmer</button>
  </div>
</section>

<section>
  <h2>Scene-controller buttons</h2>
  <p class="hint">Each LC7001 scene is one button on your scene controller. The scene itself doesn't need to do anything on the Legrand side &mdash; pressing the button is the signal.</p>
  <table>
    <thead>
      <tr>
        <th style="width:22%">Name</th>
        <th style="width:24%">LC7001 scene</th>
        <th style="width:28%">Hue target</th>
        <th style="width:10%">Action</th>
        <th style="width:10%">Bright %</th>
        <th></th>
      </tr>
    </thead>
    <tbody id="scenes"></tbody>
  </table>
  <div id="scenes-empty" class="empty" hidden>No buttons mapped yet.</div>
  <div class="toolbar">
    <button onclick="addScene()">Add button</button>
  </div>
</section>

<div class="toolbar" style="margin-bottom:32px">
  <button class="primary" onclick="save()">Save &amp; apply</button>
  <span id="saved" class="saved"></span>
</div>

</main>
<script>
let devices = { zones: [], scenes: [], hue: [] };
let links = [], scenes = [], lastEvent = 0;

const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function hueOptions(sel) {
  const groups = devices.hue.filter(t => t.resource === 'grouped_light');
  const bulbs = devices.hue.filter(t => t.resource === 'light');
  const opt = t => `<option value="${t.resource}|${t.id}"${
    sel === t.resource + '|' + t.id ? ' selected' : ''}>${esc(t.label)}</option>`;
  return `<option value="">-- pick --</option>` +
    (groups.length ? `<optgroup label="Rooms &amp; zones">${groups.map(opt).join('')}</optgroup>` : '') +
    (bulbs.length ? `<optgroup label="Single bulbs">${bulbs.map(opt).join('')}</optgroup>` : '');
}

function zoneOptions(sel) {
  return `<option value="">-- pick --</option>` + devices.zones.map(z =>
    `<option value="${z.zid}"${z.zid === sel ? ' selected' : ''}>${
      esc(z.name)} (ZID ${z.zid}${z.type ? ', ' + esc(z.type) : ''})</option>`).join('');
}

function sceneOptions(sel) {
  return `<option value="">-- pick --</option>` + devices.scenes.map(s =>
    `<option value="${s.sid}"${s.sid === sel ? ' selected' : ''}>${
      esc(s.name)} (SID ${s.sid})</option>`).join('');
}

const RAMP_MODES = [
  { id: 'snappy', label: 'Snappy' },
  { id: 'fade',   label: 'Soft fade' },
  { id: 'steady', label: 'No bounce' },
];

const stateText = s => !s ? '&mdash;'
  : (s.on ? '<b>on</b> ' + (s.level == null ? '' : s.level + '%') : 'off');

function renderLinks() {
  const tb = document.getElementById('links');
  tb.innerHTML = links.map((l, i) => `
    <tr>
      <td data-label="Name"><input type="text" data-f="name" autocomplete="off" value="${esc(l.name)}" oninput="links[${i}].name=this.value"></td>
      <td data-label="Dimmer"><select data-f="zid" onchange="links[${i}].lc7001_zid=pickId(this.value)">${zoneOptions(l.lc7001_zid)}</select></td>
      <td data-label="Hue target"><select data-f="hue" onchange="setHue(links,${i},this.value)">${
        hueOptions(l.hue_resource && l.hue_id ? l.hue_resource + '|' + l.hue_id : '')}</select></td>
      <td data-label="Min %"><input type="number" data-f="min" autocomplete="off" min="1" max="100" value="${l.min_brightness ?? 1
        }" oninput="links[${i}].min_brightness=+this.value"></td>
      <td data-label="Max %"><input type="number" data-f="max" autocomplete="off" min="1" max="100" value="${l.max_brightness ?? 100
        }" oninput="links[${i}].max_brightness=+this.value"></td>
      <td data-label="Follow Hue"><input type="checkbox" data-f="follow" ${l.follow_hue !== false ? 'checked' : ''
        } onchange="links[${i}].follow_hue=this.checked"></td>
      <td data-label="Paddle hold"><select data-f="ramp" onchange="links[${i}].ramp_mode=this.value" title="How to handle the LC7001 announcing where a ramp is headed before it reports where the paddle actually stopped">
        ${RAMP_MODES.map(m => `<option value="${m.id}"${
          (l.ramp_mode || 'fade') === m.id ? ' selected' : ''}>${m.label}</option>`).join('')}
      </select></td>
      <td class="state" data-label="Now" id="state-${i}">${stateText(l.state)}</td>
      <td data-label="" class="row-actions">
        <button class="link" onclick="identify(links[${i}])" title="Flash this Hue target">flash</button>
        <button class="link" onclick="links.splice(${i},1);renderLinks()">remove</button>
      </td>
    </tr>`).join('');
  document.getElementById('links-empty').hidden = links.length > 0;
}

function renderScenes() {
  const tb = document.getElementById('scenes');
  tb.innerHTML = scenes.map((s, i) => `
    <tr>
      <td data-label="Name"><input type="text" data-f="name" autocomplete="off" value="${esc(s.name)}" oninput="scenes[${i}].name=this.value"></td>
      <td data-label="Scene"><select data-f="sid" onchange="scenes[${i}].lc7001_sid=pickId(this.value)">${sceneOptions(s.lc7001_sid)}</select></td>
      <td data-label="Hue target"><select data-f="hue" onchange="setHue(scenes,${i},this.value)">${
        hueOptions(s.hue_resource && s.hue_id ? s.hue_resource + '|' + s.hue_id : '')}</select></td>
      <td data-label="Action"><select data-f="act" onchange="scenes[${i}].on=this.value==='on'">
        <option value="on"${s.on !== false ? ' selected' : ''}>turn on</option>
        <option value="off"${s.on === false ? ' selected' : ''}>turn off</option>
      </select></td>
      <td data-label="Bright %"><input type="number" data-f="bright" autocomplete="off" min="1" max="100" value="${s.brightness ?? 60
        }" oninput="scenes[${i}].brightness=+this.value"></td>
      <td data-label="" class="row-actions">
        <button class="link" onclick="identify(scenes[${i}])" title="Flash this Hue target">flash</button>
        <button class="link" onclick="scenes.splice(${i},1);renderScenes()">remove</button>
      </td>
    </tr>`).join('');
  document.getElementById('scenes-empty').hidden = scenes.length > 0;
}

// "" means nothing picked. Everything else is a real id -- and ZID 0 / SID 0
// are real ids, so this must not treat a numeric zero as "unset".
function pickId(value) {
  return value === '' ? null : +value;
}
const isPicked = v => v !== null && v !== undefined && v !== '';

function setHue(list, i, value) {
  const [resource, id] = (value || '|').split('|');
  list[i].hue_resource = resource; list[i].hue_id = id;
}
function addLink() {
  links.push({ name: 'New dimmer', lc7001_zid: null, hue_resource: '', hue_id: '',
               min_brightness: 1, max_brightness: 100, follow_hue: true,
               ramp_mode: 'fade' });
  renderLinks();
}
function addScene() {
  scenes.push({ name: 'New button', lc7001_sid: null, hue_resource: '', hue_id: '',
                on: true, brightness: 60 });
  renderScenes();
}
async function identify(row) {
  if (!row.hue_id) return;
  await fetch('/api/identify', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ resource: row.hue_resource, id: row.hue_id }) });
}
// Read the rows back out of the DOM instead of trusting the model the
// oninput/onchange handlers built. Chrome restores form-control values across a
// reload without firing any event, so after a refresh a row can LOOK filled in
// while the JS behind it is still the blank row that was rendered -- which is
// exactly how a mapping gets silently dropped on save. The DOM is what the user
// sees, so the DOM wins.
function harvest() {
  const val = (tr, f) => {
    const el = tr.querySelector('[data-f="' + f + '"]');
    return el ? (el.type === 'checkbox' ? el.checked : el.value) : '';
  };
  const hue = (row, v) => {
    const [resource, id] = (v || '|').split('|');
    row.hue_resource = resource; row.hue_id = id;
    return row;
  };
  links = Array.from(document.getElementById('links').children).map((tr, i) => hue({
    ...(links[i] || {}),
    name: val(tr, 'name'),
    lc7001_zid: pickId(val(tr, 'zid')),
    min_brightness: +val(tr, 'min'),
    max_brightness: +val(tr, 'max'),
    follow_hue: val(tr, 'follow'),
    ramp_mode: val(tr, 'ramp'),
  }, val(tr, 'hue')));
  scenes = Array.from(document.getElementById('scenes').children).map((tr, i) => hue({
    ...(scenes[i] || {}),
    name: val(tr, 'name'),
    lc7001_sid: pickId(val(tr, 'sid')),
    on: val(tr, 'act') === 'on',
    brightness: +val(tr, 'bright'),
  }, val(tr, 'hue')));
}

async function save() {
  const note = document.getElementById('saved');
  harvest();
  const goodLink  = r => isPicked(r.lc7001_zid) && isPicked(r.hue_id);
  const goodScene = r => isPicked(r.lc7001_sid) && isPicked(r.hue_id);
  const keptLinks = links.filter(goodLink);
  const keptScenes = scenes.filter(goodScene);

  // Point at the row. "1 row was skipped" with four rows on screen tells you
  // nothing -- highlight the offender and say which field is missing.
  const bad = [];
  const flag = (tbodyId, list, good, zidKey, what) => {
    const rows = document.getElementById(tbodyId).children;
    list.forEach((r, i) => {
      const tr = rows[i];
      if (tr) tr.classList.toggle('bad', !good(r));
      if (good(r)) return;
      const missing = [];
      if (!isPicked(r[zidKey])) missing.push(what);
      if (!isPicked(r.hue_id)) missing.push('a Hue target');
      bad.push('"' + (r.name || 'unnamed') + '" needs ' + missing.join(' and '));
    });
  };
  flag('links', links, goodLink, 'lc7001_zid', 'a Legrand dimmer');
  flag('scenes', scenes, goodScene, 'lc7001_sid', 'a scene');

  const res = await fetch('/api/config', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ links: keptLinks, scenes: keptScenes }) });

  // Say so out loud. Silently discarding a half-filled row is how you end up
  // convinced you saved a mapping that was never there.
  if (!res.ok) {
    note.className = 'saved warn'; note.textContent = 'Save failed.';
  } else if (bad.length) {
    note.className = 'saved warn';
    note.textContent = 'Saved, but not ' + bad.join('; ') + '.';
  } else {
    note.className = 'saved'; note.textContent = 'Saved and applied.';
  }
  setTimeout(() => { note.textContent = ''; note.className = 'saved'; }, 12000);
}

function pill(id, up) {
  const el = document.getElementById(id);
  el.className = 'pill ' + (up ? 'up' : 'down');
  el.textContent = el.id === 'p-lc'
    ? (up ? 'LC7001 connected' : 'LC7001 offline')
    : (up ? 'Hue connected' : 'Hue offline');
}

async function refreshState(firstRun) {
  const s = await (await fetch('/api/state')).json();
  pill('p-lc', s.lc7001); pill('p-hue', s.hue);
  if (firstRun) { links = s.links; scenes = s.scenes; return; }
  // Never re-render the tables on a poll -- that would clobber whatever the
  // user is in the middle of typing. Only the live state cells get updated.
  const byZid = Object.fromEntries(s.links.map(l => [l.lc7001_zid, l.state]));
  links.forEach((l, i) => {
    const cell = document.getElementById('state-' + i);
    if (cell && byZid[l.lc7001_zid] !== undefined) {
      l.state = byZid[l.lc7001_zid];
      cell.innerHTML = stateText(l.state);
    }
  });
}

async function refreshEvents() {
  const data = await (await fetch('/api/events?since=' + lastEvent)).json();
  if (!data.events.length) return;
  lastEvent = data.cursor;
  const log = document.getElementById('log');
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  for (const e of data.events) {
    const div = document.createElement('div');
    div.innerHTML = `<span class="t">${esc(e.time)}</span> ` + (e.kind === 'scene'
      ? `<span class="s">BUTTON</span> SID ${e.id} &mdash; ${esc(e.name)}`
      : `<span class="z">DIMMER</span> ZID ${e.id} &mdash; ${esc(e.name)} ${esc(e.detail)}`);
    log.appendChild(div);
  }
  while (log.childNodes.length > 300) log.removeChild(log.firstChild);
  if (atBottom) log.scrollTop = log.scrollHeight;
}

(async function init() {
  devices = await (await fetch('/api/devices')).json();
  await refreshState(true);
  renderLinks(); renderScenes();
  setInterval(() => refreshState(false).catch(() => {}), 4000);
  setInterval(() => refreshEvents().catch(() => {}), 1000);
})();
</script>
</body>
</html>
"""
