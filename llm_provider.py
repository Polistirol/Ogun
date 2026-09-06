"""
llm_provider.py
---------------
LLM client factory. Callers only use:

    client = get_llm_client()
    result = client.complete(system=..., user=..., max_tokens=4000)
    text = result.text

Format differences between Anthropic, LM Studio and DeepSeek
stay inside this module.

Keys are read from the .env file at the project root.
"""

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


SUPPORTED_PROVIDERS: Tuple[str, ...] = ("anthropic", "lmstudio", "deepseek")

ANTHROPIC_MODEL = "claude-sonnet-4-6"
LMSTUDIO_DEFAULT_BASE_URL = "http://localhost:1234/v1"
LMSTUDIO_DEFAULT_MODEL = "local-model"
LMSTUDIO_PLACEHOLDER_KEY = "lm-studio"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"


@dataclass
class TokenUsage:
    prompt: int = 0
    completion: int = 0
    total: int = 0
    reasoning: int = 0


@dataclass
class LLMResult:
    text: str
    model: str
    provider: str
    usage: TokenUsage
    finish_reason: str = ""


class LLMClient:
    provider: str = ""
    model: str = ""

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> LLMResult:
        raise NotImplementedError


def log_llm_start(client: LLMClient, task: str) -> None:
    print(f"Provider: {client.provider}")
    print(f"Modello:  {client.model}")
    print(f"Compito:  {task}")
    print("Invio della richiesta in corso...")


def log_llm_usage(result: LLMResult) -> None:
    u = result.usage
    extra = f", di cui reasoning {u.reasoning}" if u.reasoning else ""
    print(
        f"Token usati: {u.total} totali "
        f"(input {u.prompt} + output {u.completion}{extra})"
    )
    if result.finish_reason and result.finish_reason not in ("stop", "end_turn"):
        print(f"ATTENZIONE: finish_reason={result.finish_reason}")


def parse_json_object(text: str) -> dict:
    """Parsa un oggetto JSON, togliendo eventuali fence markdown."""
    text = (text or "").strip()
    if not text:
        raise json.JSONDecodeError("Expecting value", text, 0)
    if text.startswith("```"):
        text = text[3:]
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on", "enabled")


def _usage_from_openai(usage: Any) -> TokenUsage:
    if not usage:
        return TokenUsage()
    reasoning = 0
    details = getattr(usage, "completion_tokens_details", None)
    if details is not None:
        reasoning = getattr(details, "reasoning_tokens", 0) or 0
    reasoning = reasoning or getattr(usage, "reasoning_tokens", 0) or 0
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    completion = getattr(usage, "completion_tokens", 0) or 0
    total = getattr(usage, "total_tokens", 0) or (prompt + completion)
    return TokenUsage(prompt=prompt, completion=completion, total=total, reasoning=reasoning)


class AnthropicClient(LLMClient):
    def __init__(self):
        from anthropic import Anthropic

        self.provider = "anthropic"
        self.model = ANTHROPIC_MODEL
        self._client = Anthropic()

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> LLMResult:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        usage = TokenUsage()
        if getattr(response, "usage", None):
            prompt = getattr(response.usage, "input_tokens", 0) or 0
            completion = getattr(response.usage, "output_tokens", 0) or 0
            usage = TokenUsage(prompt=prompt, completion=completion, total=prompt + completion)
        return LLMResult(
            text=text,
            model=self.model,
            provider=self.provider,
            usage=usage,
            finish_reason=getattr(response, "stop_reason", "") or "",
        )


class OpenAICompatibleClient(LLMClient):
    """Client OpenAI-style (LM Studio locale, DeepSeek API, ecc.)."""

    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: str,
        model: str,
        extra_create: Optional[Dict[str, Any]] = None,
    ):
        from openai import OpenAI

        self.provider = provider
        self.model = model
        self._extra_create = extra_create or {}
        self._client = OpenAI(base_url=base_url, api_key=api_key)

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> LLMResult:
        kwargs: Dict[str, Any] = dict(
            model=self.model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        kwargs.update(self._extra_create)
        response = self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        message = choice.message
        content = (message.content or "").strip()
        reasoning = (getattr(message, "reasoning_content", None) or "").strip()
        if not content and reasoning:
            print(
                "ATTENZIONE: content vuoto, uso reasoning_content "
                "(tipico di DeepSeek in thinking mode se i token finiscono nel ragionamento).",
                file=sys.stderr,
            )
            content = reasoning
        return LLMResult(
            text=content,
            model=getattr(response, "model", None) or self.model,
            provider=self.provider,
            usage=_usage_from_openai(getattr(response, "usage", None)),
            finish_reason=getattr(choice, "finish_reason", "") or "",
        )


def _lmstudio_client() -> OpenAICompatibleClient:
    base_url = os.environ.get("LMSTUDIO_BASE_URL") or LMSTUDIO_DEFAULT_BASE_URL
    model = os.environ.get("LMSTUDIO_MODEL")
    if not model:
        print(
            "WARNING: LMSTUDIO_MODEL is not set. "
            f"Using default '{LMSTUDIO_DEFAULT_MODEL}'. "
            "Set LMSTUDIO_MODEL in .env to the model id loaded in "
            "LM Studio (the local server must already be running).",
            file=sys.stderr,
        )
        model = LMSTUDIO_DEFAULT_MODEL
    return OpenAICompatibleClient(
        provider="lmstudio",
        base_url=base_url,
        api_key=LMSTUDIO_PLACEHOLDER_KEY,
        model=model,
    )


def _deepseek_client() -> OpenAICompatibleClient:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError(
            "DEEPSEEK_API_KEY is not set. Add it to the .env file at the project root."
        )
    model = os.environ.get("DEEPSEEK_MODEL")
    if not model:
        print(
            "WARNING: DEEPSEEK_MODEL is not set. "
            f"Using default '{DEEPSEEK_DEFAULT_MODEL}'. "
            "Set DEEPSEEK_MODEL in .env.",
            file=sys.stderr,
        )
        model = DEEPSEEK_DEFAULT_MODEL

    # V4 enables thinking by default: tokens go to reasoning_content and
    # `content` stays empty (the JSON char 0 error). For structured JSON
    # we disable it unless DEEPSEEK_THINKING=1.
    thinking = "enabled" if _env_flag("DEEPSEEK_THINKING") else "disabled"
    extra_create: Dict[str, Any] = {
        "extra_body": {"thinking": {"type": thinking}},
        "response_format": {"type": "json_object"},
    }
    return OpenAICompatibleClient(
        provider="deepseek",
        base_url=DEEPSEEK_BASE_URL,
        api_key=api_key,
        model=model,
        extra_create=extra_create,
    )


def get_llm_client(provider: Optional[str] = None) -> LLMClient:
    """
    Return an LLM client.

    Priority: `provider` argument (e.g. --provider) > env LLM_PROVIDER >
    default "anthropic". Allowed values: anthropic, lmstudio, deepseek.
    """
    name = (provider or os.environ.get("LLM_PROVIDER") or "anthropic").strip().lower()
    if name == "anthropic":
        return AnthropicClient()
    if name == "lmstudio":
        return _lmstudio_client()
    if name == "deepseek":
        return _deepseek_client()
    raise ValueError(
        f"Unknown LLM_PROVIDER: {name!r}. "
        f"Allowed values: {', '.join(SUPPORTED_PROVIDERS)}."
    )
