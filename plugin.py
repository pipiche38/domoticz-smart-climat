"""
<plugin key="domoticz-ac-pilot" name="Domoticz AC Pilot" author="patrick" version="1.0.0">
    <description>
        <h2>Domoticz AC Pilot</h2>
        <p>Creates a high-level <b>master control</b> for one or more AC splits that
        are already controllable in Domoticz through existing widgets. When the master
        control is operated, the plugin fans the command out to those existing widgets
        (referenced by idx) via the Domoticz JSON API on localhost.</p>
        <p>In the auto modes the plugin runs a <b>thermostatic fan regulation</b>: it
        reads each split's ambient temperature sensor, compares it with the Target Temp,
        and picks the fan speed automatically. An optional outdoor sensor boosts the fan
        when the outside load is high. Optional <b>motion sensors</b> add an eco setback:
        when a room is empty the split targets 25&deg;C in cooling / 18&deg;C in heating.
        A <b>warm-up learning</b> mode records how fast each room reaches target (persisted
        in a Domoticz user variable) and pre-empts the fan higher at the start of a mode
        change so the room converges faster on the next run.</p>
        <p>Sensors are sanity-checked: a temperature idx must be a Temp device, a motion
        idx must be a Motion sensor, and any sensor not updated for 60 minutes is treated
        as timed out and its value is ignored.</p>
        <p>Widgets created:</p>
        <ul style="margin-top:0">
            <li><b>Master</b> selector: Off / Cold-Auto / Heat-Auto / Manual</li>
            <li><b>Target Temp</b> setpoint (the comfort target while occupied)</li>
        </ul>
        <p>The idx fields are <b>position-aligned CSV lists</b>: the i-th idx in each list
        belongs to split i. Example for two splits: On/Off <code>64,70</code> &middot;
        Mode <code>65,71</code> &middot; Fan <code>66,72</code> &middot;
        SetTemp <code>67,73</code> &middot; Ambient <code>68,74</code> &middot;
        Motion <code>69,75</code>.</p>
    </description>
    <params>
        <param field="Mode1" label="On/Off idx (CSV; blank = power via Mode Off level)" width="280px"/>
        <param field="Mode2" label="Mode idx (CSV)" width="200px" required="true"/>
        <param field="Mode3" label="Fan idx (CSV)" width="200px" required="true"/>
        <param field="Mode4" label="SetTemp idx (CSV, optional)" width="200px"/>
        <param field="Address" label="Ambient temp idx (CSV; one split = all are averaged)" width="260px" required="true"/>
        <param field="Username" label="External temp idx (optional)" width="200px"/>
        <param field="Port" label="Motion idx (CSV, optional; any = occupied)" width="240px"/>
        <param field="Mode5" label="Levels (JSON)" width="400px" required="true" default="{&quot;mode&quot;:{&quot;off&quot;:0,&quot;cold&quot;:30,&quot;heat&quot;:40,&quot;fan_only&quot;:50},&quot;fan&quot;:{&quot;auto&quot;:10,&quot;silence&quot;:20,&quot;lvl1&quot;:30,&quot;lvl2&quot;:40,&quot;lvl3&quot;:50,&quot;lvl4&quot;:60,&quot;lvl5&quot;:70}}"/>
        <param field="Mode6" label="Debug" width="100px">
            <options>
                <option label="True" value="true"/>
                <option label="False" value="false" default="true"/>
            </options>
        </param>
    </params>
</plugin>
"""

import DomoticzEx as Domoticz
import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime

# --- Fixed Domoticz JSON API target (localhost, no auth) ---------------------
DOMOTICZ_HOST = "127.0.0.1"
DOMOTICZ_PORT = 8080
HTTP_TIMEOUT = 5

# --- Sensor sanity ------------------------------------------------------------
SENSOR_TIMEOUT_MIN = 60   # a sensor not updated in this long is "timed out"
LASTUPDATE_FMT = "%Y-%m-%d %H:%M:%S"

# --- Plugin device / unit identifiers ----------------------------------------
DEVICE_ID = "DomoticzACPilot"
UNIT_MASTER = 1
UNIT_TARGETTEMP = 2

# --- Master selector levels ---------------------------------------------------
LVL_OFF = 0
LVL_COLD = 10
LVL_HEAT = 20
LVL_MANUAL = 30

MASTER_OPTIONS = {
    "LevelActions": "|||",
    "LevelNames": "Off|Cold-Auto|Heat-Auto|Manual",
    "LevelOffHidden": "false",
    "SelectorStyle": "1",
}

DEFAULT_TARGET_TEMP = 22.0

# --- Thermostatic fan regulation ---------------------------------------------
HEARTBEAT_INTERVAL = 30  # seconds; Domoticz caps this around 30s
# Each FAN_STEP_DEG of directional error (deg C in the "wrong" direction) climbs
# one step up the fan ladder. Adapts to however many fan levels are configured.
FAN_STEP_DEG = 0.5
# Outdoor load (deg C) in the working direction above which the fan is bumped
# one extra band (hot outside while cooling / cold outside while heating).
EXT_BOOST_DELTA = 8.0

# --- Ambient sensor fusion ----------------------------------------------------
AVG_DEFAULT = "median"      # how to combine several ambient sensors per split
SENSOR_VAR_ALPHA = 0.1      # EMA weight for per-sensor deviation (weighted avg)
SENSOR_VAR_EPS = 0.01       # floor so a steady sensor can't get infinite weight
LEARN_SAVE_MIN = 15         # min minutes between periodic saves of sensor weights

# --- Occupancy / eco setback --------------------------------------------------
# A detected motion latches the room "occupied" for this long; each new
# detection re-arms the full window. Avoids chaotic comfort<->eco flipping.
MOTION_HOLD_MIN = 30
ECO_COLD_TEMP = 25.0        # target while empty in Cold-Auto
ECO_HEAT_TEMP = 18.0        # target while empty in Heat-Auto

# --- Warm-up learning ---------------------------------------------------------
LEARN_VAR_PREFIX = "DomoticzACPilot_"  # user variable name = prefix + HardwareID
WARMUP_START_ERR = 1.5      # error (deg C) that opens a warm-up episode
WARMUP_DONE_ERR = 0.3       # error at/under which the episode is "reached target"
WARMUP_REF_RATE = 0.5       # deg C/min considered "responsive enough" (no boost)
LEARN_ALPHA = 0.3           # EMA weight for new rate samples
SLOWNESS_MIN = 0.3          # floor on warm-up aggressiveness for fast rooms
DEFAULT_SLOWNESS = 0.6      # warm-up aggressiveness before anything is learned


def _json_get(params):
    """GET /json.htm and return the parsed JSON dict, or None on failure."""
    url = "http://%s:%d/json.htm?%s" % (
        DOMOTICZ_HOST,
        DOMOTICZ_PORT,
        urllib.parse.urlencode(params, quote_via=urllib.parse.quote),
    )
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
        data = json.loads(raw)
        Domoticz.Debug("JSON %s -> %s" % (url, raw))
        return data
    except Exception as exc:
        Domoticz.Error("JSON call failed: %s (%s)" % (url, exc))
        return None


def _json_call(params):
    """GET /json.htm for a command. Returns True on success."""
    data = _json_get(params)
    return data is not None and data.get("status") == "OK"


def _set_switch(idx, on):
    return _json_call({
        "type": "command", "param": "switchlight",
        "idx": idx, "switchcmd": "On" if on else "Off",
    })


def _set_level(idx, level):
    return _json_call({
        "type": "command", "param": "switchlight",
        "idx": idx, "switchcmd": "Set Level", "level": level,
    })


def _set_setpoint(idx, temp):
    return _json_call({
        "type": "command", "param": "setsetpoint",
        "idx": idx, "setpoint": temp,
    })


# Remember which (idx, reason) we have already flagged, so a persistent problem
# is logged once at Error level rather than every heartbeat.
_SENSOR_WARNED = set()


def _warn_once(idx, reason, msg):
    key = (str(idx), reason)
    if key not in _SENSOR_WARNED:
        _SENSOR_WARNED.add(key)
        Domoticz.Error(msg)
    else:
        Domoticz.Debug(msg)


def _clear_warn(idx):
    for reason in ("missing", "type", "stale", "value"):
        _SENSOR_WARNED.discard((str(idx), reason))


def _device(idx):
    """Return the first result dict for device <idx>, or None."""
    if not idx:
        return None
    data = _json_get({"type": "command", "param": "getdevices", "rid": idx})
    if not data:
        return None
    results = data.get("result") or []
    if not results:
        _warn_once(idx, "missing", "No device found for idx %s." % idx)
        return None
    return results[0]


def _is_fresh(idx, dev):
    """False if the device hasn't updated within SENSOR_TIMEOUT_MIN."""
    lu = dev.get("LastUpdate")
    if not lu:
        return True   # cannot tell; assume usable
    try:
        age = (datetime.now() - datetime.strptime(lu, LASTUPDATE_FMT)).total_seconds()
    except (ValueError, TypeError):
        return True
    if age > SENSOR_TIMEOUT_MIN * 60:
        _warn_once(idx, "stale",
                   "Sensor idx %s timed out (last update %s, > %d min); value ignored."
                   % (idx, lu, SENSOR_TIMEOUT_MIN))
        return False
    return True


def _read_temp(idx):
    """Temperature (float) from a fresh temperature device <idx>, else None."""
    dev = _device(idx)
    if dev is None:
        return None
    if dev.get("Temp") is None:
        _warn_once(idx, "type", "Device idx %s is not a temperature sensor "
                                "(no 'Temp' field); ignored." % idx)
        return None
    if not _is_fresh(idx, dev):
        return None
    try:
        value = float(dev["Temp"])
    except (ValueError, TypeError):
        _warn_once(idx, "value", "Bad temperature on idx %s (%s)." % (idx, dev.get("Temp")))
        return None
    _clear_warn(idx)
    return value


def _read_active(idx):
    """True/False if <idx> is a fresh Motion sensor; None if unusable."""
    dev = _device(idx)
    if dev is None:
        return None
    if "motion" not in str(dev.get("SwitchType", "")).lower():
        _warn_once(idx, "type", "Device idx %s is not a Motion sensor "
                                "(SwitchType=%s); ignored." % (idx, dev.get("SwitchType")))
        return None
    if not _is_fresh(idx, dev):
        return None
    _clear_warn(idx)
    status = str(dev.get("Status", "")).strip().lower()
    return status not in ("off", "0", "false", "")


def _read_on(idx):
    """Current On/Off state of a switch device <idx>: True/False, or None.
    No freshness gate — a control device only changes when commanded."""
    dev = _device(idx)
    if dev is None:
        return None
    status = str(dev.get("Status", "")).strip().lower()
    return status not in ("off", "0", "false", "")


def _read_level(idx):
    """Current selector Level (int) of device <idx>, or None."""
    dev = _device(idx)
    if dev is None:
        return None
    try:
        return int(dev.get("Level"))
    except (ValueError, TypeError):
        return None


def _parse_csv(text):
    return [item.strip() for item in (text or "").split(",") if item.strip()]


class BasePlugin:
    def __init__(self):
        # split = {onoff, mode, fan, settemp, ambient[list], motion[list],
        #          last_fan, last_setpoint, last_motion, warm-up learning ...}
        self.splits = []
        self.cool_level = 20
        self.heat_level = 10
        self.off_level = 0        # Mode selector's Off level (mode-only units)
        self.fan_levels = []      # speed levels low -> high
        self.ext_idx = None
        self.target_temp = DEFAULT_TARGET_TEMP
        self.master_level = LVL_OFF
        self.learn_enabled = True
        self.var_name = LEARN_VAR_PREFIX + "0"
        self.var_idx = None
        self.avg_mode = AVG_DEFAULT
        self.sensor_var = {}      # idx -> learned deviation variance
        self.sensors_dirty = False
        self.last_save_ts = 0.0

    # -- lifecycle ------------------------------------------------------------
    def onStart(self):
        Domoticz.Debugging(1 if Parameters["Mode6"] == "true" else 0)

        onoff = _parse_csv(Parameters["Mode1"])
        mode = _parse_csv(Parameters["Mode2"])
        fan = _parse_csv(Parameters["Mode3"])
        settemp = _parse_csv(Parameters["Mode4"])
        ambient = _parse_csv(Parameters["Address"])
        motion = _parse_csv(Parameters["Port"])

        # Mode + Fan define the split count; On/Off is optional (mode-only units
        # power on/off through the Mode selector's Off level instead).
        n = min(len(mode), len(fan))
        if len(mode) != len(fan):
            Domoticz.Error("Mode/Fan idx lists differ in length (%d vs %d); using %d split(s)."
                           % (len(mode), len(fan), n))
        if onoff and len(onoff) < n:
            Domoticz.Error("On/Off idx list shorter than splits; the rest are driven "
                           "via the Mode selector's Off level.")
        if settemp and len(settemp) < n:
            Domoticz.Error("SetTemp idx list shorter than splits; missing entries skipped.")
        # Ambient & motion sensors: one split -> every listed idx belongs to it;
        # several splits -> commas separate splits, '+' groups sensors per split.
        amb_groups = self._idx_groups(ambient, n)
        mot_groups = self._idx_groups(motion, n)
        for i, grp in enumerate(amb_groups):
            if not grp:
                Domoticz.Error("Split %d has no ambient sensor; it will not regulate." % i)

        now = time.time()
        for i in range(n):
            self.splits.append({
                "onoff": onoff[i] if i < len(onoff) else None,
                "mode": mode[i],
                "fan": fan[i],
                "settemp": settemp[i] if i < len(settemp) else None,
                "ambient": amb_groups[i],   # list of sensor idx
                "motion": mot_groups[i],     # list of motion idx (OR'd)
                "last_fan": None,
                "last_setpoint": None,
                "last_motion": now,   # assume occupied at startup
                # warm-up learning (rate persisted; the rest is transient):
                "rate": None,         # learned approach rate, deg C/min
                "n": 0,               # number of samples behind the rate
                "warmup_active": False,
                "warmup_start_err": 0.0,
                "warmup_start_ts": 0.0,
                "dirty": False,       # learning changed, needs persisting
            })
        Domoticz.Log("Configured %d split(s)." % len(self.splits))

        # Mode5 = JSON:
        #   {"mode":{"cold":30,"heat":40,...},
        #    "fan":{"auto":10,"silence":20,"lvl1":30,...}}
        # Regulation ladder = fan levels sorted ascending by value, excluding "auto".
        try:
            cfg = json.loads(Parameters["Mode5"])
            self.cool_level = int(cfg["mode"]["cold"])
            self.heat_level = int(cfg["mode"]["heat"])
            self.off_level = int(cfg["mode"].get("off", 0))
            fan = {name: int(level) for name, level in cfg["fan"].items()
                   if name.strip().lower() != "auto"}
            self.fan_levels = sorted(fan.values())
            self.learn_enabled = bool(cfg.get("learn", True))
            self.avg_mode = str(cfg.get("avg", AVG_DEFAULT)).strip().lower()
            if self.avg_mode not in ("mean", "median", "weighted"):
                self.avg_mode = AVG_DEFAULT
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            self.fan_levels = []
            Domoticz.Error("Could not parse Levels JSON '%s' (%s); fan regulation disabled."
                           % (Parameters["Mode5"], exc))
        if not self.fan_levels:
            Domoticz.Error("No valid fan speed levels configured; fan regulation disabled.")

        ext = _parse_csv(Parameters["Username"])
        self.ext_idx = ext[0] if ext else None

        self.var_name = LEARN_VAR_PREFIX + str(Parameters.get("HardwareID", "0"))
        Domoticz.Debug("cool=%d heat=%d fan_levels=%s ext_idx=%s learn=%s var=%s"
                       % (self.cool_level, self.heat_level, self.fan_levels,
                          self.ext_idx, self.learn_enabled, self.var_name))

        self._ensure_units()
        self._read_back_state()
        if self.learn_enabled:
            self._load_learning()
        self.last_save_ts = time.time()   # throttle periodic saves from startup
        Domoticz.Heartbeat(HEARTBEAT_INTERVAL)

    def onStop(self):
        Domoticz.Debug("onStop")

    # -- command handling -----------------------------------------------------
    def onCommand(self, DeviceID, Unit, Command, Level, Color):
        Domoticz.Debug("onCommand: Device=%s Unit=%s Command=%s Level=%s"
                       % (DeviceID, Unit, Command, Level))

        if Unit == UNIT_MASTER:
            if Command == "Off":
                level = LVL_OFF
            elif Command == "Set Level":
                level = int(Level)
            else:
                Domoticz.Debug("Ignoring master command '%s'." % Command)
                return
            self.master_level = level
            self._apply_master(level)
            self._update_unit(UNIT_MASTER, 1 if level > 0 else 0, str(level))

        elif Unit == UNIT_TARGETTEMP:
            self.target_temp = float(Level)
            self._update_unit(UNIT_TARGETTEMP, 0, str(self.target_temp))
            self._regulate()   # pushes the right setpoint based on occupancy

    def onHeartbeat(self):
        if self.master_level in (LVL_COLD, LVL_HEAT):
            self._regulate()

    # -- master mode application ---------------------------------------------
    def _power(self, s, on):
        """Power a split on/off. With an On/Off idx, drive the switch; otherwise
        (mode-only unit) turn OFF via the Mode selector's Off level. Powering ON
        a mode-only unit is implicit in setting its Cold/Heat mode level."""
        if s["onoff"]:
            _set_switch(s["onoff"], on)
        elif not on:
            _set_level(s["mode"], self.off_level)

    def _is_powered(self, s):
        """Actual power state of a split: True/False, or None if unreadable.
        Read from the On/Off switch if present, else the Mode selector level
        (off when it sits at the configured Off level)."""
        if s["onoff"]:
            return _read_on(s["onoff"])
        level = _read_level(s["mode"])
        return None if level is None else (level != self.off_level)

    def _apply_master(self, level):
        if level == LVL_OFF:
            for s in self.splits:
                self._power(s, False)
                self._reset_split(s)
            return

        if level == LVL_MANUAL:
            # Yield control: power on, leave Mode/Fan/SetTemp to the user.
            for s in self.splits:
                self._power(s, True)
                self._reset_split(s)
            return

        mode_level = self.cool_level if level == LVL_COLD else self.heat_level
        for s in self.splits:
            if s["onoff"]:
                _set_switch(s["onoff"], True)
            _set_level(s["mode"], mode_level)   # also powers on a mode-only unit
            self._reset_split(s)
        # We just powered on, so don't second-guess it on this pass.
        self._regulate(check_power=False)

    # -- thermostatic fan regulation -----------------------------------------
    def _regulate(self, check_power=True):
        if self.master_level not in (LVL_COLD, LVL_HEAT) or not self.fan_levels:
            return
        now = time.time()
        ext = _read_temp(self.ext_idx) if self.ext_idx else None
        eco = ECO_COLD_TEMP if self.master_level == LVL_COLD else ECO_HEAT_TEMP

        for s in self.splits:
            if check_power and self._is_powered(s) is False:
                # User powered the unit off (e.g. via the remote): back off and
                # do not fight it. Regulation resumes once it is on again.
                Domoticz.Debug("Mode idx %s is off externally; skipping regulation."
                               % s["mode"])
                self._reset_split(s)
                continue

            occupied = self._occupied(s, now)
            target = self.target_temp if occupied else eco

            if s["settemp"] and s["last_setpoint"] != target:
                if _set_setpoint(s["settemp"], target):
                    s["last_setpoint"] = target

            amb, used = self._room_temp(s)
            if amb is None:
                continue
            if self.master_level == LVL_COLD:
                error = amb - target
                boost = ext is not None and (ext - target) > EXT_BOOST_DELTA
            else:
                error = target - amb
                boost = ext is not None and (target - ext) > EXT_BOOST_DELTA

            if self.learn_enabled:
                self._update_warmup(s, error, now)
            level = self._fan_for(s, error, boost)
            Domoticz.Debug(
                "Fan idx %s: amb=%.1f(%d/%d %s) target=%.1f(%s) error=%.2f ext=%s "
                "boost=%s warmup=%s rate=%s -> %d"
                % (s["fan"], amb, used, len(s["ambient"]), self.avg_mode, target,
                   "occ" if occupied else "ECO", error, ext, boost,
                   s["warmup_active"], s["rate"], level)
            )
            if s["last_fan"] != level and _set_level(s["fan"], level):
                s["last_fan"] = level

        if not self.learn_enabled:
            return
        if any(s["dirty"] for s in self.splits):
            self._save_learning(now)               # warm-up sample: save now
        elif self.sensors_dirty and (now - self.last_save_ts) >= LEARN_SAVE_MIN * 60:
            self._save_learning(now)               # sensor weights: throttled

    # -- ambient sensor fusion ------------------------------------------------
    @staticmethod
    def _idx_groups(tokens, n):
        """One idx list per split. n==1: every token belongs to the split.
        n>1: each token is a split, '+' grouping multiple sensors within it."""
        if n <= 1:
            flat = [x.strip() for tok in tokens for x in tok.split("+") if x.strip()]
            return [flat]
        groups = []
        for i in range(n):
            tok = tokens[i] if i < len(tokens) else ""
            groups.append([x.strip() for x in tok.split("+") if x.strip()])
        return groups

    def _room_temp(self, s):
        """Fuse a split's ambient sensors. Returns (temp_or_None, count_used)."""
        readings = [(idx, _read_temp(idx)) for idx in s["ambient"]]
        readings = [(idx, t) for idx, t in readings if t is not None]
        temps = [t for _, t in readings]
        if not temps:
            return None, 0
        if len(temps) == 1:
            return temps[0], 1
        if self.avg_mode == "mean":
            return sum(temps) / len(temps), len(temps)
        if self.avg_mode == "weighted":
            return self._weighted_temp(readings, temps), len(temps)
        return self._median(temps), len(temps)   # default: robust median

    @staticmethod
    def _median(vals):
        s = sorted(vals)
        k = len(s)
        mid = k // 2
        return s[mid] if k % 2 else (s[mid - 1] + s[mid]) / 2.0

    def _weighted_temp(self, readings, temps):
        """Inverse-variance weighting: sensors that consistently deviate from the
        group consensus are learned (in-memory EMA) and quietly down-weighted."""
        consensus = self._median(temps)
        num = den = 0.0
        weights = {}
        for idx, t in readings:
            dev2 = (t - consensus) ** 2
            var = self.sensor_var.get(idx)
            var = dev2 if var is None else (1 - SENSOR_VAR_ALPHA) * var + SENSOR_VAR_ALPHA * dev2
            self.sensor_var[idx] = var
            w = 1.0 / (var + SENSOR_VAR_EPS)
            weights[idx] = w
            num += w * t
            den += w
        result = num / den if den else consensus
        self.sensors_dirty = True
        # Readable trace: each sensor's temp and its share (%) of the average.
        parts = " ".join("%s=%.1f(%.0f%%)" % (idx, t, 100.0 * weights[idx] / den)
                         for idx, t in readings) if den else ""
        Domoticz.Debug("Sensor fusion: consensus=%.2f -> %.2f | %s"
                       % (consensus, result, parts))
        return result

    def _occupied(self, s, now):
        sensors = s["motion"]
        if not sensors:
            return True
        any_readable = False
        for idx in sensors:
            active = _read_active(idx)
            if active is None:
                continue          # stale/invalid sensor: ignore it
            any_readable = True
            if active:            # OR: any sensor seeing motion re-arms the hold
                s["last_motion"] = now
                break
        if not any_readable:
            return True           # no usable motion sensor: stay comfortable
        return (now - s["last_motion"]) <= MOTION_HOLD_MIN * 60

    def _fan_for(self, s, error, boost):
        """Steady-state ladder index, lifted by an active warm-up pre-empt."""
        top = len(self.fan_levels) - 1
        if error <= 0:
            # At or past target: no demand. Idle the fan to its slowest step,
            # regardless of the outdoor boost (boost only helps us *reach* target).
            return self.fan_levels[0]
        idx = int(error / FAN_STEP_DEG)
        if boost:
            idx += 1
        if s["warmup_active"] and s["warmup_start_err"] > 0:
            # Jump high at the start of the episode, easing down as the room
            # nears target. Aggressiveness scales with the room's slowness.
            remaining = max(0.0, min(1.0, error / s["warmup_start_err"]))
            warm_idx = math.ceil(remaining * top * self._slowness(s))
            idx = max(idx, warm_idx)
        return self.fan_levels[max(0, min(idx, top))]

    def _slowness(self, s):
        """Warm-up aggressiveness in (SLOWNESS_MIN, 1.0]. Unknown rooms start at
        DEFAULT_SLOWNESS; learning then raises it for slow rooms (more reactive
        next time) and lowers it for fast rooms that converge on their own."""
        rate = s["rate"]
        if not rate or rate <= 0:
            return DEFAULT_SLOWNESS
        return max(SLOWNESS_MIN, min(1.0, WARMUP_REF_RATE / rate))

    def _update_warmup(self, s, error, now):
        """Open/close a warm-up episode and learn the room's approach rate."""
        if not s["warmup_active"]:
            if error >= WARMUP_START_ERR:
                s["warmup_active"] = True
                s["warmup_start_err"] = error
                s["warmup_start_ts"] = now
                Domoticz.Debug("Warm-up OPEN for fan idx %s at error=%.2f"
                               % (s["fan"], error))
            return
        if error <= WARMUP_DONE_ERR:
            elapsed_min = (now - s["warmup_start_ts"]) / 60.0
            covered = s["warmup_start_err"] - error
            Domoticz.Debug("Warm-up CLOSE for fan idx %s: covered %.2f C in %.1f min"
                           % (s["fan"], covered, elapsed_min))
            if elapsed_min > 0.1 and covered > 0:
                self._learn_rate(s, covered / elapsed_min)
            else:
                Domoticz.Debug("Warm-up sample for idx %s discarded (too short/no gain)."
                               % s["fan"])
            s["warmup_active"] = False
        elif error > s["warmup_start_err"]:
            # Target moved further away mid-episode: rebaseline.
            s["warmup_start_err"] = error
            s["warmup_start_ts"] = now

    def _learn_rate(self, s, rate):
        if s["n"] == 0 or not s["rate"]:
            s["rate"] = rate
        else:
            s["rate"] = (1 - LEARN_ALPHA) * s["rate"] + LEARN_ALPHA * rate
        s["n"] += 1
        s["dirty"] = True
        Domoticz.Log("Learned warm-up rate for fan idx %s: %.2f C/min (n=%d)"
                     % (s["fan"], s["rate"], s["n"]))

    # -- learning persistence (Domoticz user variable) ------------------------
    def _find_var(self):
        """Return (idx, value) of our user variable, or (None, None)."""
        data = _json_get({"type": "command", "param": "getuservariables"})
        for v in (data or {}).get("result") or []:
            if v.get("Name") == self.var_name:
                return v.get("idx"), v.get("Value")
        return None, None

    def _load_learning(self):
        self.var_idx, value = self._find_var()
        if not self.var_idx:
            return
        try:
            stored = json.loads(value or "{}")
        except (ValueError, TypeError):
            stored = {}
        for i, s in enumerate(self.splits):
            rec = stored.get(str(i))
            if not isinstance(rec, dict):
                continue
            if rec.get("rate"):
                s["rate"] = float(rec["rate"])
                s["n"] = int(rec.get("n", 1))
            for idx, var in (rec.get("sensors") or {}).items():
                try:
                    self.sensor_var[idx] = float(var)
                except (ValueError, TypeError):
                    pass
        Domoticz.Debug("Loaded learning from var %s: %s" % (self.var_idx, stored))

    def _save_learning(self, now):
        payload = {}
        for i, s in enumerate(self.splits):
            rec = {}
            if s["rate"]:
                rec["rate"] = round(s["rate"], 3)
                rec["n"] = s["n"]
            sensors = {idx: round(self.sensor_var[idx], 4)
                       for idx in s["ambient"] if idx in self.sensor_var}
            if sensors:
                rec["sensors"] = sensors
            if rec:
                payload[str(i)] = rec
        self.last_save_ts = now
        self.sensors_dirty = False
        value = json.dumps(payload)
        action = "update" if self.var_idx else "add"
        Domoticz.Log("Saving learning to user variable '%s' (%s): %s"
                     % (self.var_name, action, value))
        if self.var_idx:
            ok = _json_call({"type": "command", "param": "updateuservariable",
                             "idx": self.var_idx, "vname": self.var_name,
                             "vtype": 2, "vvalue": value})
        else:
            ok = _json_call({"type": "command", "param": "adduservariable",
                             "vname": self.var_name, "vtype": 2, "vvalue": value})
            if ok:
                self.var_idx, _ = self._find_var()   # capture idx for future updates
        if ok:
            Domoticz.Log("User variable '%s' saved (idx=%s)." % (self.var_name, self.var_idx))
        else:
            Domoticz.Error("Failed to save user variable '%s'." % self.var_name)
        for s in self.splits:
            s["dirty"] = False

    # -- helpers --------------------------------------------------------------
    @staticmethod
    def _reset_split(s):
        s["last_fan"] = None
        s["last_setpoint"] = None
        s["warmup_active"] = False

    def _ensure_units(self):
        if not self._unit_exists(UNIT_MASTER):
            Domoticz.Unit(
                Name="Master", DeviceID=DEVICE_ID, Unit=UNIT_MASTER,
                TypeName="Selector Switch", Switchtype=18,
                Options=MASTER_OPTIONS, Used=1,
            ).Create()
        if not self._unit_exists(UNIT_TARGETTEMP):
            Domoticz.Unit(
                Name="Target Temp", DeviceID=DEVICE_ID, Unit=UNIT_TARGETTEMP,
                Type=242, Subtype=1, Used=1,
            ).Create()

    def _read_back_state(self):
        if self._unit_exists(UNIT_MASTER):
            try:
                self.master_level = int(Devices[DEVICE_ID].Units[UNIT_MASTER].sValue or 0)
            except (ValueError, TypeError):
                self.master_level = LVL_OFF
        if self._unit_exists(UNIT_TARGETTEMP):
            try:
                self.target_temp = float(Devices[DEVICE_ID].Units[UNIT_TARGETTEMP].sValue)
            except (ValueError, TypeError):
                self.target_temp = DEFAULT_TARGET_TEMP
        Domoticz.Debug("State: master_level=%d target_temp=%s"
                       % (self.master_level, self.target_temp))

    @staticmethod
    def _unit_exists(unit):
        return DEVICE_ID in Devices and unit in Devices[DEVICE_ID].Units

    @staticmethod
    def _update_unit(unit, nvalue, svalue):
        u = Devices[DEVICE_ID].Units[unit]
        u.nValue = nvalue
        u.sValue = svalue
        u.Update()


_plugin = BasePlugin()


def onStart():
    _plugin.onStart()


def onStop():
    _plugin.onStop()


def onCommand(DeviceID, Unit, Command, Level, Color):
    _plugin.onCommand(DeviceID, Unit, Command, Level, Color)


def onHeartbeat():
    _plugin.onHeartbeat()
