# HearthMage

LAN-only web app to control **Farho "Nexho NT"** electric-heating hubs (for
ELKAtherm / Farho radiators) directly over the local network. No vendor cloud,
no proprietary app, no credentials.

The hub speaks an unauthenticated plaintext UDP protocol on port 6653,
worked out by observing the hub on the LAN and validated against real hardware.
See `docs/nexho-protocol.md` for the wire protocol and how to confirm it against
your own hub.

## Security

- **LAN-only.** The Nexho hub has no auth on its LAN interface. Never
  port-forward or expose it (or this app) to the internet.
- **Optional password.** By default the app is open to anyone on the LAN, which
  is fine on a trusted home network. On a shared or untrusted network (guest
  wifi, IoT VLAN), set `HEARTHMAGE_PASSWORD` to require a sign-in: the app is then
  the only thing that can control the heaters, since the hub itself can't
  authenticate. The session cookie is `HttpOnly` and `SameSite=Strict`, and
  mutating requests also get a same-origin (CSRF) check.
- **Bind deliberately.** `HEARTHMAGE_BIND` defaults to `0.0.0.0` (all interfaces).
  To limit exposure, bind to a specific LAN address, or bind to `127.0.0.1` and
  put a reverse proxy (with TLS) in front.

## Setup

1. Install the runtime: `mise install` (creates and activates `.venv`).
2. Install dependencies: `pip install -e ".[dev]"` (add the `mqtt` extra —
   `pip install -e ".[dev,mqtt]"` — only if you want the Home Assistant bridge).
3. Run the tests: `pytest`.
4. Start it: `python run.py`, then open `http://<host>:8080/`.

### First run

The app needs no configuration to start. On the first visit it detects that no
hub is set and redirects you to the settings page, where you either:

- press **Scan network** to discover the hub automatically — it sweeps your /24
  with one `OPS1/` UDP probe per address and lists any hub it finds with its IDU
  serial, so you can pick it with one click; or
- type the hub's IP by hand (find it in your router's DHCP table and reserve a
  static lease — the hub drops all TCP but answers UDP `OPS1/` on port 6653).

Save the hub and you land on the rooms page. Zone names are edited on the same
settings page. `HEARTHMAGE_BASE_IP` can seed the hub address for a headless start,
but the UI-saved value always wins.

## Configuration

Settings are stored in a JSON config file, edited through the web UI.
Environment variables seed defaults so the app can also be configured headless;
a value saved through the UI wins over its environment seed.

Several sidecar files are written next to the config file: `readings.json`
(last-known temperatures), `schedules.json` (weekly programs), `energy.json`
(last energy reading per zone), `history.db` (SQLite temperature/energy
history), `sync.json` (schedule-sync state), `holiday.json` (holiday holds), and
a `backups/` folder of timestamped config/schedule snapshots. Only the config
and schedules are irreplaceable — the rest are caches or history that repopulate
on their own.

| Var | Meaning | Default |
| --- | --- | --- |
| `HEARTHMAGE_CONFIG_FILE` | Config file location (readings and schedules are stored beside it) | `~/.config/hearthmage/config.json` |
| `HEARTHMAGE_BASE_IP` | Hub LAN IP (seed; the UI-saved value wins) | — |
| `HEARTHMAGE_HUB_PORT` | Hub UDP port | `6653` |
| `HEARTHMAGE_ZONE_NAMES` | `id:name` pairs, comma-separated | zones show as "Zone N" |
| `HEARTHMAGE_BIND` / `HEARTHMAGE_PORT` | Web server bind / port | `0.0.0.0` / `8080` |
| `HEARTHMAGE_FAKE=1` | In-memory demo data, no hub | off |
| `HEARTHMAGE_LOG_LEVEL` | Log verbosity (`DEBUG`/`INFO`/`WARNING`) | `INFO` |
| `HEARTHMAGE_PRICE_PER_KWH` | Unit price seed for cost estimates | `0` |
| `HEARTHMAGE_MQTT_HOST` | MQTT broker host (enables the HA bridge) | off |
| `HEARTHMAGE_MQTT_PORT` / `_USERNAME` / `_PASSWORD` | Broker port and credentials | `1883` / — / — |
| `HEARTHMAGE_MQTT_BASE_TOPIC` / `_NODE_ID` | Topic prefix and device node id | `hearthmage` / `hub` |
| `HEARTHMAGE_PASSWORD` | Require this password to use the app | off (open) |
| `HEARTHMAGE_SECRET_KEY` | Session-cookie signing key | generated and stored |

## Capabilities and limits

- **Zero-config first run and hub auto-discovery:** with no hub set, the app
  guides you to setup and can find the hub itself by scanning the local network
  (`OPS1/` probe per /24 address), matching each reply's IDU serial. No manual
  IP hunting required, though you can enter one by hand.
- **Works:** list zones, read room temperature + setpoint, set a zone's target
  temperature in half-degree steps, and turn a zone off (a 0 setpoint) — all
  locally.
- **Weekly schedules:** per-day heating blocks, written to the hub as native
  programs, with per-zone program on/off. The hub cannot serve schedule detail
  back over the LAN, so this app owns the definitions (stored locally) and
  pushes them in the background; the schedule page shows the sync state
  (pending / synced / failed). The sync state is persisted, and a background
  task re-pushes any failed zone from the stored schedule once the radiator is
  reachable again, so a push that failed while a radiator was asleep is not lost
  on restart.
- **Backup and restore:** the settings page exports your hub config and
  schedules as one JSON file and imports it back, and every change to those
  files is snapshotted automatically (kept under a `backups/` folder next to
  the config). Since the hub cannot return schedule detail over the LAN, this
  export is the only recovery path if the config directory is lost.
- **Energy monitoring:** per-radiator consumption read from the hub's run-time
  counters, shown as 7-day and monthly kWh bars with a whole-home total and
  optional cost (set a price per kWh). The poller sweeps energy hourly (one zone
  per cycle so it never stalls temperature polling), and the page also refreshes
  on demand; readings are cached to disk. (The counter framing is inferred and
  unconfirmed — a live attempt got `ER` from every radiator, so this data may not
  be available over the LAN on this hardware; see `docs/nexho-protocol.md`.)
- **Home Assistant (MQTT):** an optional MQTT bridge publishes each zone as a
  Home Assistant climate entity (auto-discovered) and accepts temperature and
  off commands, so HomeKit, geofencing, and automations work through HA. It is
  off unless `HEARTHMAGE_MQTT_HOST` is set. **This app must stay the only thing
  that talks to the hub** (the hub handles one conversation at a time); point
  Home Assistant and every integration at this app over MQTT, never at the hub
  directly.
- **Holiday hold:** hold a zone at a frost temperature until a date, set on the
  hub itself so it keeps working (and auto-resumes) even while this app is off.
  Cancelling restores the zone's previous setpoint. Confirmed live.
- **Installable (PWA):** a web manifest and a small service worker let you add
  HearthMage to a phone's home screen and open it like an app. It stays LAN-only;
  the service worker caches the app shell for fast loads and shows the home page
  if a navigation fails offline.
- **History:** the poller's temperature readings and daily energy totals are
  logged to a small SQLite database, shown as per-zone temperature sparklines on
  a History page. This also retains energy history past the hub's own short
  window.
- **Per-radiator settings:** each zone has a settings page to lock the
  radiator's keypad, toggle open-window detection, and set the temperature-offset
  calibration, read from and written to the radiator over the LAN (confirmed
  live).
- **Observability:** structured logs of per-zone reads/writes, hub round-trip
  timing, and acks/timeouts (set `HEARTHMAGE_LOG_LEVEL=DEBUG` for the detail). A
  `/healthz` endpoint reports poller liveness, how long since the hub last
  answered, and per-zone freshness (200 when healthy, 503 when the hub has gone
  quiet or the app is unconfigured). The rooms page shows a banner when the hub
  has not answered for several poll intervals.
- **Not available:** scene presets (Boost, Party, and so on). Those have no
  single-packet opcode on the LAN, so day-to-day control is setpoint and off
  (a zone with an active program still follows its schedule).
- **RF flakiness, handled:** the hub reaches radiators over 868 MHz RF, which is
  slow and intermittent. A **background poller** thread reads the hub on an
  interval and keeps a per-zone cache, so the page loads instantly. Each zone
  hydrates on its own (loading -> reading), holds its last known value when the
  radiator goes quiet, and shows "Off / not responding" when a zone has never
  answered. Last-known readings survive restarts (cached on disk).

## Deploy as a service

Two deploy targets are provided; pick the one that matches your host.

### Deploy on macOS (launchd)

Runs as your login account, using the repo's venv and the config under
`~/.config/hearthmage` — no root, no dedicated user. Edit the absolute paths
in `deploy/local.hearthmage.plist` (project directory, your home) to match
your machine, then:

    cp deploy/local.hearthmage.plist ~/Library/LaunchAgents/
    launchctl load -w ~/Library/LaunchAgents/local.hearthmage.plist

It starts at login and restarts on exit. Logs go to
`~/Library/Logs/hearthmage.log` (and `.err.log`). To stop and remove:

    launchctl unload -w ~/Library/LaunchAgents/local.hearthmage.plist

### Deploy on Linux (systemd)

Copy the project to `/opt/hearthmage`, create the venv there, then create
`/opt/hearthmage/.env` (the unit requires it; see `.env.example`). The unit
sandboxes the service (`ProtectHome=true`), so the default config path under
`~/.config` is not writable — point the config into the app directory:

    HEARTHMAGE_CONFIG_FILE=/opt/hearthmage/config.json

Then:

    sudo useradd --system --no-create-home hearthmage
    sudo chown -R hearthmage:hearthmage /opt/hearthmage
    sudo cp deploy/hearthmage.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now hearthmage

### Run on a NAS (Docker)

`Dockerfile` and `deploy/docker-compose.yml` run HearthMage as a container.
The compose file uses **host networking on purpose**: the app finds the hub by
sweeping the LAN, which only works when the container shares the host's
network. That also means no port mapping — the app listens on the host
directly (default 8080; on Synology, DSM's own 5000/5001 don't clash).

On a Synology NAS:

1. Copy this repo to a shared folder on the NAS.
2. In **Container Manager**, create a **Project** pointing at
   `deploy/docker-compose.yml`. It builds the image from the `Dockerfile` at
   the repo root.
3. Start the project. Open `http://<nas-ip>:8080`.

State (config, schedules, `history.db`, backups) lives in a `data/` folder
next to the compose file, mounted at `/data`. The container runs as uid
`10001`, so that folder must be writable by it — on Synology give the folder
read/write, or `chown 10001 data`. To carry over an existing install, drop
your `config.json` into `data/` before the first start.

Set a password and MQTT broker (and, if auto-discovery doesn't find the hub,
a fixed `HEARTHMAGE_BASE_IP`) by uncommenting the `environment` entries in the
compose file. To build and run it anywhere Docker is present:

    docker compose -f deploy/docker-compose.yml up -d --build
