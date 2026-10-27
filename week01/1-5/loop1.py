MAX_STEPS = 4


def run_knowledge_agent(
    task: str,
    call_model: ModelCall,
    search_docs: Callable[[str], str],
    cached: CachedAnswer | None = None,
) -> str:
    observations: list[str] = []

    for _ in range(MAX_STEPS):
        context = task
        if observations:
            context += "\n\n已获得资料：\n" + "\n".join(observations)

        result = safe_decide(
            task=context,
            call_model=call_model,
            cached=cached if not observations else None,
        )

        if result.status == "failed":
            raise RuntimeError(result.message)

        decision = result.decision
        assert decision is not None

        if decision.action == "finish":
            assert decision.answer is not None
            return decision.answer

        assert decision.query is not None
        observations.append(search_docs(decision.query))

    raise RuntimeError("agent exceeded maximum steps")


def test_invalid_output_never_calls_tool():
    calls = 0

    def invalid_model(_messages, _schema):
        return '{"action": "delete_docs"}'

    def search_docs(_query):
        nonlocal calls
        calls += 1
        return "不应执行"

    try:
        run_knowledge_agent(
            task="查询差旅报销材料",
            call_model=invalid_model,
            search_docs=search_docs,
        )
    except RuntimeError:
        pass

    assert calls == 0