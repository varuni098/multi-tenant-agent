# Settings and inheritance

Each page has five settings: `theme`, `language`, `layout`, `brand_color` and `show_tips`.
A page does not need to set all of them. Missing values are inherited.

## How a value is chosen

The effective value of a setting is the first one found, from most to least specific:

1. **Page override:** set directly on the page.
2. **Tenant default:** set once for the whole tenant.
3. **Template:** the value from the tenant's template (for example `standard`).
4. **Built-in default:** the setting's own default.

A page override always wins. Removing it (for example with undo) makes the page inherit
again.

## Explaining a value

Ask "Why is the settings page light?" and the assistant explains the chain: which level
supplied the value and which other levels it overrides. This comes from the knowledge
graph, so it reflects the current configuration.

## Tenant-wide defaults

Setting a value without naming a page sets the tenant default. Every page without its own
override uses it. This is the quickest way to change many pages consistently.

## Valid values

| Setting | Values | Default |
|---|---|---|
| theme | light, dark | light |
| language | en, es, fr, de | en |
| layout | comfortable, compact | comfortable |
| brand_color | a hex colour such as #2563eb | #2563eb |
| show_tips | true, false | true |

Values are case-insensitive: "Dark" is stored as `dark`. Anything else is rejected with the
list of valid values.
