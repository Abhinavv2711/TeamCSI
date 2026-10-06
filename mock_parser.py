"""Simulates the serial output of an ESP32 Wi-Fi sensing node.

Prints one JSON object per second to stdout, mimicking what would
normally arrive over a serial connection from the ESP32.

States are held for realistic dwell times, and every packet carries a
synthetic 64-subcarrier CSI amplitude profile whose character matches
the current state: flat noise (empty), erratic spikes (motion), or a
smooth breathing-band ripple (sleeping).
"""

import json
import math
import random
import time

STATE_LABELS = {
    0: "Empty",
    1: "Moving",
    2: "Sleeping",
}

SUBCARRIERS = 64

DWELL_TIMES = {
    0: (25, 60),
    1: (6, 14),
    2: (35, 90),
}

TRANSITIONS = {
    0: [1],
    1: [0, 2],
    2: [1],
}


def synth_csi(state, breath_phase):
    """64 subcarrier amplitudes shaped by the occupancy state."""
    if state == 2:
        return [round(math.sin(breath_phase + i * 0.14) * 1.15
                      + math.sin(breath_phase * 2.7 + i * 0.05) * 0.18
                      + random.gauss(0, 0.07), 2)
                for i in range(SUBCARRIERS)]
    if state == 1:
        amps = []
        for _ in range(SUBCARRIERS):
            v = random.gauss(0, 0.5)
            if random.random() < 0.09:
                v += random.choice([-1, 1]) * random.uniform(1.8, 4.4)
            amps.append(round(max(-6.0, min(6.0, v)), 2))
        return amps
    return [round(random.gauss(0, 0.10), 2) for _ in range(SUBCARRIERS)]


def main():
    seq = 0
    rssi = random.uniform(-60, -56)
    breath_phase = 0.0

    state = random.choice([0, 0, 1])
    dwell_until = time.time() + random.uniform(*DWELL_TIMES[state])

    while True:
        if state == 2:
            breath_phase += 0.55

        reading = {
            "seq": seq,
            "state": state,
            "label": STATE_LABELS[state],
            "timestamp": time.time(),
            "rssi": round(rssi, 1),
            "confidence": round(random.uniform(
                0.93, 0.99 if state != 1 else 0.88), 2),
            "packet_size": SUBCARRIERS,
            "csi": synth_csi(state, breath_phase),
        }
        print(json.dumps(reading), flush=True)

        seq += 1
        rssi = max(-72, min(-50, rssi + random.gauss(0, 0.4)))

        if time.time() >= dwell_until:
            state = random.choice(TRANSITIONS[state])
            dwell_until = time.time() + random.uniform(*DWELL_TIMES[state])
            breath_phase = 0.0

        time.sleep(1)


if __name__ == "__main__":
    main()
