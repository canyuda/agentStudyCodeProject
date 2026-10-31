"""统一错误封装：OpenAI 风格错误对象 + 网关错误分类学。

【教学定位】
网关下连两种协议各异的上游，上对多种调用方。如果错误格式随上游走，
调用方就要写两套错误处理——所以所有错误在出口处统一为一个形状：

    {"error": {"message": ..., "type": ..., "param": ..., "code": ...}}

状态码语义表（需求 7.1，也是调用方唯一需要记住的契约）：
    401 网关密钥无效            404 别名/提示词/检查点不存在
    422 参数校验/模板渲染/结构化输出终局不合规
    429 网关限流，或上游限流且无可回退路由
    502 上游失败                503 无健康路由

设计模式：工厂函数集中构造错误（而不是到处 GatewayError(404, ...)），
保证同一类错误的 type/code/状态码永远一致——这也是"统一错误形状"
测试能成立的根基。
"""

from __future__ import annotations

from typing import Any


class GatewayError(Exception):
    """与北向错误对象 1:1 对应的异常。

    抛出它 -> main.py 的全局异常处理器捕获 -> to_payload() 序列化为
    {"error": {...}} JSON 返回。整个网关的错误出口只有这一个类。
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        error_type: str,
        code: str | None = None,
        param: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code  # HTTP 状态码
        self.message = message  # 人类可读的错误描述
        self.error_type = error_type  # 粗分类（authentication_error 等）
        self.code = code  # 细分类机器码（model_not_found 等）
        self.param = param  # 出错的具体请求参数（如 "model"）

    def to_payload(self) -> dict[str, Any]:
        """序列化为北向错误体（OpenAI 风格外层包一层 "error"）。"""
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }


def invalid_api_key() -> GatewayError:
    """401：缺失或错误的 Bearer 网关密钥。"""
    return GatewayError(401, "Invalid API key provided for the gateway", "authentication_error", "invalid_api_key")


def model_not_found(model: str) -> GatewayError:
    """404：请求携带了未配置的模型别名（param 指向 model 字段便于定位）。"""
    return GatewayError(
        404, f"The model '{model}' does not exist", "invalid_request_error", "model_not_found", param="model"
    )


def no_healthy_route(model: str) -> GatewayError:
    """503：别名存在，但全部候选路由都被熔断或禁用。"""
    return GatewayError(
        503, f"No healthy route available for model '{model}'", "service_unavailable_error", "no_healthy_route"
    )


def rate_limited(model: str) -> GatewayError:
    """429：令牌桶耗尽（维度 = 调用方密钥 + 模型别名）。"""
    return GatewayError(
        429,
        f"Rate limit exceeded for model '{model}'; slow down or contact the operator",
        "rate_limit_error",
        "rate_limit_exceeded",
    )


def upstream_failed(status: int, detail: str) -> GatewayError:
    """上游失败的最终归一（所有路由耗尽后调用）。

    映射规则（需求 7.1）：
    - 网络失败/超时（status<=0）或上游 5xx -> 统一 502，message 保留上游原文
      （5xx 是上游自己的毛病，调用方不需要区分是 500 还是 503）
    - 上游 4xx -> 状态码原样透传（是调用方的请求有问题，透传才便于排查）
    """
    if status <= 0 or status >= 500:
        return GatewayError(502, f"Upstream provider failed: {detail}", "api_error", "upstream_error")
    return GatewayError(status, f"Upstream provider rejected the request: {detail}", "upstream_error", "upstream_error")


def invalid_request(message: str, param: str | None = None) -> GatewayError:
    """422：请求参数校验失败。"""
    return GatewayError(422, message, "invalid_request_error", "invalid_request", param=param)


def render_failed(message: str) -> GatewayError:
    """422：模板渲染失败（缺变量 / 沙箱拦截），绝不静默渲染成空串。"""
    return GatewayError(422, message, "invalid_request_error", "template_render_error")


def structured_invalid(detail: str) -> GatewayError:
    """422：结构化输出修复轮次耗尽后仍不合规（终局失败）。"""
    return GatewayError(
        422, f"Structured output failed schema validation: {detail}", "invalid_request_error",
        "structured_output_invalid",
    )


def not_found(message: str) -> GatewayError:
    """404：通用资源不存在（提示词/检查点等）。"""
    return GatewayError(404, message, "invalid_request_error", "not_found")
