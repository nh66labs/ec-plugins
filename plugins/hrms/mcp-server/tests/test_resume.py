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
