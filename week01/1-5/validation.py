from pydantic import ValidationError


invalid_output = """
{
  "action": "finish",
  "query": null,
  "answer": null
}
"""

try:
    AgentDecision.model_validate_json(invalid_output)
except ValidationError as exc:
    for item in exc.errors(
        include_url=False,
        include_input=False,
    ):
        print(item["loc"], item["type"], item["msg"])