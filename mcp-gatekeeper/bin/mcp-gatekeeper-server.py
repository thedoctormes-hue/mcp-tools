#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp-gatekeeper-server.py — MCP-привратник портов/таймеров агентов.

Единый MCP-сервер = привратник. Агент не может напрямую забиндить порт или
поставить таймер — только через этот сервер. Все решения принимает
детерминированный PDP (policy-as-code), БЕЗ LLM в ядре.

Транспорт (как у соседей в mcp-tools):
  MCP_TRANSPORT=http  -> streamable-http (systemd, 127.0.0.1:8888 по умолчанию)
  MCP_TRANSPORT=stdio -> stdio (локально)

Также поддерживается CLI-режим (--cli ...) для shell-зародыша
register-port-timer.sh и для тестов — вызывает ту же логику Gatekeeper
напрямую, без MCP-транспорта.

Контракт: docs/CONTRACT.md. Политика: policies/policy_v1.yaml.
"""

import argparse
import fcntl
import json
import re
import subprocess
import os
import socket
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except Exception as exc:  # pragma: no cover
    sys.stderr.write(f"mcp-gatekeeper: PyYAML required: {exc}\n")
    raise

from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
HERE = Path(__file__).resolve().parent
REPO = HERE.parent  # mcp-gatekeeper/
DEFAULT_POLICY = REPO / "policies" / "policy_v1.yaml"
DEFAULT_DATA = REPO / "data"

# Make the local `gatekeeper` package importable (mcp-gatekeeper/ holds gatekeeper/).
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from gatekeeper.store import LeaseStore, Lease
GATEKEEPER_VERSION = "1.0.0"

mcp = FastMCP(
    "mcp-gatekeeper",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8888")),
)


# --------------------------------------------------------------------------- #
# Logging с rate-limit (защита диска)
# --------------------------------------------------------------------------- #
import logging

_logger = logging.getLogger("mcp-gatekeeper")
if not _logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _logger.addHandler(_h)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False  # не дублируем в root-логгер (избегаем двойного вывода)


class LogRateLimiter:
    """Пропускает не более max_per_sec сообщений в секунду (защита диска)."""

    def __init__(self, max_per_sec: int = 20):
        self.max = max(1, int(max_per_sec))
        self._hits: deque = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.time()
        with self._lock:
            while self._hits and self._hits[0] <= now - 1.0:
                self._hits.popleft()
            if len(self._hits) >= self.max:
                return False
            self._hits.append(now)
            return True


_rate_limiter = LogRateLimiter(20)


def log(msg: str, level: int = logging.INFO) -> None:
    """Лог с rate-limit. Критичные аудит-записи (journal) этим НЕ ограничиваются."""
    if _rate_limiter.allow():
        _logger.log(level, msg)
    # при превышении лимита — тихо дропаем (защита диска)


# --------------------------------------------------------------------------- #
# sd_notify — one-shot lifecycle-уведомления systemd (без внешних зависимостей).
# Используется ТОЛЬКО для READY=1 (старт) и STOPPING=1 (shutdown). НЕ для
# периодического heartbeat/WATCHDOG — «живость» сервера доказуется ответом на
# реальный запрос агента (событийная модель, см. docs/CONTRACT.md).
# --------------------------------------------------------------------------- #
def sd_notify(state: str) -> bool:
    """Отправить состояние systemd notify. Возвращает True, если отправлено."""
    sock = os.environ.get("NOTIFY_SOCKET")
    if not sock:
        return False
    addr = sock
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.connect(addr)
        s.sendall((state + "\n").encode("utf-8"))
        s.close()
        return True
    except Exception:
        return False


# Глобальный флаг для graceful shutdown (НЕ watchdog — сервер не шлёт heartbeat)
_STOP = threading.Event()


# --------------------------------------------------------------------------- #
# Модель данных
# --------------------------------------------------------------------------- #
# Gatekeeper — ядро PDP + состояние + журнал
# --------------------------------------------------------------------------- #
class Gatekeeper:
    def __init__(self, policy: Dict[str, Any], data_dir: Path, fail_fast: bool = False):
        self.data_dir = Path(data_dir)
        self.lock = threading.RLock()
        self.policy = policy
        self.fail_fast = fail_fast
        gk = policy.get("gatekeeper", {})
        # ADR-0058 step 2: persistent lease timeout (default 1 day) with
        # fallback to the legacy 300s key. Prevents leases from expiring on
        # a service restart / brief outage (grace is applied on load()).
        self.lease_timeout = float(
            gk.get("lease_persistent_timeout_sec", gk.get("lease_timeout_sec", 86400))
        )
        self.heartbeat_interval = float(gk.get("heartbeat_interval_sec", 60))
        self.lease_user = str(gk.get("lease_user", "mcp-gatekeeper"))
        self.allow_root_backdoor = bool(gk.get("allow_root_backdoor", True))
        self.authorized_root_agents = list(gk.get("authorized_root_agents", []))
        self.justification_mode = str(gk.get("justification_mode", "v1_exact"))
        self.log_rate = int(gk.get("log_rate_limit_per_sec", 20))
        _rate_limiter.max = max(1, self.log_rate)
        self.fact_capture_interval = float(gk.get("fact_capture_interval_sec", 300))
        self.agents = {a["id"]: a for a in policy.get("agents", [])}
        self.quotas = policy.get("quotas", {"max_ports": 3, "max_timers": 5})
        self.reserve = policy.get("reserve", {"block_privileged_below": 1024, "blocked_ports": []})
        # Глобальный разрешённый диапазон (ADR-0047 P4). Заменяет неверную
        # концепцию per-agent port_range: порты делятся по НАЗНАЧЕНИЮ, а не по
        # агентам. Любой агент может брать любой порт в диапазоне (ЗавЛаб 12.07).
        apr = policy.get("gatekeeper", {}).get("allowed_port_range", [1024, 65535])
        self.allowed_port_range = (int(apr[0]), int(apr[1]))

        # ADR-0058 step 1: durable lease store (sqlite) + JSON snapshot mirror.
        # State lives here, not in a plain dict, so it survives restarts.
        self.store = LeaseStore(self.data_dir / "leases.db", self.data_dir / "leases.json")
        self._load_state()
        if self.lease_user == "root":
            log("WARN: policy.gatekeeper.lease_user=root — нарушение least-privilege!", logging.WARNING)

    @property
    def leases(self) -> Dict[str, Lease]:
        # ADR-0058: live view of the durable store's in-memory cache
        # (keeps the 43 existing tests working — they read gk.leases).
        return self.store.all_as_dict()

    # ---- загрузка/валидация политики ----
    def validate_policy(self) -> List[str]:
        errs: List[str] = []
        if not self.agents:
            errs.append("policy.agents пуст — ни один агент неизвестен")
        # Примечание: per-agent port_range больше НЕ используется (ЗавЛаб 12.07 —
        # порты делятся по назначению, а не по агентам). Диапазон проверяется
        # глобально через gatekeeper.allowed_port_range в check_port_range.
        q = self.quotas
        if not isinstance(q.get("max_ports"), int) or q.get("max_ports", 0) < 1:
            errs.append("quotas.max_ports должен быть >=1")
        if not isinstance(q.get("max_timers"), int) or q.get("max_timers", 0) < 1:
            errs.append("quotas.max_timers должен быть >=1")
        r = self.reserve
        try:
            int(r.get("block_privileged_below", 1024))
        except Exception:
            errs.append("reserve.block_privileged_below должен быть int")
        if not isinstance(r.get("blocked_ports", []), list):
            errs.append("reserve.blocked_ports должен быть списком")
        return errs

    # ---- журнал (атомарный append JSONL) ----
    def _journal_path(self) -> Path:
        return self.data_dir / "port-timer-log.jsonl"

    def journal(self, event: Dict[str, Any]) -> None:
        """Атомарная дозапись в JSONL. Критично — НЕ ограничивается rate-limit."""
        event.setdefault("when", datetime.now(timezone.utc).isoformat())
        line = json.dumps(event, ensure_ascii=False) + "\n"
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            path = self._journal_path()
            with open(path, "a", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception as exc:
            # graceful degradation: аудит не должен ронять сервер
            log(f"WARN: journal write failed: {exc}", logging.WARNING)

    # ---- состояние (активные lease) ----
    def _state_path(self) -> Path:
        return self.data_dir / "leases.json"

    def _load_state(self) -> None:
        # ADR-0058: load durable leases from the store (sqlite + legacy JSON
        # migration). Each loaded lease gets a grace: last_heartbeat reset to now.
        self.store.load()

    def _save_state(self) -> None:
        # ADR-0058: mirror current leases to the JSON snapshot.
        try:
            self.store.snapshot()
        except Exception as exc:
            log(f"WARN: state save failed: {exc}", logging.WARNING)

    # ---- PDP- шаги ----
    def check_identity(self, agent: str) -> Tuple[bool, str]:
        if not agent:
            return False, "agent не указан"
        if agent not in self.agents:
            return False, f"агент '{agent}' неизвестен (нет в политике)"
        return True, ""

    def agent_port_range(self, agent: str) -> Optional[Tuple[int, int]]:
        a = self.agents.get(agent)
        if not a:
            return None
        rng = a.get("port_range")
        return (int(rng[0]), int(rng[1]))

    def check_port_range(self, agent: str, port: int) -> Tuple[bool, str]:
        # Цель ограничения (ADR-0047/ADR-0055, уточнение ЗавЛаба 12.07):
        # НЕ разделение портов по агентам, а защита резерва + аудит. Любой
        # агент может брать любой порт в разрешённом диапазоне лаборатории
        # (по назначению, без коллизий с резервом). Реальные коллизии
        # (два сервиса на один порт) пресекает ядро ОС, не политика.
        lo, hi = self.allowed_port_range
        if not (lo <= port <= hi):
            return False, f"порт {port} вне разрешённого диапазона лаборатории ({lo}-{hi})"
        return True, ""

    def check_reserve(self, port: int) -> Tuple[bool, str]:
        below = int(self.reserve.get("block_privileged_below", 1024))
        if port < below:
            return False, f"порт {port} привилегированный (<{below}), запрещён"
        if port in self.reserve.get("blocked_ports", []):
            return False, f"порт {port} зарезервирован лабораторией (reserve)"
        return True, ""

    def _count(self, agent: str, kind: str) -> int:
        n = 0
        for l in self.leases.values():
            if l.agent != agent:
                continue
            if kind == "port" and l.port is not None:
                n += 1
            if kind == "timer" and l.timer_action is not None:
                n += 1
        return n

    def check_quota(self, agent: str, kind: str) -> Tuple[bool, str]:
        if kind == "port":
            lim = int(self.quotas.get("max_ports", 3))
            c = self._count(agent, "port")
            if c >= lim:
                return False, f"квота портов исчерпана для '{agent}' ({c}/{lim})"
        else:
            lim = int(self.quotas.get("max_timers", 5))
            c = self._count(agent, "timer")
            if c >= lim:
                return False, f"квота таймеров исчерпана для '{agent}' ({c}/{lim})"
        return True, ""

    def _is_expired(self, l: Lease) -> bool:
        """Lease считается истёкшим, если по нему не было heartbeat дольше lease_timeout."""
        return (time.time() - l.last_heartbeat) > l.lease_timeout

    def check_dedup_port(self, port: int, agent: str = "") -> Tuple[bool, str]:
        # Ловим ТОЛЬКО перекрёстные претензии: другой агент заявляет тот же порт,
        # что уже занят — это реальный конфликт намерений (squatting). Повторная
        # регистрация тем же агентом (restart сервиса) = refresh, не отказ
        # (ЗавЛаб 12.07). Физические коллизии (два сервиса на один порт) пресекает
        # ядро ОС, не политика.
        for l in list(self.leases.values()):
            if l.port == port and l.agent != agent:
                # ADR-0058 step 3: истёкший чужой lease не блокирует —
                # считаем порт свободным (разрешаем перерегистрацию).
                if self._is_expired(l):
                    continue
                return False, (
                    f"порт {port} уже заявлен агентом '{l.agent}' "
                    f"(project={l.project_id}, request_id={l.request_id})"
                )
        return True, ""

    def check_dedup_timer(self, action: str, schedule: str, agent: str = "") -> Tuple[bool, str]:
        # Зеркалит check_dedup_port: только перекрёстные претензии (другой агент
        # заявляет тот же action+schedule) — реальный конфликт намерений. Повторная
        # регистрация тем же агентом (restart таймера) = refresh, не отказ
        # (ЗавЛаб 12.07 / Уровень 1-Г).
        for l in list(self.leases.values()):
            if l.timer_action == action and l.timer_schedule == schedule and l.agent != agent:
                # ADR-0058 step 3: истёкший чужой lease не блокирует.
                if self._is_expired(l):
                    continue
                return False, (
                    f"таймер (action={action!r}, schedule={schedule!r}) уже активен "
                    f"у агента '{l.agent}' (request_id={l.request_id})"
                )
        return True, ""

    def _suggest_free_port(self, agent: str) -> Optional[int]:
        lo, hi = self.allowed_port_range
        taken = {l.port for l in self.leases.values() if l.port is not None}
        for p in range(lo, hi + 1):
            if p < int(self.reserve.get("block_privileged_below", 1024)):
                continue
            if p in self.reserve.get("blocked_ports", []):
                continue
            if p not in taken:
                return p
        return None

    def check_justification(self, agent: str, what_for: str, port: Optional[int] = None) -> Tuple[bool, str]:
        if not what_for or not str(what_for).strip():
            return False, "what_for обязателен (justification)"
        wf = str(what_for).strip()
        if len(wf) < 4:
            return False, "what_for слишком короткий (justification, min 4 символа)"
        # v1: точный (exact) match дедупа оправданий.
        # Повторная регистрация ТОГО ЖЕ ресурса тем же агентом (restart
        # сервиса/таймера) = refresh, НЕ аномалия (ЗавЛаб 12.07: инфра-рестарты
        # легитимны и идемпотентны). Блокируем только дубликат у ДРУГОГО агента.
        if self.justification_mode.startswith("v1"):
            return self._v1_exact_dedup(agent, wf)
        # v2: семантический дедуп. При сбое/недоступности эмбеддера —
        # fail-closed: НЕ разрешаем молча, логируем WARNING и откатываемся
        # к строгой v1_exact проверке (блокирует перехват чужого оправдания).
        elif self.justification_mode.startswith("v2"):
            try:
                dup = self._semantic_dedup(agent, wf, port)
                if dup:
                    return False, f"дубликат justification (semantic): похоже на '{dup}'"
            except Exception as exc:
                log(
                    f"WARN: semantic dedup недоступен, fail-closed fallback к "
                    f"v1_exact: {exc}",
                    logging.WARNING,
                )
                return self._v1_exact_dedup(agent, wf)
        return True, ""

    def _v1_exact_dedup(self, agent: str, wf: str) -> Tuple[bool, str]:
        """Строгий exact-match дедуп оправданий (v1).

        Блокирует ТОЛЬКО перехват чужого оправдания (другой агент с тем же
        текстом) — аномалия squatting. Тот же агент может переиспользовать
        текст (restart / другой ресурс) — это легитимно (ЗавЛаб 12.07:
        любой агент может порты/таймеры, ядро ловит коллизии).

        Используется напрямую в v1-режиме и как fail-closed fallback во
        v2-режиме при сбое семантического дедупа.
        """
        for l in self.leases.values():
            if l.what_for == wf and l.agent != agent:
                return False, f"дубликат justification (exact match): '{wf}' уже есть у '{l.agent}'"
        return True, ""

    def _semantic_dedup(self, agent: str, what_for: str, port: Optional[int]) -> Optional[str]:
        """Заглушка v2: семантический дедуп через лаб. семпамять (ONNX+FAISS).
        fail-closed: при недоступности эмбеддера БРОСАЕТ исключение (не молчит),
        чтобы вызывающий check_justification откатился к v1_exact (строго).
        Раньше здесь стоял fail-open (возврат None => не блокировать)."""
        # TODO(v2): запрос к http://127.0.0.1:8082 (onnx-embedder) и сравнение
        # косинусного сходства с existing what_for того же агента.
        for l in self.leases.values():
            if l.agent == agent and l.what_for == what_for and (port is None or l.port == port):
                return l.what_for
        return None

    def check_least_privilege(self, run_as: Optional[str], agent: Optional[str] = None) -> Tuple[bool, str]:
        # Среда lab: все сервисы бегут от root (non-root юзеров нет), поэтому
        # run_as=root — норма для легитимных сервисов (reindex и т.п.). Блокируем
        # run_as=root ТОЛЬКО для неизвестных агентов (защита от анонимного
        # privilege escalation). Для известных агентов разрешаем — least-privilege
        # недостижим без non-root юзеров в системе.
        if run_as and str(run_as).lower() == "root" and agent not in self.agents:
            return False, "run_as=root запрещён для неизвестных агентов (least-privilege); используйте as_root backdoor"
        return True, "ok"
        return True, ""

    # ---- единая PDP-цепочка ----
    def pdp(self, req: Dict[str, Any]) -> Tuple[bool, str]:
        agent = req.get("agent", "")
        project_id = req.get("project_id", "")
        what_for = req.get("what_for", "")
        port = req.get("port")
        timer = req.get("timer")  # dict {action, schedule} или None
        run_as = req.get("run_as")
        as_root = bool(req.get("as_root", False))

        # 9. Root backdoor — ТОЛЬКО для авторизованных агентов (Фаза 2, ADR-0055).
        # Обходит ВСЕ PDP-проверки, но строго аудируется как BYPASS=root.
        # Запрещён для неизвестных/неавторизованных агентов (закрывает дыру 5).
        if as_root:
            if not self.allow_root_backdoor:
                return False, "root backdoor отключён политикой (allow_root_backdoor=false)"
            if agent not in self.authorized_root_agents:
                return False, (
                    f"агент '{agent}' НЕ авторизован для root-bypass (as_root); "
                    f"разрешены только: {', '.join(self.authorized_root_agents) or '<никто>'}"
                )
            return True, "BYPASS=root"

        # 1. Identity
        ok, reason = self.check_identity(agent)
        if not ok:
            return False, reason

        # 8. Project-scoped lease — project_id обязателен
        if not project_id or not str(project_id).strip():
            return False, "project_id обязателен (project-scoped lease)"

        # 2-5. Порт (если есть)
        if port is not None:
            ok, reason = self.check_port_range(agent, int(port))
            if not ok:
                return False, reason
            ok, reason = self.check_reserve(int(port))
            if not ok:
                return False, reason
            ok, reason = self.check_quota(agent, "port")
            if not ok:
                return False, reason
            ok, reason = self.check_dedup_port(int(port), agent)
            if not ok:
                sug = self._suggest_free_port(agent)
                extra = f"; предлагаю свободный: {sug}" if sug else ""
                return False, reason + extra

        # таймер (если есть)
        if timer:
            action = timer.get("action")
            schedule = timer.get("schedule")
            if not action or not schedule:
                return False, "timer требует action и schedule"
            ok, reason = self.check_quota(agent, "timer")
            if not ok:
                return False, reason
            ok, reason = self.check_dedup_timer(action, schedule, agent)
            if not ok:
                return False, reason

        # 6. Justification
        ok, reason = self.check_justification(agent, what_for, port)
        if not ok:
            return False, reason

        # 7. Least-privilege
        ok, reason = self.check_least_privilege(run_as, agent=agent)
        if not ok:
            return False, reason

        return True, "OK"

    # ---- операции ----
    def _new_request_id(self) -> str:
        return "rk-" + uuid.uuid4().hex[:12]

    def _mk_lease(self, req: Dict[str, Any], kind: str, port, timer, bypass=None) -> Lease:
        now = time.time()
        return Lease(
            request_id=self._new_request_id(),
            agent=req["agent"],
            project_id=req["project_id"],
            kind=kind,
            port=port,
            timer_action=timer.get("action") if timer else None,
            timer_schedule=timer.get("schedule") if timer else None,
            what_for=str(req.get("what_for", "")).strip(),
            run_as=str(req.get("run_as") or self.lease_user),
            issued_user=self.lease_user,
            acquired_at=now,
            last_heartbeat=now,
            lease_timeout=self.lease_timeout,
            bypass=bypass,
            unit=req.get("unit"),
        )

    def register_port(self, agent, project_id, port, what_for, run_as=None, as_root=False, bypass_reason=None, unit=None) -> Dict[str, Any]:
        req = dict(agent=agent, project_id=project_id, what_for=what_for, port=port,
                   run_as=run_as, as_root=as_root, bypass_reason=bypass_reason, unit=unit)
        allow, reason = self.pdp(req)
        if not allow:
            self._audit("register_port", req, "REJECT", reason)
            return self._reject("register_port", req, reason)
        with self.lock:
            # Идемпотентность (ЗавЛаб 12.07): повторная регистрация того же порта
            # этим агентом (restart сервиса) обновляет lease, а не плодит новые
            # — иначе квота быстро исчерпалась бы на инфра-рестартах.
            for rid, l in list(self.leases.items()):
                if l.agent == agent and l.port == int(port):
                    self.store.delete(rid)
            lease = self._mk_lease(req, "port", int(port), None,
                                   bypass="root" if as_root and self.allow_root_backdoor else None)
            self.store.put(lease)
            self._save_state()
        self._audit("register_port", req, "ALLOW", reason, lease)
        return self._allow("register_port", lease)

    def register_timer(self, agent, project_id, action, schedule, what_for, run_as=None, as_root=False, bypass_reason=None, unit=None) -> Dict[str, Any]:
        req = dict(agent=agent, project_id=project_id, what_for=what_for,
                   timer=dict(action=action, schedule=schedule), run_as=run_as,
                   as_root=as_root, bypass_reason=bypass_reason, unit=unit)
        allow, reason = self.pdp(req)
        if not allow:
            self._audit("register_timer", req, "REJECT", reason)
            return self._reject("register_timer", req, reason)
        with self.lock:
            # Идемпотентность (Уровень 1-Г): повторная регистрация того же таймера
            # этим агентом (restart таймера) обновляет lease, а не плодит новые.
            for rid, l in list(self.leases.items()):
                if l.agent == agent and l.timer_action == action and l.timer_schedule == schedule:
                    self.store.delete(rid)
            lease = self._mk_lease(req, "timer", None, dict(action=action, schedule=schedule),
                                   bypass="root" if as_root and self.allow_root_backdoor else None)
            self.store.put(lease)
            self._save_state()
        self._audit("register_timer", req, "ALLOW", reason, lease)
        return self._allow("register_timer", lease)

    def register_service(self, agent, project_id, port, action, schedule, what_for, run_as=None, as_root=False, bypass_reason=None, unit=None) -> Dict[str, Any]:
        """Порт + таймер атомарно, один request_id. Если любая проверка падает — ничего не выдаём."""
        timer = dict(action=action, schedule=schedule)
        req = dict(agent=agent, project_id=project_id, what_for=what_for,
                   port=port, timer=timer, run_as=run_as, as_root=as_root,
                   bypass_reason=bypass_reason, unit=unit)
        allow, reason = self.pdp(req)
        if not allow:
            self._audit("register_service", req, "REJECT", reason)
            return self._reject("register_service", req, reason)
        with self.lock:
            # Идемпотентность: refresh того же порта этим агентом.
            for rid, l in list(self.leases.items()):
                if l.agent == agent and l.port == int(port):
                    self.store.delete(rid)
            lease = self._mk_lease(req, "service", int(port), timer,
                                   bypass="root" if as_root and self.allow_root_backdoor else None)
            self.store.put(lease)
            self._save_state()
        self._audit("register_service", req, "ALLOW", reason, lease)
        return self._allow("register_service", lease)

    def release(self, request_id: str, by_agent: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            lease = self.leases.get(request_id)
            if not lease:
                return {"status": "NOT_FOUND", "request_id": request_id,
                        "error": "lease не найден"}
            if by_agent and by_agent != lease.agent:
                return {"status": "FORBIDDEN", "request_id": request_id,
                        "error": f"только tenant '{lease.agent}' может освободить (вы: '{by_agent}')"}
            self.store.delete(request_id)
            self._save_state()
        self.journal(dict(request_id=request_id, when=datetime.now(timezone.utc).isoformat(),
                          what_for=lease.what_for, why="RELEASE", agent=lease.agent,
                          project=lease.project_id, action="release",
                          port=lease.port, timer=lease.timer_action, by=by_agent or lease.agent))
        log(f"release: {request_id} ({lease.kind}) агент={lease.agent} project={lease.project_id}")
        return {"status": "RELEASED", "request_id": request_id, "kind": lease.kind}

    def heartbeat(self, request_id: str) -> Dict[str, Any]:
        with self.lock:
            lease = self.leases.get(request_id)
            if not lease:
                return {"status": "NOT_FOUND", "request_id": request_id}
            lease.last_heartbeat = time.time()
            self.store.put(lease)
        # heartbeat аудируем облегчённо (rate-limit защищает диск)
        if _rate_limiter.allow():
            self.journal(dict(request_id=request_id, when=datetime.now(timezone.utc).isoformat(),
                              what_for=lease.what_for, why="HEARTBEAT", agent=lease.agent,
                              project=lease.project_id, action="heartbeat"))
        return {"status": "OK", "request_id": request_id, "last_heartbeat": lease.last_heartbeat}

    def transfer(self, request_id: str, to_agent: str, project_id: str, by_agent: Optional[str] = None) -> Dict[str, Any]:
        ok, reason = self.check_identity(to_agent)
        if not ok:
            return {"status": "REJECT", "request_id": request_id, "error": reason}
        with self.lock:
            lease = self.leases.get(request_id)
            if not lease:
                return {"status": "NOT_FOUND", "request_id": request_id}
            if by_agent and by_agent != lease.agent:
                return {"status": "FORBIDDEN", "request_id": request_id,
                        "error": f"handoff только текущим tenant '{lease.agent}'"}
            old_agent = lease.agent
            lease.agent = to_agent
            lease.project_id = project_id
            lease.last_heartbeat = time.time()
            self.store.put(lease)
        self.journal(dict(request_id=request_id, when=datetime.now(timezone.utc).isoformat(),
                          what_for=lease.what_for, why="HANDOFF", agent=f"{old_agent}->{to_agent}",
                          project=project_id, action="transfer", by=by_agent or old_agent))
        log(f"handoff: {request_id} {old_agent} -> {to_agent} (project={project_id})")
        return {"status": "TRANSFERRED", "request_id": request_id, "agent": to_agent,
                "project_id": project_id}

    def list_leases(self, agent: Optional[str] = None, project_id: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            items = [l.to_dict() for l in self.leases.values()
                     if (agent is None or l.agent == agent)
                     and (project_id is None or l.project_id == project_id)]
        return {"status": "OK", "count": len(items), "leases": items}

    # ---- Fact visibility (ADR-0056, Уровень 1-Б/В): read-only скан системы ----
    # Гейткипер НЕ хранит Fact (реальное состояние системы) — только Intent
    # (lease). Эти методы читают живую систему (systemd/ss) и возвращают Fact.
    # НЕ мутируют state. Запись observed в state — отдельная задача (Уровень 1-А/Г).
    def _run(self, cmd: List[str], timeout: float = 5.0):
        """Запустить команду, вернуть (rc, stdout). rc=None при ошибке/таймауте."""
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return r.returncode, r.stdout
        except Exception as exc:  # pragma: no cover - зависит от окружения
            return None, f"ERR:{exc}"

    def scan_units(self, kind: Optional[str] = None) -> Dict[str, Any]:
        units: List[Dict[str, Any]] = []
        errors: List[str] = []
        types = ("service", "socket", "timer") if kind is None else (kind,)
        for t in types:
            rc, out = self._run(["systemctl", "list-units", f"--type={t}",
                                 "--all", "--no-legend"], timeout=10)
            if rc is None:
                errors.append(f"systemctl {t}: {out}")
                continue
            if rc != 0:
                errors.append(f"systemctl list-units {t} rc={rc}")
                continue
            for line in out.splitlines():
                parts = line.split()
                if len(parts) < 4:
                    continue
                units.append({"unit": parts[0], "load": parts[1],
                              "active": parts[2], "sub": parts[3], "type": t})
        return {"units": units, "errors": errors}

    def scan_ports(self) -> Dict[str, Any]:
        ports: List[Dict[str, Any]] = []
        errors: List[str] = []
        for proto, name in (("-tlnp", "tcp"), ("-ulnp", "udp")):
            rc, out = self._run(["ss", proto], timeout=5)
            if rc is None:
                errors.append(f"ss {name}: {out}")
                continue
            if rc != 0:
                errors.append(f"ss {proto} rc={rc}")
                continue
            for line in out.splitlines()[1:]:
                parts = line.split()
                if len(parts) < 4:
                    continue
                local = parts[3]
                m = re.search(r":(\d+)$", local)
                if not m:
                    continue
                port = int(m.group(1))
                proc = " ".join(parts[5:]) if len(parts) > 5 else ""
                comm_m = re.search(r'users:\(\("([^"]+)"', proc)
                comm = comm_m.group(1) if comm_m else ""
                addr = local.rsplit(":", 1)[0]
                ports.append({"proto": name, "port": port, "addr": addr, "comm": comm})
        # дедуп по порту (tcp приоритетнее udp)
        seen: Dict[int, Dict[str, Any]] = {}
        for p in ports:
            seen.setdefault(p["port"], p)
        return {"ports": list(seen.values()), "errors": errors}

    def scan_timers(self) -> Dict[str, Any]:
        timers: List[Dict[str, Any]] = []
        errors: List[str] = []
        rc, out = self._run(["systemctl", "list-timers", "--all",
                             "--no-legend"], timeout=10)
        if rc is None:
            errors.append(f"systemctl list-timers: {out}")
        elif rc == 0:
            for line in out.splitlines():
                parts = line.split()
                if not parts:
                    continue
                unit = None
                activates = None
                for tok in parts:
                    if tok.endswith(".timer"):
                        unit = tok
                    elif tok.endswith(".service"):
                        activates = tok
                if unit:
                    timers.append({"unit": unit, "activates": activates})
        else:
            errors.append(f"systemctl list-timers rc={rc}")
        return {"timers": timers, "errors": errors}

    def scan_fact(self) -> Dict[str, Any]:
        u = self.scan_units()
        p = self.scan_ports()
        t = self.scan_timers()
        return {
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "units": u["units"], "units_errors": u["errors"],
            "ports": p["ports"], "ports_errors": p["errors"],
            "timers": t["timers"], "timers_errors": t["errors"],
            "counts": {"units": len(u["units"]),
                       "ports": len(p["ports"]),
                       "timers": len(t["timers"])}
        }

    def list_units(self, kind: Optional[str] = None) -> Dict[str, Any]:
        """Read-API: список systemd-юнитов (Fact, read-only)."""
        return self.scan_units(kind)

    def list_ports(self) -> Dict[str, Any]:
        """Read-API: список слушающих портов (Fact, read-only)."""
        return self.scan_ports()

    def list_timers(self) -> Dict[str, Any]:
        """Read-API: список systemd-таймеров (Fact, read-only)."""
        return self.scan_timers()

    def reconcile(self) -> Dict[str, Any]:
        """ADR-0056 Уровень 1-В: сверка Fact (система) vs Intent (lease). Read-only."""
        fact = self.scan_fact()
        with self.lock:
            leases = list(self.leases.values())
        intent_ports = {l.port for l in leases if l.port is not None}
        fact_ports = {p["port"] for p in fact["ports"]}
        # ФАКТ есть, ИНТЕНТа нет -> возможный обход gatekeeper (прямой bind / нет lease)
        unregistered = sorted(fact_ports - intent_ports)
        # ИНТЕНТ есть, ФАКТа нет -> stale lease (сервис не поднялся / уже нет)
        stale = sorted(intent_ports - fact_ports)
        return {
            "scanned_at": fact["scanned_at"],
            "intent": {"leases": len(leases), "ports": sorted(intent_ports)},
            "fact": {"ports_listening": sorted(fact_ports),
                     "units": fact["counts"]["units"],
                     "timers": fact["counts"]["timers"]},
            "drift": {
                "unregistered_listening_ports": unregistered,
                "stale_leased_ports_not_listening": stale,
            },
            "caveats": [
                "leases НЕ хранят имя systemd-юнита -> точное сопоставление "
                "unit<->lease невозможно (ROOT 8); точный unit-drift появится "
                "после добавления поля unit в Lease (ADR-0056 Уровень 1-А/Г).",
                "timers сопоставляются только по наличию .timer-юнитов; "
                "совпадение action<->unit не гарантировано.",
            ],
            "errors": fact["units_errors"] + fact["ports_errors"] + fact["timers_errors"],
        }

    # ---- Fact persistence (ADR-0056, Уровень 1-А): гейткипер хранит Fact ----
    # Не только намерения (lease), но и реальное состояние системы (снимки).
    def _fact_path(self) -> Path:
        return self.data_dir / "facts_latest.json"

    def _fact_history_path(self) -> Path:
        return self.data_dir / "facts_history.jsonl"

    def capture_fact(self) -> Dict[str, Any]:
        """Снять снимок Fact (реальная система) и сохранить в state гейткипера.

        Делает гейткипер хранителем Fact (SSOT), а не только намерений (lease).
        """
        fact = self.scan_fact()
        fact["captured_at"] = datetime.now(timezone.utc).isoformat()
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._fact_path().write_text(json.dumps(fact, ensure_ascii=False), encoding="utf-8")
            with open(self._fact_history_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps({"captured_at": fact["captured_at"],
                                    "counts": fact["counts"]}, ensure_ascii=False) + "\n")
        except Exception as exc:
            log(f"WARN: fact capture failed: {exc}", logging.WARNING)
            fact["error"] = str(exc)
        return fact

    def get_fact(self, history: bool = False) -> Dict[str, Any]:
        """Прочитать последний сохранённый снимок Fact (history=True → история)."""
        path = self._fact_history_path() if history else self._fact_path()
        if not path.exists():
            return {"status": "EMPTY", "path": str(path)}
        try:
            if history:
                rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
                return {"status": "OK", "path": str(path), "history": rows}
            return {"status": "OK", "path": str(path),
                    "fact": json.loads(path.read_text(encoding="utf-8"))}
        except Exception as exc:
            return {"status": "ERROR", "error": str(exc)}

    def reaper_tick(self) -> List[str]:
        """Освобождает lease, по которым не было heartbeat дольше lease_timeout."""
        now = time.time()
        released: List[str] = []
        with self.lock:
            for rid, l in list(self.leases.items()):
                if now - l.last_heartbeat > l.lease_timeout:
                    self.store.delete(rid)
                    released.append(rid)
                    self.journal(dict(request_id=rid, when=datetime.now(timezone.utc).isoformat(),
                                      what_for=l.what_for, why="LEASE_TIMEOUT", agent=l.agent,
                                      project=l.project_id, action="release",
                                      reason="heartbeat timeout", port=l.port, timer=l.timer_action))
            if released:
                self._save_state()
        for rid in released:
            log(f"reaper: освобождён по таймауту {rid}")
        return released

    # ---- помощники ответов/аудита ----
    def _allow(self, action: str, lease: Lease) -> Dict[str, Any]:
        return {
            "status": "ALLOW",
            "request_id": lease.request_id,
            "kind": lease.kind,
            "agent": lease.agent,
            "project_id": lease.project_id,
            "port": lease.port,
            "timer_action": lease.timer_action,
            "timer_schedule": lease.timer_schedule,
            "issued_user": lease.issued_user,
            "lease_timeout_sec": lease.lease_timeout,
            "bypass": lease.bypass,
        }

    def _reject(self, action: str, req: Dict[str, Any], reason: str) -> Dict[str, Any]:
        return {
            "status": "REJECT",
            "request_id": self._new_request_id(),
            "action": action,
            "agent": req.get("agent"),
            "project_id": req.get("project_id"),
            "error": reason,
        }

    def _audit(self, action: str, req: Dict[str, Any], decision: str, reason: str, lease: Optional[Lease] = None) -> None:
        ev = dict(
            request_id=(lease.request_id if lease else self._new_request_id()),
            when=datetime.now(timezone.utc).isoformat(),
            what_for=req.get("what_for"),
            why=f"{decision}:{reason}" + (" [BYPASS=root]" if req.get("as_root") and self.allow_root_backdoor else ""),
            agent=req.get("agent"),
            project=req.get("project_id"),
            action=action,
            port=req.get("port"),
            timer=(req.get("timer", {}).get("action") if req.get("timer") else None),
            decision=decision,
        )
        self.journal(ev)
        lvl = logging.INFO if decision == "ALLOW" else logging.WARNING
        log(f"{action}: {decision} agent={req.get('agent')} project={req.get('project_id')} :: {reason}", lvl)


# --------------------------------------------------------------------------- #
# Глобальный экземпляр (для MCP-инструментов). Импорт НЕ должен падать.
# --------------------------------------------------------------------------- #
def _load_policy_file(path: Path, fail_fast: bool) -> Dict[str, Any]:
    if not path.exists():
        msg = f"policy not found: {path}"
        if fail_fast:
            sys.stderr.write(f"mcp-gatekeeper: {msg}\n")
            sys.exit(1)
        log(f"WARN: {msg}; используется пустая политика", logging.WARNING)
        return {"agents": [], "quotas": {}, "reserve": {}, "gatekeeper": {}}
    try:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception as exc:
        if fail_fast:
            sys.stderr.write(f"mcp-gatekeeper: policy parse error: {exc}\n")
            sys.exit(1)
        log(f"WARN: policy parse error: {exc}", logging.WARNING)
        return {"agents": [], "quotas": {}, "reserve": {}, "gatekeeper": {}}


GK = Gatekeeper(
    _load_policy_file(Path(os.environ.get("GATEKEEPER_POLICY", DEFAULT_POLICY)), fail_fast=False),
    Path(os.environ.get("GATEKEEPER_DATA", DEFAULT_DATA)),
    fail_fast=False,
)


# --------------------------------------------------------------------------- #
# MCP-инструменты
# --------------------------------------------------------------------------- #
@mcp.tool()
def register_port(agent: str, project_id: str, port: int, what_for: str,
                  run_as: str = None, as_root: bool = False, bypass_reason: str = None, unit: str = None) -> Dict[str, Any]:
    """Зарегистрировать ТОЛЬКО порт через привратник.

    PDP-цепочка: Identity -> Диапазон портов -> Квота -> Резерв ->
    Дедуп -> Justification -> Least-privilege -> Project-scoped lease.
    root (as_root=True) обходит проверки, но аудируется как BYPASS=root.

    Args:
        agent: id агента (из политики, напр. 'raven').
        project_id: id проекта (обязателен, lease привязан к проекту).
        port: порт из пула агента.
        what_for: обоснование (justification, обязательно).
        run_as: под каким юзером планируется запуск (не root).
        as_root: root backdoor — обойти PDP (аудируется).
        bypass_reason: обоснование обхода (для аудита).
    """
    return GK.register_port(agent, project_id, port, what_for, run_as, as_root, bypass_reason, unit)


@mcp.tool()
def register_timer(agent: str, project_id: str, action: str, schedule: str, what_for: str,
                   run_as: str = None, as_root: bool = False, bypass_reason: str = None, unit: str = None) -> Dict[str, Any]:
    """Зарегистрировать ТОЛЬКО таймер через привратник.

    Args:
        agent: id агента.
        project_id: id проекта.
        action: что запускать.
        schedule: расписание (cron-подобная строка).
        what_for: обоснование (justification, обязательно).
        run_as / as_root / bypass_reason: см. register_port.
    """
    return GK.register_timer(agent, project_id, action, schedule, what_for, run_as, as_root, bypass_reason, unit)


@mcp.tool()
def register_service(agent: str, project_id: str, port: int, action: str, schedule: str,
                     what_for: str, run_as: str = None, as_root: bool = False,
                     bypass_reason: str = None, unit: str = None) -> Dict[str, Any]:
    """Зарегистрировать порт + таймер АТОМАРНО (один request_id).

    Если любая PDP-проверка для порта ИЛИ таймера падает — ничего не выдаётся.
    """
    return GK.register_service(agent, project_id, port, action, schedule, what_for, run_as, as_root, bypass_reason, unit)


@mcp.tool()
def release_resource(request_id: str, by_agent: str = None) -> Dict[str, Any]:
    """Освободить ресурс по request_id (tenant или явный by_agent)."""
    return GK.release(request_id, by_agent)


@mcp.tool()
def heartbeat(request_id: str) -> Dict[str, Any]:
    """Продлить lease (heartbeat). Сбрасывает таймаут освобождения."""
    return GK.heartbeat(request_id)


@mcp.tool()
def transfer_lease(request_id: str, to_agent: str, project_id: str, by_agent: str = None) -> Dict[str, Any]:
    """Handoff lease между агентами (project-scoped). Только текущий tenant."""
    return GK.transfer(request_id, to_agent, project_id, by_agent)


@mcp.tool()
def list_leases(agent: str = None, project_id: str = None) -> Dict[str, Any]:
    """Список активных lease (фильтр по agent/project_id)."""
    return GK.list_leases(agent, project_id)


@mcp.tool()
def check_health() -> Dict[str, Any]:
    """Здоровье привратника: версия, PDP-счётчики, сводка политики."""
    with GK.lock:
        ports = sum(1 for l in GK.leases.values() if l.port is not None)
        timers = sum(1 for l in GK.leases.values() if l.timer_action is not None)
    return {
        "status": "OK",
        "version": GATEKEEPER_VERSION,
        "agents_known": len(GK.agents),
        "active_leases": len(GK.leases),
        "active_ports": ports,
        "active_timers": timers,
        "justification_mode": GK.justification_mode,
        "allow_root_backdoor": GK.allow_root_backdoor,
        "lease_user": GK.lease_user,
    }


@mcp.tool()
def list_units(kind: str = None) -> Dict[str, Any]:
    """Список systemd-юнитов (Fact, read-only): service/socket/timer.

    kind=None -> все три типа. Не мутирует state гейткипера.
    """
    return GK.scan_units(kind)


@mcp.tool()
def list_ports() -> Dict[str, Any]:
    """Список слушающих портов (Fact, read-only) из ss -tlnp/-ulnp.

    Возвращает proto/port/addr/comm. Не мутирует state.
    """
    return GK.scan_ports()


@mcp.tool()
def list_timers() -> Dict[str, Any]:
    """Список systemd-таймеров (Fact, read-only) из systemctl list-timers.

    Не мутирует state.
    """
    return GK.scan_timers()


@mcp.tool()
def reconcile() -> Dict[str, Any]:
    """Сверка Fact (реальная система) vs Intent (lease): дрифт портов/юнитов/таймеров.

    Read-only. Показывает порты, что слушают, но НЕ в lease
    (unregistered_listening_ports — возможный обход gatekeeper), и lease-порты,
    которые НЕ слушают (stale_leased_ports_not_listening — stale/waiting).
    """
    return GK.reconcile()


@mcp.tool()
def capture_fact() -> Dict[str, Any]:
    """Снять снимок реального состояния системы (Fact) и сохранить в state гейткипера (SSOT)."""
    return GK.capture_fact()


@mcp.tool()
def get_fact(history: bool = False) -> Dict[str, Any]:
    """Прочитать последний сохранённый снимок Fact (history=True → история captures)."""
    return GK.get_fact(history)


# --------------------------------------------------------------------------- #
# Фоновые потоки: reaper (lease timeout) — сервер НЕ шлёт heartbeat/watchdog.
# «Жив ли сервер» доказывается ответом на реальный запрос агента (событийно).
# --------------------------------------------------------------------------- #
def _reaper_loop() -> None:
    while not _STOP.is_set():
        try:
            GK.reaper_tick()
        except Exception as exc:
            log(f"WARN: reaper error: {exc}", logging.WARNING)
        _STOP.wait(max(5.0, min(GK.lease_timeout / 10.0, 60.0)))


def _fact_capture_loop() -> None:
    # ADR-0056 Уровень 1-А: непрерывно запоминаем реальное состояние системы (SSOT).
    while not _STOP.is_set():
        try:
            GK.capture_fact()
        except Exception as exc:  # pragma: no cover - зависит от окружения
            log(f"WARN: fact capture error: {exc}", logging.WARNING)
        _STOP.wait(max(30.0, GK.fact_capture_interval))


def _install_signal_handlers() -> None:
    import signal

    def _handler(signum, frame):
        log(f"signal {signum}, shutting down", logging.INFO)
        _STOP.set()
        sd_notify("STOPPING=1")

    try:
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# CLI-режим (для register-port-timer.sh и тестов)
# --------------------------------------------------------------------------- #
def _cli() -> int:
    ap = argparse.ArgumentParser(prog="mcp-gatekeeper-server", description="MCP Gatekeeper CLI")
    ap.add_argument("--policy", default=os.environ.get("GATEKEEPER_POLICY", str(DEFAULT_POLICY)))
    ap.add_argument("--data", default=os.environ.get("GATEKEEPER_DATA", str(DEFAULT_DATA)))
    sub = ap.add_subparsers(dest="cmd", required=True)

    def _add_common(p):
        p.add_argument("--agent", required=True)
        p.add_argument("--project", required=True)
        p.add_argument("--what-for", required=True)
        p.add_argument("--run-as", default=None)
        p.add_argument("--as-root", action="store_true")
        p.add_argument("--bypass-reason", default=None)
        p.add_argument("--unit", default=None)

    p = sub.add_parser("register-port")
    _add_common(p); p.add_argument("--port", type=int, required=True)
    p = sub.add_parser("register-timer")
    _add_common(p); p.add_argument("--action", required=True); p.add_argument("--schedule", required=True)
    p = sub.add_parser("register-service")
    _add_common(p); p.add_argument("--port", type=int, required=True)
    p.add_argument("--action", required=True); p.add_argument("--schedule", required=True)
    p = sub.add_parser("release"); p.add_argument("--request-id", required=True); p.add_argument("--by-agent", default=None)
    p = sub.add_parser("heartbeat"); p.add_argument("--request-id", required=True)
    p = sub.add_parser("transfer"); p.add_argument("--request-id", required=True)
    p.add_argument("--to-agent", required=True); p.add_argument("--project", required=True); p.add_argument("--by-agent", default=None)
    p = sub.add_parser("list"); p.add_argument("--agent", default=None); p.add_argument("--project", default=None)
    sub.add_parser("health")
    sub.add_parser("fact")
    sub.add_parser("reconcile")
    sub.add_parser("capture-fact")
    sub.add_parser("get-fact")

    args = ap.parse_args()
    policy = _load_policy_file(Path(args.policy), fail_fast=True)
    gk = Gatekeeper(policy, Path(args.data), fail_fast=True)
    errs = gk.validate_policy()
    if errs:
        sys.stderr.write("policy validation FAILED:\n" + "\n".join(f"  - {e}" for e in errs) + "\n")
        return 1

    if args.cmd == "register-port":
        out = gk.register_port(args.agent, args.project, args.port, args.what_for, args.run_as, args.as_root, args.bypass_reason, args.unit)
    elif args.cmd == "register-timer":
        out = gk.register_timer(args.agent, args.project, args.action, args.schedule, args.what_for, args.run_as, args.as_root, args.bypass_reason, args.unit)
    elif args.cmd == "register-service":
        out = gk.register_service(args.agent, args.project, args.port, args.action, args.schedule, args.what_for, args.run_as, args.as_root, args.bypass_reason, args.unit)
    elif args.cmd == "release":
        out = gk.release(args.request_id, args.by_agent)
    elif args.cmd == "heartbeat":
        out = gk.heartbeat(args.request_id)
    elif args.cmd == "transfer":
        out = gk.transfer(args.request_id, args.to_agent, args.project, args.by_agent)
    elif args.cmd == "list":
        out = gk.list_leases(args.agent, args.project)
    elif args.cmd == "fact":
        out = gk.scan_fact()
    elif args.cmd == "reconcile":
        out = gk.reconcile()
    elif args.cmd == "capture-fact":
        out = gk.capture_fact()
    elif args.cmd == "get-fact":
        out = gk.get_fact()
    else:
        out = gk.check_health()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("status") in ("ALLOW", "OK", "RELEASED", "TRANSFERRED", None) else 2


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #
def main() -> None:
    # Ручной разбор --policy/--data: server-mode раньше игнорировал эти
    # флаги (грузил default), из-за чего test_fail_fast_on_bad_policy не
    # срабатывал. Если указаны — пересоздаём глобальный GK с fail_fast,
    # чтобы validate_policy() в server-mode видел именно эту политику.
    _policy_path = os.environ.get("GATEKEEPER_POLICY", DEFAULT_POLICY)
    _data_path = os.environ.get("GATEKEEPER_DATA", DEFAULT_DATA)
    _args = sys.argv[1:]
    for _i, _a in enumerate(_args):
        if _a == "--policy" and _i + 1 < len(_args):
            _policy_path = _args[_i + 1]
        elif _a == "--data" and _i + 1 < len(_args):
            _data_path = _args[_i + 1]
    global GK
    if _policy_path != os.environ.get("GATEKEEPER_POLICY", DEFAULT_POLICY) or \
       _data_path != os.environ.get("GATEKEEPER_DATA", DEFAULT_DATA):
        GK = Gatekeeper(
            _load_policy_file(Path(_policy_path), fail_fast=True),
            Path(_data_path),
            fail_fast=True,
        )

    # CLI-режим при явных аргументах
    if len(sys.argv) > 1 and sys.argv[1] in (
        "register-port", "register-timer", "register-service",
        "release", "heartbeat", "transfer", "list", "health",
        "fact", "reconcile", "capture-fact", "get-fact",
    ):
        sys.exit(_cli())

    # Fail-fast валидация политики при старте (systemd Restart=on-failure)
    errs = GK.validate_policy()
    if errs:
        sys.stderr.write("mcp-gatekeeper: policy validation FAILED:\n" + "\n".join(f"  - {e}" for e in errs) + "\n")
        sys.exit(1)

    _install_signal_handlers()
    threading.Thread(target=_reaper_loop, name="reaper", daemon=True).start()
    threading.Thread(target=_fact_capture_loop, name="fact-capture", daemon=True).start()

    transport = os.environ.get("MCP_TRANSPORT", "http").lower()
    log(f"mcp-gatekeeper {GATEKEEPER_VERSION} starting ({transport}), policy={DEFAULT_POLICY}", logging.INFO)
    sd_notify("READY=1")  # one-shot: уведомляем systemd о готовности (Type=simple игнорирует)
    if transport == "http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
    _STOP.set()


if __name__ == "__main__":
    main()
