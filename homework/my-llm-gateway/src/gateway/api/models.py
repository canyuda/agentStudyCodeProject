"""GET /v1/models —— 公开别名列表（真实上游模型 ID 绝不泄露）。

【教学定位】对外只暴露"逻辑模型"（别名），物理供应商与真实模型 ID
是内部实现细节——调用方与上游解耦后，网关侧换供应商/换模型对调用方
零感知（改配置即可），这也是 /v1/models 只返回别名的根本理由。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends

from gateway.api.deps import AppState, get_state, require_auth

router = APIRouter()


@router.get("/v1/models")
async def list_models(state: AppState = Depends(get_state), _key: str = Depends(require_auth)):
    return {
        "object": "list",
        "data": [
            {"id": alias, "object": "model", "created": int(time.time()), "owned_by": state.config.service.name}
            for alias in state.router.public_models()
        ],
    }
