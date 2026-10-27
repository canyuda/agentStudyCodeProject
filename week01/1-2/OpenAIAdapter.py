class OpenAIResponsesAdapter(ModelAdapter):
    name = "openai-responses"
    capabilities = ModelCapabilities(
        chat_completions=False,
        responses=True,
        structured_output="native_schema",
        tool_calling=True,
        supports_temperature=True,
        supports_top_p=True,
    )

    def __init__(self, api_key: str, model: str) -> None:
        self.model = model
        self.client = OpenAI(
            api_key=api_key,
            timeout=30.0,
            max_retries=0,
        )

    def generate(self, request: ModelRequest) -> ModelResult:
        kwargs: dict[str, Any] = {}

        if request.output_schema:
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "agent_result",
                    "strict": True,
                    "schema": request.output_schema,
                }
            }

        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.top_p is not None:
            kwargs["top_p"] = request.top_p

        raw = self.client.responses.create(
            model=self.model,
            instructions=request.system,
            input=request.user,
            max_output_tokens=request.max_output_tokens,
            **kwargs,
        )

        if raw.status != "completed":
            raise RuntimeError(f"MODEL_NOT_COMPLETED: {raw.status}")

        text = raw.output_text or ""
        data = json.loads(text) if request.output_schema else None
        usage = raw.usage

        return ModelResult(
            kind="structured" if data is not None else "text",
            text=text,
            data=data,
            finish_reason=raw.status,
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            request_id=raw.id,
        )