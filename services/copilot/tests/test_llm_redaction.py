"""The LLM receives only schema + de-identified values: PHI is stripped from every
outbound prompt, and tests never call a real model."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from scoutoss_copilot.clients import static_schema
from scoutoss_copilot.llm import (
    AzureOpenAILLM,
    FakeLLM,
    LLMError,
    Message,
    OpenAICompatibleLLM,
    PHILeakError,
    RedactingLLM,
    build_provider,
)
from scoutoss_copilot.prompt import allow_terms, build_messages, render_schema
from scoutoss_copilot.settings import CopilotSettings

PHI_QUESTIONS = [
    ("How many CT studies for John Doe SSN 123-45-6789", ["John Doe", "123-45-6789"]),
    ("patient Jane Roe MRN: 00123456 born 1950-03-14", ["Jane Roe", "00123456", "1950-03-14"]),
    (
        "studies for Doe^John, phone 555-867-5309, email jdoe@example.org",
        ["Doe^John", "867-5309", "jdoe@example.org"],
    ),
]


def outbound(fake: FakeLLM) -> str:
    return "\n".join(m.content for m in fake.prompts[-1])


@pytest.mark.parametrize(("question", "secrets"), PHI_QUESTIONS)
def test_prompt_builder_includes_schema_and_redacts_phi(question: str, secrets: list[str]) -> None:
    schema = static_schema()
    fake = FakeLLM(default="{}")
    llm = RedactingLLM(fake, allow_terms=lambda: allow_terms(schema))
    llm.complete(build_messages(question, schema))
    sent = outbound(fake)
    for s in secrets:
        assert s not in sent
    assert "dicom_studies" in sent and "patient_pseudonym" in sent and "clinical_events" in sent
    assert "CT studies" in sent or "studies" in sent


def test_history_is_redacted_too() -> None:
    fake = FakeLLM(default="{}")
    history = [
        {"question": "cohort for Dr. Gregory House", "plan": {"steps": []}, "cohort_id": "c1"}
    ]
    RedactingLLM(fake).complete(build_messages("and of those?", static_schema(), history))
    assert "Gregory House" not in outbound(fake)
    assert any(m.role == "assistant" for m in fake.prompts[-1])


def test_allow_terms_keep_deidentified_vocabulary() -> None:
    schema = static_schema()
    schema["indices"]["dicom_studies"]["examples"] = {"manufacturers": ["Acme Zentrix"]}
    fake = FakeLLM(default="{}")
    RedactingLLM(fake, allow_terms=lambda: allow_terms(schema)).complete(
        build_messages("studies on Acme Zentrix scanners", schema)
    )
    assert "Acme Zentrix scanners" in fake.prompts[-1][-1].content
    assert "Acme Zentrix" in render_schema(schema)


def test_leak_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    import scoutoss_copilot.llm as llm_mod

    monkeypatch.setattr(
        llm_mod, "redact_text", lambda text, **_: text
    )  # simulate a broken redactor
    fake = FakeLLM(default="{}")
    with pytest.raises(PHILeakError):
        RedactingLLM(fake).complete([Message("user", "John Doe SSN 123-45-6789")])
    assert fake.prompts == []  # nothing was sent


def test_fake_llm_matching_queue_and_exhaustion() -> None:
    fake = FakeLLM(responses={"ct": "A"}, queue=["B"])
    assert fake.complete([Message("user", "how many CT")]) == "A"
    assert fake.complete([Message("user", "other")]) == "B"
    with pytest.raises(LLMError):
        fake.complete([Message("user", "other")])


def test_openai_compatible_provider() -> None:
    with respx.mock() as router:
        route = router.post("http://vllm.test/v1/chat/completions").respond(
            json={"choices": [{"message": {"content": '{"steps": []}'}}]}
        )
        llm = OpenAICompatibleLLM("http://vllm.test/v1", "llama-3-70b", "k")
        assert llm.complete([Message("user", "q")]) == '{"steps": []}'
        body = json.loads(route.calls.last.request.content)
        assert body["model"] == "llama-3-70b" and body["temperature"] == 0
        assert route.calls.last.request.headers["authorization"] == "Bearer k"
        router.post("http://vllm.test/v1/chat/completions").respond(500)
        with pytest.raises(LLMError):
            llm.complete([Message("user", "q")])


def test_azure_provider() -> None:
    with respx.mock() as router:
        route = router.post(
            "https://aoai.test/openai/deployments/gpt-4o/chat/completions?api-version=2024-10-21"
        ).respond(json={"choices": [{"message": {"content": "{}"}}]})
        llm = AzureOpenAILLM("https://aoai.test", "gpt-4o", "secret", "2024-10-21")
        assert llm.complete([Message("system", "s"), Message("user", "q")]) == "{}"
        req = route.calls.last.request
        assert req.headers["api-key"] == "secret" and "model" not in json.loads(req.content)
        route.mock(side_effect=httpx.ConnectError("x"))
        with pytest.raises(LLMError):
            llm.complete([Message("user", "q")])


def _settings(**kw: Any) -> CopilotSettings:
    return CopilotSettings.model_validate(kw)


def test_build_provider() -> None:
    assert build_provider(_settings(LLM_PROVIDER="fake")).name == "fake"
    assert build_provider(_settings(LLM_PROVIDER="heuristic")).name == "heuristic"
    assert (
        build_provider(
            _settings(
                LLM_PROVIDER="azure", AZURE_OPENAI_ENDPOINT="https://a", AZURE_OPENAI_API_KEY="k"
            )
        ).name
        == "azure"
    )
    assert (
        build_provider(_settings(LLM_PROVIDER="openai_compatible", LLM_BASE_URL="http://v")).name
        == "openai_compatible"
    )
    with pytest.raises(LLMError, match="AZURE"):
        build_provider(_settings(LLM_PROVIDER="azure"))
    with pytest.raises(LLMError, match="LLM_BASE_URL"):
        build_provider(_settings(LLM_PROVIDER="openai_compatible"))
