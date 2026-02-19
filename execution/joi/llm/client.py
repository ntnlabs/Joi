import logging
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger("joi.llm")


@dataclass
class LLMResponse:
    text: str
    model: str
    done: bool
    error: Optional[str] = None


class OllamaClient:
    """Simple Ollama API client."""

    def __init__(self, base_url: str, model: str, timeout: float = 60.0, num_ctx: int = 0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.num_ctx = num_ctx  # 0 = use model default

    def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        model: Optional[str] = None,
    ) -> LLMResponse:
        """
        Generate a response from the LLM.

        Args:
            prompt: The user's message
            system: Optional system prompt
            model: Optional model override (None = use client's default model)

        Returns:
            LLMResponse with the generated text
        """
        url = f"{self.base_url}/api/generate"
        use_model = model or self.model

        payload = {
            "model": use_model,
            "prompt": prompt,
            "stream": False,
        }

        if system:
            payload["system"] = system

        if self.num_ctx > 0:
            payload["options"] = {"num_ctx": self.num_ctx}

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()

            return LLMResponse(
                text=data.get("response", ""),
                model=data.get("model", use_model),
                done=data.get("done", True),
            )

        except httpx.TimeoutException:
            logger.error("Ollama request timed out")
            return LLMResponse(
                text="",
                model=use_model,
                done=False,
                error="timeout",
            )
        except httpx.HTTPStatusError as exc:
            logger.error("Ollama HTTP error: %s", exc)
            return LLMResponse(
                text="",
                model=use_model,
                done=False,
                error=f"http_error: {exc.response.status_code}",
            )
        except Exception as exc:
            logger.error("Ollama error: %s", exc)
            return LLMResponse(
                text="",
                model=use_model,
                done=False,
                error=str(exc),
            )

    def chat(self, messages: list, system: Optional[str] = None, model: Optional[str] = None) -> LLMResponse:
        """
        Chat completion with message history.

        Args:
            messages: List of {"role": "user"|"assistant", "content": "..."}
            system: Optional system prompt (None = don't send, use model's baked-in prompt)
            model: Optional model override (None = use client's default model)

        Returns:
            LLMResponse with the generated text
        """
        url = f"{self.base_url}/api/chat"
        use_model = model or self.model

        payload = {
            "model": use_model,
            "messages": messages,
            "stream": False,
        }

        if system:
            # Prepend system message
            payload["messages"] = [{"role": "system", "content": system}] + messages

        if self.num_ctx > 0:
            payload["options"] = {"num_ctx": self.num_ctx}

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()

            message = data.get("message", {})
            return LLMResponse(
                text=message.get("content", ""),
                model=data.get("model", use_model),
                done=data.get("done", True),
            )

        except httpx.TimeoutException:
            logger.error("Ollama chat timed out")
            return LLMResponse(
                text="",
                model=use_model,
                done=False,
                error="timeout",
            )
        except httpx.HTTPStatusError as exc:
            logger.error("Ollama chat HTTP error: %s", exc)
            return LLMResponse(
                text="",
                model=use_model,
                done=False,
                error=f"http_error: {exc.response.status_code}",
            )
        except Exception as exc:
            logger.error("Ollama chat error: %s", exc)
            return LLMResponse(
                text="",
                model=use_model,
                done=False,
                error=str(exc),
            )
