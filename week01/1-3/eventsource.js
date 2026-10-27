type RunEvent = {
  schema_version: "1";
  run_id: string;
  seq: number;
  type: string;
  created_at: string;
  data: Record<string, unknown>;
};

type SubscribeRunOptions = {
  withCredentials?: boolean;
  subscriptionToken?: string;
  initialText?: string;
};

type RunSubscription = {
  source: EventSource;
  close: () => void;
  getCheckpoint: () => { lastSeq: number; lastEventId: string | null; text: string };
};

async function createRun(prompt: string): Promise<string> {
  const response = await fetch("/v1/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });

  if (!response.ok) {
    throw new Error(`create run failed: ${response.status}`);
  }

  const body = await response.json();
  return body.run_id;
}

function buildEventsUrl(
  runId: string,
  options: SubscribeRunOptions,
): string {
  const url = new URL(`/v1/runs/${runId}/events`, window.location.origin);

  // EventSource 只能发 GET，也不支持自定义 Authorization Header。
  // 如果要做鉴权，常见做法有两种：
  // 1. 同站 Cookie + EventSource(..., { withCredentials: true })
  // 2. 为订阅接口签发一个短期、只读的 subscription token
  // 不要把长期 API Key 放进 URL 查询参数。
  if (options.subscriptionToken) {
    url.searchParams.set("subscription_token", options.subscriptionToken);
  }

  return url.toString();
}

function parseRunEvent(raw: Event): RunEvent {
  return JSON.parse((raw as MessageEvent).data) as RunEvent;
}

function subscribeRun(
  runId: string,
  options: SubscribeRunOptions = {},
): RunSubscription {
  let text = options.initialText ?? "";
  let lastSeq = -1;
  let lastEventId: string | null = null;

  const source = new EventSource(buildEventsUrl(runId, options), {
    withCredentials: options.withCredentials ?? false,
  });

  // 只要服务端持续发送 `id: <seq>`，浏览器就会在同一个 EventSource
  // 实例的自动重连过程中自动维护 Last-Event-ID。
  // 也就是说，网络抖动后浏览器会自动带上 Last-Event-ID 请求头，
  // 后端只要像 app.py 一样按 seq 恢复游标即可。
  function rememberCheckpoint(raw: MessageEvent, event: RunEvent) {
    lastSeq = Math.max(lastSeq, event.seq);
    lastEventId = raw.lastEventId || String(lastSeq);
  }

  source.addEventListener("run.started", (raw) => {
    const event = parseRunEvent(raw);
    rememberCheckpoint(raw as MessageEvent, event);
  });

  source.addEventListener("text.delta", (raw) => {
    const event = parseRunEvent(raw);

    // EventSource 自动重连后，服务端可能会从上一个 checkpoint 继续推送；
    // 客户端仍然应该按 seq 去重，避免重复渲染。
    if (event.seq <= lastSeq) return;
    rememberCheckpoint(raw as MessageEvent, event);

    text += String(event.data.delta ?? "");
    scheduleRender(text);
  });

  source.addEventListener("run.completed", (raw) => {
    const event = parseRunEvent(raw);
    rememberCheckpoint(raw as MessageEvent, event);
    source.close();
    showCompleted(event.data);
  });

  source.addEventListener("run.failed", (raw) => {
    const event = parseRunEvent(raw);
    rememberCheckpoint(raw as MessageEvent, event);
    source.close();
    showFailure(event.data);
  });

  source.addEventListener("run.cancelled", (raw) => {
    const event = parseRunEvent(raw);
    rememberCheckpoint(raw as MessageEvent, event);
    source.close();
    showCancelled();
  });

  source.onerror = () => {
    // 这里不要创建第二个 Run。
    // 对同一个 EventSource 实例，浏览器会自动重连，并自动带上 Last-Event-ID。
    showReconnecting({ runId, lastEventId, lastSeq });
  };

  return {
    source,
    close: () => source.close(),
    getCheckpoint: () => ({ lastSeq, lastEventId, text }),
  };
}
