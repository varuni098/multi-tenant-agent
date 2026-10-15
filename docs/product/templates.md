# Templates

A template is a base configuration a tenant inherits from. Values in the template apply to
every page that has no override and no tenant default for that setting.

## Available templates

- **standard:** light theme, comfortable layout. New tenants start here.
- **dark_first:** dark theme, compact layout. Suits dashboards and operations screens.

## Previewing a template switch

Before switching, an admin can ask "What would change if we switched to the dark_first
template?". The assistant lists every page and setting whose effective value would change.
Pages with their own override are not affected by the template.

## Switching templates

Only admins can switch the tenant's template, and the switch needs confirmation. The
assistant shows a summary such as "Switch the tenant template from 'standard' to
'dark_first'". Nothing changes until you confirm it.

Overrides you have set on pages, and tenant defaults, keep their values after the switch.
