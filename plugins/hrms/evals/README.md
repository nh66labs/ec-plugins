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
```

Compare two runs with `python -m app.evals compare BEFORE.json AFTER.json`.

## What they assume

- **employee-cases**: the employee has one project manager (with several, the
  form also asks who approves, and `asks` fails — add `approver` for such a
  person); a balance of casual leave; next Tuesday free of their own leave and
  of holidays.
- **manager-cases**: at least one request waiting on the manager.
- Dates are not checked: "next Friday" is worked out on the day of the run.
