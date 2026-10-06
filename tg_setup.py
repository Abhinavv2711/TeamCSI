"""One-time Telegram setup helper.

1. In Telegram, message @BotFather -> /newbot -> copy the token into
   telegram_config.json ("token" field).
2. Open your new bot's chat and send it ANY message (e.g. "hi").
3. Run:  venv\\Scripts\\python.exe tg_setup.py
4. Copy the chat id it prints into telegram_config.json ("chat" field).
"""

import json
import urllib.request

with open("telegram_config.json", encoding="utf-8") as fh:
    token = json.load(fh)["token"]

url = f"https://api.telegram.org/bot{token}/getUpdates"
with urllib.request.urlopen(url, timeout=10) as resp:
    data = json.load(resp)

chats = {}
for update in data.get("result", []):
    msg = update.get("message") or update.get("channel_post") or update.get("edited_message")
    if msg and "chat" in msg:
        chat = msg["chat"]
        label = chat.get("first_name") or chat.get("title") or chat.get("username") or "?"
        chats[str(chat["id"])] = label

if chats:
    print("Chats that have messaged your bot:")
    for chat_id, name in chats.items():
        print(f'    {name}: chat id = {chat_id}')
    print("\nPaste the id into telegram_config.json as \"chat\".")
else:
    print("No messages yet - open your bot in Telegram and send it any message first.")
