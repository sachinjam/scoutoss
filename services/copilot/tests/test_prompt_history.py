"""Earlier cohorts reach the LLM only as C1, C2... references, never as database IDs."""

from __future__ import annotations

from scoutoss_copilot.clients import static_schema
from scoutoss_copilot.prompt import build_messages, resolve_cohort_ref

ID_A = "58239610-c448-40b6-a50d-1554bb2bd596"
ID_B = "0b6f1c2e-9d4a-4e1f-8a77-3c2d1e0f9a8b"

HISTORY = [
    {"question": "CT chest", "plan": {"steps": []}, "cohort_id": ID_A},
    {
        "question": "of those, female",
        "plan": {"steps": [], "base_cohort_id": ID_A},
        "cohort_id": ID_B,
    },
    {"question": "rejected turn without a cohort", "plan": {"steps": []}},
]


def test_history_uses_references_not_ids() -> None:
    messages = build_messages("and of those?", static_schema(), HISTORY)
    sent = "\n".join(m.content for m in messages)
    assert ID_A not in sent and ID_B not in sent
    assistant = [m.content for m in messages if m.role == "assistant"]
    assert '"cohort_id": "C1"' in assistant[0]
    # the stored plan already pointed at the first cohort: shown as its reference
    assert '"base_cohort_id": "C1"' in assistant[1] and '"cohort_id": "C2"' in assistant[1]
    assert '"cohort_id": null' in assistant[2]


def test_unknown_base_cohort_in_history_is_dropped() -> None:
    history = [
        {"question": "q", "plan": {"steps": [], "base_cohort_id": "gone"}, "cohort_id": ID_B}
    ]
    sent = build_messages("q2", static_schema(), history)[-2].content
    assert '"base_cohort_id": null' in sent and "gone" not in sent


def test_resolve_cohort_ref() -> None:
    assert resolve_cohort_ref("C1", HISTORY) == ID_A
    assert resolve_cohort_ref(" c2 ", HISTORY) == ID_B
    assert resolve_cohort_ref("C3", HISTORY) == "C3"  # turn 3 had no cohort: lookup will fail
    assert resolve_cohort_ref("gone", HISTORY) == "gone"
    assert resolve_cohort_ref(None, HISTORY) is None
