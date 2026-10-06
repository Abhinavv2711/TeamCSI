import sys
import time
import threading
with open('app.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

start_idx = -1
end_idx = -1
for i, line in enumerate(lines):
    if line.startswith('def serial_loop():'):
        start_idx = i
    if start_idx != -1 and line.startswith('def mock_loop():'):
        end_idx = i
        break

new_code = '''def serial_loop():
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

'''

lines = lines[:start_idx] + [new_code] + lines[end_idx:]
with open('app.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)
