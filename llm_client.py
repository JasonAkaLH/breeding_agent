from openai import AsyncOpenAI

class LLMClient:
    def __init__(self, api_key: str, base_url: str, max_retries: int = 3, timeout: int = 30):
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=max_retries,
            timeout=timeout,
        )

    async def generate_text(self, kwargs: dict) -> str:
        stream = await self.client.chat.completions.create(**kwargs)
        for chunk in stream:
            yield chunk.choices[0].delta.content