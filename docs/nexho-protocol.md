# Farho "Nexho NT" — LAN control protocol

This documents the local-network control protocol of the Farho **Nexho NT**
hub, that controls ELKAtherm / Farho
electric radiators. The wire format below was worked out by observing the hub's
own responses on the LAN and confirmed live against a real hub.

## How the system is built

- **Hub:** Farho **Nexho NT** internet module — Ethernet to the router, mains
  powered, addressed by an **IDU serial** (printed on a sticker).
- **Radiators:** reached from the hub over proprietary **868 MHz RF** (slow and
  lossy; an unreachable radiator simply doesn't answer).
- **Two control paths:**
  - **Cloud** (remote): the vendor app reaches the hub through the Farho cloud
    when off the local network. This project does not use the cloud path.
  - **LAN** (same network): app → hub directly over **plaintext UDP, port
    6653**, **no authentication, no encryption**. This is what this project uses.

When the phone is on the same subnet as the hub, the vendor app talks straight
to it over the LAN.

## LAN wire protocol (UDP 6653)

- Requests are ASCII strings, terminated with `/`, sent as one datagram.
- Responses are one datagram; the last byte is a `\x00` NUL (strip it).
- The hub relays to radiators over RF, so reads/writes need retries; a radiator
  that is switched off answers `ER`.

### Commands

| Command | Purpose | Response |
|---|---|---|
| `OPS1/` | Handshake / installation identity | `OPOK,OPS1,<f2>,…,<idA>,<idB>,<idU_lo>,<idU_mid>,<idU_hi>,…` (≥12 fields) |
| `OPS2/` | List climate (heating) zones | `OPOK,OPS2,<zone>,<zone>,…` (a `0` field ends the list) |
| `OPS6/` | List load (on/off) zones | `OPOK,OPS6,…` (empty on this install) |
| `R#<z>#1#0#0*?T/` | Read zone status | `OK,<tempWhole>,<tempTenths>,<setpoint>` or `ER,<n>` |
| `D#<z>#1#0#0*T<deg>/` | Set zone target temperature | reply starting `OK` on success |
| `D#<z>#1#0#0*P#…/` | Write a weekly program pattern (see below) | `OK` |
| `D#<z>#1#0#0*ON/` / `…*OFF/` | Activate / deactivate the zone's weekly program | `OK` |
| `R#<z>#1#0#0*OD<day>/` | Query one weekday's program (summary only over LAN) | `OK` / `ER,1` |
| `R#<z>#1#0#0*UDEL/` | **Reset** the consumption counters (vendor app: "confirm reset"). Never send it as a probe. | `OK` |
| `R#<z>#1#0#0*?UD/` | Read daily consumption counters | `OK,<watts>,<8 counters>` / `ER,1` |
| `R#<z>#1#0#0*?UM/` | Read monthly consumption counters | `OK,<watts>,<counters>` / `ER,1` |
| `D#200#<z>#<slot>#0#0*Q#<1\|0>/` | Load on/off (no load zones here) | `OK` |

### Field mappings (confirmed live)

- **IDU** from `OPS1/`: `idU = hi*65536 + mid*256 + lo`. Example reply
  `OPOK,OPS1,24,183,0,0,0,38,40,176,61,0` → `61*256 + 176 = 15792`, matching the
  hub sticker.
- **Zone status** `R#`: e.g. `OK,20,04,007` → **room temperature 20.4 °C**
  (`whole + tenths/10`), **setpoint 7 °C** (last field). `ER,1` = radiator
  unreachable over RF.
- **Set temperature**: `D#2#1#0#0*T12/` sets zone 2 to 12 °C; verified by the
  read-back changing to `…,012` and by the physical radiator display.
- **OFF and half degrees (confirmed live)**: the setpoint field encodes more
  than whole degrees.
  - `*T0` turns the zone **off**: the read-back setpoint is `000` and the
    radiator shows off. (Live test on zone 1: `*T0` -> `…,000`, restored after.)
    In day-to-day use the app's Off button (`*T0`) appeared to do nothing, so
    the app now sends Off as `*T1` (1 °C) instead. Why `*T0` fails in practice
    is not established (inferred, not live-tested).
  - A setpoint **>= 128 is a half degree**: `temp = value - 127.5`, so 20.5 °C
    is sent and read back as `148`. (Live test: `*T148` -> `…,148` = 20.5 °C.)
  - Whole degrees 1..127 pass through unchanged.

The `1` after the zone id is the per-zone module slot (1 for a single-module
zone).

## Weekly schedules (native hub programs)

Confirmed by observing the app's program editor and cross-checking against the
hub's replies. A schedule is a single packet per day-pattern, not a
memory-write sequence.

**Write** one pattern to a set of days (climate = programType 199):

```
D#<zone>#<modules>#0#0*P#<dayBitmask>#<b1>#<b2>#<b3>#<b4>#<b5>#<b6>/
```

- `<modules>`: number of modules in the zone (1 for single-module zones; the
  same slot value used by `*T`).
- `<dayBitmask>`: `sum(2**dayIndex)` where **Mon=0, Tue=1, Wed=2, Thu=3, Fri=4,
  Sat=5, Sun=6** (confirmed from the app's day-list order).
- Always exactly **6 block groups**. Each enabled block is
  `<cmd>#<startH>#<startM>#<endH>#<endM>` (hour 24 is normalised to 0, so a
  block ending at midnight is sent as `…#0#0`; this app does the same —
  inferred from the vendor writer, not live-tested). Each
  disabled block is the sentinel `5#24#60#24#60` for climate zones
  (`253#24#60#24#60` for load/light/blind types).
- Reply starts `OK` on success.

**Read** one weekday's program block detail (**confirmed live 2026-11-01**):

```
R#<zone>#1#0#0*?P<day>/  ->  OK,P<day>,11,<b1>,<b2>,<b3>,<b4>,<b5>,<b6>
```

- Found in the decompiled HeatApp 4.1.1 (`uk.co.southwestheatingsolutions.heatapp`,
  the UK build of the Nexho app; `a0/y0.java`, its "resynchronise programs"
  path). Each block is `cmd,startH,startM,endH,endM`, the same shape `*P#`
  writes. The app keeps a block when `cmd <= 240` and `startH <= 23`, and reads
  an end of `0:00` as 1440 (midnight), matching the write-side normalisation.
- Live, all 28 zone-days returned six disabled blocks (`5,24,60,24,60`), which
  matches every zone's program having been cleared. The `11` after `P<day>` is
  unknown (constant on all four radiators).
- So block detail **is** readable over the LAN. Earlier notes said it was not
  because they used `*OD`, which is a different read (below).
- Round trip confirmed live 2026-11-01 on zone 3, Wednesday: wrote
  `6#3#0#3#30#6#23#30#0#0` (06 C 03:00-03:30 and 23:30-24:00), read back the same
  blocks, then cleared it. This app verifies every push this way.
- A `*P#` write can get `ER,1` straight back (radiator missed the RF window)
  and the identical write a few seconds later `OK`; the vendor app resends on
  `ER`, and so does this app for program reads, writes and `*ON`/`*OFF`.

`R#<zone>#1#0#0*OD<day>/` is the older per-day read the app uses on its
normal sync path, expecting `OK,` plus 35 fields; a bare `OK` is treated by the
app as a command error. On this hub it returns only `ER,1` or a bare `OK`
(live 2026-11-01: `ER,1` for all 28 zone-days; `R#1#0#0#0*OD0/` gave bare `OK`).
Do not use it for block detail.

**Program on/off (confirmed live):** `D#<zone>#<modules>#0#0*ON/` activates the
zone's stored weekly program (the hub then drives the setpoint from the
schedule); `…*OFF/` deactivates it and the zone holds its manual setpoint.
Reply starts `OK`. The hub does not report the active/inactive state back over
the LAN, so this app tracks the last state it set locally.

**Block command / temperature (`cmd`): CONFIRMED whole °C.** A live behavioural
test wrote a block at cmd `14` covering the current time on zone 1; the zone's
setpoint changed to 14 °C within ~4 s (observed via the `*?T` status read), and
reverted when the program was cleared. Native `*P#` writes take effect on the
radiator, and the block temperature uses the same whole-degree encoding as `*T`.

## Energy consumption (inferred; LAN reads return ER on this hub)

Radiators report consumption as run-time counters. **`*UDEL` resets them**:
in the vendor app it sits behind a "confirm reset" dialog (`ConsumptionMenu`).
This app used it as a reachability probe before every hourly energy read until
2026-11-01, which wiped the counters each time. The reads are:

```
R#<zone>#1#0#0*?UD/  ->  OK,<ratedWatts>,<c0..c7>   (8 daily buckets, c0 = today)
R#<zone>#1#0#0*?UM/  ->  OK,<ratedWatts>,<counters> (monthly buckets)
```

Each counter unit is a **6-minute** interval the element was on, so

    kWh = counter * (6 / 60) * (ratedWatts / 1000)

and cost is `kWh * price_per_kWh`. **Status: still unconfirmed, and possibly
unavailable over LAN on this hardware.** The `OK,<watts>,<counters>` framing and
the 6-minute unit are inferred from the vendor app. A live attempt across all
four zones — waking each with a `*?T` read first, trying framing variants, and
retrying persistently — got `OK` from `*UDEL` (the reset) but **`ER,1` every
time** from `*?UD`/`*?UM`. Two variants (`R#1#0#0#0*?UD/` and `R#1#1#0#0*?U/`)
returned a bare `OK` with no counters. So on this hub the radiators do not return
energy counters over the LAN (much like schedule block-detail). The app still
decodes and displays whatever a real reply would contain, guarded so the `ER`
case is simply "no reading". If a radiator ever does answer `*?UD`, capture the
payload and confirm the field order here. See `src/hearthmage/energy.py`.

## Holiday hold (`*RH`; fields from the vendor app)

The hub holds a zone until the occupants are back, then switches to an
"arrival" temperature or back to the weekly program. Field meanings come from
the decompiled vendor app (`AdvConfigHoliday`, `a0/j.java`; screen labels
"Arrival date", "Arrival time", "Arrival temperature"):

```
D#<zone>#<mod>#0#0*RH#<days from today>#<hour>#<minute>#<arrival>/  ->  OK
D#<zone>#<mod>#0#0*RH#0#0#0#251/                                     ->  OK   (cancel)
R#<zone>#1#0#0*?RH/  ->  OK,RH,<status...>,<days left>@10,<hour>@11,<minute>@12,<arrival>@13,...
```

- `<arrival>` uses the `*T` encoding (half degree = value + 127.5); `251` means
  "resume the weekly program" (the vendor sends it for zones with a program).
- Read-back: `[10]` days left, `<= 240` means a hold is set and `253` none;
  `[13]` `251` = program. Live 2026-11-01, no holds: `...,253,0,0,253,...`.
- What the zone runs at *during* the hold is not stated by the app; presumably
  its anti-ice temperature (`1#20`, 7 °C by default). Not live-tested.
- Before 2026-11-01 this app sent `*RH#<day of month>#<month>#0#<temp>`, which
  the hub reads as "<day-of-month> days from now at <month>:00". The earlier
  live test (`*RH#5#11#0#7`, setpoint dropping to 7) fits either reading.
- Cancelling was seen live to leave the zone at its holiday level, so this app
  restores the previous target on cancel unless a weekly program is running
  (the vendor app restores nothing).

## Radiator status and clock (`*?F`, live read; fields partly inferred)

```
R#<zone>#1#0#0*?F/  ->  OK,14,9,5,21,19,17,6,11,69,0,0,0,0,1,0,44,244,0,33,v200110
```

Live on all four zones 2026-11-01. Fields after `OK`: `[1],[2]` room temperature
whole/tenths and `[3]` setpoint (same as `*?T`), `[4..6]` the radiator's clock
`HH,MM,SS`, `[7]` weekday (Mon=0; read 6 on a Sunday), `[8]` read `11` on all
four (the app treats `> 11` as a feature/firmware threshold; also matches the
month, so unconfirmed), last field a firmware string (`v200110`, `v200825`).
`[9]` and `[16..19]` vary per radiator and are unknown. The four clocks were
within a minute of local time and already on GMT after the 2026-10-25 change.

**Set the clock** (from the app's "Set the time" button; not live-tested):
`OPH<hh><mm><ss><weekday>/<dd>,<mm>,<yy>/` with two-digit fields, weekday
Mon=0, year minus 2000.

## Hub memory and cloud reporting

`OPMR<addr>/` reads and `OPMW<addr>,<value>/` writes hub memory. Address
`3898` is the app's **"Send data to external server"** toggle (`1` = on).
Live 2026-11-01: `OPMR3898/` -> `OPOK,1` (on); `OPMW3898,0/` -> bare `OPOK`;
read back `OPOK,0` (off). The app's Settings page shows and toggles it. The app also has a
remote connection path that sends this same protocol through a gateway at
`49.13.37.164`. Other `OPMW` addresses the app writes are per-zone tables
(base 120/240/360 for climate zones, 608/609 for load zones); not mapped here.

## Per-radiator element config (confirmed live)

Individual radiator settings are addressed by a `(group, index)` element pair:
read with `*?E`, write with `*SEP`. Confirmed live on this hub (reads answered,
and a reversible window-sensor write applied and read back, then restored):

```
R#<zone>#1#0#0*?E#<group>#<index>/      ->  OK,<value>
D#<zone>#1#0#0*SEP#<group>#<index>#<v>/ ->  OK   (applies and persists)
```

The plain `*SEP` write acks and takes effect; no separate commit packet was
needed. Known elements:

| Setting | group#index | Value |
|---|---|---|
| Keypad lock | `0#8` | bit0: 1 = locked, 0 = unlocked. The vendor keeps bit7 on write (meaning unknown). |
| Open-window detection | `0#6` | 1 = on, 0 = off |
| Anti-ice (frost protection) temperature | `1#20` | 0..17 °C; all four radiators read 7 (default). The radiator heats to this even when off or set lower. |
| Sensor offset | `0#9` | signed byte in tenths of a degree, -9.9..+9.9 °C; all four read 0. Written with **`R#`**, not `D#`: `R#<z>#<module>#0#0*SEP#0#9#<round(off*10) & 255>/`. |
| Temperature units | `1#224` | 36 or 136 = °F; all four read 255 (unset, °C). |
| Output power level | `1#124` | 0..5 (vendor; not used here) |
| Boost time | `1#22` | 1..19, in 5-minute steps (vendor; not used here) |

Corrected 2026-11-01 from the decompiled vendor app (HeatApp 4.1.1): `1#20` was
earlier documented, and exposed in this app, as "temperature offset 0..17". It
is the anti-ice temperature (vendor screen "Frost protection"); the real offset
is `0#9`. Writes succeed only on a bare `OK` (the vendor treats `OK,...` as an
error). See `src/hearthmage/nexho_client.py` (`read_config` / `set_config`).

## Not available on the LAN protocol

- **Turning a heater off is available** via `*T0` (a 0 setpoint renders the zone
  off, confirmed live above) — it is only **structural** zone enable/disable and
  **scene/preset activation** that have no single-packet opcode; the vendor app
  implements those as low-level memory writes (`OPMW<addr>,<val>/`, `OPZ*`).
  Program activation (`*ON`/`*OFF`, above) is the other on/off-shaped command
  that does exist. This app exposes setpoint (including off), and schedule
  control.

## Verify against your own hub

Confirm that *your* hub speaks this protocol before trusting the app with it.
Everything here is LAN-only and read-mostly; the single write test is reversible.

### 1. Find the hub

- The router DHCP table is the reliable way (reserve a static lease while
  there). The hub drops all TCP, so a TCP port scan shows nothing; it only
  answers UDP `OPS1/` on port 6653.
- Or let the app scan for you: start it (`python run.py`), open the settings
  page, and use **Scan network** — one UDP probe per address on your /24.

### 2. Handshake (`OPS1/`)

```bash
python3 - <<'EOF'
import socket
HUB = "HUB_IP"  # your hub's IP on the LAN, e.g. from the router DHCP table
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(3)
s.sendto(b"OPS1/", (HUB, 6653))
print(s.recvfrom(1024)[0].rstrip(b"\x00").decode())
EOF
```

- A reply starting `OPOK,OPS1` confirms the Nexho LAN protocol.
- Fields [9], [10], [11] (zero-indexed) are the lo/mid/hi bytes of the IDU
  serial: `lo + mid*256 + hi*65536` should match the sticker on the hub.
  Example: `OPOK,OPS1,24,183,0,0,0,38,40,176,61,0` → `176*1 + 61*256 = 15792`.

### 3. List zones (`OPS2/`)

Send `OPS2/` the same way. Expect `OPOK,OPS2,1,2,3,…,0` — the digits before the
first `0` are your climate zone ids.

### 4. Read a zone (`R#…*?T/`)

Send `R#1#1#0#0*?T/` (zone 1). Expect either:

- `OK,20,04,007` — room temperature 20.4 °C, setpoint 7 °C; or
- `ER,1` — the radiator was asleep over RF. This is normal: retry a few times
  with a few seconds between attempts. The hub handles one conversation at a
  time and an RF attempt blocks it for ~5 s, so don't hammer it.

### 5. Prove a write (`D#…*T<deg>/`)

Note the zone's current setpoint from step 4, then set it one degree away, e.g.
`D#1#1#0#0*T12/`. Expect a reply starting `OK`. Confirm with a re-read (the last
field changes, e.g. `…,012`) and on the radiator's own display, then set it
back.

### 6. Run the app against the hub

Start `python run.py` (no `HEARTHMAGE_FAKE`), point it at the hub via the settings
page, and check that zones hydrate from "loading" to live readings and that a
setpoint change from the UI reaches the radiator.

### 7. Confirm it's truly cloud-free

Block the hub's outbound internet at the router and repeat step 6. Everything
still works — the LAN path never touches the vendor cloud — so the block can
stay permanent. Keeping the hub firewalled from the internet is the right
default.

## Security note

The LAN interface has **no authentication or encryption** — anything on the
network can read and control the heaters. Treat the hub as a trusted-LAN-only
device: use it only for local control of your own hub, and do not expose the hub
or this app to the internet. Give the hub time to answer before resending; one
command at a time.
