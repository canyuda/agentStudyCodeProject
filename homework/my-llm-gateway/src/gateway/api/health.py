"""健康检查 / 就绪检查（无鉴权，供容器探活）。

【教学定位：liveness 与 readiness 的区别】
- /healthz 存活探针：进程还活着就返回 200（K8s 据此决定是否重启容器）
- /readyz  就绪探针：依赖（账本/模板库连接）就绪才 200
  （据此决定是否把流量导给本实例——滚动发布时新实例 ready 前不接流量）
两个端点绝不能要求鉴权，否则探活组件本身就会被挡在门外。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> dict:
    state = request.app.state.gateway
    ready = state.ledger.connected and state.prompts.connected
    return {"status": "ready" if ready else "not_ready"}
