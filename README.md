# CTFd Campus Audiences

CTFd plugin that automatically assigns **users or teams to audiences from a Custom Field** and **challenges to modules from tags**.

The plugin detects capabilities from the installed CTFd models rather than trusting the version string. This is useful when running a 3.8.0-based checkout with later commits cherry-picked or backported.

## Compatibility modes

### CTFd 3.8.0 without native Modules/Audiences

The plugin creates its own \`campus_*\` tables and applies the same access rules in the plugin. No CTFd core files or migrations need to be modified.

In this compatibility mode, plugin-managed modules/audiences are internal to the plugin and are not displayed in CTFd's native Modules/Audiences administration UI, because those native models and APIs do not exist in that checkout.

### Commits that include native Modules/Audiences

When \`Audiences\`, \`AudienceMembers\`, \`Modules\`, \`ModuleAudienceAccess\`, and \`Challenges.module_id\` are available, the plugin uses those native models. Challenge assignments are written to \`Challenges.module_id\`, and audience/module access is stored in CTFd's native tables.

The detection is feature-based, so a checkout reporting \`3.8.0\` can still use native models when they are present.

## Features

- Assign users to audiences from a user Custom Field (default: \`campus\`).
- Assign challenges to modules from their tags.
- Configure which audiences can access each module.
- Synchronize existing users and challenges at startup or from the admin plugin page.
- Automatically resync when a user or team Custom Field entry, team membership, or challenge tag changes.
- In CTFd Teams mode, use a non-empty Team Custom Field as the audience source of truth; fill it automatically from the members' non-empty User Custom Fields only when they all agree.
- Enforce module access on challenge lists, challenge details, nested challenge APIs, standalone hint/solution APIs, hint/solution unlocks, attempts, challenge files, and solution-file downloads.
- Keep challenges without a plugin-managed tag assignment public in the legacy compatibility mode.
- Deny regular users access to a challenge with tags mapped to multiple modules (fail closed).
- Administrators retain access to all challenges.

## Install

The plugin directory must be named \`ctfd_campus_audiences\`:

\`\`\`bash
cd /path/to/CTFd/CTFd/plugins
git clone https://github.com/CyberNight-Efrei/ctfd-campus-audiences.git ctfd_campus_audiences
\`\`\`

Edit \`settings.json\`, then restart CTFd. On startup the plugin creates its tracking tables, creates fallback tables only when native models are missing, and synchronizes existing users/challenges.

The plugin appears under **Admin Panel → Plugins → Campus Audiences**. Use the synchronization button after changing \`settings.json\` or to rebuild assignments manually.

## Configuration

Example \`settings.json\`:

\`\`\`json
{
  "user_field": "Campus",
  "team_field": "Campus",
  "campus_to_audience": {
    "paris": "Paris",
    "bordeaux": "Bordeaux"
  },
  "tag_to_module": {
    "paris": "Paris",
    "bordeaux": "Bordeaux"
  },
  "module_to_audiences": {
    "Paris": ["Paris"],
    "Bordeaux": ["Bordeaux"]
  }
}
\`\`\`

Rules:

- Mapping keys are case-insensitive and leading/trailing whitespace is ignored.
- Custom Field values are normalized case-insensitively; surrounding single/double quotes (including JSON-encoded strings such as `"Paris"`) are removed before mapping.
- \`user_field\` is the name of a **User Custom Field**, not its label in the UI if those differ.
- \`campus_to_audience\` maps the Custom Field value to an audience name.
- \`tag_to_module\` maps a challenge tag to a module name.
- \`module_to_audiences\` defines which audiences may access a module.
- If a module is missing from \`module_to_audiences\`, it defaults to an audience with the same name.
- An explicit empty list (\`"Module X": []\`) means the plugin creates no access link for that module. Any independent manual access link already present in CTFd is preserved.
- A blank or unmapped Custom Field gives the user no plugin-managed audience.
- A challenge without a tag matching \`tag_to_module\` receives no plugin-managed module assignment. In native mode, any independent manual \`module_id\` assignment is left untouched.

## Teams mode behavior

CTFd's Teams mode shares solves and challenge access across the team. When a matching Team Custom Field exists (`team_field`, defaulting to `user_field`), its non-empty value is the source of truth for the team's audience, even when none of the members has a User Custom Field value. Administrators can fill this Team Custom Field manually. When the Team Custom Field is empty, the plugin derives it from the members' User Custom Fields: at least one member must have a non-empty campus, and all non-empty normalized values must agree. Members with empty values are ignored. If two non-empty values conflict, or all members have an empty value, the Team Custom Field stays empty. Automatically generated values are tracked so the plugin can update or clear only its own values when member data changes; manually entered values are preserved and are never replaced by automatic derivation. If no matching Team Custom Field exists (or `team_field` is explicitly set to `null`), the compatibility fallback computes the audience directly from the unanimous non-empty member values without writing a Team Custom Field.

In Users mode, each user is assigned independently.

**Upgrade note:** older plugin versions did not track whether a Team Custom Field value was generated or entered manually. On the first synchronization after upgrading, a single existing value that exactly matches the current member-derived value is adopted as generated; other existing non-empty values are kept as manual overrides. A manual edit made through CTFd after this upgrade is explicitly recorded as manual, even when it matches the current member values.

## Synchronization and ownership

- User Custom Field changes recalculate the affected team's Team Custom Field and audience immediately, unless the Team Custom Field contains an explicit manual value.
- Team Custom Field insertions, edits, and deletions are synchronized immediately; non-empty values entered through CTFd are recorded as manual overrides.
- A non-empty manual Team Custom Field is authoritative for the team audience. When it is empty, the plugin fills it only if at least one member has a non-empty User Custom Field value and all non-empty values agree; empty member values are ignored.
- If non-empty member values conflict, or all members have an empty value, the plugin clears only its own previously generated Team Custom Field value and removes the team's plugin-managed campus audience. Manually entered values are preserved.
- User team changes recalculate the Team Custom Field from the new membership of both the old and new teams.
- Challenge tag changes update the plugin-managed module assignment immediately.
- Startup/manual synchronization also processes records that existed before the plugin was installed.
- The plugin stores ownership markers so it removes only memberships/access links it created. Existing manually-added audience memberships and module access links are preserved.
- For modules configured by this plugin, an additional manually-created access link remains an independent grant. Remove such links in CTFd if the configured mapping must be the only way to access that module.

## Security

This plugin does not rely on hiding challenge cards with JavaScript. It enforces access on the server, including direct API requests and direct challenge/solution file URLs.

As with any CTFd plugin, back up the database before installation or upgrade. Runtime testing against a production instance should be performed in staging first.
