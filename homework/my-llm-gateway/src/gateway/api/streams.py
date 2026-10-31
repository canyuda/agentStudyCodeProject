"""GET /v1/streams/{request_id}/checkpoint —— 流检查点查询（功能开关控制）。

【教学定位】断线找回的查询入口：客户端凭流式响应头里的 X-Request-Id
即可查询已保存的文本与状态（流式中/成功/失败/已取消）。

两层 404 语义（需求 9）：
- 功能未开启 -> 404（feature is disabled）——默认态
- 开启但请求 ID 不存在 -> 404（no checkpoint）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from gateway.api.deps import AppState, get_state, require_auth
from gateway.errors import not_found

router = APIRouter()


@router.get("/v1/streams/{request_id}/checkpoint")
async def get_checkpoint(
    request_id: str, state: AppState = Depends(get_state), _key: str = Depends(require_auth)
):
    if not state.config.stream_checkpoint.enabled:
        raise not_found("stream checkpoint feature is disabled")
    checkpoint = state.checkpoints.get(request_id)
    if checkpoint is None:
        raise not_found(f"no checkpoint for request '{request_id}'")
    return checkpoint.as_payload()
