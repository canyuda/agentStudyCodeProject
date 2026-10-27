import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from dotenv import load_dotenv
load_dotenv()

EventType = Literal[
    "run.started",
    "text.delta",
    "run.completed",
    "run.failed",
    "run.cancelled",
]


@dataclass(frozen=True)
class ModelStreamEvent:
    # 5.2 Adapter 层统一供应商事件，Agent Loop 不直接依赖 DeepSeek chunk 结构。
    type: str
    data: dict[str, Any]


class StreamingModel(Protocol):
    async def stream(
        self,
        messages: list[dict[str, str]],
    ) -> AsyncIterator[ModelStreamEvent]: ...


class DeepSeekChatAdapter:
    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self.client = client
        self.model = model

    async def stream(
        self,
        messages: list[dict[str, str]],
    ) -> AsyncIterator[ModelStreamEvent]:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            max_tokens=1024,
            stream_options={"include_usage": True},
            extra_body={"thinking": {"type": "disabled"}},
        )

        async for chunk in response:
            choice = chunk.choices[0] if chunk.choices else None

            if choice and choice.delta.content:
                yield ModelStreamEvent(
                    type="text.delta",
                    data={"delta": choice.delta.content},
                )

            if choice and choice.finish_reason:
                yield ModelStreamEvent(
                    type="model.finished",
                    data={"finish_reason": choice.finish_reason},
                )

            if chunk.usage is not None:
                yield ModelStreamEvent(
                    type="model.usage",
                    data={
                        "input_tokens": chunk.usage.prompt_tokens,
                        "output_tokens": chunk.usage.completion_tokens,
                        "total_tokens": chunk.usage.total_tokens,
                    },
                )


@dataclass(frozen=True)
class RunEvent:
    run_id: str
    seq: int
    type: EventType
    data: dict[str, Any]



@dataclass
class RunState:
    run_id: str
    status: str = "created"
    next_seq: int = 0
    events: list[RunEvent] = field(default_factory=list)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def append(self, event_type: EventType, data: dict[str, Any]) -> RunEvent:
        async with self.changed:
            event = RunEvent(
                run_id=self.run_id,
                seq=self.next_seq,
                type=event_type,
                data=data,
            )
            self.next_seq += 1
            self.events.append(event)
            self.changed.notify_all()
            return event


RUNS: dict[str, RunState] = {}
RUN_TASKS: dict[str, asyncio.Task[None]] = {}

MODEL_NAME = "deepseek-v4-flash"
SYSTEM_PROMPT = "你是代码审查助手，只报告有证据的问题。"
_model_instance: StreamingModel | None = None


def get_model() -> StreamingModel:
    global _model_instance

    if _model_instance is not None:
        return _model_instance

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("请先设置环境变量 DEEPSEEK_API_KEY")

    client = AsyncOpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        timeout=60.0,
        max_retries=0,
    )
    _model_instance = DeepSeekChatAdapter(client=client, model=MODEL_NAME)
    return _model_instance


async def execute_run(
    state: RunState,
    model: StreamingModel,
    messages: list[dict[str, str]],
) -> None:
    state.status = "running"
    await state.append("run.started", {"status": state.status})

    text_parts: list[str] = []
    usage: dict[str, Any] = {}
    finish_reason: str | None = None

    try:
        async for model_event in model.stream(messages):
            if state.cancel_event.is_set():
                state.status = "cancelled"
                await state.append(
                    "run.cancelled",
                    {
                        "reason": "user_requested",
                        "partial_text": "".join(text_parts),
                    },
                )
                return

            if model_event.type == "text.delta":
                delta = model_event.data["delta"]
                text_parts.append(delta)
                await state.append("text.delta", {"delta": delta})
            elif model_event.type == "model.usage":
                usage = model_event.data
            elif model_event.type == "model.finished":
                finish_reason = model_event.data["finish_reason"]

        if finish_reason == "length":
            state.status = "failed"
            await state.append(
                "run.failed",
                {
                    "code": "OUTPUT_TRUNCATED",
                    "retryable": False,
                    "partial_text": "".join(text_parts),
                },
            )
            return

        state.status = "completed"
        await state.append(
            "run.completed",
            {
                "text": "".join(text_parts),
                "usage": usage,
                "finish_reason": finish_reason,
            },
        )
    except asyncio.CancelledError:
        state.status = "cancelled"
        if not state.events or state.events[-1].type != "run.cancelled":
            await state.append(
                "run.cancelled",
                {
                    "reason": "task_cancelled",
                    "partial_text": "".join(text_parts),
                },
            )
        raise
    except Exception:
        state.status = "failed"
        await state.append(
            "run.failed",
            {
                "code": "MODEL_STREAM_FAILED",
                "retryable": True,
                "partial_text": "".join(text_parts),
            },
        )


def encode_sse(event: RunEvent) -> str:
    payload = json.dumps(asdict(event), ensure_ascii=False)
    return f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"


def encode_heartbeat() -> str:
    return ": keep-alive\n\n"


async def subscribe(
    request: Request,
    state: RunState,
    after_seq: int,
) -> AsyncIterator[str]:
    cursor = after_seq + 1

    while True:
        while cursor < len(state.events):
            event = state.events[cursor]
            cursor += 1
            yield encode_sse(event)

            if event.type in {"run.completed", "run.failed", "run.cancelled"}:
                return

        if await request.is_disconnected():
            # 浏览器断线只停止订阅，不直接取消已有 Run。
            return

        try:
            async with state.changed:
                await asyncio.wait_for(state.changed.wait(), timeout=15.0)
        except TimeoutError:
            yield encode_heartbeat()


class CreateRunRequest(BaseModel):
    prompt: str = Field(min_length=1, description="发送给模型的用户提示词")


app = FastAPI(title="DeepSeek Streaming Gateway")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(
        """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>DeepSeek Streaming Demo</title>
  <style>
    :root {
      color-scheme: light;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    body {
      margin: 0;
      padding: 24px;
      background: #f5f7fb;
      color: #1f2937;
    }
    .container {
      max-width: 1100px;
      margin: 0 auto;
      display: grid;
      gap: 16px;
    }
    .card {
      background: #ffffff;
      border: 1px solid #dbe3f0;
      border-radius: 16px;
      padding: 20px;
      box-shadow: 0 8px 24px rgba(15, 23, 42, 0.06);
    }
    h1, h2 {
      margin: 0 0 12px;
    }
    h3 {
      margin: 0 0 10px;
      font-size: 16px;
    }
    p {
      margin: 0 0 12px;
      line-height: 1.6;
    }
    textarea {
      width: 100%;
      min-height: 100px;
      padding: 12px;
      border: 1px solid #cbd5e1;
      border-radius: 12px;
      font: inherit;
      resize: vertical;
      box-sizing: border-box;
    }
    .actions {
      display: flex;
      gap: 12px;
      margin-top: 12px;
      flex-wrap: wrap;
    }
    button {
      border: 0;
      border-radius: 999px;
      padding: 10px 16px;
      font: inherit;
      cursor: pointer;
      background: #2563eb;
      color: white;
    }
    button.secondary {
      background: #475569;
    }
    button.ghost {
      background: #e2e8f0;
      color: #0f172a;
    }
    button:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .hero {
      display: grid;
      gap: 16px;
      grid-template-columns: 1fr;
      align-items: start;
    }
    .meta {
      display: grid;
      gap: 8px;
      font-size: 14px;
    }
    .status-grid {
      display: grid;
      gap: 10px;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      margin-bottom: 12px;
    }
    .status-box {
      padding: 14px;
      border-radius: 12px;
      background: #f8fafc;
      border: 1px solid #dbe3f0;
    }
    .status-label {
      font-size: 13px;
      color: #475569;
      margin-bottom: 6px;
    }
    .status-value {
      font-size: 15px;
      font-weight: 600;
    }
    .mono, pre, .run-id-input {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }
    pre {
      margin: 0;
      padding: 14px;
      border-radius: 12px;
      background: #0f172a;
      color: #e2e8f0;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-word;
      line-height: 1.5;
    }
    .run-id-input {
      width: 100%;
      box-sizing: border-box;
      padding: 12px;
      border: 1px solid #cbd5e1;
      border-radius: 10px;
      background: #ffffff;
      color: #0f172a;
      font-size: 14px;
    }
    #rendered-text {
      min-height: 120px;
      padding: 14px;
      border-radius: 12px;
      background: #f8fafc;
      border: 1px solid #dbe3f0;
      white-space: pre-wrap;
      line-height: 1.7;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 16px;
    }
    .hint {
      color: #475569;
      font-size: 14px;
      margin-top: 10px;
    }
  </style>
</head>
<body>
  <div class="container">
    <section class="hero">
      <section class="card">
        <h1>DeepSeek V4 Flash 浏览器流式演示</h1>
        <p>这个页面专门用来演示两件事：浏览器刷新不会重复创建模型调用，以及客户端断线后还能重新连接已有 Run。</p>
        <textarea id="prompt">请用中文解释 EventSource 为什么适合订阅 SSE</textarea>
        <div class="actions">
          <button id="create-run-button">创建 Run 并开始订阅</button>
          <button id="disconnect-button" class="ghost" disabled>断开当前订阅</button>
          <button id="reconnect-button" class="ghost" disabled>重连已有 RunID</button>
          <button id="clear-messages-button" class="ghost">清除消息</button>
          <button id="cancel-run-button" class="secondary" disabled>取消当前 Run</button>
        </div>
        <div class="hint">刷新页面后只保留上一次的 RunID，不会自动重连，也不会再次创建模型调用。</div>
        <div class="hint">如果浏览器标签页一直在转圈，通常不是卡住了，而是 <span class="mono">EventSource</span> 正在保持一个打开的 SSE 长连接。</div>
      </section>
    </section>

    <section class="card meta">
      <div class="status-grid">
        <div class="status-box">
          <div class="status-label">当前 Run ID</div>
          <input id="run-id" class="run-id-input" type="text" readonly value="" placeholder="尚未创建" />
        </div>
        <div class="status-box">
          <div class="status-label">连接状态</div>
          <div id="connection-state" class="status-value">未连接</div>
        </div>
        <div class="status-box">
          <div class="status-label">当前页面行为</div>
          <div id="page-behavior" class="status-value">尚未创建 Run</div>
        </div>
      </div>
    </section>

    <section class="grid">
      <section class="card">
        <h2>拼接后的模型文本</h2>
        <div id="rendered-text">等待模型输出...</div>
      </section>
      <section class="card">
        <h2>SSE 原始事件日志</h2>
        <pre id="event-log">等待事件...</pre>
      </section>
    </section>
  </div>

  <script>
    const promptInput = document.getElementById("prompt");
    const createRunButton = document.getElementById("create-run-button");
    const disconnectButton = document.getElementById("disconnect-button");
    const reconnectButton = document.getElementById("reconnect-button");
    const clearMessagesButton = document.getElementById("clear-messages-button");
    const cancelRunButton = document.getElementById("cancel-run-button");
    const runIdEl = document.getElementById("run-id");
    const connectionStateEl = document.getElementById("connection-state");
    const pageBehaviorEl = document.getElementById("page-behavior");
    const renderedTextEl = document.getElementById("rendered-text");
    const eventLogEl = document.getElementById("event-log");
    const STORAGE_KEY = "deepseek-streaming-demo-state";

    let currentRunId = null;
    let eventSource = null;

    function setConnectionState(text) {
      connectionStateEl.textContent = text;
    }

    function setPageBehavior(text) {
      pageBehaviorEl.textContent = text;
    }

    function appendLog(line) {
      const hasPlaceholder = eventLogEl.textContent === "等待事件...";
      eventLogEl.textContent = hasPlaceholder ? line : eventLogEl.textContent + "\\n" + line;
      eventLogEl.scrollTop = eventLogEl.scrollHeight;
    }

    function savePageState() {
      const state = {
        prompt: promptInput.value,
        runId: currentRunId,
      };
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    }

    function loadPageState() {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) {
        return null;
      }

      try {
        return JSON.parse(raw);
      } catch {
        localStorage.removeItem(STORAGE_KEY);
        return null;
      }
    }

    function updateActionButtons() {
      const hasRun = Boolean(currentRunId);
      const isConnected = Boolean(eventSource);
      disconnectButton.disabled = !hasRun || !isConnected;
      reconnectButton.disabled = !hasRun || isConnected;
      cancelRunButton.disabled = !hasRun;
    }

    function closeEventSource() {
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
      updateActionButtons();
    }

    function resetOutput() {
      renderedTextEl.textContent = "等待模型输出...";
      eventLogEl.textContent = "等待事件...";
      savePageState();
    }

    function handleTerminalEvent(type, payload) {
      appendLog(`${type}: ${JSON.stringify(payload, null, 2)}`);
      setConnectionState("已结束");
      if (type === "run.cancelled") {
        currentRunId = null;
        runIdEl.value = "";
        runIdEl.placeholder = "尚未创建";
        setPageBehavior("当前 Run 已取消，刷新页面后不会再显示这个 RunID");
      } else {
        setPageBehavior("当前 Run 已结束，不会再产生新的模型事件");
      }
      createRunButton.disabled = false;
      closeEventSource();
      savePageState();
    }

    function subscribeToRun(runId) {
      closeEventSource();
      currentRunId = runId;
      runIdEl.value = runId;
      runIdEl.placeholder = "";
      savePageState();
      setConnectionState("连接中...");
      setPageBehavior("正在订阅已有 Run，只会走 GET /events，不会再次创建模型调用");
      updateActionButtons();

      eventSource = new EventSource(`/v1/runs/${runId}/events`);

      eventSource.onopen = () => {
        setConnectionState("已连接，正在接收 SSE");
        appendLog("EventSource 已连接");
        setPageBehavior("当前是活跃订阅，模型如果还在运行，事件会持续到达");
        updateActionButtons();
        savePageState();
      };

      eventSource.onerror = () => {
        setConnectionState("连接异常或服务端已关闭流");
        setPageBehavior("订阅连接断开了，但 Run 不一定取消，可以重新连接");
        closeEventSource();
        savePageState();
      };

      eventSource.addEventListener("run.started", (event) => {
        const payload = JSON.parse(event.data);
        appendLog(`run.started: ${JSON.stringify(payload, null, 2)}`);
        savePageState();
      });

      eventSource.addEventListener("text.delta", (event) => {
        const payload = JSON.parse(event.data);
        const delta = payload.data.delta;
        renderedTextEl.textContent =
          renderedTextEl.textContent === "等待模型输出..."
            ? delta
            : renderedTextEl.textContent + delta;
        appendLog(`text.delta: ${JSON.stringify(payload, null, 2)}`);
        savePageState();
      });

      eventSource.addEventListener("run.completed", (event) => {
        handleTerminalEvent("run.completed", JSON.parse(event.data));
      });

      eventSource.addEventListener("run.failed", (event) => {
        handleTerminalEvent("run.failed", JSON.parse(event.data));
      });

      eventSource.addEventListener("run.cancelled", (event) => {
        handleTerminalEvent("run.cancelled", JSON.parse(event.data));
      });
    }

    async function createRun() {
      const prompt = promptInput.value.trim();
      if (!prompt) {
        window.alert("请先输入 prompt");
        return;
      }

      createRunButton.disabled = true;
      currentRunId = null;
      runIdEl.value = "";
      runIdEl.placeholder = "创建中...";
      setConnectionState("准备创建 Run");
      setPageBehavior("即将调用 POST /v1/runs 创建一次新的模型调用");
      resetOutput();
      closeEventSource();
      updateActionButtons();

      try {
        const response = await fetch("/v1/runs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ prompt }),
        });

        const rawText = await response.text();
        if (!response.ok) {
          throw new Error(rawText);
        }

        const payload = JSON.parse(rawText);
        currentRunId = payload.run_id;
        runIdEl.value = payload.run_id;
        runIdEl.placeholder = "";
        appendLog(`create_run: ${JSON.stringify(payload, null, 2)}`);
        setPageBehavior("这次点击已经创建了一个新的 Run，后续刷新或重连都不需要再次 POST");
        updateActionButtons();
        savePageState();
        subscribeToRun(payload.run_id);
      } catch (error) {
        runIdEl.value = "";
        runIdEl.placeholder = "创建失败";
        setConnectionState("创建失败");
        setPageBehavior("创建 Run 失败，请检查服务端或 API Key");
        appendLog(`create_run_error: ${String(error)}`);
        createRunButton.disabled = false;
        updateActionButtons();
        savePageState();
      }
    }

    async function cancelRun() {
      if (!currentRunId) {
        return;
      }

      try {
        const response = await fetch(`/v1/runs/${currentRunId}/cancel`, {
          method: "POST",
        });
        const payload = await response.json();
        appendLog(`cancel_run: ${JSON.stringify(payload, null, 2)}`);
        closeEventSource();
        currentRunId = null;
        runIdEl.value = "";
        runIdEl.placeholder = "尚未创建";
        setConnectionState("已发送取消请求");
        setPageBehavior("已显式发送取消请求；页面已清除当前 RunID，刷新后不会恢复它");
        updateActionButtons();
        savePageState();
      } catch (error) {
        appendLog(`cancel_run_error: ${String(error)}`);
        savePageState();
      }
    }

    function disconnectSubscription() {
      if (!eventSource) {
        return;
      }

      appendLog("manual_disconnect: 浏览器主动关闭了 EventSource 连接");
      setConnectionState("订阅已断开");
      setPageBehavior("这里只断开了客户端订阅，服务端 Run 仍然可以继续运行");
      closeEventSource();
      savePageState();
    }

    function reconnectToRun() {
      if (!currentRunId) {
        return;
      }

      appendLog(`manual_reconnect: 准备重新订阅 ${currentRunId}`);
      setPageBehavior("正在重新连接已有 Run，不会创建第二次模型调用");
      subscribeToRun(currentRunId);
    }

    function clearMessages() {
      renderedTextEl.textContent = "等待模型输出...";
      eventLogEl.textContent = "消息已清除";
      setPageBehavior(
        currentRunId
          ? "消息已清除，但当前 Run ID 仍保留，可继续重连或取消"
          : "消息已清除，当前没有活跃 Run"
      );
      savePageState();
    }

    function restoreSavedRun() {
      const saved = loadPageState();
      if (!saved) {
        setPageBehavior("尚未创建 Run");
        setConnectionState("未连接");
        runIdEl.value = "";
        runIdEl.placeholder = "尚未创建";
        renderedTextEl.textContent = "等待模型输出...";
        eventLogEl.textContent = "等待事件...";
        updateActionButtons();
        return;
      }

      if (saved.prompt) {
        promptInput.value = saved.prompt;
      }

      renderedTextEl.textContent = "等待模型输出...";
      eventLogEl.textContent = "等待事件...";

      currentRunId = saved.runId || null;
      runIdEl.value = currentRunId || "";
      runIdEl.placeholder = currentRunId ? "" : "尚未创建";

      if (currentRunId) {
        setConnectionState("未连接");
        setPageBehavior("页面已恢复上一次的 RunID；当前不会自动重连，请手动点击“重连已有 RunID”");
        createRunButton.disabled = false;
      } else {
        setConnectionState("未连接");
        setPageBehavior("已恢复上一次输入，但当前没有活跃 Run");
      }
      updateActionButtons();
    }

    promptInput.addEventListener("input", () => {
      savePageState();
    });
    createRunButton.addEventListener("click", createRun);
    disconnectButton.addEventListener("click", disconnectSubscription);
    reconnectButton.addEventListener("click", reconnectToRun);
    clearMessagesButton.addEventListener("click", clearMessages);
    cancelRunButton.addEventListener("click", cancelRun);
    restoreSavedRun();
  </script>
</body>
</html>
        """
    )


@app.post("/v1/runs")
async def create_run(body: CreateRunRequest) -> dict[str, str]:
    try:
        model = get_model()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    run_id = f"run_{uuid.uuid4().hex}"
    state = RunState(run_id=run_id)
    RUNS[run_id] = state

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": body.prompt},
    ]
    task = asyncio.create_task(execute_run(state, model, messages))
    RUN_TASKS[run_id] = task
    task.add_done_callback(lambda _: RUN_TASKS.pop(run_id, None))

    return {"run_id": run_id, "status": state.status}


@app.get("/v1/runs/{run_id}/events")
async def stream_events(
    run_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    state = RUNS.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="run not found")

    try:
        after_seq = int(last_event_id) if last_event_id is not None else -1
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid Last-Event-ID") from exc

    return StreamingResponse(
        subscribe(request, state, after_seq),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/v1/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict[str, str]:
    state = RUNS.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="run not found")

    if state.status in {"completed", "failed", "cancelled"}:
        return {"run_id": run_id, "status": state.status}

    state.cancel_event.set()
    task = RUN_TASKS.get(run_id)
    if task is not None:
        task.cancel()

    return {"run_id": run_id, "status": "cancelling"}
