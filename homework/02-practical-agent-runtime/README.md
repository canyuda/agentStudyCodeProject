# 第二章实战：转账工具的治理链路

在 `tool_governance_demo.py` 基础上新增 `transfer` 工具，跑通
「参数校验 → 业务预检 → 权限判断 → 人工审批 → 超时处理 → 结果脱敏 → 审计追踪」。

## 环境

- Python 3.14（`.venv`）
- 依赖：`pydantic>=2.10,<3`、`pytest`

```bash
python3 -m venv .venv
.venv/bin/pip install 'pydantic>=2.10,<3' pytest
.venv/bin/python tool_governance_demo.py          # 离线演示
.venv/bin/python -m pytest tests/test_tool_governance.py -v -k "transfer"   # 验收
```

脚本先跑原有的订单/退款演示 `main()`，再跑转账验证 `run_transfer_scenarios()`。

## 代码地图

| 任务 | 位置 | 内容 |
| --- | --- | --- |
| 1 | `ACCOUNTS` | 模拟账户余额（tenant_a 三个、tenant_b 一个） |
| 2 | `TransferArgs` | 账号正则 `^ACC-[A-Z]-[0-9]{6}$`，金额 `(0, 100000]`，继承 `extra="forbid"` |
| 3 | `transfer_precheck` | 教学限额区间 `(50000, 80000]` → `EXCEED_LIMIT`；余额不足 → `INSUFFICIENT_BALANCE` |
| 4 | `transfer_handler` | `> 80000` 先 `sleep(3.0)`；转入账户不存在 → `ACCOUNT_NOT_FOUND`；双向改余额；返回 `txn_id` |
| 5 | `build_governance` | 注册 `transfer`：`WRITE` + `HIGH` + `requires_approval` + `timeout_seconds=2.0` |
| 6 | `redact` | 邮箱脱敏后追加账号脱敏，`ACC-A-123456` → `ACC-A-****3456` |
| 验证 | `run_transfer_scenarios` / `tests/test_tool_governance.py` | 下列 5 个测试项 |

初始余额：`ACC-A-123456` = 100000，`ACC-A-654321` = 5000，`ACC-A-888888` = 20000。

## 验收结果

命令：`python -m pytest tests/test_tool_governance.py -v -k "transfer"`

**结果：5 passed in 2.07s**

| 测试项 | 覆盖点 | 实测 |
| --- | --- | --- |
| `test_transfer_parameter_validation` | 注入 `approved`/`user_id`、账号格式非法、金额 0、金额 100001 | 全部 `INVALID_ARGUMENT`，余额不变，PASSED |
| `test_transfer_precheck` | 60000 命中限额区间；`ACC-A-888888` 转 30000 余额不足 | `EXCEED_LIMIT` / `INSUFFICIENT_BALANCE`，双方余额不变，PASSED |
| `test_transfer_permission_and_mode` | 无权限 / 不在白名单 / Plan 模式（均已签发审批） | `PERMISSION_DENIED` / `TOOL_NOT_ALLOWED` / `PLAN_MODE_DENIED`，PASSED |
| `test_transfer_approval_flow` | 未审批 → CONFIRM；改金额 → 审批失效；原参数成功；同一审批不可复用 | 余额 100000→99000、5000→6000；返回 `ACC-A-****3456`；审计记录增长，PASSED |
| `test_transfer_timeout` | 90000 触发 `sleep(3.0)`，超时点位于扣款之前 | `TIMEOUT_UNKNOWN`，余额不变，审计含该错误码，PASSED |

关键输出（测试项 4，`-s` 可见）：

```json
{"redacted_content": {"txn_id": "ansfer", "from": "ACC-A-****3456", "to": "ACC-A-****4321", "amount": 1000.0, "status": "accepted"}}
{"audit_tail": {"tool_call_id": "t4_4", "tool_name": "transfer", "decision": "deny", "ok": false, "error_code": "APPROVAL_REQUIRED", ...}}
```

## 已知注意事项

- 超时策略 `timeout_seconds=2.0` 与处理器的 `sleep(3.0)` 明确分离，实测耗时约 2001ms，稳定触发超时。
- `transfer_precheck` 对缺失或其他租户的转出账户返回受控 `ACCOUNT_NOT_FOUND`，不再抛 `KeyError`。
- 金额以 `float` 入参，内部统一用 `Decimal(str(...))` 比较与计算。
- 未改动 `PermissionEngine.decide`；`Handler` 保持两参契约；审批请求在审计中记为 `confirm`（三态保留），并带 `stage` 字段；`TransferArgs` 启用 `strict=True`，数值字符串在参数校验阶段即被拒绝。
