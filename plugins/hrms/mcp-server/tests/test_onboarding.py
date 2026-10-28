"""Onboarding a new employee, against a fake HRMS that records what reached it.

What matters: HR reads choices and outcomes as sentences with no ids; the
create carries only what HR said, never PAN, Aadhaar or bank details; a repeat
reports the first create without making a second; a clash with anyone else is
refused before anything reaches the HRMS's create; and the HRMS's own refusal
(not an admin, a bad field) reaches HR as it said it.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.testclient import TestClient

from hrms_mcp import onboarding
from tests.test_server import (  # noqa: F401, F811 - pytest fixtures
    FakeHrms,
    call,
    client,
    hrms,
    rpc,
    text,
)

OPTIONS = {
    "designations": [
        {"id": "d-1", "name": "HR Manager"}, {"id": "d-2", "name": "Software Engineer"},
    ],
    "companies": [{"id": "c-1", "name": "Hamon"}],
    "statuses": [{"id": "s-1", "name": "Active"}, {"id": "s-2", "name": "Probation"}],
    "company_email_domain": "hamon.in",
}

ETHAN = {
    "name": "Ethan Parker",
    "company_email": "EthanParker@hamon.in",
    "employee_code": "H101",
    "designation": "Software Engineer",
    "company": "Hamon",
    "date_of_joining": "2026-11-02",
    "phone_number": "9876543210",
}

CREATED = {
    "created": True, "id": "emp-new", "name": "Ethan Parker", "employee_code": "H101",
    "company_email": "ethanparker@hamon.in", "designation": "Software Engineer",
    "company": "Hamon", "status": "Active", "date_of_joining": "2026-11-02",
}


def _sent(fake: FakeHrms, tool: str) -> list[dict[str, Any]]:
    return [c["arguments"] for c in fake.calls() if c["name"] == tool]


def test_the_options_are_names_and_the_domain_with_no_ids(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["get_employee_form_options"] = OPTIONS
    said = text(call(client, "get_new_employee_options"))
    assert "Designations: HR Manager, Software Engineer" in said
    assert "Statuses: Active, Probation (Active unless HR says otherwise)" in said
    assert "must be an address at hamon.in" in said
    assert "d-1" not in said and "c-1" not in said and "s-1" not in said


def test_a_check_with_nothing_taken_says_so(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["find_existing_employee"] = {"matches": []}
    said = text(call(client, "check_new_employee", {"company_email": " ethanparker@hamon.in "}))
    assert said == "None of these details is used by anyone yet."
    assert _sent(hrms, "find_existing_employee") == [{"company_email": "ethanparker@hamon.in"}]


def test_a_check_names_who_has_each_detail(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["find_existing_employee"] = {"matches": [
        {"matched_on": "company_email", "id": "emp-9", "name": "Ethan Parker",
         "employee_code": "H090", "company_email": "ethanparker@hamon.in", "deactivated": True},
        {"matched_on": "employee_code", "id": "emp-3", "name": "Anu Thomas",
         "employee_code": "H101", "company_email": "anu@hamon.in", "deactivated": False},
    ]}
    said = text(call(client, "check_new_employee",
                     {"company_email": "ethanparker@hamon.in", "employee_code": "H101"}))
    assert "The company email is already used by Ethan Parker (H090), who is deactivated." in said
    assert "The employee code is already used by Anu Thomas (H101)." in said
    assert "emp-9" not in said and "emp-3" not in said


def test_a_create_carries_only_what_hr_said(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["find_existing_employee"] = {"matches": []}
    hrms.results["create_employee"] = CREATED
    result = call(client, "create_employee", ETHAN)
    said = text(result)
    assert not result.get("isError"), said
    assert said == (
        "Created. Ethan Parker (H101): Software Engineer at Hamon, joining Mon 2 Nov 2026, "
        "status Active. They sign in to HR-Assist with ethanparker@hamon.in."
    )
    [sent] = _sent(hrms, "create_employee")
    assert sent == {**ETHAN, "company_email": "ethanparker@hamon.in"}
    # Never an id naming the person, never a sensitive detail.
    assert not {"employee_id", "caller_id", "pan", "aadhar", "bank_detail"} & set(sent)
    assert "emp-new" not in said


def test_status_and_personal_email_go_only_when_given(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["find_existing_employee"] = {"matches": []}
    hrms.results["create_employee"] = {**CREATED, "status": "Probation"}
    call(client, "create_employee",
         {**ETHAN, "status": "Probation", "personal_email": " Ethan.P@Home.example "})
    [sent] = _sent(hrms, "create_employee")
    assert sent["status"] == "Probation"
    assert sent["personal_email"] == "ethan.p@home.example"
    [looked] = _sent(hrms, "find_existing_employee")
    assert looked["personal_email"] == "ethan.p@home.example"


def test_the_company_email_is_never_taken_as_the_personal_one(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    result = call(client, "create_employee", {**ETHAN, "personal_email": "ethanparker@hamon.in"})
    assert result["isError"]
    assert "personal email is the company email" in text(result)
    assert hrms.calls() == []


def test_a_repeat_reports_the_first_and_creates_nothing(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.results["find_existing_employee"] = {"matches": [
        {"matched_on": "company_email", "id": "emp-new", "name": "Ethan Parker",
         "employee_code": "H101", "company_email": "ethanparker@hamon.in", "deactivated": False},
        {"matched_on": "employee_code", "id": "emp-new", "name": "Ethan Parker",
         "employee_code": "H101", "company_email": "ethanparker@hamon.in", "deactivated": False},
    ]}
    said = text(call(client, "create_employee", ETHAN))
    assert said == "Already created — nothing was changed. Ethan Parker (H101) is in the HRMS."
    assert _sent(hrms, "create_employee") == []


def test_the_hrms_saying_already_created_is_reported_as_such(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    """Two deliveries can race past the check; the HRMS then reports the first."""
    hrms.results["find_existing_employee"] = {"matches": []}
    hrms.results["create_employee"] = {**CREATED, "created": False}
    said = text(call(client, "create_employee", ETHAN))
    assert said.startswith("Already created — nothing was changed. Ethan Parker (H101):")


def test_a_clash_with_someone_else_is_refused_before_the_create(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.results["find_existing_employee"] = {"matches": [
        {"matched_on": "employee_code", "id": "emp-3", "name": "Anu Thomas",
         "employee_code": "H101", "company_email": "anu@hamon.in", "deactivated": False},
    ]}
    result = call(client, "create_employee", ETHAN)
    assert result["isError"]
    said = text(result)
    assert "The employee code is already used by Anu Thomas (H101)." in said
    assert "Nothing was created." in said
    assert _sent(hrms, "create_employee") == []


def test_the_hrms_refusal_reaches_hr_as_it_said_it(
    client: TestClient, hrms: FakeHrms, caplog  # noqa: F811
) -> None:
    hrms.results["find_existing_employee"] = {"matches": []}
    hrms.errors["create_employee"] = "the company email must be an address at hamon.in"
    with caplog.at_level(logging.INFO, logger="hrms_mcp"):
        result = call(client, "create_employee", {**ETHAN, "company_email": "ethan@gmail.com"})
    assert result["isError"]
    assert "must be an address at hamon.in" in text(result)
    # The outcome is logged, never what HR typed.
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "create_employee refused" in logged
    assert "9876543210" not in logged and "gmail" not in logged


def test_only_an_admin_gets_anything(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    """A read tells anyone else so as an answer, which the reply can cite — a
    failed call cites nothing, and the platform would then show a generic
    "couldn't find anything". The create, run only after Confirm, refuses."""
    for tool in ("get_employee_form_options", "find_existing_employee", "create_employee"):
        hrms.errors[tool] = "access denied: admin only"
    for tool, args in (
        ("get_new_employee_options", {}),
        ("check_new_employee", {"company_email": "ethanparker@hamon.in"}),
    ):
        result = call(client, tool, args)
        assert not result.get("isError"), tool
        assert text(result) == onboarding.NOT_HR_ADMIN, tool
    result = call(client, "create_employee", ETHAN)
    assert result["isError"]
    assert onboarding.NOT_HR_ADMIN in text(result)
    assert _sent(hrms, "create_employee") == []


def test_other_refusals_still_fail(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    hrms.errors["get_employee_form_options"] = "The HRMS did not answer in time."
    result = call(client, "get_new_employee_options")
    assert result["isError"]
    assert "did not answer in time" in text(result)


def test_the_new_tools_carry_no_person_and_the_create_is_a_write(client: TestClient) -> None:  # noqa: F811
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    create = tools["create_employee"]
    assert create["annotations"]["readOnlyHint"] is False
    assert set(create["inputSchema"]["required"]) == {
        "name", "company_email", "employee_code", "designation", "company",
        "date_of_joining", "phone_number",
    }
    for name in ("get_new_employee_options", "check_new_employee", "create_employee"):
        props = set(tools[name]["inputSchema"].get("properties", {}))
        assert not {"employee_id", "caller_id", "pan", "aadhar", "bank_detail"} & props, name


def _descriptions(client: TestClient) -> dict[str, str]:  # noqa: F811
    tools = rpc(client, "tools/list").json()["result"]["tools"]
    return {t["name"]: " ".join(t["description"].split()) for t in tools}


def test_the_onboarding_guide_reaches_the_model(client: TestClient) -> None:  # noqa: F811
    """The conversation with HR is said in the descriptions, which the model
    reads in full every turn — the server's instructions have no room left."""
    said = _descriptions(client)
    start = said["get_new_employee_options"]
    # What it is comes first: an agent's tool picker sees only 240 characters.
    assert start.startswith("Start here to onboard a new employee")
    assert "HRMS admins only" in start[:240]
    for rule in (
        "Never ask again for anything they already said",
        "call this and check_new_employee with the company email, together",
        "Ask, in one short message, only for what is still missing",
        "Only one company: use it, do not ask",
        "The status is Active: never ask about it",
        "The company email is never one",
        "Never ask for, or pass on, PAN, Aadhaar, bank details",
        "call get_leave_context and take the date from it",
        'never ask "shall I go ahead?" yourself',
    ):
        assert rule in start, rule
    assert "a deactivated employee still holds theirs" in said["check_new_employee"]
    create = said["create_employee"]
    assert "uploads their documents there" in create
    assert "do not retry the same details" in create
    assert "anyone else is told so, and nothing is created" in create


def test_the_server_instructions_still_fit(client: TestClient) -> None:  # noqa: F811
    result = rpc(
        client, "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {},
         "clientInfo": {"name": "enterprise-claw", "version": "2"}},
    ).json()["result"]
    assert len(result["instructions"]) <= 4_000
    assert "onboard" not in result["instructions"].lower()


# --- documents are never taken in the chat ------------------------------------------


def test_the_answer_links_straight_to_their_own_profile() -> None:
    said = onboarding.documents_answer("https://hr.example.com/", "emp-0042")
    assert "Upload them on My Profile → Documents in HR-Assist: " in said
    assert "https://hr.example.com/employees/emp-0042" in said
    assert "not taken in this chat" in said
    assert "Only you and HR can see them" in said
    # Without the person, the dashboard; without the address, the page by name.
    assert "HR-Assist (https://hr.example.com), under My Profile → Documents" in (
        onboarding.documents_answer("https://hr.example.com")
    )
    assert "HR-Assist, under My Profile → Documents" in onboarding.documents_answer("")


def test_where_to_upload_is_a_read_anyone_gets(client: TestClient, hrms: FakeHrms) -> None:  # noqa: F811
    result = call(client, "where_to_upload_documents")
    assert not result.get("isError")
    assert "My Profile → Documents" in text(result)
    # It only reads who is asking; nothing is filed.
    assert [c["name"] for c in hrms.calls()] == ["get_user_session"]


def test_where_to_upload_answers_even_when_the_hrms_does_not(
    client: TestClient, hrms: FakeHrms  # noqa: F811
) -> None:
    hrms.errors["get_user_session"] = "The HRMS did not answer in time."
    result = call(client, "where_to_upload_documents")
    assert not result.get("isError")
    assert "My Profile → Documents" in text(result)


def test_where_to_upload_is_called_for_any_question_about_sending_a_document(
    client: TestClient,  # noqa: F811
) -> None:
    said = _descriptions(client)["where_to_upload_documents"]
    assert said.startswith("Where an employee uploads their documents")
    assert "or sends a file in the chat" in said
    assert "Never ask anyone to send a document" in said


def test_hr_is_told_what_became_of_the_setup_email() -> None:
    record = {**CREATED}
    assert "setup email" not in onboarding.created_line(record)
    for state, said in (
        ("sending", "on its way to their personal email"),
        ("no_personal_email", "press Retry on Setup email under Onboarding"),
        ("switched_off", "switched off on this server"),
        ("not_set_up", "email is not set up on this server"),
    ):
        assert said in onboarding.created_line({**record, "setup_email": state}), state


def test_an_unknown_or_missing_setup_email_state_is_never_silence() -> None:
    record = {**CREATED}
    # A state this plugin does not know: HR is told to check, not left to assume.
    said = onboarding.created_line({**record, "setup_email": "bounced_somehow"})
    assert "don't know" in said and "Onboarding card" in said
    # No tracker at all: said so, with what to do.
    said = onboarding.created_line({**record, "onboarding_started": False})
    assert "onboarding could not be started" in said and "no setup email was sent" in said
    # An older HRMS that sends neither says nothing, since nothing can be relied on.
    assert "setup email" not in onboarding.created_line(record)


def test_the_guide_asks_hr_for_the_personal_email(client: TestClient) -> None:  # noqa: F811
    said = _descriptions(client)["get_new_employee_options"]
    assert "Ask for their personal email too — the setup email goes there" in said

