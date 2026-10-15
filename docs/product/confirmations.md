# Confirming destructive actions

Some actions are hard to reverse, so they wait for your approval: deleting a page, changing
a setting on many pages at once, and switching templates.

## How confirmation works

1. You ask for the action, for example "Delete the billing page".
2. The assistant checks it and shows exactly what will happen: "Please confirm: Delete page
   'billing' and its 2 setting override(s)."
3. Nothing changes yet. You approve it in the app, which uses a single-use confirmation
   token.

## Rules for confirmation tokens

- A token can be used once. Using it again is rejected.
- A token expires after five minutes.
- Only the person who requested the action can approve it.
- The assistant itself can never approve an action.

## If something changed meanwhile

If the page was deleted or renamed between the request and your approval, the confirmation
fails safely and nothing changes. Ask again to get a fresh summary.
