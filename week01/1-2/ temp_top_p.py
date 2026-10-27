import os
from collections import Counter
from openai import OpenAI

MODEL = "deepseek-v4-flash"
TRIALS = int(os.getenv("TRIALS", "3"))
PROMPT = (
    "为一个“把模型错误统一归一化”的 Python 函数起更短的名字。"
    "允许使用缩写、同义词或不同动词形式。"
    "只输出 1 个 snake_case 名称，不要解释。"
)

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
    timeout=30.0,
    max_retries=0,
)


def sample(*, temperature: float, top_p: float) -> str:
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": PROMPT}],
        temperature=temperature,
        top_p=top_p,
        max_tokens=16,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return response.choices[0].message.content.strip()


def run_case(*, label: str, temperature: float, top_p: float) -> None:
    values = [sample(temperature=temperature, top_p=top_p) for _ in range(TRIALS)]
    counts = Counter(values)

    print(f"\n[{label}] temperature={temperature}, top_p={top_p}")
    print(f"样本: {values}")
    print(f"去重数: {len(counts)}/{TRIALS}")
    print("Top 频次:", counts.most_common(5))


def main() -> None:
    print("=== 固定 top_p=1.0，只观察 temperature ===")
    for temperature in [0.0, 0.3, 0.8, 1.3]:
        run_case(
            label="temperature 对照组",
            temperature=temperature,
            top_p=1.0,
        )

    print("\n=== 固定 temperature=1.0，只观察 top_p ===")
    for top_p in [1.0, 0.7, 0.3, 0.1]:
        run_case(
            label="top_p 对照组",
            temperature=1.0,
            top_p=top_p,
        )


if __name__ == "__main__":
    main()
