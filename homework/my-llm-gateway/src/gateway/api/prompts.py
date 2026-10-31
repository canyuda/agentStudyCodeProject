"""提示词模板端点：创建(即新版本)/列表/查询/激活切换/渲染预览。

【教学定位】五个端点对应模板生命周期：
    POST /v1/prompts                     创建（同 ID 再创建 = 新版本）
    GET  /v1/prompts                     列表（各 ID 的版本与激活态）
    GET  /v1/prompts/{id}?version=n      查询（默认激活版本，可指定）
    POST /v1/prompts/{id}/activate       切换激活版本
    POST /v1/prompts/{id}/render         渲染预览（调试利器）

错误映射：PromptNotFoundError -> 404；RenderError（缺变量/沙箱拦截）
-> 422。注意渲染预览走的是与正式调用【同一个】render 函数——
预览看到的即模型将看到的，所见即所得。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from gateway.api.deps import AppState, get_state, require_auth
from gateway.errors import not_found, render_failed
from gateway.prompts.render import RenderError, render
from gateway.prompts.store import PromptNotFoundError

router = APIRouter()


class PromptCreate(BaseModel):
    prompt_id: str
    template: str


class PromptActivate(BaseModel):
    version: int


class PromptRenderPreview(BaseModel):
    variables: dict[str, str] = {}
    version: int | None = None


@router.post("/v1/prompts")
async def create_prompt(
    payload: PromptCreate, state: AppState = Depends(get_state), _key: str = Depends(require_auth)
):
    return await state.prompts.create(payload.prompt_id, payload.template)


@router.get("/v1/prompts")
async def list_prompts(state: AppState = Depends(get_state), _key: str = Depends(require_auth)):
    return {"data": await state.prompts.list_all()}


@router.get("/v1/prompts/{prompt_id}")
async def get_prompt(
    prompt_id: str,
    version: int | None = None,
    state: AppState = Depends(get_state),
    _key: str = Depends(require_auth),
):
    try:
        return await state.prompts.get(prompt_id, version)
    except PromptNotFoundError as exc:
        raise not_found(str(exc)) from exc


@router.post("/v1/prompts/{prompt_id}/activate")
async def activate_prompt(
    prompt_id: str,
    payload: PromptActivate,
    state: AppState = Depends(get_state),
    _key: str = Depends(require_auth),
):
    try:
        await state.prompts.set_active(prompt_id, payload.version)
    except PromptNotFoundError as exc:
        raise not_found(str(exc)) from exc
    return {"prompt_id": prompt_id, "active_version": payload.version}


@router.post("/v1/prompts/{prompt_id}/render")
async def render_prompt(
    prompt_id: str,
    payload: PromptRenderPreview,
    state: AppState = Depends(get_state),
    _key: str = Depends(require_auth),
):
    try:
        stored = await state.prompts.get(prompt_id, payload.version)
    except PromptNotFoundError as exc:
        raise not_found(str(exc)) from exc
    try:
        rendered = render(stored["template"], payload.variables)
    except RenderError as exc:
        raise render_failed(str(exc)) from exc
    return {"prompt_id": prompt_id, "version": stored["version"], "rendered": rendered}
