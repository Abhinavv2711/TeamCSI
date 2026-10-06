"""Telegram alert delivery for WifiSense.

Configure once via telegram_config.json next to this file:
    {"token": "123456:ABC...", "chat": "123456789"}
or via TEAMCSI_TG_TOKEN / TEAMCSI_TG_CHAT environment variables.

send_alert() never blocks the caller: delivery happens on a daemon thread,
and repeated alerts of the same kind are rate-limited.
"""

import json
import os
import threading
import time
import urllib.parse
import urllib.request

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram_config.json")
COOLDOWN_SECS = 20.0

_last_sent = {}
_lock = threading.Lock()


def _clean(value):
    value = str(value or "").strip()
    return "" if value.upper().startswith("PASTE_") else value


def _load_config():
    cfg = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        pass
    return {
        "token": _clean(os.environ.get("TEAMCSI_TG_TOKEN") or cfg.get("token")),
        "chat": _clean(os.environ.get("TEAMCSI_TG_CHAT") or cfg.get("chat")),
    }


def configured():
    cfg = _load_config()
    return bool(cfg["token"] and cfg["chat"])


def _deliver(token, chat, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            return resp.status == 200
    except Exception as exc:
        print(f"[alert] delivery failed: {exc}")
        return False


def send_alert(kind, text, force=False):
    """Send text to the configured Telegram chat (async, rate-limited per kind)."""
    def worker():
        cfg = _load_config()
        if not cfg["token"] or not cfg["chat"]:
            return                      # not configured -> silent no-op
        now = time.time()
        with _lock:
            if not force and now - _last_sent.get(kind, 0.0) < COOLDOWN_SECS:
                return
            _last_sent[kind] = now
        stamp = time.strftime("%H:%M:%S")
        if _deliver(cfg["token"], cfg["chat"], f"{text}\n— WifiSense · {stamp}"):
            print(f"[alert] sent ({kind}): {text}")

    threading.Thread(target=worker, daemon=True).start()
