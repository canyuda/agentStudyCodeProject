"""一个可执行的上下文预算器示例。

这段代码把“大模型上下文预算”这个抽象概念，落成了可以直接运行、
可以被单元测试、也可以被业务代码复用的预算规则对象。
"""

from dataclasses import dataclass

@dataclass(frozen=True)
class ContextBudget:
    """用不可变数据对象描述一次请求的上下文预算配置。

    1. 制定一个预算参数的数据模型，便于传递和复用。
    2. `frozen=True` 表示预算配置创建后不再被随意修改，减少运行时状态污染。
    3. 这也是把“配置”和“决策逻辑”分离开的一个基础动作。
    """

    context_window: int
    reserve_output: int
    protocol_overhead: int
    safety_margin: int

    @property
    def max_business_input(self) -> int:
        """计算真正可分配给业务输入的 token 上限。

        1. 总窗口并不等于都能拿来放用户输入，还要扣掉输出预留、协议开销和安全余量。
        2. 这里把“预算计算公式”收口成一个可复用属性，调用方不需要重复手写。
        3. 主动校验 `value <= 0`，是在用代码保护配置正确性，避免错误预算进入后续流程。
        """

        value = (
            self.context_window
            - self.reserve_output
            - self.protocol_overhead
            - self.safety_margin
        )
        if value <= 0:
            raise ValueError("上下文预算配置无效")
        return value


def choose_context_action(estimated_input: int, budget: ContextBudget) -> str:
    """根据预计输入规模，返回当前请求应该采取的上下文处理策略。

    1. 这一步把“容量判断”翻译成“业务动作”，让预算器真正可以执行决策。
    2. 用比例阈值而不是写死某个 token 数，说明规则可以跟随不同模型窗口一起伸缩。
    3. 返回的是策略名而不是直接执行动作，方便上层系统继续编排 summarize、reject 等流程。
    """

    ratio = estimated_input / budget.max_business_input
    if ratio <= 0.75:
        return "keep"
    if ratio <= 1.0:
        return "summarize_old_tool_results"
    return "externalize_or_reject"


# 这里给出一组可直接运行的示例预算配置，方便把抽象规则变成具体数字。
budget = ContextBudget(
    context_window=1_000_000,
    reserve_output=16_000,
    protocol_overhead=8_000,
    safety_margin=32_000,
)
