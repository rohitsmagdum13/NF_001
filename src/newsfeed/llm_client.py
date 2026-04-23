"""Unified LLM client. Bedrock (primary) with OpenAI fallback.

Contract (from CLAUDE.md):
    - Every LLM call goes through ``LLMClient.complete()``. Never call
      ``boto3`` or ``openai`` from stage modules.
    - Structured output is mandatory: caller passes a Pydantic model;
      response is validated before return.
    - Bedrock uses Converse API with ``tool_use`` to force schema.
    - OpenAI uses ``response_format={"type": "json_schema", ...}``.
    - On Bedrock throttling / timeout / parse failure → fall back to OpenAI.
      Log provider, latency, tokens, and fallback flag to ``audit_log``.

Import lazily. boto3/openai are heavy — we only touch them inside methods
so that unit tests that mock the client don't pay the import cost.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Literal, TypeVar

from loguru import logger
from pydantic import BaseModel, ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from newsfeed.config import Settings, get_settings
from newsfeed.db import AuditLog, get_session

ModelTier = Literal["cheap", "quality"]
Provider = Literal["bedrock", "openai"]

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Base for all LLM client errors."""


class LLMProviderError(LLMError):
    """Underlying provider SDK raised an error we can retry / fall back on."""


class LLMStructuredOutputError(LLMError):
    """Provider returned output that failed Pydantic validation after retries."""


# Exception types worth falling back on (set at call time to avoid import cost).
_RETRYABLE_BEDROCK = (
    "ThrottlingException",
    "ServiceUnavailableException",
    "ModelTimeoutException",
    "ReadTimeoutError",
    "ConnectTimeoutError",
)


class LLMClient:
    """Primary-with-fallback LLM client with structured output + audit."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._bedrock: Any | None = None
        self._openai: Any | None = None

    # --- Public API ---

    def complete(
        self,
        prompt: str,
        schema: type[T],
        model_tier: ModelTier,
        *,
        system: str | None = None,
        max_retries: int | None = None,
        run_id: str | None = None,
        stage: str | None = None,
        candidate_id: int | None = None,
        prompt_version: str | None = None,
    ) -> T:
        """Call the LLM, parse a schema-shaped response, and audit the call.

        Raises:
            LLMStructuredOutputError: both providers exhausted without a
                schema-valid response.
        """
        run_id = run_id or uuid.uuid4().hex
        retries = max_retries if max_retries is not None else self.settings.llm.max_retries

        # Try primary.
        try:
            return self._call_with_audit(
                provider=self.settings.llm.primary,
                prompt=prompt,
                schema=schema,
                model_tier=model_tier,
                system=system,
                retries=retries,
                run_id=run_id,
                stage=stage,
                candidate_id=candidate_id,
                prompt_version=prompt_version,
                fallback_triggered=False,
            )
        except LLMProviderError as exc:
            logger.warning(
                "primary provider failed; falling back",
                primary=self.settings.llm.primary,
                fallback=self.settings.llm.fallback,
                error=str(exc),
            )

        # Fall back.
        try:
            return self._call_with_audit(
                provider=self.settings.llm.fallback,
                prompt=prompt,
                schema=schema,
                model_tier=model_tier,
                system=system,
                retries=retries,
                run_id=run_id,
                stage=stage,
                candidate_id=candidate_id,
                prompt_version=prompt_version,
                fallback_triggered=True,
            )
        except LLMProviderError as exc:
            raise LLMStructuredOutputError(f"Both providers failed. Last error: {exc}") from exc

    # --- Internal: audit + dispatch ---

    def _call_with_audit(
        self,
        *,
        provider: Provider,
        prompt: str,
        schema: type[T],
        model_tier: ModelTier,
        system: str | None,
        retries: int,
        run_id: str,
        stage: str | None,
        candidate_id: int | None,
        prompt_version: str | None,
        fallback_triggered: bool,
    ) -> T:
        model = self._resolve_model(provider, model_tier)
        started = time.monotonic()
        try:
            raw, input_tokens, output_tokens = self._dispatch(
                provider=provider,
                prompt=prompt,
                schema=schema,
                model=model,
                system=system,
                retries=retries,
            )
        except Exception as exc:  # noqa: BLE001 — re-raise as LLMProviderError below
            latency_ms = int((time.monotonic() - started) * 1000)
            self._record_audit(
                run_id=run_id,
                stage=stage,
                candidate_id=candidate_id,
                prompt_version=prompt_version,
                model=model,
                provider=provider,
                fallback_triggered=fallback_triggered,
                input_tokens=None,
                output_tokens=None,
                latency_ms=latency_ms,
            )
            raise LLMProviderError(f"{provider} call failed: {exc}") from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        self._record_audit(
            run_id=run_id,
            stage=stage,
            candidate_id=candidate_id,
            prompt_version=prompt_version,
            model=model,
            provider=provider,
            fallback_triggered=fallback_triggered,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
        )

        try:
            return schema.model_validate(raw)
        except ValidationError as exc:
            raise LLMProviderError(f"{provider} returned invalid schema: {exc}") from exc

    def _dispatch(
        self,
        *,
        provider: Provider,
        prompt: str,
        schema: type[T],
        model: str,
        system: str | None,
        retries: int,
    ) -> tuple[dict[str, Any], int | None, int | None]:
        if provider == "bedrock":
            return self._call_bedrock(
                prompt=prompt, schema=schema, model=model, system=system, retries=retries
            )
        return self._call_openai(
            prompt=prompt, schema=schema, model=model, system=system, retries=retries
        )

    def _resolve_model(self, provider: Provider, tier: ModelTier) -> str:
        if provider == "bedrock":
            return (
                self.settings.bedrock.cheap_model
                if tier == "cheap"
                else self.settings.bedrock.quality_model
            )
        return (
            self.settings.openai.cheap_model
            if tier == "cheap"
            else self.settings.openai.quality_model
        )

    # --- Provider implementations ---

    def _call_bedrock(
        self,
        *,
        prompt: str,
        schema: type[T],
        model: str,
        system: str | None,
        retries: int,
    ) -> tuple[dict[str, Any], int | None, int | None]:
        import boto3  # noqa: PLC0415 — deliberate lazy import (heavy SDK)
        from botocore.exceptions import ClientError  # noqa: PLC0415

        # boto3/botocore ship without py.typed, so the dynamic client is Any.

        if self._bedrock is None:
            boto_kwargs: dict[str, Any] = {
                "service_name": "bedrock-runtime",
                "region_name": self.settings.bedrock.region,
                "aws_access_key_id": self.settings.aws_access_key_id,
                "aws_secret_access_key": self.settings.aws_secret_access_key,
            }
            if self.settings.aws_session_token:
                boto_kwargs["aws_session_token"] = self.settings.aws_session_token
            self._bedrock = boto3.client(**boto_kwargs)

        tool_spec = _pydantic_to_bedrock_tool(schema)

        @retry(
            reraise=True,
            stop=stop_after_attempt(retries + 1),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(ClientError),
        )
        def _invoke() -> dict[str, Any]:
            kwargs: dict[str, Any] = {
                "modelId": model,
                "messages": [{"role": "user", "content": [{"text": prompt}]}],
                "toolConfig": {
                    "tools": [{"toolSpec": tool_spec}],
                    "toolChoice": {"tool": {"name": tool_spec["name"]}},
                },
                "inferenceConfig": {"temperature": 0.0, "maxTokens": 2048},
            }
            if system:
                kwargs["system"] = [{"text": system}]
            assert self._bedrock is not None  # established above
            result: dict[str, Any] = self._bedrock.converse(**kwargs)
            return result

        try:
            response = _invoke()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _RETRYABLE_BEDROCK:
                raise LLMProviderError(f"retryable bedrock error: {code}") from exc
            raise

        tool_use = _extract_bedrock_tool_use(response, tool_spec["name"])
        usage = response.get("usage", {}) or {}
        return tool_use, usage.get("inputTokens"), usage.get("outputTokens")

    def _call_openai(
        self,
        *,
        prompt: str,
        schema: type[T],
        model: str,
        system: str | None,
        retries: int,
    ) -> tuple[dict[str, Any], int | None, int | None]:
        from openai import (  # noqa: PLC0415 — deliberate lazy import (heavy SDK)
            APIConnectionError,
            APIError,
            OpenAI,
            RateLimitError,
        )

        if self._openai is None:
            if not self.settings.openai_api_key:
                raise LLMProviderError("OPENAI_API_KEY not set")
            self._openai = OpenAI(api_key=self.settings.openai_api_key)

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        schema_dict = schema.model_json_schema()

        @retry(
            reraise=True,
            stop=stop_after_attempt(retries + 1),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type((APIConnectionError, RateLimitError)),
        )
        def _invoke() -> Any:
            return self._openai.chat.completions.create(  # type: ignore[union-attr]
                model=model,
                messages=messages,
                temperature=0.0,
                response_format={"type": "json_object"},
            )

        try:
            completion = _invoke()
        except (APIConnectionError, RateLimitError, APIError) as exc:
            raise LLMProviderError(f"openai error: {exc}") from exc

        content = completion.choices[0].message.content or "{}"
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMProviderError(f"openai returned non-JSON: {exc}") from exc

        usage = getattr(completion, "usage", None)
        in_tok = getattr(usage, "prompt_tokens", None) if usage else None
        out_tok = getattr(usage, "completion_tokens", None) if usage else None
        return parsed, in_tok, out_tok

    # --- Audit ---

    def _record_audit(
        self,
        *,
        run_id: str,
        stage: str | None,
        candidate_id: int | None,
        prompt_version: str | None,
        model: str,
        provider: Provider,
        fallback_triggered: bool,
        input_tokens: int | None,
        output_tokens: int | None,
        latency_ms: int,
    ) -> None:
        try:
            with get_session() as session:
                session.add(
                    AuditLog(
                        run_id=run_id,
                        stage=stage,
                        candidate_id=candidate_id,
                        prompt_version=prompt_version,
                        model=model,
                        provider=provider,
                        fallback_triggered=1 if fallback_triggered else 0,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        latency_ms=latency_ms,
                    )
                )
        except Exception as exc:  # noqa: BLE001 — audit must never break the pipeline
            logger.error("failed to write audit_log row", error=str(exc))


# --- Helpers ---


def _pydantic_to_bedrock_tool(schema: type[BaseModel]) -> dict[str, Any]:
    """Build a Bedrock Converse toolSpec from a Pydantic model."""
    return {
        "name": schema.__name__,
        "description": schema.__doc__ or f"Return a {schema.__name__} object.",
        "inputSchema": {"json": schema.model_json_schema()},
    }


def _extract_bedrock_tool_use(response: dict[str, Any], tool_name: str) -> dict[str, Any]:
    """Pull the tool_use payload from a Bedrock Converse response."""
    output = response.get("output", {}) or {}
    message = output.get("message", {}) or {}
    for block in message.get("content", []) or []:
        tu = block.get("toolUse") if isinstance(block, dict) else None
        if tu and tu.get("name") == tool_name:
            payload = tu.get("input", {})
            if isinstance(payload, dict):
                return payload
    raise LLMProviderError(f"bedrock response missing toolUse={tool_name!r}")
