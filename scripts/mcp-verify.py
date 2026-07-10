#!/usr/bin/env python3
"""
mcp-verify.py — reusable MCP Streamable-HTTP клиент для верификации серверов.

Правильно агрегирует SSE (собирает все `data:` строки в один JSON-RPC ответ).
Использование:
  python3 mcp-verify.py --url http://127.0.0.1:8087/mcp --tool lab_memory_search \
      --args '{"query":"compaction","top_k":3}'
  python3 mcp-verify.py --url http://127.0.0.1:8087/mcp --list-tools
"""
import argparse
import json
import urllib.request


def _rpc(url, method, params=None, sid=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params or {}}).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if sid:
        headers["Mcp-Session-Id"] = sid
    req = urllib.request.Request(url, data=body, headers=headers)
    resp = urllib.request.urlopen(req, timeout=30)
    new_sid = resp.headers.get("Mcp-Session-Id", sid)
    raw = resp.read().decode()

    # Корректная агрегация SSE: собираем все data: строки в один JSON
    data_chunks = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            data_chunks.append(line[len("data:"):].strip())
    if data_chunks:
        # берём последний JSON-RPC сообщение (ответ на наш вызов)
        for chunk in reversed(data_chunks):
            try:
                return json.loads(chunk), new_sid
            except json.JSONDecodeError:
                continue
    # если не SSE, а чистый JSON
    try:
        return json.loads(raw), new_sid
    except json.JSONDecodeError:
        return {"error": "cannot parse response", "raw": raw[:500]}, new_sid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--tool", default=None)
    ap.add_argument("--args", default="{}")
    ap.add_argument("--list-tools", action="store_true")
    args = ap.parse_args()

    # 1. initialize
    r, sid = _rpc(args.url, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "mcp-verify", "version": "1.0"},
    })
    # 2. notifications/initialized (ignore response)
    try:
        _rpc(args.url, "notifications/initialized", {}, sid)
    except Exception:
        pass

    if args.list_tools:
        r, _ = _rpc(args.url, "tools/list", {}, sid)
        tools = r.get("result", {}).get("tools", [])
        print(f"Tools ({len(tools)}):")
        for t in tools:
            print(f"  - {t['name']}: {(t.get('description') or '')[:70]}")
        return

    if not args.tool:
        print("Specify --tool or --list-tools")
        return

    # 3. tools/call
    params = {"name": args.tool, "arguments": json.loads(args.args)}
    r, _ = _rpc(args.url, "tools/call", params, sid)
    res = r.get("result", {})
    if "content" in res:
        # FastMCP кладёт результат в content[0].text как JSON-строку
        text = res["content"][0].get("text", "")
        try:
            parsed = json.loads(text)
            print(json.dumps(parsed, ensure_ascii=False, indent=2))
        except json.JSONDecodeError:
            print(text)
    else:
        print(json.dumps(r, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
