"""Flask + Flask-SocketIO smart-home server for WifiSense.

Data sources (TEAMCSI_MODE):
  auto    - live ESP32 serial if a port answers, otherwise mock_parser.py
  serial  - ESP32 CSV over USB serial (TEAMCSI_PORT, TEAMCSI_BAUD)
  mock    - mock_parser.py subprocess (original behaviour)

Incoming readings pass through signal conditioning before reaching clients:
state debounce (majority vote over N packets), optional confidence gate, and
exponential smoothing of the CSI waveform. If the serial feed goes quiet for
SERIAL_FALLBACK_SECS, an in-process simulator takes over so a live demo never
freezes; it hands control back the moment real data returns.

A phone-friendly director page at /control can force states (Empty, Moving,
Sleeping, Intruder) which bypass conditioning for instant scene changes.
"""

import json
import math
import os
import random
import socket
import subprocess
import sys
import threading
import time
from collections import deque

import serial
import serial.tools.list_ports

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

import alerts
from mock_parser import synth_csi

PARSER_SCRIPT = "mock_parser.py"

MODE = os.environ.get("TEAMCSI_MODE", "auto")          # auto | serial | mock
SERIAL_PORT = os.environ.get("TEAMCSI_PORT", "")       # e.g. COM3 ("" = first ESP32-looking port)
BAUD_RATE = int(os.environ.get("TEAMCSI_BAUD", "115200"))
SERIAL_FALLBACK_SECS = float(os.environ.get("TEAMCSI_FALLBACK", "15"))

# CSV layout coming from the ESP32: state[, rssi[, confidence]][, csi amplitudes...]
CSV_STATE_IDX = int(os.environ.get("TEAMCSI_CSV_STATE", "0"))
CSV_RSSI_IDX = os.environ.get("TEAMCSI_CSV_RSSI", "")   # column index or "" if absent
CSV_CONF_IDX = os.environ.get("TEAMCSI_CSV_CONF", "")

STABLE_N = int(os.environ.get("TEAMCSI_STABLE_N", "3"))       # packets to confirm a state change
MIN_CONFIDENCE = float(os.environ.get("TEAMCSI_MIN_CONF", "0"))  # 0 disables the gate
CSI_ALPHA = float(os.environ.get("TEAMCSI_CSI_ALPHA", "0.5")) # waveform smoothing factor

# ---- Occupancy classifier (ESP32 CSI Tool raw stream) ----
MOTION_VAR_TH = float(os.environ.get("TEAMCSI_MOTION_TH", "2.0"))   # aggregate z that counts as motion
MOTION_PEAK_Z = float(os.environ.get("TEAMCSI_MOTION_PEAK", "3.4")) # single-packet blockage spike
SLEEP_BAND_LO, SLEEP_BAND_HI = 0.13, 0.55                           # breathing band, Hz
SLEEP_SNR_TH = float(os.environ.get("TEAMCSI_SLEEP_SNR", "1.35"))   # band/noise ratio
SLEEP_QUIET_SECS = float(os.environ.get("TEAMCSI_QUIET_SECS", "10"))# quiet time before sleep verdict
BASE_ALPHA = float(os.environ.get("TEAMCSI_BASE_ALPHA", "0.02"))    # baseline EMA speed (calm only)
DISPLAY_ALPHA = 0.05                                                # waveform cosmetics adapt faster
WARMUP_SECS = float(os.environ.get("TEAMCSI_WARMUP", "8"))          # discard stream right after open

STATE_LABELS = {0: "Empty", 1: "Moving", 2: "Sleeping"}

TEMP_MIN, TEMP_MAX = 16, 30
BRIGHTNESS_MIN, BRIGHTNESS_MAX = 5, 100

app = Flask(__name__)
app.config["SECRET_KEY"] = "wifi-sense-dashboard"

# threading mode keeps things simple: reading the subprocess's stdout is a
# blocking call, so it needs a real OS thread rather than a greenlet.
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

# ---- Shared state (parser thread + HTTP handlers) ----
lock = threading.Lock()

sensor = {"state": 0, "label": STATE_LABELS[0], "timestamp": time.time()}
away_mode = {"enabled": False}
energy_saved = 0.0

override = {"state": None}          # None = follow live feed; int = director-forced state
source_status = {"mode": MODE, "feed": "starting"}   # feed: live | simulated | directed

# Occupancy headcount: None = gentle auto estimate wandering between 4 and 6;
# an int pins the displayed value until released.
people = {"count": None}
_people_auto = {"value": 5, "next_shift": time.time() + 12, "last_emitted": None}

devices = {
    "hvac": {"on": True, "temp": 22, "mode": "auto"},
    "lighting": {"on": True, "brightness": 80, "mode": "auto"},
}

activity_log = []  # newest first
history = []       # state transitions, oldest first: [{"state": int, "timestamp": float}]

# ---- Signal-conditioning state (owned by the ingestion path) ----
stable_state = 0
candidate_state = None
candidate_count = 0
smoothed_csi = []
sim_active = threading.Event()      # fallback simulator currently emitting
active_serial_port = None

def send_oled_state(state_label):
    global active_serial_port
    if active_serial_port and active_serial_port.is_open:
        try:
            active_serial_port.write(f"OLED:{state_label}\n".encode('utf-8'))
        except Exception as e:
            print(f"[serial] failed to write OLED state: {e}")


def log_activity(kind, message):
    entry = {"kind": kind, "message": message, "timestamp": time.time()}
    with lock:
        activity_log.insert(0, entry)
        del activity_log[50:]
    socketio.emit("activity", entry)
    return entry


def automation_targets(state):
    """Device values the ecosystem wants for each presence state."""
    if state == 0:      # Empty: everything powered down
        return {"hvac": {"on": False}, "lighting": {"on": False, "brightness": 0}}
    if state == 2:      # Sleeping: low-power HVAC, lights off
        return {"hvac": {"on": True, "temp": 20}, "lighting": {"on": False, "brightness": 0}}
    return {"hvac": {"on": True, "temp": 22},   # Moving / occupied
            "lighting": {"on": True, "brightness": 80}}


def apply_automation(state):
    """Push automated targets onto any device still in auto mode."""
    targets = automation_targets(state)
    with lock:
        for name, target in targets.items():
            device = devices[name]
            if device["mode"] != "auto":
                continue
            changed = False
            for key, value in target.items():
                if device[key] != value:
                    device[key] = value
                    changed = True
            if changed:
                socketio.emit("device_update", {"name": name, "device": dict(device)})


def _emit_reading(state, timestamp, rssi, confidence, csi):
    """Update shared state + broadcast one conditioned packet."""
    global energy_saved

    label = STATE_LABELS.get(state, "Unknown")

    with lock:
        previous = sensor["state"]
        sensor["state"] = state
        sensor["label"] = label
        sensor["timestamp"] = timestamp
        sensor["rssi"] = rssi
        sensor["confidence"] = confidence
        sensor["packet_size"] = len(csi)
        sensor["seq"] = getattr(_emit_reading, "seq", 0)
        _emit_reading.seq = getattr(_emit_reading, "seq", 0) + 1
        if state != previous or not history:
            history.append({"state": state, "timestamp": timestamp})
            del history[:-2000]
        if state == 0:  # empty room -> HVAC/lighting idle -> energy accrues
            energy_saved += 0.0009 + random.random() * 0.0004
        snapshot_energy = energy_saved

    payload = {"state": state, "label": label,
               "timestamp": timestamp,
               "energy_saved": round(snapshot_energy, 4),
               "seq": sensor["seq"],
               "rssi": rssi,
               "confidence": confidence,
               "packet_size": len(csi),
               "csi": csi}
    socketio.emit("sensor_update", payload)

    if state != previous:
        apply_automation(state)
        descriptions = {
            0: "Room became empty — eco shutdown engaged",
            1: "Motion detected",
            2: "Occupant settled — sleep mode active",
        }
        message = descriptions.get(state, f"State changed to {label}")
        log_activity("sensor", message)
        if state == 1 and away_mode["enabled"]:
            alerts.send_alert("intruder", "INTRUDER ALERT — motion detected while Away Mode is armed!", force=True)
            send_oled_state("Intruder")
        else:
            alerts.send_alert("state", f"WifiSense: {message}")
            send_oled_state(label)


def _condition(raw_state, raw_csi, confidence):
    """Debounce + gate a live reading. Returns (state_to_publish, csi)."""
    global stable_state, candidate_state, candidate_count

    # Smooth waveform regardless of classification stability.
    global smoothed_csi
    if len(raw_csi) == 64:
        if not smoothed_csi:
            smoothed_csi = list(raw_csi)
        else:
            smoothed_csi = [round(CSI_ALPHA * a + (1 - CSI_ALPHA) * b, 2)
                            for a, b in zip(raw_csi, smoothed_csi)]
    csi = smoothed_csi or raw_csi

    gated = MIN_CONFIDENCE > 0 and confidence is not None and confidence < MIN_CONFIDENCE
    if gated or raw_state not in STATE_LABELS:
        return stable_state, csi

    if raw_state == stable_state:
        candidate_state, candidate_count = None, 0
    elif raw_state == candidate_state:
        candidate_count += 1
        if candidate_count >= STABLE_N:
            stable_state = raw_state
            candidate_state, candidate_count = None, 0
    else:
        candidate_state, candidate_count = raw_state, 1

    return stable_state, csi


def process_live_packet(data):
    """Ingest one packet from any live feed (serial or mock subprocess).

    While the director console holds a scene, live packets are dropped so the
    forced state stays on screen untouched.
    """
    if override["state"] is not None:
        return
    ts = float(data.get("timestamp", time.time()))
    state, csi = _condition(int(data.get("state", 0)),
                            data.get("csi") or [],
                            data.get("confidence"))
    _emit_reading(state, ts, data.get("rssi"), data.get("confidence"), csi)


def _auto_people():
    now = time.time()
    if now >= _people_auto["next_shift"]:
        _people_auto["value"] = max(4, min(6, _people_auto["value"] + random.choice([-1, 0, 1])))
        _people_auto["next_shift"] = now + random.uniform(12, 25)
    return _people_auto["value"]


def current_people():
    with lock:
        pinned = people["count"]
    return pinned if pinned is not None else _auto_people()


def director_loop():
    """Keep emitting the forced scene once per second until released."""
    while True:
        with lock:
            state = override["state"]
        if state is not None:
            process_directed_packet(state)

        # Headcount: broadcast only when the displayed estimate changes.
        count = current_people()
        if count != _people_auto["last_emitted"]:
            _people_auto["last_emitted"] = count
            socketio.emit("people_update", {"count": count})
        time.sleep(1)


def process_directed_packet(state):
    """Build a scene-perfect packet for the director console; no conditioning."""
    phase = time.time() % 100
    csi = synth_csi(state, phase)
    _emit_reading(state, time.time(),
                  round(random.uniform(-62, -55), 1),
                  round(random.uniform(0.94, 0.98), 2), csi)


def parse_esp32_line(line):
    """Parse one ESP32 CSI Tool row: CSI_DATA,AP,<mac>,<rssi>,...,len,[re im re im ...]."""
    line = line.strip()
    bracket = line.find("[")
    close = line.rfind("]")
    if bracket == -1 or close == -1 or close < bracket:
        return None
    head = line[:bracket].split(",")
    try:
        rssi = float(head[3])
        mac = head[2]
        iq = [float(v) for v in line[bracket + 1:close].split()]
    except (IndexError, ValueError):
        return None
    if len(iq) < 128:
        return None
    amps = [math.hypot(iq[i], iq[i + 1]) for i in range(0, len(iq) - 1, 2)][:64]
    # Drop the first/last few subcarriers: DC offset, pilot tones and guard
    # edges carry junk that dominates any statistical model.
    return {"rssi": rssi, "mac": mac, "amps": amps[3:61], "full": amps}


class CsiClassifier:
    """Occupancy from the CSI stream, using the same features as the
    dashboard's theory pages: disturbance variance -> Moving, breathing-band
    ripple while quiet -> Sleeping, stable baseline -> Empty.

    Classification runs on aggregate scalars (mean subcarrier amplitude and
    RSSI) because per-subcarrier z-scores are dominated by bursty phone
    traffic; per-subcarrier deviations feed the waveform display only.
    """

    def __init__(self):
        # scalar classification model
        self.mu_m = None                   # mean-amplitude baseline
        self.var_m = 1.0
        self.mu_r = None                   # rssi baseline
        self.var_r = 4.0
        self.act_q = deque()               # (t, activity) short window
        self.m_q = deque(maxlen=600)       # (t, mean amplitude) breathing window
        self.quiet_since = None
        self.t_start = time.time()
        self.last_activity = 0.0
        self.mac_lock = None
        self.mac_counts = {}
        # cosmetic per-subcarrier model for the live chart
        self.dmu = None
        self.dvar = None

    def render(self, full_amps):
        """Cosmetic waveform: fast-adapting per-subcarrier deviations."""
        if self.dmu is None:
            self.dmu = list(full_amps)
            self.dvar = [4.0] * len(full_amps)
            return [0.0] * len(full_amps)
        z = [(a - m) / math.sqrt(max(v, 0.05))
             for a, m, v in zip(full_amps, self.dmu, self.dvar)]
        for i, a in enumerate(full_amps):
            d = a - self.dmu[i]
            self.dmu[i] += DISPLAY_ALPHA * d
            self.dvar[i] += DISPLAY_ALPHA * (d * d - self.dvar[i])
        return [max(-6, min(6, v)) for v in z]

    def update(self, amps, rssi, ts):
        """Feed one packet; returns (state, confidence)."""
        m = sum(amps) / len(amps)

        if self.mu_m is None:
            self.mu_m, self.mu_r = m, rssi

        z_m = (m - self.mu_m) / math.sqrt(max(self.var_m, 1e-6))
        z_r = (rssi - self.mu_r) / math.sqrt(max(self.var_r, 1e-6))
        activity = max(abs(z_m), 0.7 * abs(z_r))
        self.last_activity = abs(z_m)

        self.act_q.append((ts, activity))
        while self.act_q and ts - self.act_q[0][0] > 1.5:
            self.act_q.popleft()
        e_short = sum(v for _, v in self.act_q) / max(len(self.act_q), 1)

        # Track the resting channel only while things look calm.
        if e_short < MOTION_VAR_TH * 0.7:
            self.mu_m += BASE_ALPHA * (m - self.mu_m)
            self.var_m += BASE_ALPHA * ((m - self.mu_m) ** 2 - self.var_m)
            self.mu_r += BASE_ALPHA * (rssi - self.mu_r)
            self.var_r += BASE_ALPHA * ((rssi - self.mu_r) ** 2 - self.var_r)

        self.m_q.append((ts, m))

        if ts - self.t_start < WARMUP_SECS:
            return 0, 0.5

        if e_short >= MOTION_VAR_TH or abs(z_m) >= MOTION_PEAK_Z:
            self.quiet_since = None
            return 1, min(0.96, 0.72 + e_short / 8)

        if self.quiet_since is None:
            self.quiet_since = ts
        if ts - self.quiet_since >= SLEEP_QUIET_SECS:
            snr = self._breath_snr()
            if snr >= SLEEP_SNR_TH:
                return 2, min(0.95, 0.78 + snr / 6)
        return 0, 0.90

    def _breath_snr(self):
        """Ratio of 0.13-0.55 Hz band power to fast-noise power in mean amplitude."""
        if len(self.m_q) < 40:
            return 0.0
        t0 = self.m_q[0][0]
        span = self.m_q[-1][0] - t0
        if span < SLEEP_QUIET_SECS:
            return 0.0
        pairs = list(self.m_q)
        vals = [v for _, v in pairs]
        n = len(vals)
        mean = sum(vals) / n
        vals = [v - mean for v in vals]

        def band_power(lo, hi):
            total, steps = 0.0, 6
            for k in range(steps):
                f = lo + (hi - lo) * (k + 0.5) / steps
                w = 2 * math.pi * f
                c = sum(v * math.cos(w * (t - t0)) for v, (t, _) in zip(vals, pairs))
                s = sum(v * math.sin(w * (t - t0)) for v, (t, _) in zip(vals, pairs))
                total += (c * c + s * s) / (n * n)
            return total / steps

        signal = band_power(SLEEP_BAND_LO, SLEEP_BAND_HI)
        noise = band_power(1.2, 3.0) + 1e-12
        return signal / noise


esp32_classifier = CsiClassifier()
_esp32_last_emit = 0.0


def _pad_display64(z):
    """Re-expand edge-trimmed deviations onto the full 64-subcarrier chart."""
    return [round(v, 2) for v in ([z[0]] * 3 + list(z) + [z[-1]] * 3)]


def esp32_packet(text):
    """Full-rate ingestion: classify every packet, emit to clients ~3x/s."""
    global _esp32_last_emit
    parsed = parse_esp32_line(text)
    if not parsed:
        return
    clf = esp32_classifier
    mac = parsed.get("mac", "")

    if clf.mac_lock is None:
        # Learn which transmitter is the actual sensing client before judging motion.
        if time.time() - clf.t_start < WARMUP_SECS + 15:
            clf.mac_counts[mac] = clf.mac_counts.get(mac, 0) + 1
            return
        if not clf.mac_counts:
            return
        clf.mac_lock = max(clf.mac_counts, key=clf.mac_counts.get)
        clf.t_start = time.time()       # warmup restarts once the lock is set
        print(f"[classifier] locked onto client {clf.mac_lock} "
              f"({clf.mac_counts[clf.mac_lock]} pkts); ignoring other transmitters")
    elif mac != clf.mac_lock:
        return                          # neighbour network on the same channel — ignore

    now = time.time()
    display = _pad_display64(clf.render(parsed["full"]))
    state, confidence = clf.update(parsed["amps"], parsed["rssi"], now)

    if now - _esp32_last_emit < 0.33:
        return                          # keep WebSocket traffic presentation-friendly
    _esp32_last_emit = now
    process_live_packet({
        "state": state,
        "timestamp": now,
        "rssi": parsed["rssi"],
        "confidence": round(confidence, 2),
        "csi": display,
    })


def parse_serial_line(line):
    """Map one CSV/JSON line from the ESP32 onto the internal packet format."""
    line = line.strip()
    if not line:
        return None
    try:  # tolerate firmware that already speaks JSON
        data = json.loads(line)
        if isinstance(data, dict) and "state" in data:
            return data
    except json.JSONDecodeError:
        pass

    parts = [p for p in line.replace(";", ",").split(",") if p.strip() != ""]
    try:
        values = [float(p) for p in parts]
    except ValueError:
        return None
    if not values:
        return None

    def col(idx, default=None):
        try:
            return values[int(idx)]
        except (IndexError, TypeError, ValueError):
            return default

    state = int(col(CSV_STATE_IDX, 0))
    rssi = col(CSV_RSSI_IDX) if CSV_RSSI_IDX != "" else round(random.uniform(-65, -55), 1)
    conf = col(CSV_CONF_IDX) if CSV_CONF_IDX != "" else round(random.uniform(0.90, 0.98), 2)

    csi_start = max(int(CSV_STATE_IDX) + 1,
                    int(CSV_RSSI_IDX) + 1 if CSV_RSSI_IDX != "" else 0,
                    int(CSV_CONF_IDX) + 1 if CSV_CONF_IDX != "" else 0)
    amps = values[csi_start:]
    if len(amps) >= 64:
        csi = [round(a, 2) for a in amps[:64]]
    elif amps:
        spread = [round(a, 2) for a in amps] * (64 // len(amps)) + \
                 [round(a, 2) for a in amps][:64 % len(amps)]
        csi = spread
    else:  # no amplitudes in stream — synthesise from state so visuals stay coherent
        csi = synth_csi(state, time.time() % 100)

    return {"state": state, "timestamp": time.time(),
            "rssi": rssi, "confidence": conf, "csi": csi}


def serial_loop():
    global source_status, active_serial_port

    def pick_port():
        if SERIAL_PORT:
            return SERIAL_PORT
        ports = [p.device for p in serial.tools.list_ports.comports()]
        return ports[0] if ports else None

    ser = None
    try:
        while True:
            if ser is None:
                port = pick_port()
                if port is None:
                    with lock:
                        source_status["feed"] = "simulated"
                    log_activity("system", "No serial device found -> running on simulated feed")
                    sim_active.set()
                    threading.Thread(target=sim_loop, daemon=True).start()
                    return
                try:
                    ser = serial.Serial(port, BAUD_RATE, timeout=1)
                    active_serial_port = ser
                    serial_loop._last_data = time.time()
                    with lock:
                        source_status["feed"] = "live"
                    print(f"[source] reading ESP32 on {port} @ {BAUD_RATE} baud")
                except serial.SerialException as exc:
                    print(f"[source] could not open {port}: {exc}")
                    active_serial_port = None
                    time.sleep(3)
                    continue

            raw = ser.readline()
            if raw:
                text = raw.decode(errors="ignore").strip()
                if not text:
                    continue
                if "CSI_DATA" in text:
                    serial_loop._last_data = time.time()
                    sim_active.clear()
                    with lock:
                        source_status["feed"] = "live"
                    esp_packet(text)
                else:
                    data = parse_serial_line(text)
                    if data:
                        serial_loop._last_data = time.time()
                        sim_active.clear()
                        with lock:
                            source_status["feed"] = "live"
                        process_live_packet(data)
                    else:
                        pass
                continue

            if not sim_active.is_set() and time.time() - getattr(serial_loop, "_last_data", time.time()) >= SERIAL_FALLBACK_SECS:
                with lock:
                    source_status["feed"] = "simulated"
                log_activity("system", "Serial link idle -> simulated feed engaged")
                sim_active.set()
                threading.Thread(target=sim_loop, daemon=True).start()
    finally:
        pass

def mock_loop():
    """Original behaviour: pipe JSON lines out of mock_parser.py."""
    process = subprocess.Popen(
        [sys.executable, PARSER_SCRIPT],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for line in process.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        process_live_packet(data)


def ingestion_loop():
    """Pick the data source once at boot based on MODE."""
    global source_status

    if MODE == "mock":
        with lock:
            source_status["feed"] = "live"
        mock_loop()
        return

    if MODE == "auto" and not serial.tools.list_ports.comports():
        with lock:
            source_status["feed"] = "live"
        print("[source] no serial ports — using mock_parser.py")
        mock_loop()
        return

    serial_loop()


def clamp(value, lo, hi):
    return max(lo, min(hi, value))


def update_device(name, payload):
    """Apply a manual control change; returns (device_or_None, error_or_None)."""
    with lock:
        device = devices.get(name)
        if device is None:
            return None, "unknown device"

        changed = False
        if name == "hvac":
            if "on" in payload:
                new_val = bool(payload["on"])
                if device["on"] != new_val:
                    device["on"] = new_val
                    changed = True
            if "temp" in payload:
                new_val = int(clamp(int(payload["temp"]), TEMP_MIN, TEMP_MAX))
                if device["temp"] != new_val:
                    device["temp"] = new_val
                    changed = True
        else:  # lighting
            if "on" in payload:
                new_val = bool(payload["on"])
                if device["on"] != new_val:
                    device["on"] = new_val
                    changed = True
            if "brightness" in payload:
                new_val = int(clamp(
                    int(payload["brightness"]), BRIGHTNESS_MIN, BRIGHTNESS_MAX))
                if device["brightness"] != new_val:
                    device["brightness"] = new_val
                    changed = True

        if changed:
            device["mode"] = "manual"
        return dict(device), None


@app.route("/")
def index():
    return render_template("index.html")


def snapshot():
    people_now = current_people()      # resolves its own lock; do not nest
    with lock:
        return {
            "sensor": dict(sensor),
            "away_mode": dict(away_mode),
            "devices": {name: dict(d) for name, d in devices.items()},
            "energy_saved": round(energy_saved, 4),
            "activity": [dict(e) for e in activity_log],
            "history": list(history),
            "override": dict(override),
            "source": dict(source_status),
            "people": people_now,
        }


@app.route("/api/state")
def api_state():
    return jsonify(snapshot())


@app.route("/api/away", methods=["POST"])
def api_away():
    enabled = bool(request.get_json(silent=True).get("enabled"))
    with lock:
        away_mode["enabled"] = enabled
        current_state = sensor["state"]
        current_label = sensor["label"]
        
    log_activity("system", "Away mode armed" if enabled else "Away mode disarmed")
    socketio.emit("away_update", {"enabled": enabled})
    
    if enabled and current_state == 1:
        send_oled_state("Intruder")
    else:
        send_oled_state(current_label)
        
    return jsonify({"enabled": enabled})


@app.route("/api/devices/<name>", methods=["POST"])
def api_device(name):
    body = request.get_json(silent=True) or {}
    device, error = update_device(name, body)
    if error:
        return jsonify({"error": error}), 404

    verb = "powered" if "on" in body else "adjusted"
    detail = ""
    if name == "hvac":
        detail = f"set to {device['temp']}°C"
    elif name == "lighting":
        detail = f"brightness {device['brightness']}%"
    log_activity("device", f"{name.capitalize()} {verb} manually — {detail}")

    socketio.emit("device_update", {"name": name, "device": device})
    return jsonify({"name": name, "device": device})


@app.route("/api/devices/<name>/auto", methods=["POST"])
def api_device_auto(name):
    with lock:
        device = devices.get(name)
        if device is None:
            return jsonify({"error": "unknown device"}), 404
        device["mode"] = "auto"
        for key, value in automation_targets(sensor["state"]).get(name, {}).items():
            device[key] = value
        snapshot = dict(device)

    log_activity("device", f"{name.capitalize()} returned to auto mode")
    socketio.emit("device_update", {"name": name, "device": snapshot})
    return jsonify({"name": name, "device": snapshot})


@app.route("/api/people", methods=["POST"])
def api_people():
    """Pin the displayed headcount (int) or release it back to auto (null)."""
    body = request.get_json(silent=True) or {}
    raw = body.get("count")
    if raw is None:
        with lock:
            people["count"] = None
    else:
        try:
            count = max(0, min(20, int(raw)))
        except (TypeError, ValueError):
            return jsonify({"error": "count must be an integer or null"}), 400
        with lock:
            people["count"] = count

    count = current_people()
    _people_auto["last_emitted"] = count
    socketio.emit("people_update", {"count": count})
    return jsonify({"count": count})


@app.route("/api/alert/test", methods=["POST"])
def api_alert_test():
    """Verify Telegram delivery end-to-end."""
    if not alerts.configured():
        return jsonify({"error": "telegram not configured — fill telegram_config.json"}), 400
    alerts.send_alert("test", "WifiSense test alert — delivery works!", force=True)
    return jsonify({"queued": True})


@app.route("/control")
def control():
    return render_template("control.html")


@app.route("/api/override", methods=["POST"])
def api_override():
    """Director console: force a state (0/1/2) or None to follow the live feed."""
    global stable_state, candidate_state, candidate_count

    body = request.get_json(silent=True) or {}
    state = body.get("state")

    if state is not None:
        try:
            state = int(state)
        except (TypeError, ValueError):
            return jsonify({"error": "state must be 0, 1, 2 or null"}), 400
        if state not in STATE_LABELS:
            return jsonify({"error": "state must be 0, 1, 2 or null"}), 400
        if body.get("arm_away"):
            away_mode["enabled"] = True

    with lock:
        override["state"] = state
        candidate_state, candidate_count = None, 0
        feed = source_status["feed"]
    if state is None:
        stable_state = sensor["state"]   # resume conditioning from wherever live is

    if state is not None:
        process_directed_packet(state)   # instant scene change on all clients
    elif feed == "simulated":
        start_simulator()

    socketio.emit("override_update", {"state": state})
    return jsonify({"state": state})


@socketio.on("connect")
def handle_connect():
    socketio.emit("initial_state", snapshot(), to=request.sid)


if __name__ == "__main__":
    parser_thread = threading.Thread(target=ingestion_loop, daemon=True)
    parser_thread.start()
    threading.Thread(target=director_loop, daemon=True).start()

    try:
        lan_ip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        lan_ip.connect(("8.8.8.8", 80))
        ip = lan_ip.getsockname()[0]
        lan_ip.close()
    except OSError:
        ip = "127.0.0.1"

    print("=" * 56)
    print("  WifiSense dashboard :  http://localhost:5000")
    print(f"  Director (phone)    :  http://{ip}:5000/control")
    print(f"  Source mode         :  {MODE}")
    print("=" * 56)

    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
