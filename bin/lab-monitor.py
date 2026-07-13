#!/usr/bin/env python3
"""
lab-monitor.py — монитор лаборатории (Доминика)
Ежечасная сводка по 8 категориям + слой реагирования (advise) + полный дамп (--full).
Дизайн: projects/mcp-tools/docs/monitor-design.md

Категории (8):
  1. Агенты колонии        2. Платформа OpenClaw     3. MCP-сервисы
  4. Память и поиск        5. Данные и хранилища     6. Сеть и внешний доступ
  7. Проекты и код         8. Ресурсы хоста

Поведение:
  - без флагов: компактная сводка (8 строк OK/FAIL + гибрид-шапка + блок провалов + совет)
  - --full: развёрнутый дамп (per-agent, доктор-warn целиком, диск по разделам, докер, логи)
  - дрейф варнингов доктора: базовые (allowlist) игнорируются, 🔴 только на НОВЫХ
"""
import datetime
import json
import os
import random
import re
import socket
import subprocess
import sys

WORKSPACES = "/root/LabDoctorM/workspaces"
PROJECTS   = "/root/LabDoctorM/projects"
STATE_DIR  = "/root/LabDoctorM/workspaces/dominika/monitor-state"
os.makedirs(STATE_DIR, exist_ok=True)

AGENTS = ["kotolizator","mangust","raven","owl","bestia","streikbrecher","dominika","antcat"]
MSK = datetime.timezone(datetime.timedelta(hours=3))
NOW = datetime.datetime.now(MSK)

# сводный сборник цитат (ЗавЛаб: перенёс + смержил все grimoire.md -> nevermind.md)
QUOTE_FILE = "/root/LabDoctorM/workspaces/dominika/nevermind.md"

# === ПОРОГИ ЧЕСТНОСТИ (каждый — откуда норма) ===
# Монитор обязан сверять значения именно с этими порогами, а не с "магическими числами".
THRESHOLDS = {
    "disk_warn_pct": 85,    # норма <85%  (источник: ADR-039, практика ротации диска лаборатории)
    "disk_crit_pct": 95,    # КРИТ     >=95% (диск почти полон)
    "nrestarts_ok": 5,      # оставлено для ПРОЧИХ сервисов (накопленный lifetime-порог). Для gateway — см. оконную логику ниже.
    "restart_window": "1h", # ОКНО отчёта = час (cron heartbeat-dominika: 0 * * * * MSK). Источник: логика ЗавЛаба 2026-07-13 — отчёт приходит каждый час, перезапуски считаем за это окно, чтобы сверять с памятью «я сам рестартил?».
    "nrestarts_window_auto_ok": 0,  # авто-перезапусков (systemd сам поднял после падения) за окно: норма 0; >=1 → 🔴 подозрительно. Ручные рестарты ЗавЛаб знает/ожидает; авто = нежданное падение.
    "load_warn_x": 1.0,     # load1 < ядер            = ок
    "load_high_x": 2.0,     # load1 < 2×ядер          = повышенная (не сбой); >=2×ядер = тревога
}

# варнинги доктора, которые считаем "базовым шумом" (известны, приняты) -> в allowlist
DOCTOR_ALLOWLIST = [
    "message tool unavailable", "config-health.json", "legacy state migration",
    "plugin", "tavily", "memory-core", "memory-wiki", "low-power",
    "NODE_COMPILE_CACHE", "OPENCLAW_NO_RESPAWN",
    "plaintext sec", "openclaw.json contains plaintext",
]

# маршрутизация категория(провал) -> кто релевантен для расследования
ROUTE = {
    1: "соответствующий агент + Мангуст (аналитик)",
    2: "Котолизатор / Муравей",
    3: "Муравей",
    4: "Муравей / Ворон",
    5: "Муравей",
    6: "Бестия / Ворон",
    7: "Штрейкбрехер",
    8: "Муравей",
}
# маршрутизация категория(провал) -> кто релевантен для расследования
ROUTE = {
    1: "соответствующий агент + Мангуст (аналитик)",
    2: "Котолизатор / Муравей",
    3: "Муравей",
    4: "Муравей / Ворон",
    5: "Муравей",
    6: "Бестия / Ворон",
    7: "Штрейкбрехер",
    8: "Муравей",
}

# COMPUTED-running-to-guidance — умные советы по провалам (cid -> текст)
ADVICE = {
    1: lambda ok, s, d: "проверь heartbeat-крон агента и что агент отвечает (sessions_list)" if not ok
        else "отчёт дошёл — агенты живы (факт доставки); сверься при необходимости",
    2: lambda ok, s, d: "systemctl restart openclaw-gateway.service — ТОЛЬКО по прямой команде «рестарт»!"
        if "down" in s.lower()
        else ("root-cause: journalctl -u openclaw-gateway --since '-1h'" if "авто-перезапуск" in s.lower()
              else ("сверься с памятью: ты сам рестартил?" if "ручн" in s.lower()
                    else "gateway ок — действий нет")),
    3: lambda ok, s, d: "упавшие MCP: проверь systemctl status mcp-* и порты; при необходимости restart"
        if not ok else "MCP ок — действий нет",
    4: lambda ok, s, d: ("ONNX :8082 DOWN → systemctl status onnx-embedder.service; sudo journalctl -u onnx-embedder --since '-15m'"
        if "onnx" in d.lower() and "down" in d.lower()
        else ("lab_search FAIL → запусти reindex: python3 /root/LabDoctorM/projects/lab-memory/scripts/reindex.py --incremental"
              if "fail" in d.lower() and "reindex active" not in d.lower()
              else ("reindex уже идёт — НЕ запускай второй раз; дождись завершения"
                    if "reindex active" in d.lower()
                    else "проверь ONNX/embedding и lab_search vectors"))),
    5: lambda ok, s, d: ("PostgreSQL DOWN → systemctl status postgresql; sudo journalctl -u postgresql --since '-15m'"
        if "pg" in d.lower() and "down" in d.lower()
        else ("disk высокий → du -sh /var /tmp /root 2>/dev/null; найди и очисти (trash > rm), но сначала фактчек"
              if "disk" in d.lower() and ("85" in d or "95" in d or "крит" in d.lower() or "высок" in d.lower())
              else "сверься по diag (PG/disk)")),
    6: lambda ok, s, d: ("VPN DOWN → systemctl status amnezia-awg2; проверь конфиг VPN"
        if "vpn" in d.lower() and ("down" in d.lower() or "упал" in d.lower())
        else ("searxng DOWN → systemctl status searxng; curl -s localhost:8889"
              if "searxng" in d.lower() and ("down" in d.lower() or "упал" in d.lower())
              else ("SSL истёк/FAIL → обнови сертификат (certbot renew / провайдер)"
                    if "ssl" in d.lower() and ("истёк" in d.lower() or "expire" in d.lower() or "fail" in d.lower())
                    else "сверься по diag (VPN/searxng/SSL)"))),
    7: lambda ok, s, d: ("git-dirty — рабочая норма; если хочешь чисто — ./bin/lab-commit.sh <агент>"
        if "git-dirty" in d.lower() or "git-dirty" in s.lower()
        else ("инциденты открыты → сверься по projects/*/incidents, закрой или эскалируй"
              if "инцидент" in d.lower()
              else "сверься по diag (git/инциденты)")),
    8: lambda ok, s, d: ("load высокий → htop — кто жрёт CPU; не убивай без понимания"
        if "load" in d.lower() and ("высок" in d.lower() or "crit" in d.lower() or "крит" in d.lower())
        else ("RAM высокая → free -m; найди процесс-пожиратель, не убивай systemd-сервисы"
              if "ram" in d.lower() and ("высок" in d.lower() or "крит" in d.lower())
              else ("docker DOWN → docker ps -a; systemctl status docker"
                    if "docker" in d.lower() and "down" in d.lower()
                    else "сверься по diag (load/RAM/docker)"))),
}

ROUTE_SKILLS = "research + labsearch + Археолог корней"

# «Что это» простым языком — контекстная подсказка для каждой категории (для --full)
CAT_HINT = {
    1: "САМ ФАКТ, что этот отчёт ДОШЁЛ = OpenClaw и агент-отправитель живы на 100% (не дошёл бы иначе). Правило ЗавЛаба: пришёл отчёт → живы; не пришёл → мертвы. Строка ниже лишь проверяет целостность файлов-памяти агентов на диске (НЕ живость).",
    2: "Сам движок OpenClaw (гейтвей). Живость доказана самим фактом доставки отчёта. Перезапуски теперь считаются ЗА ОКНО ОТЧЁТА (1ч), а не за всю жизнь юнита: ручные рестарты (ты сам делал) — 💡 сверься с памятью; авто-перезапуски (systemd сам поднял после падения, маркер 'Scheduled restart') — 🔴 подозрительно, нужен root-cause. Исторический lifetime-счётчик (NRestarts) показан для справки, но тревогу НЕ управляет (горел бы вечно).",
    3: "MCP — внутренние сервисы-помощники: память/поиск, хранилище ключей, привратник портов. Список берётся живьём из systemd (не захардкожен).",
    4: "Семантический поиск лабы (ONNX + FAISS). vectors — сколько записей в индексе. reindex = авто-обновление.",
    5: "Базы и диск. disk — заполненность; норма <85%, тревога с 85%, крит 95%.",
    6: "Внешний доступ. VPN, метапоиск searxng, SSL-сертификат сайта (чтоб не протёк).",
    7: "Код проектов. git-dirty = несохранённые правки (рабочая норма, не сбой). Инциденты: «открыто» = без метки resolved/closed в шапке файла.",
    8: "Железо. load — загрузка CPU (норма < числа ядер). RAM — занято/всего; available = сколько реально доступно приложениям (free + reclaimable cache, buff/cache). total = used + free + buff/cache.",
}


def get_random_quote():
    """Рандомная цитата из сводного гримуара (nevermind.md).
    Честно: если файла/цитат нет — возвращает None (не выдумываем)."""
    path = QUOTE_FILE
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            lines = [ln[2:].strip() for ln in f if ln.startswith("- ") and len(ln) > 2]
    except Exception:
        return None
    if not lines:
        return None
    q = random.choice(lines)
    if len(q) > 300:
        q = q[:300].rstrip() + "…"
    return q


def clean_line(s):
    """Убирает box-символы рамок и лишние пробелы из строк доктора."""
    s = s.strip().strip("│┃|").strip()
    s = s.replace("─", "").replace("WARNING:", "").strip()
    return re.sub(r"\s{2,}", " ", s).strip()


def run(cmd, timeout=12, cwd=None):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               timeout=timeout, cwd=cwd)
    except Exception:
        return None


def port_ok(port, host="127.0.0.1", timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def doctor_warnings():
    """openclaw doctor кэшируется раз в сутки; возвращает {all, count, new}.
    new пересчитывается при КАЖДОМ чтении по актуальному DOCTOR_ALLOWLIST."""
    cache = os.path.join(STATE_DIR, "doctor.json")
    data = None
    if os.path.isfile(cache):
        try:
            data = json.load(open(cache))
        except Exception:
            data = None
    if data and (NOW.replace(tzinfo=None) - datetime.datetime.fromisoformat(data["ts"])).total_seconds() < 24*3600:
        # пересчитываем new по текущему allowlist (он мог измениться)
        data["new"] = [w for w in data.get("all", [])
                        if not any(sub.lower() in w.lower() for sub in DOCTOR_ALLOWLIST)]
        return data
    r = run("openclaw doctor", timeout=90)
    warns = []
    if r and r.stdout:
        for line in r.stdout.splitlines():
            s = line.strip()
            if "⚠" in s or ("warn" in s.lower() and "──" not in s and not s.startswith("◇")):
                warns.append(clean_line(s))
    new = [w for w in warns if not any(sub.lower() in w.lower() for sub in DOCTOR_ALLOWLIST)]
    out = {"ts": NOW.replace(tzinfo=None).isoformat(), "all": warns, "count": len(warns), "new": new}
    try:
        json.dump(out, open(cache, "w"))
    except Exception:
        pass
    return out


# ---------- Категории ----------

def cat_agents():
    # ЖИВОСТЬ агентов доказана самим ФАКТОМ доставки этого отчёта (правило ЗавЛаба:
    # пришёл = живы 100%, не пришёл = мертвы 100%). Проверка целостности файлов-памяти
    # (grimoire.md) убрана: файлы смержены в nevermind.md (ЗавЛаб, 2026-07), монитор
    # искал несуществующие пути и лгал «ОТСУТСТВУЕТ» для всех агентов.
    return (True, "живы (отчёт дошёл)", [])


def classify_restarts(journal_text, lifetime_nrest, window="1h"):
    """Чистая функция: классифицирует перезапуски gateway из текста journalctl за окно.
    Возвращает dict: total/auto/manual/lifetime/window/classification.
    classification: 'ok' (0 за окно) | 'manual' (были старты, но ручные) | 'auto' (systemd сам поднимал).
    Маркер авто-перезапуска = 'Scheduled restart' (systemd Restart=always после падения).
    Первый старт при загрузке сервера тоже считается 'Starting' без 'Scheduled restart'
    и попадает в manual — это ок: загрузка не является падением.
    """
    total = len(re.findall(r"(Started|Starting) OpenClaw Gateway", journal_text))
    auto = len(re.findall(r"Scheduled restart", journal_text))
    manual = max(0, total - auto)
    if auto >= 1:
        classification = "auto"
    elif total >= 1:
        classification = "manual"
    else:
        classification = "ok"
    return {
        "total": total, "auto": auto, "manual": manual,
        "lifetime": lifetime_nrest, "window": window,
        "classification": classification,
    }


def cat_openclaw():
    r = run("systemctl is-active openclaw-gateway.service", timeout=6)
    active = bool(r and r.stdout.strip() == "active")
    nr = run("systemctl show -p NRestarts openclaw-gateway.service", timeout=6)
    nrest = "?"
    if nr and nr.stdout:
        m = re.search(r"NRestarts=(\d+)", nr.stdout)
        if m:
            nrest = m.group(1)
    win = THRESHOLDS["restart_window"]
    jl = run(f"journalctl -u openclaw-gateway.service --since '-{win}' --no-pager", timeout=15)
    jtext = (jl.stdout or "") if jl else ""
    cls = classify_restarts(jtext, nrest, win)
    dw = doctor_warnings()
    if not active:
        detail = f"gateway DOWN (история lifetime: {nrest})"
    elif cls["classification"] == "auto":
        detail = (f"gateway работает, АВТО-перезапусков за {win}: {cls['auto']} "
                  f"(systemd сам поднимал после падения — подозрительно, нужен root-cause)")
    elif cls["classification"] == "manual":
        detail = (f"gateway работает, ручных рестартов за {win}: {cls['manual']} "
                  f"(сверься с памятью: ты сам рестартил в этом часу?)")
    else:
        detail = f"gateway работает, перезапусков за {win}: 0"
    if dw["new"]:
        detail += f"\n⚠️ самопроверка: {len(dw['new'])} НОВЫХ замечаний: {', '.join(w[:50] for w in dw['new'][:2])}"
    else:
        detail += f"\n⚠️ самопроверка: {dw['count']} старое безопасное замечание, новых нет"
    ok = active and cls["classification"] != "auto" and not dw["new"]
    out = [f"перезапуски за {win}: total={cls['total']} (ручные ~{cls['manual']}, авто {cls['auto']}); "
           f"история lifetime: {nrest}"]
    # Предупреждения доктора выводятся целиком в выделенной секции 🩺 внизу полного дампа
    # (и в коротком отчёте — через summary-строку ⚠️ самопроверка), чтобы не дублировать данные.
    return ok, detail, out


def cat_mcp():
    """Динамически спрашиваем systemd: какие mcp-*.service РЕАЛЬНО запущены.
    Не хардкодим число — чтобы не врать при появлении/удалении сервисов."""
    known_ports = {"mcp-memory": 8087, "mcp-apikeys": 8086, "mcp-gatekeeper": 8888}
    r = run("systemctl list-units --type=service --state=running 'mcp-*' --no-legend --no-pager", timeout=8)
    services = []
    if r and r.stdout:
        for line in r.stdout.splitlines():
            unit = line.strip().split()[0] if line.strip() else ""
            if unit.endswith(".service") and "heartbeat-collect" not in unit:
                services.append(unit[:-len(".service")])
    up, out = 0, []
    for svc in sorted(services):
        p = known_ports.get(svc)
        if p is not None:
            ok = port_ok(p)
            out.append(f"{svc} (порт {p}): {'работает' if ok else 'DOWN'}")
        else:
            ok = True
            out.append(f"{svc}: работает (systemd)")
        up += 1 if ok else 0
    total = len(services)
    if total == 0:
        return False, "ни одного MCP не запущено!", ["ожидали memory/apikeys/gatekeeper"]
    return (up == total), f"{up}/{total} работают", out


def cat_memory():
    onnx = port_ok(8082)
    ls = run("python3 /root/LabDoctorM/projects/lab-memory/scripts/lab_search.py health",
             timeout=45, cwd="/root/LabDoctorM/projects/lab-memory")
    ls_ok, vec = False, "?"
    if ls and ls.stdout:
        try:
            d = json.loads(ls.stdout)
            ls_ok = bool(d.get("faiss_loaded") and d.get("onnx_available") and d.get("vectors", 0) > 0)
            vec = d.get("vectors", "?")
        except Exception:
            pass
    ri = run("systemctl is-active reindex-incremental.timer reindex-full.timer", timeout=6)
    ri_active = bool(ri and "active" in ri.stdout)
    ok = onnx and ls_ok
    detail = f"ONNX :8082 {'ok' if onnx else 'DOWN'}; lab_search vectors={vec} {'ok' if ls_ok else 'FAIL'}; reindex {'active' if ri_active else 'off'}"
    return ok, detail, [f"reindex-incremental.timer: {'active' if ri_active else 'off'}"]


def cat_data():
    out = []
    # PostgreSQL (docker api-hub-db-1)
    pg = run("docker ps --filter name=api-hub-db-1 --format '{{.Status}}'", timeout=8)
    pg_up = bool(pg and pg.stdout.strip() and "Up" in pg.stdout)
    out.append(f"PostgreSQL(api-hub-db-1): {'up' if pg_up else 'DOWN/off'}")
    # SQLite state
    sq = "/root/.openclaw/state/openclaw.sqlite"
    sq_ok = os.path.isfile(sq) and os.path.getsize(sq) > 0
    out.append(f"SQLite state: {'ok' if sq_ok else 'FAIL'} ({os.path.getsize(sq)//1024//1024 if sq_ok else 0} MB)")
    # disk
    df = run("df -h / | tail -1 | awk '{print $5}'", timeout=6)
    disk = df.stdout.strip() if df else "?"
    pct = int(disk.rstrip("%")) if disk and disk.rstrip("%").isdigit() else 0
    ok = pg_up and sq_ok and pct < THRESHOLDS["disk_warn_pct"]
    disk_hint = "ок" if pct < THRESHOLDS["disk_warn_pct"] else ("тревога" if pct < THRESHOLDS["disk_crit_pct"] else "КРИТ")
    return ok, f"PostgreSQL {'up' if pg_up else 'DOWN'}; disk {disk} (норма <{THRESHOLDS['disk_warn_pct']}% — {disk_hint})", out


def cat_network():
    out = []
    vpn = run("docker ps --filter name=amnezia-awg2 --format '{{.Status}}'", timeout=8)
    vpn_up = bool(vpn and vpn.stdout.strip() and "Up" in vpn.stdout)
    out.append(f"VPN(amnezia-awg2): {'up' if vpn_up else 'DOWN'}")
    sx = port_ok(8889)
    out.append(f"searxng(:8889): {'ok' if sx else 'DOWN'}")
    # SSL shtab-ai.ru
    ssl = run("echo | timeout 8 openssl s_client -servername shtab-ai.ru -connect shtab-ai.ru:443 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null", timeout=12)
    ssl_ok = bool(ssl and ssl.stdout.strip())
    exp = ssl.stdout.strip().replace("notAfter=", "") if ssl_ok else "?"
    out.append(f"SSL shtab-ai.ru: {'ok' if ssl_ok else 'FAIL'} (exp {exp})")
    ok = vpn_up and sx and ssl_ok
    return ok, f"VPN {'up' if vpn_up else 'DOWN'}; searxng {'ok' if sx else 'DOWN'}; SSL {'ok' if ssl_ok else 'FAIL'}", out


def cat_projects():
    out = []
    dirty = 0
    repos_dirty = []
    for p in ["lab-memory", "mcp-tools", "api-hub", "DoctorM_and_Ai"]:
        d = os.path.join(PROJECTS, p)
        if not os.path.isdir(d):
            continue
        g = run("git status --porcelain | wc -l", timeout=8, cwd=d)
        n = int(g.stdout.strip()) if g and g.stdout.strip().isdigit() else 0
        dirty += n
        repos_dirty.append((p, n))
    # инциденты: всего / закрыто / открыто (честный подсчёт, не всё = «открытое»)
    inc_total, inc_closed = 0, 0
    open_incidents = []
    closed_re = re.compile(r"status:\s*(resolved|closed|done)", re.IGNORECASE)
    now = datetime.datetime.now().timestamp()
    for root in [WORKSPACES, PROJECTS]:
        for _ in os.listdir(root) if os.path.isdir(root) else []:
            idir = os.path.join(root, _, "incidents")
            if not os.path.isdir(idir):
                continue
            for f in os.listdir(idir):
                if not f.endswith(".md"):
                    continue
                inc_total += 1
                fpath = os.path.join(idir, f)
                try:
                    with open(fpath, errors="ignore") as fh:
                        head = fh.read(600)
                    is_closed = bool(closed_re.search(head))
                except Exception:
                    is_closed = False
                if is_closed:
                    inc_closed += 1
                else:
                    open_incidents.append((f, _, os.path.getmtime(fpath)))
    inc_open = inc_total - inc_closed
    pct = round(inc_closed / inc_total * 100) if inc_total else 0
    # детализация по репозиториям
    for p, n in repos_dirty:
        out.append(f"{p}: {n} файл(ов)" if n else f"{p}: 0 (чистый)")
    out.append(f"инциденты: {inc_open} открыто / {inc_total} ({inc_closed} закрыто)")
    # топ-5 старейших открытых инцидентов (застой)
    oldest = sorted(open_incidents, key=lambda x: x[2])[:5]
    if oldest:
        out.append("старейшие открытые (застой):")
        for f, owner, mtime in oldest:
            days = int((now - mtime) / 86400)
            out.append(f"  · {f[:-3]} ({owner}, {days} дн)")
    repo_list = ", ".join(f"{p} {n}" for p, n in repos_dirty if n) or "нет"
    ok = True  # информационная категория (WIP/INC — базовый шум лаборатории, не сбой)
    summary = (f"незакоммичено {dirty} файлов ({repo_list}) — не сбой; "
               f"инциденты {inc_open} открыто / {inc_total} ({inc_closed} закрыто, {pct}% решено)")
    return ok, summary, out


def cat_host():
    out = []
    la = run("cat /proc/loadavg | awk '{print $1, $2, $3}'", timeout=5)
    load = la.stdout.strip() if la else "?"
    out.append(f"loadavg: {load}")
    # RAM: used/total, free, buff/cache, available — чтобы уравнение сходилось на глаз
    mem = run("free -m | awk '/Mem:/ {print $3, $2, $4, $6, $7}'", timeout=5)
    used = total = free_m = buff = avail = "?"
    if mem:
        p = mem.stdout.split()
        if len(p) >= 5:
            used, total, free_m, buff, avail = p[0], p[1], p[2], p[3], p[4]
    ram = f"{used}/{total} MB"
    out.append(f"RAM: {ram} — used {used}; free {free_m}; buff/cache {buff}; available {avail} MB")
    dp = run("docker ps --format '{{.Names}}'", timeout=8)
    conts = dp.stdout.strip().splitlines() if dp and dp.stdout.strip() else []
    cont = str(len(conts))
    out.append(f"контейнеры ({cont}): {', '.join(conts) if conts else 'нет'}")
    ncpu = run("nproc", timeout=5)
    cores = ncpu.stdout.strip() if ncpu and ncpu.stdout.strip().isdigit() else "?"
    # load(1мин): всплески до ~2×ядер — норма; устойчивое превышение — тревога
    load1 = 0.0
    try:
        load1 = float(load.split()[0])
    except Exception:
        pass
    ncores = int(cores) if cores.isdigit() else 4
    lw, lh = THRESHOLDS["load_warn_x"], THRESHOLDS["load_high_x"]
    if load1 < ncores*lw:
        load_hint = "ок"
    elif load1 < ncores*lh:
        load_hint = "повышенная"
    else:
        load_hint = "ВЫСОКАЯ"
    pct = round(load1 / ncores * 100) if ncores else 0
    out.append(f"ядер CPU: {cores} (нагрузка {load1} из {cores} = {pct}%, норма <100%)")
    ok = load1 < ncores*lh  # тревога только при load1 ≥ lh×ядер
    summary = (f"нагрузка CPU {load1} из {cores} ядер (~{pct}%, {load_hint}); "
               f"память {ram} (доступно {avail} MB); контейнеры {cont} запущено")
    return ok, summary, out


def self_factcheck(results):
    """Встроенный гард честности. Ловит самого себя: противоречие между
    заголовком (summary/✅) и деталями/порогами. Без этого монитор может
    выдать ✅ при значении вне нормы или при расхождении заголовок↔детали."""
    problems = []
    for cid, name, ok, summary, details in results:
        det = "\n".join(details)
        if cid == 2:
            if ok and "DOWN" in summary:
                problems.append(f"{name}: ✅ но gateway DOWN")
            if ok and "АВТО-перезапусков за" in summary:
                problems.append(f"{name}: ✅ но АВТО-перезапусков за окно (не должно быть при ok)")
            if ok and "НОВЫХ замечаний" in summary:
                problems.append(f"{name}: ✅ но есть НОВЫЕ замечания доктора")
        elif cid == 3:
            m = re.search(r"(\d+)/(\d+) работают", summary)
            if m and int(m.group(1)) != int(m.group(2)) and ok:
                problems.append(f"{name}: ✅ но {m.group(1)}/{m.group(2)} работают")
            if ok and "DOWN" in det:
                problems.append(f"{name}: ✅ но есть DOWN в деталях")
        elif cid == 4:
            if ok and ("DOWN" in det or "FAIL" in det or "FAIL" in summary):
                problems.append(f"{name}: ✅ но FAIL/DOWN в данных")
        elif cid == 5:
            m = re.search(r"disk (\d+)%", summary)
            if m and int(m.group(1)) >= THRESHOLDS["disk_warn_pct"] and ok:
                problems.append(f"{name}: ✅ но disk {m.group(1)}% (норма <{THRESHOLDS['disk_warn_pct']})")
            if ok and "DOWN" in summary:
                problems.append(f"{name}: ✅ но PostgreSQL DOWN")
        elif cid == 6:
            if ok and ("DOWN" in det or "FAIL" in det or "DOWN" in summary or "FAIL" in summary):
                problems.append(f"{name}: ✅ но DOWN/FAIL в данных")
        elif cid == 8:
            m = re.search(r"1мин ([\d.]+)", summary)
            cm = re.search(r"норма <(\d+)", summary)
            if m and cm:
                l1 = float(m.group(1))
                cores = int(cm.group(1))
                if l1 >= THRESHOLDS["load_high_x"] * cores and ok:
                    problems.append(f"{name}: ✅ но load1 {l1} ≥ тревожного {THRESHOLDS['load_high_x']*cores}")
    return problems


def independent_probe():
    """НЕЗАВИСИМЫЙ замер каждой категории другим кодом/командой,
    чтобы сверить с тем, что выдал монитор (ловит хардкод и раси).
    Возвращает {cid: строка_независимого_замера}."""
    probe = {}
    probe[1] = "агенты: живость доказана доставкой отчёта (grimoire.md смержены в nevermind.md)"
    ra = run("systemctl is-active openclaw-gateway.service", timeout=6)
    probe[2] = f"gateway: {ra.stdout.strip() if ra else '?'}"
    r = run("systemctl list-units --type=service --state=running 'mcp-*' --no-legend --no-pager", timeout=8)
    svcs = []
    if r and r.stdout:
        for line in r.stdout.splitlines():
            u = line.strip().split()[0] if line.strip() else ""
            if u.endswith(".service") and "heartbeat-collect" not in u:
                svcs.append(u[:-len(".service")])
    kp = {"mcp-memory": 8087, "mcp-apikeys": 8086, "mcp-gatekeeper": 8888}
    up = sum(1 for s in svcs if (port_ok(kp[s]) if s in kp else True))
    probe[3] = f"mcp запущено/порты отвечают: {up}/{len(svcs)}"
    ls = run("python3 /root/LabDoctorM/projects/lab-memory/scripts/lab_search.py health", timeout=45, cwd="/root/LabDoctorM/projects/lab-memory")
    vec = "?"
    if ls and ls.stdout:
        try:
            vec = json.loads(ls.stdout).get("vectors", "?")
        except Exception:
            pass
    probe[4] = f"lab_search vectors: {vec}"
    df = run("df -h / | tail -1 | awk '{print $5}'", timeout=6)
    pg = run("docker ps --filter name=api-hub-db-1 --format '{{.Status}}'", timeout=8)
    probe[5] = f"disk {df.stdout.strip() if df else '?'} | PostgreSQL {'Up' if pg and 'Up' in pg.stdout else 'DOWN'}"
    vpn = run("docker ps --filter name=amnezia-awg2 --format '{{.Status}}'", timeout=8)
    sx = port_ok(8889)
    ssl = run("echo | timeout 8 openssl s_client -servername shtab-ai.ru -connect shtab-ai.ru:443 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null", timeout=12)
    probe[6] = f"VPN {'Up' if vpn and 'Up' in vpn.stdout else 'DOWN'} | searxng {'ok' if sx else 'DOWN'} | SSL {'ok' if ssl and ssl.stdout.strip() else 'FAIL'}"
    inc_total, inc_closed = 0, 0
    cre = re.compile(r"status:\s*(resolved|closed|done)", re.IGNORECASE)
    for root in [WORKSPACES, PROJECTS]:
        if not os.path.isdir(root):
            continue
        for _ in os.listdir(root):
            idir = os.path.join(root, _, "incidents")
            if not os.path.isdir(idir):
                continue
            for f in os.listdir(idir):
                if not f.endswith(".md"):
                    continue
                inc_total += 1
                try:
                    with open(os.path.join(idir, f), errors="ignore") as fh:
                        h = fh.read(600)
                    if cre.search(h):
                        inc_closed += 1
                except Exception:
                    pass
    probe[7] = f"инцидентов всего {inc_total}, открыто {inc_total - inc_closed}"
    la = run("cat /proc/loadavg", timeout=5)
    probe[8] = f"load1 {la.stdout.strip().split()[0] if la and la.stdout else '?'}"
    return probe


def selftest_report():
    """--selftest: монитор сверяет себя с независимым замером и показывает вердикт."""
    results = []
    for cid, name, fn in CATEGORIES:
        try:
            ok, summary, details = fn()
        except Exception as e:
            ok, summary, details = False, f"ERROR: {e}", []
        results.append((cid, name, ok, summary, details))
    probe = independent_probe()
    lines = ["🦊 ЛабМонитор · САМОПРОВЕРКА (--selftest)", ""]
    for cid, name, ok, summary, details in results:
        lines.append(f"[{cid}] {name}")
        lines.append(f"   монитор : {'✅' if ok else '🔴'} {summary}")
        lines.append(f"   независ. : {probe.get(cid, '—')}")
    sf = self_factcheck(results)
    lines.append("")
    if sf:
        lines.append("🔴 САМОПРОВЕРКА НАШЛА НЕСОВПАДЕНИЯ (монитор врёт!):")
        for p in sf:
            lines.append(f"   • {p}")
    else:
        lines.append("✅ САМОПРОВЕРКА: монитор честен — заголовки совпадают с деталями и нормами")
    return "\n".join(lines)


CATEGORIES = [
    (1, "Агенты",        cat_agents),
    (2, "OpenClaw",      cat_openclaw),
    (3, "MCP",           cat_mcp),
    (4, "Память/поиск",  cat_memory),
    (5, "Данные",        cat_data),
    (6, "Сеть",          cat_network),
    (7, "Проекты",       cat_projects),
    (8, "Сервер",       cat_host),
]


def build_report(full=False):
    results, fails = [], []
    for cid, name, fn in CATEGORIES:
        try:
            ok, summary, details = fn()
        except Exception as e:
            ok, summary, details = False, f"ERROR: {e}", []
        results.append((cid, name, ok, summary, details))
        if not ok:
            fails.append((cid, name, summary))

    overall = "OK" if not fails else "ТРЕВОГА"
    sf = self_factcheck(results)  # гард честности: монитор ловит сам себя
    stamp = NOW.strftime("%H:%M")
    lines = [f"🦊 ЛабМонитор · {stamp} МСК · {overall}"]

    if fails:
        lines.append("🔴 провалы:")
        for cid, name, summary in fails:
            lines.append(f"  • [{cid}] {name}: {summary}")

    for cid, name, ok, summary, details in results:
        lines.append(f"{'✅' if ok else '🔴'} {name}: {summary}")

    if sf:
        lines.append("🔴 САМОПРОВЕРКА (монитор поймал сам себя):")
        for p in sf:
            lines.append(f"  • {p}")

    # слой реагирования (advise) — умный совет по провалу, иначе fallback на маршрут
    if fails:
        lines.append("🔧 СОВЕТ (без «го» не спавню):")
        details_by_cid = {cid: details for cid, name, ok, summary, details in results}
        for cid, name, summary in fails:
            fn = ADVICE.get(cid)
            if fn:
                ctx = summary + "\n" + "\n".join(details_by_cid.get(cid, []))
                advice = fn(False, summary, ctx)
                lines.append(f"  → [{cid}] {name}: {advice}")
            else:
                lines.append(f"  → [{cid}] {name}: спавнить {ROUTE.get(cid,'?')} с набором [{ROUTE_SKILLS}]")

    if not full:
        q = get_random_quote()
        if q:
            lines.append("")
            lines.append(f"📜 Цитата часа: {q}")
        lines.append("ℹ️ полный дамп — !подробно")
        return "\n".join(lines)

    # ---------- ПОЛНЫЙ ДАМП (full) — отдельный чистый формат, без дубля сводки ----------
    icon = {True: "✅", False: "🔴"}
    dl = [f"🦊 ЛабМонитор · полный дамп · {stamp} МСК · {overall}"]
    if fails:
        dl.append("")
        dl.append("🔴 ТРЕВОГИ:")
        for cid, name, summary in fails:
            dl.append(f"  • {name}: {summary}")

    for cid, name, ok, summary, details in results:
        dl.append("")
        # OK-категория: только статус+имя (числа в деталях ниже — без дубля summary);
        # упавшая: summary в заголовке (причина тревоги сразу видна).
        if ok:
            dl.append(f"{icon[ok]} {cid}. {name}")
        else:
            dl.append(f"{icon[ok]} {cid}. {name} — {summary}")
        if cid in CAT_HINT:
            dl.append(f"    💡 {CAT_HINT[cid]}")
        for d in details:
            dl.append(f"    · {d}")

    if sf:
        dl.append("")
        dl.append("🔴 САМОПРОВЕРКА (монитор поймал сам себя):")
        for p in sf:
            dl.append(f"    · {p}")

    # диск: только реальные ФС (без docker-overlayfs дублей корня)
    ds = run("df -h -x tmpfs -x overlay -x devtmpfs | tail -n +2 | awk '{print $6\" \"$5\" (свободно \"$4\")\"}'", timeout=6)
    if ds and ds.stdout:
        seen, disk_lines = set(), []
        for line in ds.stdout.strip().splitlines():
            if "/var/lib/docker" in line:
                continue
            key = line.split()[0]
            if key in seen:
                continue
            seen.add(key)
            disk_lines.append(line.strip())
        if disk_lines:
            dl.append("")
            dl.append("💾 Диск (реальные ФС):")
            for line in disk_lines:
                dl.append(f"    · {line}")

    # докер
    dk = run("docker ps --format '{{.Names}}|{{.Status}}'", timeout=8)
    if dk and dk.stdout:
        dl.append("")
        dl.append("🐳 Docker:")
        for line in dk.stdout.strip().splitlines():
            parts = line.split("|", 1)
            nm = parts[0]
            st = parts[1] if len(parts) > 1 else "?"
            dl.append(f"    · {nm}: {st}")

    # доктор целиком (уже очищено clean_line)
    dw = doctor_warnings()
    dl.append("")
    dl.append(f"🩺 Самопроверка движка (openclaw doctor): всего замечаний {dw['count']}, из них новых {len(dw['new'])}")
    dl.append("    💡 «замечание» — не поломка, а совет от встроенного медосмотра. [старое] = известное и безопасное; [🔴 НОВОЕ] = появилось, надо глянуть.")
    for w in dw["all"]:
        tag = "🔴 НОВОЕ" if w in dw["new"] else "старое"
        dl.append(f"    · [{tag}] {w[:110]}")

    q = get_random_quote()
    if q:
        dl.append("")
        dl.append(f"📜 Цитата часа: {q}")

    return "\n".join(dl)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        print(selftest_report())
    else:
        full = "--full" in sys.argv
        print(build_report(full=full))
