# Themes and appearance

Every page has a colour theme: `light` or `dark`. The theme is a setting like any other,
so it follows the normal inheritance rules (see *Settings and inheritance*).

## Checking a page's theme

Ask the assistant "What theme is the login page?". It reads the page's effective theme and
tells you where the value comes from: the page itself, the tenant default, the tenant's
template, or the built-in default.

## Changing a theme

Editors and admins can set a page's theme, for example "Set the dashboard to dark".
Setting a theme to the value it already has changes nothing, so repeating a request is safe.

To toggle, the assistant first reads the current theme and then sets the opposite value.
This avoids flipping the theme twice when a request is retried.

## Dark mode for every page

To make every page dark, set the tenant-wide default theme instead of each page. Pages
without their own override pick it up immediately. An admin can also change several pages
at once with a bulk change, which needs confirmation.

## Appearance and accessibility

The `light` theme suits bright environments and printed screenshots. The `dark` theme
reduces glare in low light. Both themes meet the same contrast requirements.
