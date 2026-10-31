#!/usr/bin/env python
"""Real-call acceptance verification (12.2): 7 items, PASS/FAIL + evidence.

Runs against a LIVE gateway started with config/config.verify.yaml:
    GATEWAY_CONFIG=config/config.verify.yaml uvicorn gateway.main:app --port 8000 &

Key material comes only from .env (DEEPSEEK_API_KEY / GATEWAY_API_KEY) via the
gateway process; this script authenticates with GATEWAY_API_KEY. Item 7 spawns
its own in-process gateway variant with a tiny rate limit. Every item prints
PASS/FAIL with key data (request ids, tokens, latency, chunk counts...); any
FAIL exits non-zero. Full output is saved to out/verify_real_<ts>.txt.

--dry-run validates structure/config/.env without any network calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

RESULTS: list[tuple[str, bool, str]] = []


def report(item: str, ok: bool, detail: str) -> bool:
    RESULTS.append((item, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {item}: {detail}")
    return ok


def main() -> int:
    load_dotenv(ROOT / ".env")
    dry_run = "--dry-run" in sys.argv
    base_url = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000")
    key = os.environ.get("GATEWAY_API_KEY", "")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    if not key:
        print("[FAIL] precheck: GATEWAY_API_KEY missing in .env")
        return 1
    if not dry_run and not os.environ.get("DEEPSEEK_API_KEY"):
        print("[FAIL] precheck: DEEPSEEK_API_KEY missing in .env")
        return 1

    # config sanity (no network)
    os.environ.setdefault("GATEWAY_CONFIG", str(ROOT / "config" / "config.verify.yaml"))
    from gateway.config import load_config

    try:
        cfg = load_config(os.environ["GATEWAY_CONFIG"])
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] precheck: verify config invalid: {exc}")
        return 1
    print(f"config OK: providers={sorted(cfg.providers)} aliases={sorted(cfg.aliases)}")
    if dry_run:
        print("dry-run: structure OK, no requests sent")
        return 0

    client = httpx.Client(base_url=base_url, headers=headers, timeout=180)

    def latest_usage(request_id: str | None = None) -> dict:
        rows = client.get("/admin/usage?limit=5").json()["records"]
        if request_id:
            for row in rows:
                if row["request_id"] == request_id:
                    return row
        return rows[0]

    # ---------------------------------------------------------------- item 1
    try:
        ok1 = True
        for alias, protocol in (("deepseek-pro", "openai_responses"), ("deepseek-flash", "anthropic_messages")):
            r = client.post(
                "/v1/chat/completions",
                json={"model": alias, "messages": [{"role": "user", "content": "用一个词回答：1+1=?"}]},
            )
            rid = r.headers.get("x-request-id", "")
            if r.status_code != 200:
                ok1 = report(f"1 两模型可调用({alias})", False, f"HTTP {r.status_code}: {r.text[:200]}")
                continue
            content = r.json()["choices"][0]["message"]["content"]
            row = latest_usage(rid)
            ok1 &= report(
                f"1 两模型可调用({alias})",
                bool(content.strip()) and row.get("protocol") == protocol,
                f"request_id={rid} content={content[:40]!r} "
                f"protocol={row.get('protocol')} provider={row.get('provider')}",
            )
    except Exception as exc:  # noqa: BLE001
        ok1 = report("1 两模型可调用", False, f"error: {exc}")

    # ---------------------------------------------------------------- item 2
    for alias in ("deepseek-pro", "deepseek-flash"):
        try:
            chunks, first_chunk = [], None
            with client.stream(
                "POST", "/v1/chat/completions",
                json={"model": alias, "messages": [{"role": "user", "content": "从1数到5"}], "stream": True},
            ) as r:
                rid = r.headers.get("x-request-id", "")
                if r.status_code != 200:
                    report(f"2 流式({alias})", False, f"HTTP {r.status_code}")
                    continue
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        continue
                    data = json.loads(payload)
                    delta = data.get("choices") and data["choices"][0].get("delta", {}).get("content")
                    if delta:
                        chunks.append(delta)
                        first_chunk = first_chunk or delta
            row = latest_usage(rid)
            report(
                f"2 流式({alias})",
                len(chunks) >= 1 and row.get("ttft_ms") is not None,
                f"request_id={rid} 块数={len(chunks)} 首块={first_chunk!r} TTFT={row.get('ttft_ms')}ms",
            )
        except Exception as exc:  # noqa: BLE001
            report(f"2 流式({alias})", False, f"error: {exc}")

    # ---------------------------------------------------------------- item 3
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    import jsonschema

    for alias in ("deepseek-pro", "deepseek-flash"):
        try:
            r = client.post(
                "/v1/chat/completions",
                json={
                    "model": alias,
                    "messages": [{"role": "user", "content": "回答：天空是什么颜色"}],
                    "response_format": {"type": "json_schema", "json_schema": {"name": "ans", "schema": schema}},
                },
            )
            if r.status_code != 200:
                report(f"3 结构化输出({alias})", False, f"HTTP {r.status_code}: {r.text[:200]}")
                continue
            parsed = json.loads(r.json()["choices"][0]["message"]["content"])
            jsonschema.validate(parsed, schema)
            report(f"3 结构化输出({alias})", True, f"合法JSON且过Schema: {json.dumps(parsed, ensure_ascii=False)[:60]}")
        except Exception as exc:  # noqa: BLE001
            report(f"3 结构化输出({alias})", False, f"error: {exc}")

    # ---------------------------------------------------------------- item 4
    try:
        pid = f"verify-{int(time.time())}"
        created = client.post("/v1/prompts", json={
            "prompt_id": pid, "template": "你是翻译助手，把输入译成{{lang}}，只输出译文。"}).json()
        preview = client.post(f"/v1/prompts/{pid}/render", json={"variables": {"lang": "英文"}}).json()
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "deepseek-pro",
                "messages": [{"role": "user", "content": "你好，世界"}],
                "prompt_ref": {"id": pid, "variables": {"lang": "英文"}},
            },
        )
        rid = r.headers.get("x-request-id", "")
        row = latest_usage(rid)
        ok = (
            r.status_code == 200
            and created.get("version") == 1
            and "英文" in preview.get("rendered", "")
            and row.get("prompt_id") == pid
            and row.get("prompt_version") == 1
        )
        report(
            "4 模板引用",
            ok,
            f"prompt={pid} v{row.get('prompt_version')} 预览={preview.get('rendered', '')[:30]!r} "
            f"译文={r.json()['choices'][0]['message']['content'][:30]!r}",
        )
    except Exception as exc:  # noqa: BLE001
        report("4 模板引用", False, f"error: {exc}")

    # ---------------------------------------------------------------- item 5
    try:
        row = latest_usage()
        ok = (
            row.get("input_tokens") is not None
            and row.get("output_tokens") is not None
            and row.get("cache_tokens") is not None
            and row.get("total_latency_ms") is not None
            and row.get("protocol")
            and row.get("provider")
        )
        report(
            "5 可观测数据",
            ok,
            f"tokens(in/out/cache)=({row.get('input_tokens')}/{row.get('output_tokens')}/{row.get('cache_tokens')}) "
            f"latency={row.get('total_latency_ms')}ms protocol={row.get('protocol')} provider={row.get('provider')}",
        )
    except Exception as exc:  # noqa: BLE001
        report("5 可观测数据", False, f"error: {exc}")

    # ---------------------------------------------------------------- item 6
    try:
        r = client.post(
            "/v1/chat/completions",
            json={"model": "deepseek-pro-retry",
                  "messages": [{"role": "user", "content": "用一个词回答：2+2=?"}]},
        )
        rid = r.headers.get("x-request-id", "")
        row = latest_usage(rid)
        ok = r.status_code == 200 and (row.get("retries", 0) > 0 or row.get("failovers", 0) > 0)
        report(
            "6 重试与故障转移",
            ok,
            f"HTTP {r.status_code} retries={row.get('retries')} failovers={row.get('failovers')} "
            f"final_provider={row.get('provider')}",
        )
    except Exception as exc:  # noqa: BLE001
        report("6 重试与故障转移", False, f"error: {exc}")

    # ---------------------------------------------------------------- item 7
    try:
        asyncio.run(_rate_limit_probe(ROOT))
    except Exception as exc:  # noqa: BLE001
        report("7 限流", False, f"error: {exc}")

    client.close()
    failed = [name for name, ok, _ in RESULTS if not ok]
    print("-" * 60)
    print(f"TOTAL: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


async def _rate_limit_probe(root: Path) -> bool:
    """Spawn an in-process gateway with a tiny rate limit; 3rd burst call must 429
    with the unified OpenAI-style error body."""
    from gateway.config import RateLimitCfg, load_config
    from gateway.main import create_app

    cfg = load_config(str(root / "config" / "config.verify.yaml"))
    cfg = cfg.model_copy(
        update={
            "rate_limit": RateLimitCfg(requests_per_minute=2, burst=2),
            "database": cfg.database.model_copy(update={"path": str(root / "data" / "gateway-rl.db")}),
        }
    )
    app = create_app(cfg)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {os.environ['GATEWAY_API_KEY']}"},
        timeout=180,
    ) as probe:
        codes, third_body = [], None
        for i in range(3):
            r = await probe.post("/v1/chat/completions", json={
                "model": "deepseek-flash", "messages": [{"role": "user", "content": "hi"}]})
            codes.append(r.status_code)
            if i == 2:
                third_body = r.json()
        shape_ok = isinstance(third_body, dict) and set(third_body.get("error", {})) == {
            "message", "type", "param", "code"}
        ok = codes[:2] == [200, 200] and codes[2] == 429 and shape_ok
        report("7 限流", ok, f"连发三次状态码={codes}（第3次应429且错误体统一）")
        return ok


if __name__ == "__main__":
    out_dir = ROOT / "out"
    out_dir.mkdir(exist_ok=True)
    log_path = out_dir / f"verify_real_{time.strftime('%Y%m%d_%H%M%S')}.txt"

    class _Tee:
        def __init__(self, path: Path) -> None:
            self.file = path.open("w", encoding="utf-8")

        def write(self, data: str) -> int:
            self.file.write(data)
            return len(data)

        def flush(self) -> None:
            self.file.flush()

    _stdout = sys.stdout
    sys.stdout = _Tee(log_path)  # type: ignore[assignment]
    try:
        code = main()
    finally:
        sys.stdout.flush()
        sys.stdout = _stdout  # type: ignore[assignment]
    # replay the captured log to the console
    print(log_path.read_text(encoding="utf-8"), end="")
    print(f"evidence saved to: {log_path}")
    raise SystemExit(code)
