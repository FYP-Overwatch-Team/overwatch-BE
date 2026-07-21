import json

from app.integrations.gemini_client import GeminiClient


class FakeGeminiClient(GeminiClient):
    """Deterministic stand-in for Gemini; tests set canned responses instead of hitting the API."""

    def __init__(self):
        self.text_response: str = ""
        self.json_response: dict | None = None
        self.calls: list[dict] = []

    async def generate_text(self, system_instruction: str, prompt: str) -> str:
        self.calls.append({"mode": "text", "system": system_instruction, "prompt": prompt})
        return self.text_response

    async def generate_json(self, system_instruction: str, prompt: str, response_schema: dict) -> str:
        self.calls.append({"mode": "json", "system": system_instruction, "prompt": prompt, "schema": response_schema})
        return json.dumps(self.json_response or {})
