#!/usr/bin/env python3
"""Entrypoint memory-gateway: `python3 run.py` (или `python3 -m memory_gateway.server`).

Добавляет каталог проекта в sys.path, чтобы пакет memory_gateway импортировался
как при запуске из конфига агента (stdio), так и из systemd (http).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from memory_gateway.server import main  # noqa: E402

if __name__ == "__main__":
    main()
