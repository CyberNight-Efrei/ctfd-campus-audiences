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
- Automatically resync when a user Custom Field entry or a challenge tag is inserted, updated, or deleted.
- In CTFd Teams mode, assign a team only when **all current team members resolve to the same audience**. A mixed-campus team receives no campus audience.
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
- \`user_field\` is the name of a **User Custom Field**, not its label in the UI if those differ.
- \`campus_to_audience\` maps the Custom Field value to an audience name.
- \`tag_to_module\` maps a challenge tag to a module name.
- \`module_to_audiences\` defines which audiences may access a module.
- If a module is missing from \`module_to_audiences\`, it defaults to an audience with the same name.
- An explicit empty list (\`"Module X": []\`) means the plugin creates no access link for that module. Any independent manual access link already present in CTFd is preserved.
- A blank or unmapped Custom Field gives the user no plugin-managed audience.
- A challenge without a tag matching \`tag_to_module\` receives no plugin-managed module assignment. In native mode, any independent manual \`module_id\` assignment is left untouched.

## Teams mode behavior

CTFd's Teams mode shares solves and challenge access across the team. When a matching Team Custom Field exists (`team_field`, defaulting to `user_field`), the plugin uses that team's value to assign the team to one audience. The team must still contain the players who should inherit that access. An empty or unmapped Team Custom Field gives the team no campus audience. If no matching Team Custom Field exists, the compatibility fallback only adds the team to an audience when every member has a Custom Field value that maps to the same audience.

In Users mode, each user is assigned independently.

## Synchronization and ownership

- User Custom Field changes update the affected user or team membership immediately.
- Team Custom Field changes update the affected team's audience membership immediately.
- User team changes recalculate both the old and new team's audience.
- Challenge tag changes update the plugin-managed module assignment immediately.
- Startup/manual synchronization also processes records that existed before the plugin was installed.
- The plugin stores ownership markers so it removes only memberships/access links it created. Existing manually-added audience memberships and module access links are preserved.
- For modules configured by this plugin, an additional manually-created access link remains an independent grant. Remove such links in CTFd if the configured mapping must be the only way to access that module.

## Security

This plugin does not rely on hiding challenge cards with JavaScript. It enforces access on the server, including direct API requests and direct challenge/solution file URLs.

As with any CTFd plugin, back up the database before installation or upgrade. Runtime testing against a production instance should be performed in staging first.
