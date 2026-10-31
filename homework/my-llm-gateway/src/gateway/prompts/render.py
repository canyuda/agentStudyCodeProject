"""沙箱化模板渲染 + 严格未定义变量检查（提示词能力的安全核心）。

【教学定位：模板引擎的两道安全闸门】
提示词模板本质是"用户提交的代码"——Jinja2 模板支持属性访问、方法
调用等强大特性，直接用普通 Environment 渲染不可信模板等于远程代码
执行风险（模板注入，SSTI）。这里叠了两道闸：

闸 1【沙箱】SandboxedEnvironment
  拦截危险的属性/方法访问。经典攻击载荷：
      {{ ''.__class__.__mro__[1].__subclasses__() }}
  普通环境会执行并泄漏类信息；沙箱直接抛 SecurityError。

闸 2【严格未定义】StrictUndefined
  Jinja2 默认把未定义变量渲染成空串——对提示词是灾难：
  "把工单分给{{team}}" 缺 team 会静默变成"把工单分给"，
  模型只能瞎猜。StrictUndefined 让缺变量直接抛 UndefinedError，
  网关转为 422 并指明变量名（需求 5.5，fail fast 而非静默出错）。

【边界声明】沙箱 + 严格模式显著降低风险，但模板能力【不能消除
提示词注入】（攻击面还包括变量值本身的内容）——生产仍需隔离
不可信输入、限制工具权限、把模型输出当不可信数据（README 已声明）。
"""

from __future__ import annotations

from jinja2 import Environment, StrictUndefined
from jinja2.exceptions import SecurityError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment


class RenderError(Exception):
    """模板无法安全渲染（缺变量 / 沙箱拦截 / 语法错误）——上层转为 422。"""


def _env() -> Environment:
    """构建沙箱环境。每次渲染新建（模板不可信，不复用环境状态）。"""
    env: Environment = SandboxedEnvironment(
        undefined=StrictUndefined,  # 闸 2：缺变量硬报错
        autoescape=False,  # 提示词是纯文本不是 HTML，转义反而破坏内容
        keep_trailing_newline=True,  # 保留模板末尾换行（提示词排版敏感）
    )
    return env


def render(template: str, variables: dict[str, str]) -> str:
    """渲染模板；任何失败统一转成 RenderError（调用方只需处理一种异常）。

    异常分类转译：
    - UndefinedError -> 带变量名的错误（告诉用户缺什么）
    - SecurityError  -> 明示被沙箱拦截（告诉用户为什么不行）
    - 其余（语法错等）-> 兜底包装，避免裸异常穿透
    """
    try:
        compiled = _env().from_string(template)
        return compiled.render(**variables)
    except UndefinedError as exc:
        raise RenderError(f"undefined template variable: {exc.message}") from exc
    except SecurityError as exc:
        raise RenderError(f"blocked by sandboxed template engine: {exc}") from exc
    except Exception as exc:  # 模板语法错误等
        raise RenderError(f"template render failed: {exc}") from exc
