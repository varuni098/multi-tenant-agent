# Plans and features

Every tenant subscribes to a plan. Plans decide which features, and therefore which
settings, are available.

## Plans

| Plan | Features |
|---|---|
| free plan | themes, languages, layout, tips |
| pro plan | everything in free, plus custom branding |

## Custom branding

The `brand_color` setting requires the **custom_branding** feature, which only the pro plan
includes. On the free plan, a request to set a brand colour is refused before anything
changes, and the assistant names the rule: entitlement.

## Checking whether a change is allowed

Ask "Am I allowed to set the brand colour?" and the assistant checks the policy rules in
order: tenant active, permission, known setting, allowed value, entitlement. It reports the
first rule that fails, or that the change is allowed.

## Upgrading

Upgrading from the free plan to the pro plan unlocks custom branding immediately. Existing
settings are kept. Plan changes are made by your account owner, not through the assistant.
