"""Тесты монитора лаборатории (Доминика).
Покрывают чистые функции: self_factcheck (гард честности), THRESHOLDS, clean_line.
Запуск: python3 tests/test_lab_monitor.py
"""
import importlib.util

SPEC = importlib.util.spec_from_file_location(
    "lab_monitor", "/root/LabDoctorM/projects/mcp-tools/bin/lab-monitor.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def test_self_factcheck_catches_lies():
    fake = [
        (1, "Агенты", True, "8/8 grimoire-файлов на месте",
         ["dominika: grimoire-файл ОТСУТСТВУЕТ (0 строк)"]),
        (2, "OpenClaw", True, "gateway работает, авто-восстановлений: 9 (норма <5)", []),
        (3, "MCP", True, "2/3 работают", ["mcp-memory (порт 8087): DOWN"]),
        (5, "Данные", True, "PG up; disk 96% (норма <85% — КРИТ)", []),
        (8, "Хост", True, "load 0.93 (1мин 9.50 — ВЫСОКАЯ, норма <4)", []),
    ]
    probs = M.self_factcheck(fake)
    assert len(probs) == 5, probs  # 5 лжей из 5 поймано (Агенты-ложь теперь вне гарда по дизайну)


def test_self_factcheck_clean():
    honest = [
        (1, "Агенты", True, "живы (отчёт дошёл)", ["x: на месте"]),
        (2, "OpenClaw", True, "gateway работает, авто-восстановлений: 2 (норма <5)", []),
        (3, "MCP", True, "3/3 работают", ["mcp-memory (порт 8087): работает"]),
        (5, "Данные", True, "PG up; disk 81% (норма <85% — ок)", []),
        (8, "Хост", True, "load 1.0 (1мин 1.0 — ок, норма <4)", []),
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


if __name__ == "__main__":
    test_self_factcheck_catches_lies()
    test_self_factcheck_clean()
    test_thresholds()
    test_clean_line()
    print("ALL TESTS PASSED")
