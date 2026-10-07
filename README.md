# TeamCSI
Team CSI- AI-Driven Wi-Fi CSI sensing  for Eco Home Smart Energy Management,  Intruder Alert ,Fall Detection and Vital  Sign Monitoring System
# 📡 AuraSense: Ambient Wi-Fi CSI Healthcare & Vitals Monitoring

> **The "Invisible" Medical Radar** — Contactless, non-invasive overnight respiration, cardiac, and fall monitoring using ambient Wi-Fi Channel State Information (CSI) and Deep Learning.

[![SDG Alignment](https://img.shields.io/badge/SDG-3_Good_Health_%26_Well--being-green.svg)](https://sdgs.un.org/goals/goal3)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Hardware](https://img.shields.io/badge/Hardware-ESP32--S3-red.svg)](https://www.espressif.com/)
[![Framework](https://img.shields.io/badge/AI-PyTorch_%7C_TensorFlow-orange.svg)](https://pytorch.org/)

---

## 📌 Project Overview

Traditional healthcare monitoring relies on intrusive video cameras or uncomfortable wearable sensors that require constant charging. **AuraSense** converts standard ambient $2.4\text{ GHz}$ Wi-Fi signals into a high-precision medical radar:

- **Physiology:** Human breathing and cardiac cycles create micro-distortions (phase shifts & amplitude perturbations) in ambient Wi-Fi waves bouncing off the body.
- **Signal Extraction:** Dual ESP32-S3 nodes extract raw Channel State Information (CSI) across 64 subcarriers.
- **Deep Learning:** A digital signal processing pipeline combined with a **Long Short-Term Memory (LSTM)** neural network filters room multipath noise to isolate respiration rates (RPM) and detect sudden kinetic falls.
- **Autonomous LLM Scribe:** An integrated Large Language Model ingests real-time time-series telemetry to generate timestamped clinical observation logs and emergency dispatch alerts.

---

## 🛠️ System Architecture

```text
+-------------------+      +-------------------+      +-----------------------+
|  Transmitter Node | ---> |  Receiver Node    | ---> | Digital Signal Filter |
|   (ESP32-S3 AP)   |      |  (ESP32-S3 STA)   |      |  (Butterworth / PCA)  |
+-------------------+      +-------------------+      +-----------------------+
                                                                  |
                                                                  v
+-------------------+      +-------------------+      +-----------------------+
|   Web Dashboard   | <--- | LLM Triage Scribe | <--- |  PyTorch LSTM Engine  |
|  & Alert System   |      |   (Gemini API)    |      | (Vitals / Fall Class) |
+-------------------+      +-------------------+      +-----------------------+
