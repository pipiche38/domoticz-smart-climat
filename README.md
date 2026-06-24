# Domoticz AC Pilot — Domoticz plugin

*Project: **domoticz-ac-pilot** · 🇬🇧 English | [🇫🇷 Français](README.fr.md)*

A [DomoticzEx](https://wiki.domoticz.com/Developing_a_Python_plugin) Python plugin that
adds a single high-level **master control** for one or more AC splits that are
**already** controllable in Domoticz through existing widgets.

The plugin creates its own widgets (so it receives `onCommand` callbacks), and when you
operate them it relays the command to your existing AC widgets — referenced by their
**idx** — through the Domoticz JSON API on `127.0.0.1:8080`. One master action can drive
several splits at once.

## What it creates

| Unit | Type | Values |
|------|------|--------|
| **Master** | Selector switch | `Off` / `Cold-Auto` / `Heat-Auto` / `Manual` |
| **Target Temp** | Setpoint | temperature feeding the auto modes |

### Behaviour

| Master | Action sent to every split |
|--------|----------------------------|
| **Off** | On/Off → Off |
| **Cold-Auto** | On/Off → On, Mode → Cool, SetTemp → Target Temp, **Fan → regulated** |
| **Heat-Auto** | On/Off → On, Mode → Heat, SetTemp → Target Temp, **Fan → regulated** |
| **Manual** | On/Off → On only — you then drive the existing Mode/Fan/SetTemp widgets yourself |

Changing **Target Temp** re-sends the setpoint to every split **only while** the master
is in Cold-Auto or Heat-Auto, and re-runs the fan regulation. In Manual/Off the plugin
leaves the setpoint and fan alone.

#### Mode-only units (no power switch)

Some units have no separate On/Off switch — power is the Mode selector's first level
(e.g. `0` = Off, then Cool/Heat/…). For these, **leave the On/Off idx blank**. The plugin
then powers the unit through the Mode selector:

- **Off** → sets Mode to `mode.off` (default `0`).
- **Cold-Auto / Heat-Auto** → sets Mode to Cool/Heat, which powers the unit on.
- **Manual** → does nothing (no switch to flip); you drive the Mode selector yourself.

A **Fan idx is still required** for regulation. You can mix unit types across splits in
one instance: list an idx where a split has a switch, and nothing in that position where
it doesn't.

### Thermostatic fan regulation

In the auto modes the plugin does **not** use a fixed fan speed. On every heartbeat
(every 30 s) it reads each split's **ambient** temperature sensor and computes the
directional error:

- Cold-Auto: `error = ambient − target`
- Heat-Auto: `error = target − ambient`

The error climbs the **fan ladder** — your fan levels sorted ascending, **excluding
`auto`** — by one step per `FAN_STEP_DEG` (default `0.5` °C):

| error (°C) | fan step |
|------------|----------|
| ≤ 0 (at/over target) | step 1 (slowest, e.g. Silence) |
| 0.5 – 1.0 | step 2 |
| 1.0 – 1.5 | step 3 |
| … | … |
| ≥ (N−1)×0.5 | top step (fastest) |

The mapping adapts to however many fan levels you configure and clamps at the top. The
fan is only re-sent when the step actually changes, so there is no flapping.

If an **External temp idx** is configured and the outdoor load is high in the working
direction (hotter than target by more than `EXT_BOOST_DELTA` = 8 °C while cooling, or
colder than target by more than 8 °C while heating), the fan is bumped up one extra band
— but **only while the room still needs correction** (`error > 0`). Once the room reaches
or passes target the fan idles to its slowest step regardless of the outdoor temperature,
so it never keeps blowing when no heating/cooling is wanted.

The split's own thermostat still starts/stops the compressor from the setpoint — the
plugin only manages the **fan speed**. The thresholds and boost are module constants at
the top of `plugin.py`, easy to tune.

### Respecting an external power-off

On every heartbeat the plugin checks each split's **actual** power state — the On/Off
switch if configured, otherwise the Mode selector's level. If a unit has been switched
**off externally** (e.g. from the remote control), the plugin **backs off**: it skips
that split entirely, sending no mode/fan/setpoint commands, so it never fights you or
turns the unit back on. Regulation resumes automatically once the unit is on again
(turned back on from the remote, or by re-selecting the master mode). This requires the
Domoticz widget to reflect the unit's real state.

### Occupancy eco setback (optional)

If a split has a **Motion idx** configured, the plugin tracks room occupancy. A detected
motion **latches the room occupied for `MOTION_HOLD_MIN` (default 30 min)**, and each new
detection re-arms the full window — this prevents chaotic comfort↔eco flipping. Once that
window lapses with no motion, the room is considered empty and the split targets an
**eco** temperature instead of your Target Temp:

- Cold-Auto empty → `ECO_COLD_TEMP` (default **25 °C**)
- Heat-Auto empty → `ECO_HEAT_TEMP` (default **18 °C**)

The eco target is pushed to the SetTemp widget and the fan regulates around it. As soon
as motion returns, the split snaps back to your comfort Target Temp. A split with no
motion idx is always treated as occupied. These constants live at the top of `plugin.py`.

### Warm-up learning (persistent)

When a regulation episode starts with a large error (`WARMUP_START_ERR`, default 1.5 °C —
e.g. a mode change or an occupancy/setpoint jump), the plugin **pre-empts the fan to a
high step and eases it down** as the room approaches target, instead of ramping up slowly.

It also **learns** how fast each room actually closes that gap (°C/min) and stores the
value (an EMA) in a Domoticz **user variable** named `DomoticzACPilot_<HardwareID>`. The
same blob also holds the per-sensor reliability weights when `avg` is `weighted`, e.g.

```json
{"0": {"rate": 0.42, "n": 5, "eta_rate": 0.18, "eta_n": 40, "sensors": {"424": 0.002, "1173": 1.4}}, "1": {...}}
```

`rate` is the warm-up rate (used to pre-empt the fan); `eta_rate` is a separate,
continuously-learned approach rate used only for the **ETA** in the progress log
(see below). They are kept apart because the warm-up rate is measured during
fan-boosted bursts and would over-estimate progress in steady regulation.

The variable is read back on startup, so the plugin is tuned from the first heartbeat:

- **Slow rooms** (low learned rate) get a *more* aggressive warm-up next time.
- **Fast rooms** (high rate) are scaled back so they don't blast needlessly.
- **Unknown rooms** start at a moderate `DEFAULT_SLOWNESS` (0.6) and tune from there.

The warm-up rate is saved immediately when an episode completes; the sensor weights are
saved at most once every `LEARN_SAVE_MIN` (15 min) to avoid hammering the variable.

Learning is on by default. Add `"learn": false` to the Levels JSON to disable it (no
user variable is then read or written). Tuning constants (`WARMUP_*`, `LEARN_ALPHA`,
`SLOWNESS_MIN`, `DEFAULT_SLOWNESS`) are at the top of `plugin.py`. You can inspect or
reset the learned data anytime under **Setup → More Options → User Variables**.

### Progress logging

While the master is in **Cold-Auto / Heat-Auto** and a room is **still working toward
target**, the plugin writes a concise line to the **standard Domoticz log** (no Debug
needed) at most once a minute per split, so you can watch convergence at a glance:

```
Salon Clim — Vitesse: room 23.8°C -> target 22.0°C, gap 1.80°C, fan=40 (occupied) +ext-boost, closing 0.30°C/min, ETA ~10 min (hist n=42) | median of 2: 424=23.8 1173=23.9
```

Each line shows:

- **Widget name** — resolved from the **Fan idx** at startup (falls back to `fan <idx>`
  if the device can't be read), so rooms are named rather than numbered.
- **room → target / gap** — the fused ambient, the active target, and the remaining error.
- **fan / occupied|ECO / +ext-boost** — the fan level chosen and the regulation context.
- **closing …°C/min** — the live convergence rate measured since the previous line (or
  `drifting` / `steady` when the gap is not shrinking minute-to-minute).
- **ETA ~N min** — time-to-target. It prefers the **historical** approach rate
  (`eta_rate`, learned across runs and persisted in the learning JSON, shown as
  `hist n=…`), so an ETA still appears even when the live rate reads `steady`; it falls
  back to the live rate (`live`) until enough history exists. Persisting the rate means
  the ETA is meaningful from the first heartbeat after a restart.
- **fusion breakdown** — how the ambient sensors were combined (`median`/`mean`/`weighted`,
  each sensor's reading, and its share `%` in `weighted` mode).

When the room reaches target it logs one closing line and then goes **quiet** until the
gap reopens, so steady state doesn't flood the log:

```
Salon Clim — Vitesse: at target 22.0°C (room 22.1°C, gap 0.10°C) | median of 2: 424=22.1 1173=22.2
```

The full per-heartbeat detail (every 30 s) remains available at **Debug** level. The cadence
is the `STATUS_LOG_INTERVAL` constant (60 s) at the top of `plugin.py`.

## Installation

1. Copy this folder to your Domoticz plugins directory so that the file lands at
   `.../plugins/domoticz-ac-pilot/plugin.py`.
2. Restart Domoticz (or reload the Python plugins).
3. **Setup → Hardware**, add type **Domoticz AC Pilot**, fill the parameters, **Add**.

## Parameters

| Field | Meaning |
|-------|---------|
| **On/Off idx (CSV)** | each split's On/Off switch. **Optional** — leave blank for mode-only units that power on/off through the Mode selector (see below) |
| **Mode idx (CSV)** | idx of each split's existing Mode selector |
| **Fan idx (CSV)** | idx of each split's existing Fan selector |
| **SetTemp idx (CSV)** | idx of each split's existing setpoint (optional) |
| **Ambient temp idx (CSV)** | room temperature sensor(s) that drive regulation (see below) |
| **External temp idx** | idx of an outdoor temperature sensor (optional, single) |
| **Motion idx (CSV)** | motion sensor(s) for the eco setback (optional; any one = occupied) |
| **Levels (JSON)** | the **level numbers** of *your* Mode and Fan selectors (see below) |
| **Debug** | log every outgoing JSON call |

The idx lists are **position-aligned**: the *i*-th entry in each list belongs to split
*i*. Example for two splits:

```
On/Off  : 64,70
Mode    : 65,71
Fan     : 66,72
SetTemp : 67,73
Ambient : 68,74
Motion  : 69,75
```

### Levels (JSON)

A single JSON object giving the numeric **Level** values of your existing Mode and Fan
selector switches (the Level numbers, not the labels):

```json
{
  "mode": {"off": 0, "cold": 30, "heat": 40},
  "fan":  {"silence": 20, "lvl1": 30, "lvl2": 40, "lvl3": 50, "lvl4": 60, "lvl5": 70}
}
```

- `mode.cold` / `mode.heat` — levels used by **Cold-Auto** / **Heat-Auto**.
- `mode.off` *(optional, default `0`)* — the Mode selector's **Off** level, used to
  power the unit off on **mode-only units** (see below).
- `fan` — a name→level map of **every** fan level. Regulation uses these **sorted
  ascending, excluding `auto`** (Auto is never set automatically; it stays available for
  manual use). Names other than `auto` are free-form — only the level values matter.
- `learn` *(optional, default `true`)* — set `false` to disable warm-up learning.
- `avg` *(optional, default `"median"`)* — how multiple ambient sensors are fused:
  `"median"`, `"mean"`, or `"weighted"` (see below).

## Multiple sensors per room

Both the **Ambient** and **Motion** fields accept several sensors per room, entered the
same way. How you enter them depends on how many splits the hardware instance controls:

- **One split** (the usual case — one instance per room): list every sensor, comma
  separated. Example for Salon ambient: `424, 1294, 1390, 1173`.
- **Several splits in one instance**: commas separate the splits; use `+` to group
  multiple sensors within a split. Example: `68+69, 74` → split 0 uses {68, 69},
  split 1 uses {74}.

**Motion** sensors are combined with **OR**: if *any* motion sensor in the room fires,
the 30-minute occupancy hold is re-armed. Unusable (stale/mistyped) motion sensors are
ignored; if *all* of a room's motion sensors are unusable the room stays "occupied"
(comfort) as a safe fallback.

Each ambient sensor is independently sanity-checked (type + 60-min freshness); stale or
invalid ones are dropped before fusing. The fusion rule is the `avg` key in the Levels
JSON:

| `avg` | behaviour |
|-------|-----------|
| `median` *(default)* | Middle value; for an even count, the mean of the middle two (so it drops the hottest and coldest). Robust to one bad sensor, no tuning. |
| `mean` | Plain arithmetic mean of all valid sensors. |
| `weighted` | **Learned during the run:** each cycle every sensor is compared to the group consensus (median); a per-sensor deviation variance is tracked (EMA) and sensors are weighted inversely, so a consistently off sensor (e.g. near a window) is quietly down-weighted. The learned weights are **persisted** (see below) so they survive a restart. |

Find the level numbers in **Setup → Devices → (edit)** (the Level column) or via
`http://127.0.0.1:8080/json.htm?type=command&param=getdevices&rid=<idx>`.

## Sensor sanity checks

Before a sensor reading is used the plugin validates it:

- An **Ambient/External temp idx** must be a temperature device (it must expose a `Temp`
  value); otherwise the reading is ignored and an error is logged.
- A **Motion idx** must be a motion sensor (`SwitchType = "Motion"`); otherwise ignored.
- Any sensor whose **`LastUpdate` is older than `SENSOR_TIMEOUT_MIN` (60 min)** is treated
  as **timed out** and its value is **not used**.

Consequences when a reading is unusable (stale, wrong type, or unreadable):

- **Ambient** → that split's fan is left unchanged this cycle (no regulation on stale data).
- **External** → the outdoor fan boost is simply skipped.
- **Motion** → the split falls back to **occupied** (comfort Target Temp), so a broken or
  mistyped motion sensor never strands the room at the eco setpoint.

A given problem is logged once at error level (then quietly) until the sensor recovers,
to avoid filling the log every heartbeat. `SENSOR_TIMEOUT_MIN` is a constant at the top
of `plugin.py`.

> Note: with a transition-only PIR (one that reports only when motion starts/stops), a
> room left empty for over 60 minutes makes the sensor look "timed out", so the split
> reverts to comfort. If you rely on long-absence eco, use a motion sensor that reports
> periodically, or raise `SENSOR_TIMEOUT_MIN`.

## Notes

- Domoticz host/port are hardcoded to `127.0.0.1:8080` (no auth). Change the
  `DOMOTICZ_HOST` / `DOMOTICZ_PORT` constants at the top of `plugin.py` if needed.
- For independent groups of splits, add multiple **Domoticz AC Pilot** hardware instances.

## Manual endpoint check

You can confirm the JSON API works against your real idx before relying on the plugin:

```bash
curl "http://127.0.0.1:8080/json.htm?type=command&param=switchlight&idx=<onoff>&switchcmd=On"
curl "http://127.0.0.1:8080/json.htm?type=command&param=switchlight&idx=<mode>&switchcmd=Set%20Level&level=20"
curl "http://127.0.0.1:8080/json.htm?type=command&param=setsetpoint&idx=<settemp>&setpoint=22"
```

Each should return `{"status":"OK", ...}` and move the corresponding widget.
