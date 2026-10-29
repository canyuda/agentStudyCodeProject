# 01-5 · Ticket Router 工单分类 Agent

第一章 1.5 讲课堂练习：用结构化输出实现工单分类 Agent，只能执行两种动作——
`route_to_team`（转交团队）和 `ask_user`（追问用户）。

## 练习要求 ↔ 实现位置

| # | 要求 | 位置 |
|---|---|---|
| 1 | 用 Pydantic 定义 `action`、`team`、`question` | `protocol.py` · `TicketDecision` |
| 2 | 转交时必须有 `team`，追问时必须有 `question` | `protocol.py` · `model_validator(mode="after")` 跨字段校验 |
| 3 | 禁止额外字段 | `protocol.py` · `ConfigDict(extra="forbid", strict=True)` |
| 4 | 用 `model_validate_json()` 校验输出 | `agent.py` · `classify_ticket()` |
| 5 | 校验错误反喂模型，最多修复两次 | `agent.py` · 修复循环（1 次首调 + 2 次修复）；`repair.py` 生成反馈 |
| 6 | 连续失败后返回 `failed`，不能猜测团队继续执行 | `agent.py` · 终态 `TicketResult(status="failed", decision=None)` |

## 文件

```
01-5-ticket-router/
├── protocol.py     # TicketDecision 协议 + TicketResult 终态（要求 1/2/3）
├── agent.py        # classify_ticket：校验 + 修复循环 + ok/failed 终态（要求 4/5/6），可直接运行演示
├── repair.py       # build_repair_message：把 Pydantic 错误转为「哪里错/为什么/允许改什么」
└── fake_model.py   # scripted_model：脚本化假模型，零 SDK 依赖
```

## 运行

```bash
cd agentStudyCodeProject/homework/01-5-ticket-router
python agent.py        # 任意带 pydantic v2 的解释器；本仓库可用 ai-agent-pre-learn/.venv
```

演示覆盖四个场景：

| 场景 | 预期 |
|---|---|
| `ok_first_call` | 首次输出合法，1 次调用，`status=ok` |
| `ok_after_repair` | 首次缺 `team`（业务组合错误）→ 反喂校验错误 → 第 2 次修正，`calls=2` |
| `ok_ask_user` | 合法追问（`question` 有值、`team=null`） |
| `failed_no_guess` | 坏输出持续失败，3 次调用耗尽预算 → `status=failed`、`decision=None`，绝不猜团队 |

协议边界（均被拒绝）：额外字段 `extra_forbidden`、未知动作 `literal_error`、
动作与载荷组合错误 `value_error`。

## 接入真实模型

模型调用被抽象为 `ModelCall = Callable[[messages, schema], str]`，
`classify_ticket` 不感知模型来源。接入真实模型时写一个适配器返回原始文本即可
（延迟 import SDK、key 走环境变量，参照 `streamingAgentGateway` 的 Adapter 模式）：

```python
def deepseek_model(messages, schema) -> str:
    from openai import OpenAI  # deferred import
    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"])
    resp = client.chat.completions.create(...)
    return resp.choices[0].message.content
```
