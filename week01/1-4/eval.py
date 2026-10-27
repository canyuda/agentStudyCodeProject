from dataclasses import dataclass


@dataclass(frozen=True)
class EvalCase:
    id: str
    task: str
    fixture: str
    expected_status: str
    forbidden_tools: tuple[str, ...] = ()
    max_steps: int = 12


async def evaluate(agent_factory, version: str, cases: list[EvalCase]):
    records = []

    for case in cases:
        agent = agent_factory(prompt_version=version)
        result = await agent.run(
            task=case.task,
            fixture=case.fixture,
            max_steps=case.max_steps,
        )

        records.append(
            {
                "case_id": case.id,
                "status_ok": result.status == case.expected_status,
                "forbidden_tool_called": any(
                    result.trace.called(tool) for tool in case.forbidden_tools
                ),
                "steps": result.trace.model_turns,
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "verified": result.verification.passed,
            }
        )

    return records