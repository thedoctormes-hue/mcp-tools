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

# === ПОРОГИ ЧЕСТНОСТИ (каждый — откуда норма) ===
# Монитор обязан сверять значения именно с этими порогами, а не с "магическими числами".
THRESHOLDS = {
    "disk_warn_pct": 85,    # норма <85%  (источник: ADR-039, практика ротации диска лаборатории)
    "disk_crit_pct": 95,    # КРИТ     >=95% (диск почти полон)
    "nrestarts_ok": 5,      # авто-восстановлений <5 = норма (единичные падения допустимы)
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
ROUTE_SKILLS = "research + labsearch + археолог-корней (root-cause)"

# «Что это» простым языком — контекстная подсказка для каждой категории (для --full)
CAT_HINT = {
    1: "САМ ФАКТ, что этот отчёт ДОШЁЛ = OpenClaw и агент-отправитель живы на 100% (не дошёл бы иначе). Правило ЗавЛаба: пришёл отчёт → живы; не пришёл → мертвы. Строка ниже лишь проверяет целостность файлов-памяти агентов на диске (НЕ живость).",
    2: "Сам движок OpenClaw (гейтвей). Живость доказана самим фактом доставки отчёта. «авто-восстановлений» = сколько раз движок падал и система сама его подняла за всё время. Мало (<5) = ок; быстро растёт = нестабилен. baseline warn = известный шум.",
    3: "MCP — внутренние сервисы-помощники: память/поиск, хранилище ключей, привратник портов. Список берётся живьём из systemd (не захардкожен).",
    4: "Семантический поиск лабы (ONNX + FAISS). vectors — сколько записей в индексе. reindex = авто-обновление.",
    5: "Базы и диск. disk — заполненность; норма <85%, тревога с 85%, крит 95%.",
    6: "Внешний доступ. VPN, метапоиск searxng, SSL-сертификат сайта (чтоб не протёк).",
    7: "Код проектов. git-dirty = несохранённые правки (рабочая норма, не сбой). Инциденты: «открыто» = без метки resolved/closed в шапке файла.",
    8: "Железо. load — загрузка CPU (норма < числа ядер). RAM — память занято/всего.",
}


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
    out, live = [], 0
    for a in AGENTS:
        g = os.path.join(WORKSPACES, a, "grimoire.md")
        ok = os.path.isfile(g)
        n = 0
        if ok:
            try:
                with open(g) as f:
                    n = sum(1 for ln in f if ln.startswith("- "))
            except Exception:
                n = 0
        live += 1 if ok else 0
        mt = ""
        if ok:
            try:
                mt = " · изм. " + datetime.datetime.fromtimestamp(os.path.getmtime(g)).strftime("%d.%m")
            except Exception:
                mt = ""
        out.append(f"{a}: grimoire-файл {'на месте' if ok else 'ОТСУТСТВУЕТ'} ({n} строк{mt})")
    # ЖИВОСТЬ агентов доказана самим ФАКТОМ доставки этого отчёта (правило ЗавЛаба:
    # пришёл = живы 100%, не пришёл = мертвы 100%). Поэтому ok всегда True при доставке.
    # Ниже проверяем лишь ЦЕЛОСТНОСТЬ файлов-памяти (grimoire), не живость.
    return (True, f"живы (отчёт дошёл); grimoire {live}/{len(AGENTS)} на месте", out)


def cat_openclaw():
    r = run("systemctl is-active openclaw-gateway.service", timeout=6)
    active = bool(r and r.stdout.strip() == "active")
    nr = run("systemctl show -p NRestarts openclaw-gateway.service", timeout=6)
    nrest = "?"
    if nr and nr.stdout:
        m = re.search(r"NRestarts=(\d+)", nr.stdout)
        if m:
            nrest = m.group(1)
    dw = doctor_warnings()
    detail = f"gateway {'работает' if active else 'DOWN'}, авто-восстановлений: {nrest} (норма <5)"
    if dw["new"]:
        detail += f" ⚠️ самопроверка: {len(dw['new'])} НОВЫХ замечаний: {', '.join(w[:50] for w in dw['new'][:2])}"
    else:
        detail += f" ⚠️ самопроверка: {dw['count']} старое безопасное замечание, новых нет"
    ok = active and nrest.isdigit() and int(nrest) < THRESHOLDS["nrestarts_ok"] and not dw["new"]
    return ok, detail, [f"доктор: {w}" for w in dw["all"]] if dw["all"] else []


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
    out.append(f"disk /: {disk}")
    ok = pg_up and sq_ok and pct < THRESHOLDS["disk_warn_pct"]
    disk_hint = "ок" if pct < THRESHOLDS["disk_warn_pct"] else ("тревога" if pct < THRESHOLDS["disk_crit_pct"] else "КРИТ")
    return ok, f"PG {'up' if pg_up else 'DOWN'}; disk {disk} (норма <{THRESHOLDS['disk_warn_pct']}% — {disk_hint})", out


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
    for p in ["lab-memory", "mcp-tools", "api-hub", "DoctorM_and_Ai"]:
        d = os.path.join(PROJECTS, p)
        if not os.path.isdir(d):
            continue
        g = run("git status --porcelain | wc -l", timeout=8, cwd=d)
        n = int(g.stdout.strip()) if g and g.stdout.strip().isdigit() else 0
        dirty += n
        if n:
            out.append(f"{p}: {n} незакоммиченных")
    # инциденты: всего / закрыто / открыто (честный подсчёт, не всё = «открытое»)
    inc_total, inc_closed = 0, 0
    closed_re = re.compile(r"status:\s*(resolved|closed|done)", re.IGNORECASE)
    for root in [WORKSPACES, PROJECTS]:
        for _ in os.listdir(root) if os.path.isdir(root) else []:
            idir = os.path.join(root, _, "incidents")
            if not os.path.isdir(idir):
                continue
            for f in os.listdir(idir):
                if not f.endswith(".md"):
                    continue
                inc_total += 1
                try:
                    with open(os.path.join(idir, f), errors="ignore") as fh:
                        head = fh.read(600)
                    if closed_re.search(head):
                        inc_closed += 1
                except Exception:
                    pass
    inc_open = inc_total - inc_closed
    out.append(f"инциденты: всего {inc_total}, закрыто {inc_closed}, открыто {inc_open}")
    ok = True  # информационная категория (WIP/INC — базовый шум лаборатории, не сбой)
    return ok, f"git-dirty: {dirty} файл(ов) — рабочая норма; инциденты: {inc_open} открыто / {inc_total} всего", out


def cat_host():
    out = []
    la = run("cat /proc/loadavg | awk '{print $1, $2, $3}'", timeout=5)
    load = la.stdout.strip() if la else "?"
    out.append(f"loadavg: {load}")
    fr = run("free -m | awk '/Mem:/ {print $3\"/\"$2\" MB\"}'", timeout=5)
    ram = fr.stdout.strip() if fr else "?"
    out.append(f"RAM: {ram}")
    dp = run("docker ps --format '{{.Names}}' | wc -l", timeout=8)
    cont = dp.stdout.strip() if dp else "?"
    out.append(f"docker containers: {cont}")
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
    out.append(f"ядер CPU: {cores} (норма load <{cores}, всплески до {int(ncores*lh)} ок)")
    ok = load1 < ncores*lh  # тревога только при load1 ≥ lh×ядер
    return ok, f"load {load} (1мин {load1} — {load_hint}, норма <{cores}); RAM {ram}; docker {cont}", out


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
            m = re.search(r"авто-восстановлений: (\d+)", summary)
            if m and int(m.group(1)) >= THRESHOLDS["nrestarts_ok"] and ok:
                problems.append(f"{name}: ✅ но авто-восстановлений {m.group(1)} (норма <{THRESHOLDS['nrestarts_ok']})")
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
                problems.append(f"{name}: ✅ но PG DOWN")
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
    ac = sum(1 for a in AGENTS if os.path.isfile(os.path.join(WORKSPACES, a, "grimoire.md")))
    probe[1] = f"grimoire-файлов на диске: {ac}/{len(AGENTS)}"
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
    probe[5] = f"disk {df.stdout.strip() if df else '?'} | PG {'Up' if pg and 'Up' in pg.stdout else 'DOWN'}"
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
    (8, "Хост",          cat_host),
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
        lines.append(f"{name}: {'✅' if ok else '🔴'} {summary}")

    if sf:
        lines.append("🔴 САМОПРОВЕРКА (монитор поймал сам себя):")
        for p in sf:
            lines.append(f"  • {p}")

    # слой реагирования (advise)
    if fails:
        lines.append("🔧 СОВЕТ (без «го» не спавню):")
        for cid, name, summary in fails:
            lines.append(f"  → [{cid}] {name}: спавнить {ROUTE.get(cid,'?')} с набором [{ROUTE_SKILLS}]")

    if not full:
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

    return "\n".join(dl)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        print(selftest_report())
    else:
        full = "--full" in sys.argv
        print(build_report(full=full))
