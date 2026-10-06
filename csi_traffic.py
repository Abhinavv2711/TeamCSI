"""Keeps a steady stream of Wi-Fi frames flowing through the CSI access point.

Connect this computer's Wi-Fi to the ESP32 access point (SSID: CSI_network,
password: password), then run this script. It blasts small UDP packets at the
AP so every frame gives the sensing pipeline fresh channel measurements.

Usage:
    python csi_traffic.py            # 15 packets/sec to 192.168.4.1
    python csi_traffic.py 30 200     # 30 packets/sec, 200-byte payload
"""

import socket
import sys
import time

TARGET_IP = "192.168.4.1"     # default softAP gateway of the ESP32
TARGET_PORT = 8477


def main():
    rate = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
    size = int(sys.argv[2]) if len(sys.argv) > 2 else 120

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    payload = b"x" * size
    interval = 1.0 / rate
    print(f"sending {size}B x {rate:.0f}/s -> {TARGET_IP}:{TARGET_PORT}  (Ctrl+C to stop)")
    sent = 0
    try:
        while True:
            sock.sendto(payload, (TARGET_IP, TARGET_PORT))
            sent += 1
            if sent % (int(rate) * 10) == 0:
                print(f"\r{sent} packets", end="", flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\nstopped after {sent} packets")


if __name__ == "__main__":
    main()
