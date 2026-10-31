"""管理端点：分页查询用量记录 + 路由表与熔断状态。

【教学定位】管理视角 vs 调用方视角的"内外有别"：
- /admin/routes 可以看到真实模型 ID 与熔断器内部状态（运维需要）
- /v1/models 只给别名（调用方不需要也不应知道）
两个端点同样要求网关密钥；生产部署建议再叠加网络层隔离
（管理接口收内网，需求 11 的生产化建议）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from gateway.api.deps import AppState, get_state, require_auth

router = APIRouter()


@router.get("/admin/usage")
async def usage(
    limit: int = Query(default=20, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    state: AppState = Depends(get_state),
    _key: str = Depends(require_auth),
):
    records = await state.ledger.recent(limit=limit, offset=offset)
    return {"limit": limit, "offset": offset, "records": records}


@router.get("/admin/routes")
async def routes(state: AppState = Depends(get_state), _key: str = Depends(require_auth)):
    return {
        "aliases": state.router.describe(),       # routing table (incl. real model ids - admin only)
        "circuit_breakers": state.breakers.snapshot(),
    }
