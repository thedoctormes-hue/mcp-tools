# PORT_REGISTRY.md — AUTO-GENERATED (Уровень Е ADR-0056)

> **НЕ РЕДАКТИРУЙ ВРУЧНУЮ.** Этот файл генерируется
> `scripts/gen-port-registry.sh` из `policies/policy_v1.yaml`.
> Единственный источник правды — policy (reserve.blocked_ports + listen_port
> + block_privileged_below). Правки вноси в policy, затем перегенерируй.

## Роль

Read-only вид зарезервированных/ожидаемых портов для аудита
(`audit/gk-audit.sh`). Любой слушающий порт, которого НЕТ ни здесь,
ни в активных lease (gatekeeper), ни среди системных (< `block_privileged_below`
= 1024) — считается несанкционированным и генерит АЛЕРТ.

Агентские порты (8080–8099 и т.п.) разрешены ТОЛЬКО при наличии
активного lease в gatekeeper — они НЕ пре-разрешены здесь.

## Реестр (из policy)

| Port | Service | Notes |
|------|---------|-------|
| 80 | reserved/infra |  |
| 443 | https (infra) |  |
| 2222 | reserved/infra |  |
| 3000 | grafana (infra) |  |
| 5432 | PostgreSQL (infra) |  |
| 6379 | Redis (infra) |  |
| 8001 | reserved (infra) |  |
| 8086 | mcp-apikeys (infra) |  |
| 8087 | mcp-memory (infra) |  |
| 8200 | reserved (infra) |  |
| 8202 | reserved (infra) |  |
| 8300 | reserved (infra) |  |
| 8443 | reserved (infra) |  |
| 8444 | reserved (infra) |  |
| 8445 | reserved (infra) |  |
| 8888 | mcp-gatekeeper (listen_port) | собственный порт PDP (listen_port) |
| 8889 | reserved (infra) |  |
| 8899 | reserved (infra) |  |
| 9090 | Prometheus (infra) |  |
| 9100 | node_exporter (infra) |  |
| 9187 | postgres_exporter (infra) |  |
| 9443 | reserved (infra) |  |
| 10443 | reserved (infra) |  |
| 18789 | reserved (infra) |  |
| 36401 | reserved (infra) |  |

## Как обновить

1. Правь `reserve.blocked_ports` / `listen_port` / `block_privileged_below`
   в `policies/policy_v1.yaml`.
2. Перегенерируй: `bash scripts/gen-port-registry.sh`.
3. Закоммить оба файла через `lab-commit.sh`.
