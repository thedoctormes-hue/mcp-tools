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
        del M.doctor_warnings


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
    print("ALL TESTS PASSED")
