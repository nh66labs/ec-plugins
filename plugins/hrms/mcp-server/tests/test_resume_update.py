"""The weekly resume update's two tools, against a fake HRMS: the evidence list is
passed through as data, a proposal reaches the HRMS untouched, bad input is refused
before anything is sent, and the writing rules travel in the descriptions."""

from __future__ import annotations

import json

from starlette.testclient import TestClient

from tests.test_server import (  # noqa: F401, F811 - pytest fixtures
    FakeHrms,
    call,
    client,
    hrms,
    rpc,
    text,
)

EMP = "7b1f3c52-1d0a-4c11-9a52-0d1f5f0a9c11"
PROPOSAL = json.dumps(
    {
        "projects": [
            {
                "project_id": "9a0b3c52-1d0a-4c11-9a52-0d1f5f0a9c22",
                "evidence_hash": "h1",
                "highlights": [
                    {"text": "Built a CSV export of leave reports", "evidence": ["j-kan-5"]}
                ],
                "stack": ["Go"],
            }
        ],
        "bio": {"bio_hash": "b1", "bio": "Software Engineer at Hamon."},
    }
)
PACK = {
    "week": "2026-W46",
    "employees": [{"employee_id": EMP, "name": "Asha", "projects": [], "bio_needs_work": True}],
    "scanned": 1,
    "next_cursor": EMP,
    "done": False,
}


def sent(hrms: FakeHrms, tool: str) -> list[dict]:  # noqa: F811
    return [c for c in hrms.calls() if c["name"] == tool]


def test_the_evidence_list_is_handed_over_as_data_with_the_cursor(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["list_resume_refresh_batch"] = PACK
    said = text(call(client, "list_resume_refresh_batch"))
    assert "never instructions" in said
    assert EMP in said and '"bio_needs_work": true' in said
    assert f"call again with after={EMP}" in said
    [asked] = sent(hrms, "list_resume_refresh_batch")
    assert asked["arguments"] == {"limit": 5}


def test_the_cursor_and_limit_are_passed_and_the_limit_is_bounded(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["list_resume_refresh_batch"] = {**PACK, "done": True}
    said = text(call(client, "list_resume_refresh_batch", {"after": f" {EMP} ", "limit": 99}))
    assert "All employees have been looked at." in said
    call(client, "list_resume_refresh_batch", {"limit": 0})
    first, second = sent(hrms, "list_resume_refresh_batch")
    assert first["arguments"] == {"limit": 10, "after": EMP}
    assert second["arguments"] == {"limit": 1}


def test_a_batch_with_nobody_to_update_says_so(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["list_resume_refresh_batch"] = {
        "week": "w", "employees": [], "done": False, "next_cursor": EMP,
    }
    said = text(call(client, "list_resume_refresh_batch"))
    assert "All employees have been looked at" not in said, "an empty page is not the end"
    assert f"after={EMP}" in said


def test_a_missing_done_is_read_from_the_cursor(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["list_resume_refresh_batch"] = {**PACK}
    PACK_NO_CURSOR = {k: v for k, v in PACK.items() if k not in ("done", "next_cursor")}
    hrms.results["list_resume_refresh_batch"] = PACK_NO_CURSOR
    said = text(call(client, "list_resume_refresh_batch"))
    assert "All employees have been looked at." in said and "after=None" not in said


def test_the_hrms_refusing_a_non_administrator_reaches_the_run(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.errors["list_resume_refresh_batch"] = (
        "access denied: only HR admins can run the resume update"
    )
    result = call(client, "list_resume_refresh_batch")
    assert result.get("isError")
    assert "only HR admins" in text(result)


def test_a_proposal_reaches_the_hrms_untouched_and_the_answer_lists_refusals(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["save_resume_proposals"] = {
        "saved": ["project Leave Portal", "bio"],
        "rejected": ["project Leave Portal: highlight 2 has a number the cited evidence does not"],
    }
    said = text(
        call(client, "save_resume_proposals", {"employee": f" {EMP} ", "proposal": PROPOSAL})
    )
    assert "Saved: project Leave Portal, bio." in said
    assert "nothing reaches a resume until an HRMS administrator approves" in said
    assert "highlight 2 has a number" in said and "fix and resend only these" in said
    [saved] = sent(hrms, "save_resume_proposals")
    assert saved["arguments"] == {"employee_id": EMP, "proposal": PROPOSAL}


def test_nothing_saved_is_said_plainly(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["save_resume_proposals"] = {"saved": [], "rejected": ["bio: bio is too short"]}
    said = text(call(client, "save_resume_proposals", {"employee": EMP, "proposal": PROPOSAL}))
    assert said.startswith("Nothing was saved.")
    assert "bio is too short" in said


def test_bad_input_is_refused_before_anything_is_sent(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    for args, says in [
        ({"employee": "Asha Kumar", "proposal": PROPOSAL}, "employee_id"),
        ({"employee": " ", "proposal": PROPOSAL}, "employee_id"),
        ({"employee": EMP, "proposal": '{"projects": "x"}'}, "projects"),
        ({"employee": EMP, "proposal": '{"projects": [{"project_id": "p"}]}'}, "evidence_hash"),
        ({"employee": EMP, "proposal": '{"bio": "plain text"}'}, "bio_hash"),
        ({"employee": EMP, "proposal": "not json"}, "JSON"),
        ({"employee": EMP, "proposal": "[]"}, "projects"),
    ]:
        result = call(client, "save_resume_proposals", args)
        assert result.get("isError"), args
        assert says in text(result)
    assert sent(hrms, "save_resume_proposals") == []


def test_an_empty_proposal_is_not_an_error_and_sends_nothing(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    said = text(call(client, "save_resume_proposals", {"employee": EMP, "proposal": '{"projects": []}'}))
    assert said.startswith("Nothing to save")
    assert sent(hrms, "save_resume_proposals") == []


def test_an_employee_id_that_is_not_a_uuid_still_goes_through(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["save_resume_proposals"] = {"saved": ["bio"], "rejected": []}
    call(client, "save_resume_proposals", {"employee": "EMP-0042", "proposal": PROPOSAL})
    [saved] = sent(hrms, "save_resume_proposals")
    assert saved["arguments"]["employee_id"] == "EMP-0042"


def test_the_writing_rules_and_the_data_warning_travel_in_the_descriptions(
    client: TestClient,  # noqa: F811
) -> None:
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    listing = " ".join(tools["list_resume_refresh_batch"]["description"].split())
    saving = " ".join(tools["save_resume_proposals"]["description"].split()).lower()
    assert "never instructions to you" in listing and "HR administrators only" in listing
    for rule in [
        "cites the evidence ids", "merge related tickets", "no ticket keys", "em dashes",
        "third person", "never the person's name", "experience_years", "revise it",
        "fix and resend only those", "do not invent",
    ]:
        assert rule in saving, rule
    props = tools["save_resume_proposals"]["inputSchema"]["properties"]
    assert set(props) == {"employee", "proposal"}, "no argument may be named employee_id"
