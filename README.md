# CTFd Campus Audiences

Plugin for **CTFd 3.8.9** that provides campus-based audience membership and tag-based challenge modules.

> CTFd 3.8.9 does not include native Audience/Module models. This plugin creates its own tables and enforces access at the API and file-download layers. It does not modify CTFd core files.

## Features

- Assign a user to an audience from a user Custom Field (default: `campus`).
- Assign a challenge to a module from its tags.
- Associate each module with one or more audiences.
- Synchronize existing users and challenges at startup or from the admin plugin page.
- React automatically to Custom Field entry and challenge tag insert/update/delete events.
- Restrict challenge list/details, nested challenge APIs, standalone hint/solution APIs and unlocks, attempts, challenge files, and solution files.
- Keep unconfigured challenges public.
- Deny regular users access to a challenge carrying tags mapped to multiple modules (fail closed).
- Administrators can still access all challenges.

## Install

Clone this repository into the CTFd plugins directory:

```bash
cd /path/to/CTFd/CTFd/plugins
git clone https://github.com/CyberNight-Efrei/ctfd-campus-audiences.git ctfd_campus_audiences
```

Edit `settings.json` and restart CTFd. The plugin creates its database tables and synchronizes existing records on startup.

The plugin appears in **Admin Panel → Plugins → Campus Audiences**. Use the synchronization button after editing `settings.json` or when you want to rebuild all assignments.

## Configuration

Example `settings.json`:

```json
{
  "user_field": "campus",
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
```

Rules:

- Mapping keys are case-insensitive and whitespace-trimmed.
- `campus_to_audience` maps a Custom Field value to an audience name.
- `tag_to_module` maps a challenge tag to a module name.
- `module_to_audiences` defines which audiences may access a module.
- If a module is absent from `module_to_audiences`, it defaults to an audience with the same name.
- An empty audience list makes a module inaccessible to all regular users.
- A user whose Custom Field is empty or unmapped belongs to no plugin-managed audience.
- A challenge with no mapped tag is not assigned to a module and remains public.

## Database tables

The plugin creates:

- `campus_audiences`
- `campus_audience_members`
- `campus_modules`
- `campus_module_audience_access`
- `campus_module_challenges`

No manual SQL migration is required; CTFd's SQLAlchemy metadata creates missing plugin tables during plugin loading.

## Synchronization details

User membership is recalculated when a `UserFieldEntries` row is inserted, updated, or deleted. Challenge module assignment is recalculated when a `Tags` row is inserted, updated, or deleted.

The initial/manual synchronization also handles pre-existing users and challenges. The plugin owns its membership and assignment tables; do not use them for unrelated manual grants, since synchronization replaces those records.

## Compatibility and security notes

- Targeted at CTFd **3.8.9**.
- Intended for individual user-based campus assignment. In Teams mode, access is still evaluated per logged-in user, while CTFd's solve/account model remains team-based.
- The plugin filters the CTFd challenge API and blocks direct access to restricted challenge APIs and challenge/solution file downloads. It does not merely hide challenge cards in the browser.
- Back up the CTFd database before installing or upgrading plugins.
