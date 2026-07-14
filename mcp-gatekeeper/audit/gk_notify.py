#!/usr/bin/env python3
"""gk_notify.py — Telegram-нотификатор для алертов gatekeeper-audit.

Вызывается из gk-audit.sh через переменную окружения GK_NOTIFY:
    GK_NOTIFY=/path/gk_notify.py
gk-audit.sh вызывает: "$GK_NOTIFY" "GK-AUDIT ALERT port=... detail"

Поведение:
- токен берётся из $GK_TG_BOT_TOKEN, иначе из openclaw.json (аккаунт
  $GK_TG_ACCOUNT, по умолчанию raven);
- chat_id из $GK_TG_CHAT_ID, по умолчанию 173681771 (ЗавЛаб);
- дедуп: одинаковый текст не шлётся чаще раза в TTL секунд
  (по умолчанию 3600, переопределяется $GK_NOTIFY_TTL);
- при ошибках/отсутствии токена — пишет в stderr и возвращает 0
  (чтобы gk-audit.sh никогда не падал из-за нотификатора);
- сам всегда exit 0.
"""
import os
import sys
import json
import time
import hashlib
import urllib.request
import urllib.error

# module-level alias — для тестируемости (патчится в тестах)
urlopen = urllib.request.urlopen

DEFAULT_CHAT = "173681771"
OPENCLAW_JSON = "/root/.openclaw/openclaw.json"
STATE_PATH = "/var/lib/gatekeeper/notify-state.json"
TTL = int(os.environ.get("GK_NOTIFY_TTL", "3600"))


def load_config(openclaw_path=None):
    """Вернуть (bot_token, chat_id). Токен: env, иначе openclaw.json."""
    if openclaw_path is None:
        openclaw_path = OPENCLAW_JSON
    token = os.environ.get("GK_TG_BOT_TOKEN")
    chat = os.environ.get("GK_TG_CHAT_ID", DEFAULT_CHAT)
    if not token and os.path.exists(openclaw_path):
        try:
            with open(openclaw_path) as f:
                cfg = json.load(f)
            account = os.environ.get("GK_TG_ACCOUNT", "raven")
            token = cfg["channels"]["telegram"]["accounts"][account]["botToken"]
        except Exception:
            token = token  # остаётся None
    return token, chat


def compute_key(text):
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(path, state):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def should_send(key, state, ttl, now):
    last = state.get(key)
    if last is None:
        return True
    return (now - last) > ttl


def send_text(token, chat, text, timeout=10, _urlopen=urlopen):
    """Отправить text в Telegram. Возвращает True при HTTP 200, False при ошибке."""
    url = "https://api.telegram.org/bot" + token + "/sendMessage"
    data = json.dumps({"chat_id": chat, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with _urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def notify(text, token=None, chat=None, state_path=STATE_PATH, ttl=TTL, now=None,
           _send=send_text, _config=load_config):
    """Отправить алерт с дедупом. Возвращает 1 если отправлено, иначе 0."""
    now = now if now is not None else int(time.time())
    cfg_token, cfg_chat = _config()
    token = token or cfg_token
    chat = chat or cfg_chat
    if not token:
        sys.stderr.write("gk_notify: bot token not configured; skipping alert\n")
        return 0
    key = compute_key(text)
    state = load_state(state_path)
    if not should_send(key, state, ttl, now):
        return 0
    ok = _send(token, chat, text)
    if ok:
        state[key] = now
        save_state(state_path, state)
        return 1
    return 0


def main(argv):
    text = " ".join(argv[1:]) if len(argv) > 1 else ""
    if not text:
        return 0
    try:
        notify(text)
    except Exception as e:
        sys.stderr.write("gk_notify: unexpected error: %s\n" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
