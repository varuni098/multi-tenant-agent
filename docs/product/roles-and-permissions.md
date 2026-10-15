# Roles and permissions

Every user belongs to one tenant and has one role. The assistant only offers the actions
your role allows, and every action is checked again on the server.

## Viewer

Viewers can read: list pages, check themes and settings, and ask why a value is what it
is. Viewers cannot change anything. A viewer asking for a change is told they don't have
permission, and nothing changes.

## Editor

Editors can do everything a viewer can, plus change themes and settings, read the change
history, and undo changes.

## Admin

Admins can do everything an editor can, plus create and delete pages, change a setting on
many pages at once, switch templates and preview a template switch. Deleting, bulk changes
and template switches need confirmation.

## Isolation between tenants

You can only see and change your own tenant's data. Mentioning another tenant's ID in a
request is refused before the assistant runs.
