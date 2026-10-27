import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
    timeout=30.0,
    max_retries=0,
)

response = client.responses.create(
    model="deepseek-v4-flash",
    instructions=(
        "你是代码审查助手。只指出会影响正确性的问题，"
        "没有证据时不要猜测。"
    ),
    input="审查：def divide(a, b): return a / b",
    max_output_tokens=512,
    reasoning={"effort": "none"},
)

print(response.output_text)
print("status:", response.status)
print("usage:", response.usage)

for item in response.output:
    print("item type:", item.type)