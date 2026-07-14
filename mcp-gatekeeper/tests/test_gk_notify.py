"""Тесты Telegram-нотификатора gatekeeper (audit/gk_notify.py).

Покрываем: конфиг, дедуп, отправку (инъекция фейка urlopen), обработку ошибок.
Фейки передаются через параметры функций — без патча модульных глобалов.
"""
import os
import json
import importlib.util

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "gk_notify_ut", os.path.join(REPO, "audit", "gk_notify.py")
)
gk_notify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gk_notify)


def test_compute_key_stable():
    assert gk_notify.compute_key("abc") == gk_notify.compute_key("abc")
    assert gk_notify.compute_key("abc") != gk_notify.compute_key("abd")


def test_should_send_new():
    assert gk_notify.should_send("k", {}, 3600, 1000) is True


def test_should_send_recent():
    assert gk_notify.should_send("k", {"k": 1000}, 3600, 2000) is False


def test_should_send_expired():
    assert gk_notify.should_send("k", {"k": 1000}, 3600, 1000 + 3601) is True


def test_load_config_env(monkeypatch):
    monkeypatch.setenv("GK_TG_BOT_TOKEN", "TOK")
    monkeypatch.setenv("GK_TG_CHAT_ID", "999")
    t, c = gk_notify.load_config(openclaw_path="/nonexistent")
    assert t == "TOK"
    assert c == "999"


def test_load_config_fallback(tmp_path, monkeypatch):
    cfg = {"channels": {"telegram": {"accounts": {"raven": {"botToken": "FALLBACK"}}}}}
    p = tmp_path / "oc.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.delenv("GK_TG_BOT_TOKEN", raising=False)
    t, c = gk_notify.load_config(openclaw_path=str(p))
    assert t == "FALLBACK"
    assert c == gk_notify.DEFAULT_CHAT


def test_load_config_no_token(tmp_path, monkeypatch):
    p = tmp_path / "oc.json"
    p.write_text("{}")
    monkeypatch.delenv("GK_TG_BOT_TOKEN", raising=False)
    t, c = gk_notify.load_config(openclaw_path=str(p))
    assert t is None


class FakeResp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_send_text_calls_api():
    captured = {}

    def fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        captured["data"] = req.data
        return FakeResp(200)

    ok = gk_notify.send_text("TOK", "123", "hi", _urlopen=fake_urlopen)
    assert ok is True
    assert "botTOK/sendMessage" in captured["url"]
    assert json.loads(captured["data"].decode())["text"] == "hi"


def test_send_text_error():
    def fake_urlopen(req, timeout=10):
        raise gk_notify.urllib.error.URLError("boom")

    assert gk_notify.send_text("TOK", "123", "hi", _urlopen=fake_urlopen) is False


def test_notify_no_token(capsys):
    sent = gk_notify.notify(
        "alert", token=None, chat="1", state_path="/tmp/nope.json",
        now=1000, _config=lambda: (None, "1"),
    )
    assert sent == 0
    assert "bot token not configured" in capsys.readouterr().err.lower()


def test_notify_dedup(tmp_path):
    calls = {"n": 0}

    def fake_send(token, chat, text):
        calls["n"] += 1
        return True

    state = tmp_path / "st.json"
    gk_notify.notify("same alert", token="T", chat="1",
                     state_path=str(state), now=1000, _send=fake_send)
    gk_notify.notify("same alert", token="T", chat="1",
                     state_path=str(state), now=2000, _send=fake_send)
    assert calls["n"] == 1  # второй подавлен дедупом (ttl 3600)


def test_notify_sends_after_ttl(tmp_path):
    calls = {"n": 0}

    def fake_send(token, chat, text):
        calls["n"] += 1
        return True

    state = tmp_path / "st.json"
    gk_notify.notify("same alert", token="T", chat="1",
                     state_path=str(state), now=1000, _send=fake_send)
    gk_notify.notify("same alert", token="T", chat="1",
                     state_path=str(state), now=1000 + 3601, _send=fake_send)
    assert calls["n"] == 2


def test_notify_send_failure_no_state(tmp_path):
    def fake_send(token, chat, text):
        return False

    state = tmp_path / "st.json"
    sent = gk_notify.notify("x", token="T", chat="1",
                            state_path=str(state), now=1000, _send=fake_send)
    assert sent == 0
    assert not state.exists()  # при неудаче состояние не обновляется


def test_main_no_args():
    assert gk_notify.main(["gk_notify.py"]) == 0


def test_main_sends(monkeypatch):
    captured = {}

    def fake_notify(text, **kw):
        captured["text"] = text
        return 1

    monkeypatch.setattr(gk_notify, "notify", fake_notify)
    assert gk_notify.main(["gk_notify.py", "hello", "world"]) == 0
    assert captured["text"] == "hello world"
