class OneTurnLoop:
    def __init__(self, model: ModelAdapter) -> None:
        self.model = model

    def run(self, goal: str) -> AgentAction:
        request = ModelRequest(
            system=(
                "你是编码 Agent 的决策器。根据目标选择 inspect、edit、"
                "run_tests 或 finish。不要声称执行了尚未执行的动作。"
            ),
            user=goal,
            max_output_tokens=1024,
            output_schema=AgentAction.model_json_schema(),
        )

        result = self.model.generate(request)
        if result.kind != "structured" or result.data is None:
            raise RuntimeError("模型没有返回结构化动作")

        # Adapter 负责协议归一化，Loop 仍负责领域对象校验。
        return AgentAction.model_validate(result.data)

####


import os

provider = os.getenv("MODEL_PROVIDER", "deepseek")

if provider == "deepseek":
    adapter: ModelAdapter = DeepSeekChatAdapter(
        api_key=os.environ["DEEPSEEK_API_KEY"]
    )
elif provider == "openai":
    adapter = OpenAIResponsesAdapter(
        api_key=os.environ["OPENAI_API_KEY"],
        model=os.getenv("OPENAI_MODEL", "gpt-5.6"),
    )
else:
    raise ValueError(f"未知 MODEL_PROVIDER: {provider}")

loop = OneTurnLoop(adapter)
action = loop.run("检查 tests/test_api.py 失败原因，当前尚未运行测试。")
print(action)