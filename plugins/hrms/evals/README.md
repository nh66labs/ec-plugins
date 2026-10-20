# HRMS plugin — evaluation cases

What the assistant must do with the leave sentences this plugin exists for,
graded by the platform's evaluation run (`python -m app.evals`, EC-D110; card
checks EC-D186). Each case checks the card the answer laid out — which tool,
whether it waits for choices or for Confirm, what it carries, and exactly which
choices it asks for — so asking again for what was already said fails.

**Nothing is filed.** An evaluation never confirms a card, and deleting its
conversation takes the card with it. Reads (balance, holidays, the check before
a proposal) do reach the HRMS, as the person the run asks as.

## Running

Inside the platform's stack, in a Space that has the HRMS plugin, as people the
HRMS knows:

```bash
docker compose cp employee-cases.json runner:/tmp/employee-cases.json
docker compose exec runner python -m app.evals run /tmp/employee-cases.json \
    --space "SPACE NAME" --as EMPLOYEE@EMAIL --repeat 3

docker compose cp manager-cases.json runner:/tmp/manager-cases.json
docker compose exec runner python -m app.evals run /tmp/manager-cases.json \
    --space "SPACE NAME" --as PROJECT.MANAGER@EMAIL --repeat 3

docker compose cp hr-cases.json runner:/tmp/hr-cases.json
docker compose exec runner python -m app.evals run /tmp/hr-cases.json \
    --space "SPACE NAME" --as HRMS.ADMIN@EMAIL --repeat 3
```

Compare two runs with `python -m app.evals compare BEFORE.json AFTER.json`.

## What they assume

- **employee-cases**: the employee has one project manager or several (every
  one of them approves, so the form never asks who); a balance of casual
  leave; next Tuesday free of their own leave and of holidays; Sunday a day
  off, and 25 December a company holiday named Christmas.
- **manager-cases**: at least one request waiting on the manager.
  `approve-as-one-of-several-approvers` also needs a request filed under
  "every project manager must approve" with three approvers: one who has
  approved, the manager asked as, and a third still to decide.
  `pending-work-in-my-projects-only` needs the waiting request's applicant on
  two projects linked to Jira in the HRMS (Jira settings), only one of them the
  manager's, with open tickets in both.
- **hr-cases**: asked as an HRMS administrator; exactly one employee whose name
  contains "Navaneeth", with some approved leave in the last six months and the
  last quarter (change the name for another HRMS). Graded on what is never said
  — a reason, a motive, a judgement — since the figures change with the data.
  `trend-is-laid-out-as-a-card` also needs leave in two months or more of the
  last twelve, and checks the card's fixed labels, not its figures: compare
  those with the result by hand.
- **employee-cases**' `trend-of-a-colleague-is-refused`: the employee is not an
  HRMS administrator, and "Dev User" is someone else.
- Dates are not checked: "next Friday" is worked out on the day of the run.
