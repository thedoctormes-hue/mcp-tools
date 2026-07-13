"""Тесты монитора лаборатории (Доминика).
Покрывают чистые функции: self_factcheck (гард честности), THRESHOLDS, clean_line.
Запуск: python3 tests/test_lab_monitor.py
"""
import importlib.util
import os
import tempfile

SPEC = importlib.util.spec_from_file_location(
    "lab_monitor", "/root/LabDoctorM/projects/mcp-tools/bin/lab-monitor.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def test_self_factcheck_catches_lies():
    fake = [
        (1, "Агенты", True, "живы (отчёт дошёл)", []),
        (2, "OpenClaw", True, "gateway работает, АВТО-перезапусков за 1h: 9 (systemd сам поднимал)\n⚠️ самопроверка: 1 старое безопасное замечание, новых нет", []),
        (3, "MCP", True, "2/3 работают", ["mcp-memory (порт 8087): DOWN"]),
        (5, "Данные", True, "PostgreSQL up; disk 96% (норма <85% — КРИТ)", []),
        (8, "Сервер", True, "load 0.93 (1мин 9.50 — ВЫСОКАЯ, норма <4)", []),
    ]
    probs = M.self_factcheck(fake)
    assert len(probs) == 5, probs  # 5 проблем из 5 записей (Агенты вне гарда: 0; OpenClaw 1; MCP 2; Данные 1; Сервер 1)


def test_self_factcheck_clean():
    honest = [
        (1, "Агенты", True, "живы (отчёт дошёл)", ["x: на месте"]),
        (2, "OpenClaw", True, "gateway работает, перезапусков за 1h: 0\n⚠️ самопроверка: 1 старое безопасное замечание, новых нет", []),
        (3, "MCP", True, "3/3 работают", ["mcp-memory (порт 8087): работает"]),
        (5, "Данные", True, "PostgreSQL up; disk 81% (норма <85% — ок)", []),
        (8, "Сервер", True, "load 1.0 (1мин 1.0 — ок, норма <4)", []),
    ]
    assert M.self_factcheck(honest) == []


def test_thresholds():
    t = M.THRESHOLDS
    assert t["disk_warn_pct"] == 85
    assert t["disk_crit_pct"] == 95
    assert t["nrestarts_ok"] == 5
    assert t["load_warn_x"] == 1.0
    assert t["load_high_x"] == 2.0


def test_clean_line():
    assert M.clean_line("│ ─ WARNING: foo") == "foo"
    assert M.clean_line("  normal text  ") == "normal text"


def test_get_random_quote_from_tmpfile():
    d = tempfile.mkdtemp()
    f = os.path.join(d, "q.md")
    with open(f, "w") as fh:
        fh.write("- Цитата один\n- Цитата два\n# заголовок\nобычный текст\n")
    orig = M.QUOTE_FILE
    M.QUOTE_FILE = f
    try:
        q = M.get_random_quote()
        assert q in ("Цитата один", "Цитата два"), q
    finally:
        M.QUOTE_FILE = orig


def test_get_random_quote_missing_file():
    orig = M.QUOTE_FILE
    M.QUOTE_FILE = "/root/LabDoctorM/projects/mcp-tools/tests/__nonexistent__.md"
    try:
        assert M.get_random_quote() is None
    finally:
        M.QUOTE_FILE = orig


def test_get_random_quote_empty_file():
    d = tempfile.mkdtemp()
    f = os.path.join(d, "empty.md")
    with open(f, "w") as fh:
        fh.write("# только заголовок\nне bullet строка\n")
    orig = M.QUOTE_FILE
    M.QUOTE_FILE = f
    try:
        assert M.get_random_quote() is None
    finally:
        M.QUOTE_FILE = orig


def test_thresholds_extended():
    t = M.THRESHOLDS
    assert t["restart_window"] == "1h"
    assert t["nrestarts_window_auto_ok"] == 0
    # nrestarts_ok оставлен для прочих сервисов (накопленный lifetime-порог)
    assert t["nrestarts_ok"] == 5


def test_classify_restarts_ok():
    cls = M.classify_restarts("", "6", "1h")
    assert cls["classification"] == "ok"
    assert cls["total"] == 0 and cls["auto"] == 0 and cls["manual"] == 0
    assert cls["lifetime"] == "6"


def test_classify_restarts_manual():
    text = "Starting OpenClaw Gateway.\nStarted OpenClaw Gateway.\nStopping OpenClaw Gateway.\nStarting OpenClaw Gateway."
    cls = M.classify_restarts(text, "6", "1h")
    assert cls["classification"] == "manual"
    assert cls["total"] == 3
    assert cls["auto"] == 0
    assert cls["manual"] == 3


def test_classify_restarts_auto():
    text = ("Main process exited, code=killed.\nScheduled restart.\n"
            "Stopped OpenClaw Gateway.\nStarting OpenClaw Gateway.\n"
            "Scheduled restart.\nStarting OpenClaw Gateway.")
    cls = M.classify_restarts(text, "6", "1h")
    assert cls["classification"] == "auto"
    assert cls["auto"] == 2
    assert cls["total"] == 2
    assert cls["manual"] == 0


def test_cat_projects_summary_format():
    ok, summary, out = M.cat_projects()
    assert ok is True
    assert "незакоммичено" in summary, summary
    assert "не сбой" in summary, summary
    assert "решено" in summary, summary
    assert any("инциденты:" in line for line in out), out


def _mock_run(cmd, **kw):
    class R:
        pass
    r = R()
    if "loadavg" in cmd:
        r.stdout = "1.50 1.20 1.00"
    elif "free -m" in cmd:
        r.stdout = "4000 7937 1000 2000 4937"
    elif "docker ps" in cmd and "api-hub-db-1" in cmd:
        r.stdout = "Up 4 days"
    elif "docker ps" in cmd and "amnezia" in cmd:
        r.stdout = "Up 4 days"
    elif "docker ps" in cmd and "searxng" in cmd:
        r.stdout = "Up 2 days (healthy)"
    elif "docker ps" in cmd:
        r.stdout = "searxng\napi-hub-db-1\namnezia-awg2"
    elif "nproc" in cmd:
        r.stdout = "4"
    elif "systemctl show" in cmd and "NRestarts" in cmd:
        r.stdout = "NRestarts=6"
    elif "journalctl" in cmd:
        r.stdout = ""
    elif "lab_search.py health" in cmd:
        r.stdout = '{"faiss_loaded": true, "onnx_available": true, "vectors": 37596}'
    elif "systemctl is-active reindex" in cmd:
        r.stdout = "active"
    elif "systemctl is-active" in cmd:
        r.stdout = "active"
    elif "openclaw doctor" in cmd:
        r.stdout = ""
    elif "openssl" in cmd:
        r.stdout = "notAfter=Sep 26 12:42:01 2026 GMT"
    elif "git status" in cmd:
        r.stdout = "5"
    else:
        r.stdout = ""
    return r


def test_all_categories_mocked():
    orig = M.run
    M.run = _mock_run
    try:
        for fn in [M.cat_agents, M.cat_openclaw, M.cat_mcp, M.cat_memory,
                   M.cat_data, M.cat_network, M.cat_projects, M.cat_host]:
            ok, summary, out = fn()
            assert isinstance(summary, str) and len(summary) > 0, (fn, summary)
            assert isinstance(out, list)
        short = M.build_report(full=False)
        assert "ЛабМонитор" in short
        full = M.build_report(full=True)
        assert "полный дамп" in full
    finally:
        M.run = orig


def test_full_no_summary_dup_for_ok():
    """В --full OK-категория НЕ повторяет summary в заголовке (числа только в деталях).
    Доктор-варнинги и самопроверка живут ТОЛЬКО в выделенной секции 🩺 внизу дампа
    (не дублируются в [2] OpenClaw); диск % — только в 💾 (не в [5] Данные)."""
    orig = M.run
    orig_dw = M.doctor_warnings
    M.run = _mock_run
    # детерминированный доктор-варнинг, чтобы проверить отсутствие дубля текста
    M.doctor_warnings = lambda: {"count": 1, "new": [],
                                 "all": ["openclaw.json contains plaintext secret-bearing config"]}
    try:
        full = M.build_report(full=True)
        # Сервер: заголовок "✅ 8. Сервер" без ' — нагрузка CPU...'
        assert "✅ 8. Сервер" in full
        assert "8. Сервер — нагрузка" not in full, "summary дублируется в full-заголовке"
        # доктор-варнинг: текст ровно 1 раз (в секции 🩺), не дублируется в [2]
        assert full.count("openclaw.json contains plaintext") == 1, "доктор-варнинг дублируется"
        # самопроверка (⚠️-строка) в full НЕ наверху [2] — только в 🩺 внизу
        assert "⚠️ самопроверка" not in full, "⚠️ самопроверка дублируется в [2]"
        # диск % не дублируется: в [5] деталях нет 'disk /:', только в 💾 внизу
        assert "disk /:" not in full, "диск % дублируется в [5]"
    finally:
        M.run = orig
        M.doctor_warnings = orig_dw


class FakeRes:
    def __init__(self, stdout=""):
        self.stdout = stdout


def test_doctor_warnings_parse_and_cache():
    import tempfile
    d = tempfile.mkdtemp()
    orig_state = M.STATE_DIR
    M.STATE_DIR = d
    orig_run = M.run

    def _r(cmd, **k):
        if "openclaw doctor" in cmd:
            return FakeRes("⚠ WARNING: something new here\n◇ skip me\n⚠ message tool unavailable")
        return FakeRes("")
    M.run = _r
    try:
        dw = M.doctor_warnings()
        assert any("something new here" in w for w in dw["all"])
        # message tool unavailable — в allowlist -> не NEW;
        # "something new here" -> NEW (после clean_line с префиксом ⚠)
        assert dw["new"] == ["⚠ something new here"], dw
        assert dw["count"] == 2
        assert os.path.isfile(os.path.join(d, "doctor.json"))
    finally:
        M.run = orig_run
        M.STATE_DIR = orig_state


def test_doctor_warnings_cache_hit():
    import datetime as _dt
    import json as _json
    import tempfile
    d = tempfile.mkdtemp()
    orig_state = M.STATE_DIR
    M.STATE_DIR = d
    try:
        cache = os.path.join(d, "doctor.json")
        with open(cache, "w") as fh:
            fh.write(_json.dumps({"ts": _dt.datetime.now().isoformat(),
                                  "all": ["brand new warning xyz"], "count": 1, "new": []}))
        dw = M.doctor_warnings()  # кэш свежий -> run не нужен, new пересчитывается
        assert dw["new"] == ["brand new warning xyz"]
    finally:
        M.STATE_DIR = orig_state


def test_clean_line_extra():
    assert M.clean_line("│─ WARNING:  foo   bar  │") == "foo bar"


def test_cat_openclaw_branches():
    def make(active, journal, nrest):
        def _r(cmd, **k):
            if "is-active openclaw-gateway" in cmd:
                return FakeRes(active)
            if "NRestarts" in cmd:
                return FakeRes(f"NRestarts={nrest}")
            if "journalctl" in cmd:
                return FakeRes(journal)
            return FakeRes("")
        return _r
        return _r
    orig_run, orig_dw = M.run, M.doctor_warnings
    try:
        M.doctor_warnings = lambda: {"count": 1, "new": [],
                                     "all": ["openclaw.json contains plaintext secret-bearing config"]}
        M.run = make("active", "", "6")
        ok, s, o = M.cat_openclaw()
        assert ok and "перезапусков за 1h: 0" in s
        M.run = make("inactive", "", "6")
        ok, s, o = M.cat_openclaw()
        assert ok is False and "DOWN" in s
        M.run = make("active", "Scheduled restart\nStarting OpenClaw Gateway", "6")
        ok, s, o = M.cat_openclaw()
        assert ok is False and "АВТО-перезапуск" in s
        M.run = make("active", "Starting OpenClaw Gateway", "6")
        ok, s, o = M.cat_openclaw()
        assert "ручн" in s
        M.doctor_warnings = lambda: {"count": 1, "new": ["NEW DOC WARN"], "all": ["NEW DOC WARN"]}
        M.run = make("active", "", "6")
        ok, s, o = M.cat_openclaw()
        assert ok is False and "НОВЫХ замечаний" in s
    finally:
        M.run, M.doctor_warnings = orig_run, orig_dw


def test_cat_mcp():
    def make(services):
        def _r(cmd, **k):
            if "systemctl list-units" in cmd:
                return FakeRes(services)
            return FakeRes("")
        return _r
        return _r
    orig_run, orig_port = M.run, M.port_ok
    try:
        M.run = make("mcp-memory.service\nmcp-apikeys.service\nmcp-gatekeeper.service\n")
        M.port_ok = lambda p: True
        ok, s, o = M.cat_mcp()
        assert ok and s == "3/3 работают"
        M.port_ok = lambda p: p == 8087  # только memory отвечает
        ok, s, o = M.cat_mcp()
        assert ok is False and any("DOWN" in line for line in o)
        M.run = make("mcp-foo.service\n")
        M.port_ok = lambda p: True
        ok, s, o = M.cat_mcp()
        assert "работает (systemd)" in o[0]
        M.run = make("")
        ok, s, o = M.cat_mcp()
        assert ok is False and "ни одного MCP" in s
    finally:
        M.run, M.port_ok = orig_run, orig_port


def test_cat_data_disk():
    for disk, ok_expected, hint in [("78%", True, "ок"), ("86%", False, "тревога"),
                                     ("96%", False, "КРИТ"), ("?", True, "ок")]:
        def _r(cmd, **k):
            if "docker ps" in cmd and "api-hub-db-1" in cmd:
                return FakeRes("Up 4 days")
            if "df -h /" in cmd:
                return FakeRes(disk)
            return FakeRes("")
        orig = M.run
        M.run = _r
        try:
            ok, s, o = M.cat_data()
            assert ok == ok_expected, (disk, ok, s)
            assert hint in s, s
        finally:
            M.run = orig


def test_cat_memory_invalid_json():
    def _r(cmd, **k):
        if "lab_search.py health" in cmd:
            return FakeRes("not json at all")
        return FakeRes("")
    orig = M.run
    M.run = _r
    try:
        ok, s, o = M.cat_memory()
        assert ok is False
    finally:
        M.run = orig


def test_cat_network_ssl_fail():
    def _r(cmd, **k):
        if "docker ps" in cmd and "amnezia" in cmd:
            return FakeRes("Up 4 days")
        if "openssl" in cmd:
            return FakeRes("")
        return FakeRes("")
    orig_run, orig_port = M.run, M.port_ok
    M.run = _r
    M.port_ok = lambda p: True
    try:
        ok, s, o = M.cat_network()
        assert "SSL" in s and "FAIL" in s, s
    finally:
        M.run, M.port_ok = orig_run, orig_port


def test_independent_probe():
    orig_run, orig_port = M.run, M.port_ok
    M.run = _mock_run
    M.port_ok = lambda p: True
    try:
        probe = M.independent_probe()
        assert set(probe.keys()) == {1, 2, 3, 4, 5, 6, 7, 8}
    finally:
        M.run, M.port_ok = orig_run, orig_port


def test_selftest_report():
    orig_run, orig_port = M.run, M.port_ok
    M.run = _mock_run
    M.port_ok = lambda p: True
    try:
        rep = M.selftest_report()
        assert "САМОПРОВЕРКА" in rep
    finally:
        M.run, M.port_ok = orig_run, orig_port


def test_build_report_full_bottom_sections():
    def _r(cmd, **k):
        if "df -h -x" in cmd:
            return FakeRes("/ 78% (свободно 13G)\n/var 50% (свободно 5G)")
        if "docker ps --format" in cmd and "{{.Names}}|" in cmd:
            return FakeRes("searxng|Up 3 days\napi-hub-db-1|Up 4 days")
        if "openclaw doctor" in cmd:
            return FakeRes("⚠ WARNING: new doctor issue\n◇ skip")
        if "free -m" in cmd:
            return FakeRes("4000 7937 1000 2000 4937")
        if "loadavg" in cmd:
            return FakeRes("1.50 1.20 1.00")
        if "nproc" in cmd:
            return FakeRes("4")
        if "systemctl show" in cmd and "NRestarts" in cmd:
            return FakeRes("NRestarts=6")
        if "journalctl" in cmd:
            return FakeRes("")
        if "lab_search.py health" in cmd:
            return FakeRes('{"faiss_loaded": true, "onnx_available": true, "vectors": 37596}')
        if "systemctl is-active reindex" in cmd:
            return FakeRes("active")
        if "systemctl is-active" in cmd:
            return FakeRes("active")
        if "git status" in cmd:
            return FakeRes("5")
        if "docker ps" in cmd and "api-hub-db-1" in cmd:
            return FakeRes("Up 4 days")
        if "docker ps" in cmd and "amnezia" in cmd:
            return FakeRes("Up 4 days")
        if "docker ps" in cmd and "searxng" in cmd:
            return FakeRes("Up 2 days (healthy)")
        return FakeRes("")
    orig_run, orig_q, orig_dw = M.run, M.get_random_quote, M.doctor_warnings
    M.run = _r
    M.get_random_quote = lambda: "тестовая цитата"
    M.doctor_warnings = lambda: {"count": 1, "new": [], "all": ["new doctor issue"]}
    try:
        full = M.build_report(full=True)
        assert "💾 Диск (реальные ФС):" in full
        assert "/ 78%" in full
        assert "🐳 Docker:" in full
        assert "searxng: Up 3 days" in full
        assert "🩺 Самопроверка движка" in full
        assert "new doctor issue" in full
        assert "📜 Цитата часа: тестовая цитата" in full
    finally:
        M.run, M.get_random_quote, M.doctor_warnings = orig_run, orig_q, orig_dw


def test_build_report_with_fail():
    orig_cats = M.CATEGORIES
    failing = (1, "Агенты", lambda: (False, "agents DOWN — провал", ["detail line"]))
    M.CATEGORIES = [failing if c[0] == 1 else c for c in orig_cats]
    try:
        short = M.build_report(full=False)
        assert "🔴 провалы:" in short
        assert "🔧 СОВЕТ" in short
        full = M.build_report(full=True)
        assert "🔴 ТРЕВОГИ:" in full
    finally:
        M.CATEGORIES = orig_cats


def test_build_report_advice_gateway_down():
    orig_cats = M.CATEGORIES
    failing = (2, "OpenClaw", lambda: (False, "gateway DOWN — упал", ["detail"]))
    M.CATEGORIES = [failing if c[0] == 2 else c for c in orig_cats]
    try:
        rep = M.build_report(full=False)
        assert "systemctl restart openclaw-gateway" in rep
    finally:
        M.CATEGORIES = orig_cats


def test_main_entrypoint():
    import sys as _sys
    import io
    g = {
        "__name__": "__main__",
        "__file__": M.__file__,
        "run": lambda cmd, **k: FakeRes("active" if "is-active" in cmd else ""),
        "port_ok": lambda p, host="127.0.0.1", timeout=3: True,
        "get_random_quote": lambda: "q",
        "doctor_warnings": lambda: {"count": 0, "new": [], "all": []},
    }
    argv_save = _sys.argv
    out_buf = io.StringIO()
    old_out = _sys.stdout
    _sys.argv = ["lab-monitor.py", "--full"]
    try:
        _sys.stdout = out_buf
        exec(compile(open(M.__file__).read(), M.__file__, "exec"), g)
    finally:
        _sys.stdout = old_out
        _sys.argv = argv_save
    assert "ЛабМонитор" in out_buf.getvalue()


if __name__ == "__main__":
    test_self_factcheck_catches_lies()
    test_all_categories_mocked()
    test_full_no_summary_dup_for_ok()
    test_cat_projects_summary_format()
    test_self_factcheck_clean()
    test_thresholds()
    test_thresholds_extended()
    test_clean_line()
    test_classify_restarts_ok()
    test_classify_restarts_manual()
    test_classify_restarts_auto()
    test_get_random_quote_from_tmpfile()
    test_get_random_quote_missing_file()
    test_get_random_quote_empty_file()
    test_doctor_warnings_parse_and_cache()
    test_doctor_warnings_cache_hit()
    test_clean_line_extra()
    test_cat_openclaw_branches()
    test_cat_mcp()
    test_cat_data_disk()
    test_cat_memory_invalid_json()
    test_cat_network_ssl_fail()
    test_independent_probe()
    test_selftest_report()
    test_build_report_full_bottom_sections()
    test_build_report_with_fail()
    test_build_report_advice_gateway_down()
    test_main_entrypoint()
    print("ALL TESTS PASSED")
