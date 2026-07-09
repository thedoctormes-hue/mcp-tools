# apikeys-server — MCP Server for Free API Keys

**Версия:** 0.3.0
**Статус:** stable
**Автор:** kotolizator

## Описание

MCP сервер для доступа к бесплатным API ключам из free-api-hunter. Предоставляет инструменты для получения ключей, документации и проверки доступности провайдеров.

## Установка

```bash
# Сервер уже установлен как systemd unit
sudo systemctl daemon-reload
sudo systemctl enable --now mcp-apikeys
```

Сервер поддерживает два режима транспорта, выбираемых переменной окружения `MCP_TRANSPORT`:

- `MCP_TRANSPORT=http` — streamable-http (режим по умолчанию под systemd)
- `MCP_TRANSPORT=stdio` — stdio для локального запуска без systemd

Под systemd сервер запускается в HTTP-режиме на `127.0.0.1:8086`.

## Интеграция с mcporter

Сервер под systemd доступен по HTTP. Добавить в mcporter:

```bash
mcporter config add apikeys http http://127.0.0.1:8086/mcp
```

Локальный запуск в stdio-режиме (без systemd):

```bash
MCP_TRANSPORT=stdio python3 /root/LabDoctorM/projects/mcp-tools/bin/apikeys-server.py
```

## Доступные инструменты

### get_key(provider: str)

Получить API ключ и метаданные для провайдера.

**Параметры:**
- `provider` (string): Название провайдера (cerebras, cloudflare, gemini, cohere, mistral, elevenlabs, pollinations, ocr-space)

**Возвращает:**
```json
{
  "provider": "cerebras",
  "api_key": "csk-...",
  "masked_key": "csk-****...xyz",
  "status": "verified",
  "base_url": "https://api.cerebras.ai/v1",
  "auth_type": "bearer",
  "models": ["gpt-oss-120b", "zai-glm-4.7"],
  "note": ""
}
```

### list_providers()

Получить список всех доступных провайдеров.

**Возвращает:**
```json
{
  "total": 21,
  "providers": [
    {"name": "Cerebras", "status": "verified", "models_count": 2},
    {"name": "Pollinations", "status": "verified", "models_count": 23},
    ...
  ]
}
```

### get_provider_docs(provider: str)

Получить подробную документацию для провайдера.

### check_health(provider: str)

Проверить доступность API провайдера.

## Поддерживаемые провайдеры

| Провайдер | Статус | Модели | Примечания |
|-----------|--------|--------|------------|
| Cerebras | verified | gpt-oss-120b, zai-glm-4.7 | 8K context, 30 RPM |
| Cloudflare | verified | @cf/* | Edge inference |
| Pollinations | verified | 23 free | OpenAI-compatible |
| Mistral | verified | mistral-* | 1 RPS, 500K TPM |
| Cohere | blocked | command-* | 1000 calls/month |
| Gemini | rate_limited | gemini-* | EU blocked |
| OCR.space | verified | 3 engines | 25K req/month |
| ElevenLabs | verified | tts models | 10K chars/month |

## Безопасность

- Сервер предоставляет **только чтение** ключей
- Ключи **маскируются** в логах (first/last 4 символа)
- Доступ только к провайдерам в `ALLOWED_PROVIDERS`
- Ключи хранятся в `vault/free-api-hunter/` (только для чтения)

## Архитектура

```
free-api-hunter vault/
├── cerebras/api.keys          (5 ключей)
├── cloudflare/api.key          (1 ключ)
├── cohere/api.key, .2, .3, .4 (4 ключа)
├── elevenlabs/api.key.*       (7 ключей)
├── manus/api.key               (1 ключ)
├── mistral/api_key_*.key      (2 ключа)
├── ocr-space/api.key          (1 ключ)
└── pollinations/api.key        (1 ключ)
```

## Интеграция с OpenClaw

Агенты используют mcporter для вызова сервера:

```python
# В skill-матрице для new-feature/research
tools = ["apikeys.get_key", "apikeys.list_providers", "apikeys.check_health"]
```

## Логи

```bash
# Просмотр логов
journalctl -u mcp-apikeys -f

# Проверка статуса
systemctl status mcp-apikeys
```

## Источники

- `/root/LabDoctorM/projects/free-api-hunter/data/providers.json`
- `/root/LabDoctorM/vault/free-api-hunter/`
