"""LLM provider abstraction.

Providers turn chat messages into text. ``LLM_PROVIDER`` selects one:

* ``fake``              canned responses (tests only — never touches the network)
* ``heuristic``         offline rule-based planner (dev default; no data leaves the host)
* ``azure``             Azure OpenAI chat completions (GPT-4o deployment)
* ``openai_compatible`` any OpenAI-compatible endpoint (vLLM / Llama-3, llama.cpp, Ollama)

Every provider is wrapped by :class:`RedactingLLM`, the outbound PHI-redaction
middleware: no message reaches a provider until it has passed the shared redactor and the
final check finds nothing PHI-shaped left.
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import structlog
from prometheus_client import Counter, Histogram

from scoutoss_common.phi import contains_phi, redact_text

log = structlog.get_logger(__name__)

LLM_LATENCY = Histogram(
    "copilot_llm_latency_seconds",
    "LLM completion latency",
    ["provider"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60),
)
PHI_REDACTIONS = Counter(
    "copilot_phi_redactions_total", "Outbound prompt messages that needed PHI redaction"
)


@dataclass(frozen=True)
class Message:
    role: str  # system | user | assistant
    content: str


class LLMProvider(Protocol):
    name: str

    def complete(self, messages: Sequence[Message]) -> str: ...


class LLMError(RuntimeError):
    pass


class PHILeakError(RuntimeError):
    """Raised if a prompt still looks like it contains PHI after redaction."""


# --------------------------------------------------------------------------- fake
class FakeLLM:
    """Deterministic provider for tests. Responses are matched by substring of the last
    user message (first match wins) or popped from a queue; every prompt is recorded so
    tests can assert exactly what would have been sent."""

    name = "fake"

    def __init__(
        self,
        responses: dict[str, str] | None = None,
        queue: Iterable[str] = (),
        default: str | None = None,
    ) -> None:
        self.responses = dict(responses or {})
        self.queue: deque[str] = deque(queue)
        self.default = default
        self.prompts: list[list[Message]] = []

    def complete(self, messages: Sequence[Message]) -> str:
        self.prompts.append(list(messages))
        question = next((m.content for m in reversed(messages) if m.role == "user"), "")
        for needle, answer in self.responses.items():
            if needle.lower() in question.lower():
                return answer
        if self.queue:
            return self.queue.popleft()
        if self.default is not None:
            return self.default
        raise LLMError("FakeLLM has no canned response for this prompt")


# --------------------------------------------------------------------------- OpenAI-style
class OpenAICompatibleLLM:
    """POST /chat/completions on an OpenAI-compatible server (vLLM, llama.cpp, Ollama)."""

    name = "openai_compatible"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.model = model
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport
        )

    def _url(self) -> str:
        return "/chat/completions"

    def _body(self, messages: Sequence[Message]) -> dict[str, Any]:
        return {
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "response_format": {"type": "json_object"},
        }

    def complete(self, messages: Sequence[Message]) -> str:
        try:
            resp = self.http.post(self._url(), json=self._body(messages))
            resp.raise_for_status()
            data = resp.json()
            return str(data["choices"][0]["message"]["content"])
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"{self.name} completion failed: {type(exc).__name__}") from exc


class AzureOpenAILLM(OpenAICompatibleLLM):
    """Azure OpenAI: deployment-scoped URL, ``api-key`` header, ``api-version`` param."""

    name = "azure"

    def __init__(
        self,
        endpoint: str,
        deployment: str,
        api_key: str,
        api_version: str,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(endpoint, deployment, None, timeout=timeout, transport=transport)
        self.http.headers["api-key"] = api_key
        self.deployment = deployment
        self.api_version = api_version

    def _url(self) -> str:
        return (
            f"/openai/deployments/{self.deployment}/chat/completions?api-version={self.api_version}"
        )

    def _body(self, messages: Sequence[Message]) -> dict[str, Any]:
        body = super()._body(messages)
        body.pop("model")
        return body


# --------------------------------------------------------------------------- middleware
class RedactingLLM:
    """Outbound PHI-redaction middleware around any provider.

    ``allow_terms`` returns the de-identified vocabulary (index values like manufacturer
    names) that must not be mistaken for person names.
    """

    def __init__(
        self, inner: LLMProvider, allow_terms: Callable[[], Iterable[str]] = lambda: ()
    ) -> None:
        self.inner = inner
        self.name = inner.name
        self.allow_terms = allow_terms

    def sanitize(self, messages: Sequence[Message]) -> list[Message]:
        allowed = list(self.allow_terms())
        clean: list[Message] = []
        for m in messages:
            # System prompts carry the schema (JSON) — dates/IDs there are field names.
            redacted = redact_text(m.content, allow_terms=allowed, detect_names=m.role != "system")
            if redacted != m.content:
                PHI_REDACTIONS.inc()
            if m.role != "system" and contains_phi(redacted, allow_terms=allowed):
                raise PHILeakError("prompt still contains PHI after redaction")
            clean.append(Message(m.role, redacted))
        return clean

    def complete(self, messages: Sequence[Message]) -> str:
        clean = self.sanitize(messages)
        start = time.perf_counter()
        try:
            return self.inner.complete(clean)
        finally:
            elapsed = time.perf_counter() - start
            LLM_LATENCY.labels(self.name).observe(elapsed)
            log.info(
                "llm completion",
                provider=self.name,
                latency_ms=round(elapsed * 1000, 1),
                messages=len(clean),
            )


def build_provider(settings: Any, heuristic: LLMProvider | None = None) -> LLMProvider:
    kind = settings.llm_provider
    if kind == "fake":
        return FakeLLM(
            default=json.dumps({"steps": [{"index": "dicom_studies", "query": {"match_all": {}}}]})
        )
    if kind == "heuristic":
        if heuristic is None:
            from scoutoss_copilot.heuristic import HeuristicPlanner

            heuristic = HeuristicPlanner()
        return heuristic
    if kind == "azure":
        if not settings.azure_endpoint or not settings.secret("azure"):
            raise LLMError(
                "LLM_PROVIDER=azure needs AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY"
            )
        return AzureOpenAILLM(
            settings.azure_endpoint,
            settings.azure_deployment,
            settings.secret("azure"),
            settings.azure_api_version,
            timeout=settings.llm_timeout,
        )
    if kind == "openai_compatible":
        if not settings.llm_base_url:
            raise LLMError("LLM_PROVIDER=openai_compatible needs LLM_BASE_URL")
        return OpenAICompatibleLLM(
            settings.llm_base_url,
            settings.llm_model,
            settings.secret("llm"),
            timeout=settings.llm_timeout,
        )
    raise LLMError(f"unknown LLM_PROVIDER {kind}")  # pragma: no cover - pydantic restricts values
