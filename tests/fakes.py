from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

from app.core.constants import EmbedPurpose

from app.integrations.llm import REQUEST_TIMEOUT_SECONDS, CallUsage, Delta, LLMClient, ToolCall, ToolRound, Turn


class FakeLLMClient(LLMClient):
    """Deterministic stand-in for the model; tests set canned replies instead of calling a provider.

    `json_response` is the reply in the shape the grounding checks read (`answer`,
    `answerable`, `relations`); a streamed call writes it out in the reply format,
    a few characters at a time so the reader sees awkward splits. `stream_text`
    replaces that with raw text, and `thoughts` are streamed first. Each list in
    `lookups` is one round of tool calls, made (in order) while tools are allowed,
    before the reply is written.
    """

    def __init__(self):
        self.json_response: dict | None = None
        self.stream_response: str | None = None
        self.thoughts: list[str] = ["Reading the context."]
        self.lookups: list[list[ToolCall]] = []
        #: raw replies for the stream calls after the lookups, in order; the last one repeats
        self.replies: list[str] = []
        self.calls: list[dict] = []
        #: what each call reports it used, to anyone passing `on_usage`
        self.usage = CallUsage("fake/model", None, prompt_tokens=100, completion_tokens=20, cached_tokens=0, cost_usd=0.001)

    async def stream_text(
        self,
        system_instruction: str,
        prompt: str,
        *,
        history: Sequence[Turn] = (),
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        tools: Sequence[dict] = (),
        rounds: Sequence[ToolRound] = (),
        tool_choice: str = "auto",
        closing: str = "",
        session: str = "",
        on_usage: Callable[[CallUsage], Awaitable[None]] | None = None,
    ) -> AsyncIterator[Delta | ToolCall]:
        if on_usage:
            await on_usage(self.usage)
        self.calls.append({
            "mode": "stream", "system": system_instruction, "prompt": prompt, "history": list(history),
            "tools": list(tools), "rounds": list(rounds), "tool_choice": tool_choice, "closing": closing,
            "session": session,
        })
        for thought in self.thoughts:
            yield Delta("thought", thought)
        if tools and tool_choice == "auto" and len(rounds) < len(self.lookups):
            for call in self.lookups[len(rounds)]:
                yield call
            return
        later = len(self.calls) - 1 - len(self.lookups)
        if self.replies:
            text = self.replies[min(max(later, 0), len(self.replies) - 1)]
        else:
            text = self.stream_response if self.stream_response is not None else written(self.json_response or {})
        for start in range(0, len(text), 7):
            yield Delta("text", text[start:start + 7])


def written(reply: dict) -> str:
    """A reply dict in the streamed reply format."""
    relations = "; ".join(
        f"[{r['source'].strip('[]')}] {r['type']} [{r['target'].strip('[]')}]" for r in reply.get("relations", [])
    )
    answerable = "yes" if reply.get("answerable", True) else "no"
    return f"ANSWERABLE: {answerable}\nRELATIONS: {relations or 'none'}\n---\n{reply.get('answer', '')}"


class FakeEmbedder:
    """Deterministic stand-in for an embedding model: one dimension per word in `vocabulary`.

    A text's vector counts the vocabulary words in it, so texts sharing those words read as close
    in meaning and texts sharing none as unrelated. Words outside the vocabulary are ignored.
    """

    model = "fake/embedder"

    def __init__(self):
        self.vocabulary = ["export", "guest", "pdf", "report", "dark", "mode", "billing", "invoice"]
        self.calls: list[dict] = []
        self.usage = CallUsage(self.model, None, prompt_tokens=10, completion_tokens=0, cached_tokens=0, cost_usd=0.00001)

    async def embed(
        self, texts: Sequence[str], purpose: EmbedPurpose, *, on_usage: Callable[[CallUsage], Awaitable[None]] | None = None,
    ) -> list[list[float]]:
        self.calls.append({"texts": list(texts), "purpose": purpose})
        if on_usage:
            await on_usage(self.usage)
        return [[float(text.lower().count(word)) + 0.001 for word in self.vocabulary] for text in texts]
