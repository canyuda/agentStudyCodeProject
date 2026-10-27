from openai import OpenAI


client = OpenAI()

response = client.responses.parse(
    model="gpt-5.6",
    input=[
        {
            "role": "system",
            "content": (
                "你是知识库 Agent 的决策器。"
                "资料不足时继续搜索，资料充分时结束并回答。"
            ),
        },
        {
            "role": "user",
            "content": "公司差旅报销需要哪些材料？",
        },
    ],
    text_format=AgentDecision,
)

decision = response.output_parsed