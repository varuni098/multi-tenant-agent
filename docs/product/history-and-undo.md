# Change history and undo

Every change is recorded: who made it, what changed, the old and new value, and when.

## Viewing history

Editors and admins can ask "Show the recent changes" or "What changed on the login page?".
The newest changes come first. An empty old value means the page was inheriting before the
change.

## Undo

"Undo the last change" reverts the most recent setting change. You can also undo the last
change on one page only, for example "Undo the last change on the dashboard".

Undo can be repeated to step further back. Each undo is itself recorded in the history.
If a change created a new page override, undoing it removes the override, so the page
inherits again.

## What can't be undone

Deleting a page and switching templates are not undone this way. That's why they need
confirmation first.
