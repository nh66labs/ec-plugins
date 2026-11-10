"""Asking for a resume, against a fake HRMS: HR gets anyone's, everyone else only
their own; the file goes to the asker, and a name that fits several people is asked
about rather than guessed."""

from __future__ import annotations

from starlette.testclient import TestClient

from tests.test_server import (  # noqa: F401, F811 - pytest fixtures
    FakeHrms,
    call,
    client,
    hrms,
    rpc,
    text,
)

JOHN = {"id": "emp-7", "name": "John Doe", "employee_code": "H007", "company_email": "j@a.test"}
OTHER = {"id": "emp-8", "name": "John Smith", "employee_code": "H008", "company_email": "s@a.test"}
SENT = {"employee": "John Doe", "file": "John_Doe_Resume.docx", "sent_to": "your Slack DM"}


def sent_calls(hrms: FakeHrms) -> list[dict]:  # noqa: F811
    return [c for c in hrms.calls() if c["name"] == "get_employee_resume"]


def test_hr_gets_anyones_resume_with_the_summary_when_viewing(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["get_user_session"] = {"employee_id": "emp-1", "role": "admin"}
    hrms.results["resolve_employee"] = [JOHN]
    hrms.results["get_employee_resume"] = {**SENT, "summary": "John Doe - Engineer"}
    result = call(client, "get_resume", {"employee": "John Doe", "mode": "view"})
    assert not result.get("isError")
    said = text(result)
    assert "sent John Doe's resume to your Slack messages" in said
    assert "John Doe - Engineer" in said
    [sent] = sent_calls(hrms)
    assert sent["arguments"] == {"mode": "view", "employee_id": "emp-7"}


def test_download_says_the_file_was_sent_and_shows_no_summary(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["get_user_session"] = {"employee_id": "emp-1", "role": "admin"}
    hrms.results["resolve_employee"] = [JOHN]
    hrms.results["get_employee_resume"] = {**SENT, "summary": "hidden"}
    said = text(call(client, "get_resume", {"employee": "John Doe"}))
    assert "sent John Doe's resume" in said
    assert "hidden" not in said


def test_own_resume_needs_no_name(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["get_employee_resume"] = {**SENT, "employee": "Ravi"}
    result = call(client, "get_resume")
    assert not result.get("isError")
    assert "Ravi's resume" in text(result)
    [sent] = sent_calls(hrms)
    assert sent["arguments"] == {"mode": "download"}
    assert not [c for c in hrms.calls() if c["name"] == "resolve_employee"]


def test_a_name_that_fits_several_people_is_asked_about(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["get_user_session"] = {"employee_id": "emp-1", "role": "admin"}
    hrms.results["resolve_employee"] = [JOHN, OTHER]
    said = text(call(client, "get_resume", {"employee": "John"}))
    assert "John Doe (H007)" in said and "John Smith (H008)" in said
    assert not sent_calls(hrms)


def test_an_exact_name_beats_a_partial_match(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["get_user_session"] = {"employee_id": "emp-1", "role": "admin"}
    hrms.results["resolve_employee"] = [JOHN, OTHER]
    hrms.results["get_employee_resume"] = SENT
    call(client, "get_resume", {"employee": "john doe"})
    assert sent_calls(hrms)[0]["arguments"]["employee_id"] == "emp-7"


def test_a_non_admin_cannot_get_someone_elses(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["get_user_session"] = {"employee_id": "emp-1", "role": "employee"}
    hrms.results["resolve_employee"] = [JOHN]
    said = text(call(client, "get_resume", {"employee": "John Doe"}))
    assert "only get your own resume" in said
    assert not sent_calls(hrms)


def test_a_non_admin_naming_themselves_gets_their_own(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["get_user_session"] = {"employee_id": "emp-7", "role": "employee"}
    hrms.results["resolve_employee"] = [JOHN]
    hrms.results["get_employee_resume"] = SENT
    assert not call(client, "get_resume", {"employee": "John Doe"}).get("isError")
    assert sent_calls(hrms)


def test_no_such_employee_and_hrms_failures_are_errors_not_claims(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["get_user_session"] = {"employee_id": "emp-1", "role": "admin"}
    hrms.results["resolve_employee"] = []
    assert call(client, "get_resume", {"employee": "Nobody"}).get("isError")
    hrms.errors["get_employee_resume"] = "could not send the resume to your Slack DM"
    result = call(client, "get_resume")
    assert result.get("isError")
    assert "Slack DM" in text(result)


def test_it_runs_without_a_confirmation_and_takes_only_plain_strings(client: TestClient) -> None:  # noqa: F811
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    tool = tools["get_resume"]
    assert tool["annotations"]["readOnlyHint"] is True
    assert set(tool["inputSchema"]["properties"]) == {"employee", "mode"}


def test_the_description_steers_the_assistant_away_from_searching_documents(
    client: TestClient,  # noqa: F811
) -> None:
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    said = " ".join(tools["get_resume"]["description"].split())
    assert "Do NOT search the company's knowledge" in said
    assert "the one tool where HR names another person" in said


def test_a_failed_file_delivery_is_never_said_to_be_sent(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["get_employee_resume"] = {
        "employee": "Ravi", "summary": "Ravi - Engineer", "delivery_failed": "missing_scope",
    }
    said = text(call(client, "get_resume", {"mode": "view"}))
    assert "Ravi - Engineer" in said
    assert "couldn't send Ravi's resume file" in said
    assert "I've sent" not in said



RAW = {
    "employee": "John Doe",
    "file": "John_Doe_Resume.docx",
    "own": True,
    "file_after_wording": True,
    "to_refine": [
        {"ref": "a1b2c3d4", "section": "project highlight", "project": "TradeFlow",
         "text": "KAN-5 fix login bug"},
        {"ref": "e5f6a7b8", "section": "bio", "text": "Engineer at Co."},
    ],
    "to_refine_total": 2,
}
SAVED = {"saved": 2, "own": True, "employee": "John Doe", "sent_to": "your Slack DM"}


def save_calls(hrms: FakeHrms) -> list[dict]:  # noqa: F811
    return [c for c in hrms.calls() if c["name"] == "save_resume_wording"]


def test_raw_points_are_listed_as_data_and_the_file_is_held(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["get_employee_resume"] = RAW
    said = text(call(client, "get_resume"))
    first, _, points = said.partition("Points still in raw wording:")
    assert "held until its wording is saved" in first and "sent John Doe" not in first
    assert "a1b2c3d4" not in first
    assert "[a1b2c3d4] (project highlight — TradeFlow) KAN-5 fix login bug" in points
    assert "[e5f6a7b8] (bio) Engineer at Co." in points
    # The platform reads a tool's answer as quoted material: no instructions in it.
    assert "call save_resume_wording" not in said and "FOR YOU" not in said


def test_a_held_file_with_nothing_listed_is_released_not_lost(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["get_employee_resume"] = {**RAW, "to_refine": [{"text": "no ref"}]}
    hrms.results["save_resume_wording"] = {"saved": 0, "own": True, "sent_to": "your Slack DM"}
    said = text(call(client, "get_resume", {"mode": "view"}))
    assert "sent your resume to your Slack messages" in said
    [saved] = save_calls(hrms)
    assert saved["arguments"] == {"wording": "", "mode": "view"}


def test_someone_elses_held_file_is_never_handed_over_for_rewording(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["get_user_session"] = {"employee_id": "emp-1", "role": "admin"}
    hrms.results["resolve_employee"] = [JOHN]
    hrms.results["get_employee_resume"] = {**RAW, "own": False}
    said = text(call(client, "get_resume", {"employee": "John Doe"}))
    assert "nothing went to your Slack messages" in said
    assert "raw wording" not in said and "a1b2c3d4" not in said
    assert not save_calls(hrms)


def test_a_file_already_sent_lists_no_points(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["get_employee_resume"] = {**SENT, "to_refine": RAW["to_refine"]}
    said = text(call(client, "get_resume"))
    assert "sent John Doe's resume" in said and "raw wording" not in said


def test_get_resume_says_in_its_description_to_reword_and_save(client: TestClient) -> None:  # noqa: F811
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    described = " ".join(tools["get_resume"]["description"].split())
    assert "Points still in raw wording" in described
    assert "call save_resume_wording" in described and "without asking" in described
    assert "has NOT been sent yet" in described


def test_no_handoff_when_every_point_is_worded(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["get_employee_resume"] = SENT
    assert "raw wording" not in text(call(client, "get_resume"))


def test_saving_own_wording_names_no_one_and_says_where_the_file_went(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["save_resume_wording"] = SAVED
    wording = "[a1b2c3d4] Resolved a login defect\n[e5f6a7b8] Engineer building trade tools"
    result = call(client, "save_resume_wording", {"wording": wording})
    assert not result.get("isError")
    said = text(result)
    assert "Put 2 points of your resume in professional wording" in said
    assert "sent your resume to your Slack messages" in said
    assert "HR-Connect downloads use the new wording too" in said
    [saved] = save_calls(hrms)
    assert saved["arguments"] == {"wording": wording, "mode": "download"}
    assert not [c for c in hrms.calls() if c["name"] == "resolve_employee"]


def test_viewing_shows_the_reworded_summary(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["save_resume_wording"] = {**SAVED, "summary": "John Doe - Engineer"}
    said = text(call(client, "save_resume_wording", {"wording": "[a1b2c3d4] x", "mode": "view"}))
    assert said.startswith("John Doe - Engineer")
    assert save_calls(hrms)[0]["arguments"]["mode"] == "view"


def test_a_file_not_sent_is_never_said_to_be_sent(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["save_resume_wording"] = {"saved": 2, "own": True}
    said = text(call(client, "save_resume_wording", {"wording": "[a1b2c3d4] x"}))
    assert "was not sent" in said and "I've sent" not in said
    hrms.results["save_resume_wording"] = {
        "saved": 1, "own": True, "delivery_failed": "your email could not be found",
    }
    said = text(call(client, "save_resume_wording", {"wording": "[a1b2c3d4] x"}))
    assert "couldn't send the resume file" in said and "email could not be found" in said


def test_a_save_with_nothing_kept_still_says_the_file_was_sent(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["save_resume_wording"] = {
        "saved": 0, "own": True, "sent_to": "your Slack DM",
        "rejected": ["[a1b2c3d4] adds a number the original does not have"],
    }
    said = text(call(client, "save_resume_wording", {"wording": "[a1b2c3d4] Cut it by 40%"}))
    assert "sent your resume to your Slack messages as a Word file." in said
    assert "No new wording was saved" in said and "adds a number" in said


def test_saving_wording_is_an_own_resume_write_and_carries_the_rules(
    client: TestClient,  # noqa: F811
) -> None:
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    save = tools["save_resume_wording"]
    assert save["annotations"]["readOnlyHint"] is False
    assert "Use ONLY facts in the original point" in save["description"]
    assert "no" in save["description"] and "confirmation" in save["description"]
    assert "employee" not in save["inputSchema"]["properties"], "own resume only"
    assert tools["get_resume"]["annotations"]["readOnlyHint"] is True
