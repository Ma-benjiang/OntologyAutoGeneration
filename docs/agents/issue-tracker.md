# Issue tracker: GitHub

Issues and engineering tickets live in the GitHub repository configured as the
current checkout's `origin`.

Resolve the repository once per shell session instead of hardcoding an account:

```bash
REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
```

## Conventions

- Create:
  `gh issue create --repo "$REPO" --title "..." --body "..."`
- Read:
  `gh issue view <number> --repo "$REPO" --comments`
- List:
  `gh issue list --repo "$REPO" --state all --limit 100`
- Comment:
  `gh issue comment <number> --repo "$REPO" --body "..."`
- Apply labels:
  `gh issue edit <number> --repo "$REPO" --add-label "..."`
- Remove labels:
  `gh issue edit <number> --repo "$REPO" --remove-label "..."`
- Close:
  `gh issue close <number> --repo "$REPO"`
- Reopen:
  `gh issue reopen <number> --repo "$REPO"`

## Pull requests as a triage surface

**Pull requests as a request surface: no.**

## Skill terminology

- "Publish to the issue tracker" means create a GitHub issue.
- "Fetch the relevant ticket" means read the GitHub issue and its comments.
- Use issue descriptions and comments for specifications, decisions, and handoffs.
- For dependency tracking, place `Blocked by: #<number>` at the top of the issue description.

## Wayfinding operations

- **Map**: a GitHub issue labelled `wayfinder:map` with Destination, Notes, Decisions so far, Not yet specified, and Out of scope sections.
- **Child ticket**: an issue whose description begins with `Part of #<map>` and carries one `wayfinder:<type>` label.
- **Blocking**: place `Blocked by: #<number>` after the map pointer. A ticket is unblocked only when every listed blocker is closed.
- **Frontier query**: list open issues, keep children of the map that have no assignee and no open `Blocked by` issue, then take the first in map order.
- **Claim**: `gh issue edit <number> --repo "$REPO" --add-assignee "@me"`.
- **Resolve**: post a resolution comment with `gh issue comment`, close the ticket, then append a named link and one-line gist to the map's Decisions so far section.
