# README Maintenance

`README.md` is part of the operational project surface. It should stay aligned with the app, not trail behind it.

## Update README When Changing

- setup commands
- `requirements.txt`
- environment variables
- routes
- Admin workflows
- PDF handling
- checklist data structure
- extract data structure
- audit log
- deployment process
- tests
- public UI behaviour
- security assumptions

## README Update Usually Not Needed For

- tiny CSS-only visual changes that do not affect usage
- internal refactors with no user/developer impact
- typo fixes

## Required Habit

For every change, review `README.md`.

If the README needs a change, update it in the same commit.

If the README does not need a change, say this in the final response or PR summary:

```text
README reviewed — no update required.
```

