import json
import logging
import os

from flask import abort, redirect, request, url_for
from sqlalchemy import and_, delete, event, select, text

from CTFd.models import (
    Challenges,
    Fields,
    Files,
    Hints,
    Solutions,
    Tags,
    TeamFieldEntries,
    UserFieldEntries,
    Users,
    db,
)
from CTFd.utils import get_config
from CTFd.utils.decorators import admins_only
from CTFd.utils.user import get_current_user, is_admin

PLUGIN_NAME = "ctfd_campus_audiences"
CONFLICT_MODULE_NAME = "__conflicting_tag_mapping__"
SYNC_LOCK_NAME = "ctfd_campus_audiences_sync"
SYNC_LOCK_TIMEOUT_SECONDS = 300
LOGGER = logging.getLogger("ctfd-campus-audiences")
_MODEL_EVENTS_REGISTERED = False

# CTFd has native Modules/Audiences on newer commits, but not in the 3.8.0
# release tag. Detect capabilities from the models instead of version strings.
try:
    from CTFd.models import (
        AudienceMembers as AudienceMemberModel,
        Audiences as AudienceModel,
        ModuleAudienceAccess as ModuleAccessModel,
        Modules as ModuleModel,
    )

    TeamAudienceMemberModel = AudienceMemberModel
    HAS_NATIVE_AUDIENCES_AND_MODULES = True
except ImportError:
    HAS_NATIVE_AUDIENCES_AND_MODULES = False

    class CampusAudience(db.Model):
        __tablename__ = "campus_audiences"

        id = db.Column(db.Integer, primary_key=True)
        name = db.Column(db.String(128), nullable=False, unique=True)
        description = db.Column(db.Text)

    class CampusAudienceMember(db.Model):
        __tablename__ = "campus_audience_members"

        id = db.Column(db.Integer, primary_key=True)
        audience_id = db.Column(
            db.Integer,
            db.ForeignKey("campus_audiences.id", ondelete="CASCADE"),
            nullable=False,
        )
        user_id = db.Column(
            db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        )

        __table_args__ = (
            db.UniqueConstraint(
                "audience_id", "user_id", name="uq_campus_audience_user"
            ),
        )

    class CampusTeamAudienceMember(db.Model):
        __tablename__ = "campus_team_audience_members"

        id = db.Column(db.Integer, primary_key=True)
        audience_id = db.Column(
            db.Integer,
            db.ForeignKey("campus_audiences.id", ondelete="CASCADE"),
            nullable=False,
        )
        team_id = db.Column(
            db.Integer, db.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
        )

        __table_args__ = (
            db.UniqueConstraint(
                "audience_id", "team_id", name="uq_campus_audience_team"
            ),
        )

    class CampusModule(db.Model):
        __tablename__ = "campus_modules"

        id = db.Column(db.Integer, primary_key=True)
        name = db.Column(db.String(128), nullable=False, unique=True)
        description = db.Column(db.Text)

    class CampusModuleAudienceAccess(db.Model):
        __tablename__ = "campus_module_audience_access"

        id = db.Column(db.Integer, primary_key=True)
        module_id = db.Column(
            db.Integer,
            db.ForeignKey("campus_modules.id", ondelete="CASCADE"),
            nullable=False,
        )
        audience_id = db.Column(
            db.Integer,
            db.ForeignKey("campus_audiences.id", ondelete="CASCADE"),
            nullable=False,
        )

        __table_args__ = (
            db.UniqueConstraint(
                "module_id", "audience_id", name="uq_campus_module_audience"
            ),
        )

    AudienceModel = CampusAudience
    AudienceMemberModel = CampusAudienceMember
    TeamAudienceMemberModel = CampusTeamAudienceMember
    ModuleModel = CampusModule
    ModuleAccessModel = CampusModuleAudienceAccess


HAS_NATIVE_CHALLENGE_MODULE_FIELD = (
    HAS_NATIVE_AUDIENCES_AND_MODULES
    and hasattr(Challenges, "module_id")
    and "module_id" in Challenges.__table__.columns
)


# These tables record which memberships/access links/challenge assignments are
# managed by this plugin. This lets sync remove only its own changes and leave
# unrelated manual grants intact.
class CampusManagedAudienceMember(db.Model):
    __tablename__ = "campus_managed_audience_members"

    id = db.Column(db.Integer, primary_key=True)
    audience_id = db.Column(db.Integer, nullable=False)
    user_id = db.Column(db.Integer, nullable=False)
    created_by_plugin = db.Column(db.Boolean, nullable=False, default=False)

    __table_args__ = (
        db.UniqueConstraint(
            "audience_id", "user_id", name="uq_campus_managed_audience_user"
        ),
    )


class CampusManagedTeamAudienceMember(db.Model):
    __tablename__ = "campus_managed_team_audience_members"

    id = db.Column(db.Integer, primary_key=True)
    audience_id = db.Column(db.Integer, nullable=False)
    team_id = db.Column(db.Integer, nullable=False)
    created_by_plugin = db.Column(db.Boolean, nullable=False, default=False)

    __table_args__ = (
        db.UniqueConstraint(
            "audience_id", "team_id", name="uq_campus_managed_audience_team"
        ),
    )


class CampusManagedTeamFieldEntry(db.Model):
    __tablename__ = "campus_managed_team_field_entries"

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, nullable=False)
    field_id = db.Column(db.Integer, nullable=False)
    entry_id = db.Column(db.Integer, nullable=True, unique=True)
    generated_value = db.Column(db.Text, nullable=True)
    is_manual = db.Column(db.Boolean, nullable=False, default=False)

    __table_args__ = (
        db.UniqueConstraint(
            "team_id", "field_id", name="uq_campus_managed_team_field"
        ),
    )


class CampusManagedModuleAccess(db.Model):
    __tablename__ = "campus_managed_module_access"

    id = db.Column(db.Integer, primary_key=True)
    module_id = db.Column(db.Integer, nullable=False)
    audience_id = db.Column(db.Integer, nullable=False)
    created_by_plugin = db.Column(db.Boolean, nullable=False, default=False)

    __table_args__ = (
        db.UniqueConstraint(
            "module_id",
            "audience_id",
            name="uq_campus_managed_module_audience",
        ),
    )


class CampusManagedChallenge(db.Model):
    __tablename__ = "campus_managed_challenges"

    id = db.Column(db.Integer, primary_key=True)
    challenge_id = db.Column(
        db.Integer,
        db.ForeignKey("challenges.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    module_id = db.Column(db.Integer, nullable=False)


def _settings_path():
    return os.path.join(os.path.dirname(__file__), "settings.json")


def _load_settings():
    path = _settings_path()
    if not os.path.isfile(path):
        return {
            "user_field": "campus",
            "campus_to_audience": {"paris": "Paris", "bordeaux": "Bordeaux"},
            "tag_to_module": {"paris": "Paris", "bordeaux": "Bordeaux"},
            "module_to_audiences": {
                "Paris": ["Paris"],
                "Bordeaux": ["Bordeaux"],
            },
        }

    with open(path, "r", encoding="utf-8") as settings_file:
        settings = json.load(settings_file)

    if not isinstance(settings, dict):
        raise ValueError("settings.json must contain a JSON object")

    for key in ("campus_to_audience", "tag_to_module"):
        if not isinstance(settings.get(key, {}), dict):
            raise ValueError("{} must be a JSON object".format(key))

    return settings


def _clean_custom_field_value(value):
    if not isinstance(value, str):
        return None

    value = value.strip()

    # Some CTFd Custom Field entries are persisted as JSON string literals.
    # Decode them so the team field receives the actual display value.
    if len(value) >= 2 and value[0] == value[-1]:
        if value[0] == '"':
            try:
                decoded = json.loads(value)
                if isinstance(decoded, str):
                    value = decoded.strip()
            except (TypeError, ValueError):
                # Be tolerant of legacy rows containing unmatched JSON escapes.
                value = value[1:-1].strip()
        elif value[0] == "'":
            value = value[1:-1].strip()

    return value or None


def _normalise(value):
    value = _clean_custom_field_value(value)
    return value.casefold() if value else None


def _unanimous_custom_field_value(values, member_count):
    # Empty values do not create a conflict: at least one member must provide
    # a campus, and every non-empty campus must agree after normalization.
    if member_count <= 0:
        return None

    cleaned_values = [
        cleaned
        for cleaned in (_clean_custom_field_value(value) for value in values)
        if cleaned is not None
    ]
    if not cleaned_values:
        return None

    if len({_normalise(value) for value in cleaned_values}) != 1:
        return None

    return cleaned_values[0]


def _team_field_value_was_manually_changed(current_value, generated_value):
    current_value = _clean_custom_field_value(current_value)
    return (
        current_value is not None
        and _normalise(current_value) != _normalise(generated_value)
    )


def _has_untracked_team_field_value(entries, managed_entry_id):
    return any(
        entry_id != managed_entry_id
        and _clean_custom_field_value(value) is not None
        for entry_id, value in entries
    )


def _normalised_mapping(mapping):
    result = {}
    for key, value in mapping.items():
        normalised_key = _normalise(key)
        if normalised_key and isinstance(value, str) and value.strip():
            result[normalised_key] = value.strip()
    return result


def _module_audience_mapping(settings):
    configured = settings.get("module_to_audiences")
    if configured is None:
        return {}

    if not isinstance(configured, dict):
        raise ValueError("module_to_audiences must be a JSON object")

    result = {}
    for module_name, audience_names in configured.items():
        if not isinstance(module_name, str) or not module_name.strip():
            continue
        if isinstance(audience_names, str):
            audience_names = [audience_names]
        if not isinstance(audience_names, list):
            raise ValueError(
                "module_to_audiences values must be strings or lists of strings"
            )
        result[_normalise(module_name)] = [
            name.strip()
            for name in audience_names
            if isinstance(name, str) and name.strip()
        ]
    return result


def _ensure_named_row(connection, model, name):
    table = model.__table__
    row = connection.execute(
        select(table.c.id).where(table.c.name == name)
    ).first()
    if row:
        return row[0]

    values = {"name": name}
    if "description" in table.c:
        values["description"] = "Managed by CTFd Campus Audiences"
    connection.execute(table.insert().values(**values))

    row = connection.execute(
        select(table.c.id).where(table.c.name == name)
    ).first()
    return row[0]


def _sync_configuration(connection, settings):
    campus_to_audience = _normalised_mapping(
        settings.get("campus_to_audience", {})
    )
    tag_to_module = _normalised_mapping(settings.get("tag_to_module", {}))
    module_to_audiences = _module_audience_mapping(settings)

    module_names = set(tag_to_module.values())
    configured_modules = settings.get("module_to_audiences") or {}
    module_names.update(
        name
        for name in configured_modules
        if isinstance(name, str) and name.strip()
    )
    module_names.add(CONFLICT_MODULE_NAME)

    audience_names = set(campus_to_audience.values())
    for names in module_to_audiences.values():
        audience_names.update(names)

    for audience_name in sorted(audience_names):
        _ensure_named_row(connection, AudienceModel, audience_name)

    for module_name in sorted(module_names):
        _ensure_named_row(connection, ModuleModel, module_name)

    access_table = ModuleAccessModel.__table__
    managed_table = CampusManagedModuleAccess.__table__

    # Build the desired set first. Keep unchanged managed relations in place:
    # deleting and recreating every link on every worker startup causes races
    # with other Gunicorn workers and needlessly churns native CTFd access rows.
    desired_links = set()
    for module_name in sorted(module_names):
        if module_name == CONFLICT_MODULE_NAME:
            continue

        configured_audiences = module_to_audiences.get(
            _normalise(module_name), [module_name]
        )
        module_id = _ensure_named_row(connection, ModuleModel, module_name)

        for audience_name in configured_audiences:
            audience_id = _ensure_named_row(
                connection, AudienceModel, audience_name
            )
            desired_links.add((module_id, audience_id))

    old_links = connection.execute(
        select(
            managed_table.c.module_id,
            managed_table.c.audience_id,
            managed_table.c.created_by_plugin,
        )
    ).all()

    managed_pairs = set()
    for old_link in old_links:
        pair = (old_link[0], old_link[1])
        if pair in desired_links:
            # This relation is still configured; do not delete/reinsert it.
            managed_pairs.add(pair)
            if old_link[2]:
                # Repair a plugin-managed relation if it was removed externally.
                existing = connection.execute(
                    select(access_table.c.id).where(
                        and_(
                            access_table.c.module_id == pair[0],
                            access_table.c.audience_id == pair[1],
                        )
                    )
                ).first()
                if existing is None:
                    connection.execute(
                        access_table.insert().values(
                            module_id=pair[0],
                            audience_id=pair[1],
                        )
                    )
            continue

        # Remove only a stale relation that this plugin originally created.
        if old_link[2]:
            connection.execute(
                delete(access_table).where(
                    and_(
                        access_table.c.module_id == pair[0],
                        access_table.c.audience_id == pair[1],
                    )
                )
            )

        # Drop our tracking row even when the actual relation was manual.
        connection.execute(
            delete(managed_table).where(
                and_(
                    managed_table.c.module_id == pair[0],
                    managed_table.c.audience_id == pair[1],
                )
            )
        )

    # Add only relations that are not already tracked by this plugin.
    for module_id, audience_id in sorted(desired_links - managed_pairs):
        existing = connection.execute(
            select(access_table.c.id).where(
                and_(
                    access_table.c.module_id == module_id,
                    access_table.c.audience_id == audience_id,
                )
            )
        ).first()

        created_by_plugin = existing is None
        if created_by_plugin:
            connection.execute(
                access_table.insert().values(
                    module_id=module_id,
                    audience_id=audience_id,
                )
            )

        connection.execute(
            managed_table.insert().values(
                module_id=module_id,
                audience_id=audience_id,
                created_by_plugin=created_by_plugin,
            )
        )


def _delete_membership(connection, model, account_field, account_id, audience_id):
    membership_table = model.__table__
    conditions = [
        membership_table.c[account_field] == account_id,
        membership_table.c.audience_id == audience_id,
    ]
    if "team_id" in membership_table.c and account_field == "user_id":
        conditions.append(membership_table.c.team_id.is_(None))
    if "user_id" in membership_table.c and account_field == "team_id":
        conditions.append(membership_table.c.user_id.is_(None))
    connection.execute(delete(membership_table).where(and_(*conditions)))


def _audience_id_for_user(connection, user_id, settings):
    field_name = settings.get("user_field", "campus")
    campus_to_audience = _normalised_mapping(
        settings.get("campus_to_audience", {})
    )

    entries = UserFieldEntries.__table__
    fields = Fields.__table__
    statement = (
        select(entries.c.value)
        .select_from(entries.join(fields, entries.c.field_id == fields.c.id))
        .where(
            entries.c.user_id == user_id,
            entries.c.type == "user",
            fields.c.type == "user",
            fields.c.name == field_name,
        )
        .order_by(entries.c.id.desc())
    )
    row = connection.execute(statement).first()
    campus_value = row[0] if row else None
    audience_name = campus_to_audience.get(_normalise(campus_value))
    if not audience_name:
        return None
    return _ensure_named_row(connection, AudienceModel, audience_name)


def _audience_id_for_team(connection, team_id, settings):
    # A dedicated Team Custom Field takes precedence when it is defined.
    # Its name defaults to user_field so existing settings.json files keep
    # working when the admin created the same Campus field for Users and Teams.
    field_name = settings.get(
        "team_field", settings.get("user_field", "campus")
    )
    campus_to_audience = _normalised_mapping(
        settings.get("campus_to_audience", {})
    )

    entries = UserFieldEntries.__table__
    fields = Fields.__table__
    field_exists = connection.execute(
        select(fields.c.id).where(
            fields.c.type == "team",
            fields.c.name == field_name,
        )
    ).first()

    if field_exists is None:
        # Backward compatibility: when no matching Team Custom Field is
        # defined, derive the team's campus from unanimous user fields.
        return False, None

    statement = (
        select(entries.c.value)
        .select_from(entries.join(fields, entries.c.field_id == fields.c.id))
        .where(
            entries.c.team_id == team_id,
            entries.c.type == "team",
            fields.c.type == "team",
            fields.c.name == field_name,
        )
        .order_by(entries.c.id.desc())
    )
    row = connection.execute(statement).first()

    # A configured Team Custom Field is authoritative. An empty or unmapped
    # value must not silently fall back to members' individual campus values.
    if row is None:
        return True, None

    audience_name = campus_to_audience.get(_normalise(row[0]))
    if not audience_name:
        return True, None

    return True, _ensure_named_row(connection, AudienceModel, audience_name)


def _sync_user_membership(connection, user_id, audience_id):
    membership_table = AudienceMemberModel.__table__
    managed_table = CampusManagedAudienceMember.__table__

    old_memberships = connection.execute(
        select(
            managed_table.c.audience_id,
            managed_table.c.created_by_plugin,
        ).where(managed_table.c.user_id == user_id)
    ).all()

    for old_membership in old_memberships:
        if old_membership[1]:
            _delete_membership(
                connection,
                AudienceMemberModel,
                "user_id",
                user_id,
                old_membership[0],
            )

    connection.execute(
        delete(managed_table).where(managed_table.c.user_id == user_id)
    )

    if audience_id is None:
        return

    conditions = [
        membership_table.c.user_id == user_id,
        membership_table.c.audience_id == audience_id,
    ]
    if "team_id" in membership_table.c:
        conditions.append(membership_table.c.team_id.is_(None))

    existing = connection.execute(
        select(membership_table.c.id).where(and_(*conditions))
    ).first()

    created_by_plugin = existing is None
    if created_by_plugin:
        values = {"audience_id": audience_id, "user_id": user_id}
        if "team_id" in membership_table.c:
            values["team_id"] = None
        connection.execute(membership_table.insert().values(**values))

    connection.execute(
        managed_table.insert().values(
            audience_id=audience_id,
            user_id=user_id,
            created_by_plugin=created_by_plugin,
        )
    )


def _sync_team_membership(connection, team_id, audience_id):
    membership_table = TeamAudienceMemberModel.__table__
    managed_table = CampusManagedTeamAudienceMember.__table__

    old_memberships = connection.execute(
        select(
            managed_table.c.audience_id,
            managed_table.c.created_by_plugin,
        ).where(managed_table.c.team_id == team_id)
    ).all()

    for old_membership in old_memberships:
        if old_membership[1]:
            _delete_membership(
                connection,
                TeamAudienceMemberModel,
                "team_id",
                team_id,
                old_membership[0],
            )

    connection.execute(
        delete(managed_table).where(managed_table.c.team_id == team_id)
    )

    if audience_id is None:
        return

    conditions = [
        membership_table.c.team_id == team_id,
        membership_table.c.audience_id == audience_id,
    ]
    if "user_id" in membership_table.c:
        conditions.append(membership_table.c.user_id.is_(None))

    existing = connection.execute(
        select(membership_table.c.id).where(and_(*conditions))
    ).first()

    created_by_plugin = existing is None
    if created_by_plugin:
        values = {"audience_id": audience_id, "team_id": team_id}
        if "user_id" in membership_table.c:
            values["user_id"] = None
        connection.execute(membership_table.insert().values(**values))

    connection.execute(
        managed_table.insert().values(
            audience_id=audience_id,
            team_id=team_id,
            created_by_plugin=created_by_plugin,
        )
    )


def _team_custom_field_value_from_users(connection, team_id, user_field_name):
    """Return a team-wide campus when all non-empty member values agree."""
    if not isinstance(user_field_name, str) or not user_field_name.strip():
        return None

    fields = Fields.__table__
    entries = UserFieldEntries.__table__
    user_field = connection.execute(
        select(fields.c.id).where(
            fields.c.type == "user",
            fields.c.name == user_field_name,
        )
    ).first()
    if user_field is None:
        return None

    member_ids = [
        row[0]
        for row in connection.execute(
            select(Users.__table__.c.id)
            .where(Users.__table__.c.team_id == team_id)
            .order_by(Users.__table__.c.id)
        ).all()
    ]
    if not member_ids:
        return None

    rows = connection.execute(
        select(entries.c.user_id, entries.c.value)
        .where(
            entries.c.field_id == user_field[0],
            entries.c.type == "user",
            entries.c.user_id.in_(member_ids),
        )
        .order_by(entries.c.id.desc())
    ).all()

    values_by_user = {}
    # Match _audience_id_for_user: the newest row wins if legacy data contains
    # multiple entries for the same user and field.
    for user_id, value in rows:
        values_by_user.setdefault(user_id, value)

    member_values = [values_by_user.get(user_id) for user_id in member_ids]
    return _unanimous_custom_field_value(member_values, len(member_ids))


def _sync_team_custom_field_from_users(connection, team_id, settings):
    """Derive a Team Custom Field only when no manual value is present.

    Plugin-generated values are tracked so they can be refreshed or removed.
    Manual values are tracked separately and always remain authoritative.
    """
    user_field_name = settings.get("user_field", "campus")
    team_field_name = settings.get(
        "team_field", settings.get("user_field", "campus")
    )
    if not isinstance(team_field_name, str) or not team_field_name.strip():
        return False

    fields = Fields.__table__
    entries = UserFieldEntries.__table__
    managed_table = CampusManagedTeamFieldEntry.__table__

    team_field = connection.execute(
        select(fields.c.id).where(
            fields.c.type == "team",
            fields.c.name == team_field_name,
        )
    ).first()
    if team_field is None:
        return False

    field_id = team_field[0]
    conditions = [
        entries.c.field_id == field_id,
        entries.c.team_id == team_id,
        entries.c.type == "team",
    ]
    if "user_id" in entries.c:
        conditions.append(entries.c.user_id.is_(None))

    team_entries = connection.execute(
        select(entries.c.id, entries.c.value)
        .where(and_(*conditions))
        .order_by(entries.c.id.desc())
    ).all()

    managed = connection.execute(
        select(
            managed_table.c.entry_id,
            managed_table.c.generated_value,
            managed_table.c.is_manual,
        ).where(
            managed_table.c.team_id == team_id,
            managed_table.c.field_id == field_id,
        )
    ).first()

    managed_entry = None
    if managed is not None:
        managed_entry = next(
            (row for row in team_entries if row[0] == managed[0]), None
        )

        if managed[2]:
            # A manually-managed value is authoritative while it remains
            # non-empty. Clearing or deleting it permits automatic derivation.
            if (
                managed_entry is not None
                and _clean_custom_field_value(managed_entry[1]) is not None
            ):
                return True

            connection.execute(
                delete(managed_table).where(
                    managed_table.c.team_id == team_id,
                    managed_table.c.field_id == field_id,
                )
            )
            managed = None
            managed_entry = None

        elif managed_entry is not None:
            current_value = _clean_custom_field_value(managed_entry[1])
            generated_value = _clean_custom_field_value(managed[1])

            # Also recognize manual edits performed outside CTFd's ORM events.
            if _team_field_value_was_manually_changed(
                current_value, generated_value
            ):
                connection.execute(
                    managed_table.update()
                    .where(
                        managed_table.c.team_id == team_id,
                        managed_table.c.field_id == field_id,
                    )
                    .values(
                        entry_id=managed_entry[0],
                        generated_value=None,
                        is_manual=True,
                    )
                )
                return True

        else:
            # The tracked entry was deleted outside the plugin.
            connection.execute(
                delete(managed_table).where(
                    managed_table.c.team_id == team_id,
                    managed_table.c.field_id == field_id,
                )
            )
            managed = None

    managed_entry_id = (
        managed[0]
        if managed is not None and not managed[2]
        else None
    )

    manual_entries = []
    if _has_untracked_team_field_value(team_entries, managed_entry_id):
        manual_entries = [
            row
            for row in team_entries
            if row[0] != managed_entry_id
            and _clean_custom_field_value(row[1]) is not None
        ]

    if manual_entries:
        if managed_entry_id is not None:
            # A new manual value supersedes a previously-generated value.
            connection.execute(
                delete(entries).where(entries.c.id == managed_entry_id)
            )
            connection.execute(
                delete(managed_table).where(
                    managed_table.c.team_id == team_id,
                    managed_table.c.field_id == field_id,
                )
            )
        else:
            # Before ownership tracking existed, the plugin may already have
            # populated this field. Adopt a single existing value only when
            # it exactly agrees with today's member-derived value. Otherwise
            # preserve it as a manual override.
            team_value = _team_custom_field_value_from_users(
                connection, team_id, user_field_name
            )
            if (
                managed is None
                and len(manual_entries) == 1
                and team_value is not None
                and _normalise(manual_entries[0][1]) == _normalise(team_value)
            ):
                legacy_entry_id = manual_entries[0][0]
                connection.execute(
                    managed_table.insert().values(
                        team_id=team_id,
                        field_id=field_id,
                        entry_id=legacy_entry_id,
                        generated_value=team_value,
                        is_manual=False,
                    )
                )
        return True

    team_value = _team_custom_field_value_from_users(
        connection, team_id, user_field_name
    )

    if managed_entry_id is not None:
        if team_value is None:
            # A disagreement (or no member campus) clears only the value that
            # the plugin itself generated. Manual values are never deleted.
            connection.execute(
                delete(entries).where(entries.c.id == managed_entry_id)
            )
            connection.execute(
                delete(managed_table).where(
                    managed_table.c.team_id == team_id,
                    managed_table.c.field_id == field_id,
                )
            )
        else:
            connection.execute(
                entries.update()
                .where(entries.c.id == managed_entry_id)
                .values(value=team_value)
            )
            connection.execute(
                managed_table.update()
                .where(
                    managed_table.c.team_id == team_id,
                    managed_table.c.field_id == field_id,
                )
                .values(generated_value=team_value, is_manual=False)
            )
        return True

    if team_value is None:
        # No non-empty values or a conflict: leave empty Team Field rows as-is.
        return True

    # Reuse an empty Team Field row if CTFd created one, otherwise insert one.
    empty_entry = next(
        (
            row
            for row in team_entries
            if _clean_custom_field_value(row[1]) is None
        ),
        None,
    )
    if empty_entry is not None:
        entry_id = empty_entry[0]
        connection.execute(
            entries.update()
            .where(entries.c.id == entry_id)
            .values(value=team_value)
        )
    else:
        insert_values = {
            "field_id": field_id,
            "team_id": team_id,
            "type": "team",
            "value": team_value,
        }
        if "user_id" in entries.c:
            insert_values["user_id"] = None
        result = connection.execute(entries.insert().values(**insert_values))
        entry_id = result.inserted_primary_key[0]

    connection.execute(
        managed_table.insert().values(
            team_id=team_id,
            field_id=field_id,
            entry_id=entry_id,
            generated_value=team_value,
            is_manual=False,
        )
    )
    return True


def _sync_team_connection(connection, team_id, settings):
    # A non-empty Team Custom Field is authoritative, whether it was entered
    # manually or derived by this plugin. Derive it from users only while it
    # has no manual value.
    _sync_team_custom_field_from_users(connection, team_id, settings)

    has_team_field, target_audience_id = _audience_id_for_team(
        connection, team_id, settings
    )

    if not has_team_field:
        # Compatibility mode for installations without a Team Custom Field:
        # derive directly from all non-empty member values, ignoring blanks.
        team_value = _team_custom_field_value_from_users(
            connection, team_id, settings.get("user_field", "campus")
        )
        campus_to_audience = _normalised_mapping(
            settings.get("campus_to_audience", {})
        )
        audience_name = campus_to_audience.get(_normalise(team_value))
        target_audience_id = (
            _ensure_named_row(connection, AudienceModel, audience_name)
            if audience_name
            else None
        )

    _sync_team_membership(connection, team_id, target_audience_id)


def _sync_user_connection(connection, user_id, settings):
    user_mode = get_config("user_mode", default="teams")
    if user_mode == "teams":
        row = connection.execute(
            select(Users.__table__.c.team_id).where(
                Users.__table__.c.id == user_id
            )
        ).first()
        team_id = row[0] if row else None
        if team_id is not None:
            _sync_team_connection(connection, team_id, settings)
        return

    target_audience_id = _audience_id_for_user(connection, user_id, settings)
    _sync_user_membership(connection, user_id, target_audience_id)


def _user_team_changed(mapper, connection, target):
    team_ids = set()
    try:
        from sqlalchemy import inspect

        history = inspect(target).attrs.team_id.history
        team_ids.update(value for value in history.deleted if value is not None)
        team_ids.update(value for value in history.added if value is not None)
    except Exception:
        pass

    if getattr(target, "team_id", None) is not None:
        team_ids.add(target.team_id)

    settings = _load_settings()
    for team_id in team_ids:
        _sync_team_connection(connection, team_id, settings)


def _sync_challenge_connection(connection, challenge_id, settings):
    tag_to_module = _normalised_mapping(settings.get("tag_to_module", {}))
    tags = Tags.__table__
    rows = connection.execute(
        select(tags.c.value).where(tags.c.challenge_id == challenge_id)
    ).all()

    module_names = set()
    for row in rows:
        normalised_tag = _normalise(row[0])
        if normalised_tag in tag_to_module:
            module_names.add(tag_to_module[normalised_tag])

    managed_table = CampusManagedChallenge.__table__
    previous = connection.execute(
        select(managed_table.c.module_id).where(
            managed_table.c.challenge_id == challenge_id
        )
    ).first()

    if not module_names:
        # When the matching tag is removed, clear a native module assignment
        # only if it is still the assignment made by this plugin.
        if previous and HAS_NATIVE_CHALLENGE_MODULE_FIELD:
            challenge_table = Challenges.__table__
            current = connection.execute(
                select(challenge_table.c.module_id).where(
                    challenge_table.c.id == challenge_id
                )
            ).first()
            if current and current[0] == previous[0]:
                connection.execute(
                    challenge_table.update()
                    .where(challenge_table.c.id == challenge_id)
                    .values(module_id=None)
                )

        connection.execute(
            delete(managed_table).where(
                managed_table.c.challenge_id == challenge_id
            )
        )
        return

    if len(module_names) > 1:
        module_name = CONFLICT_MODULE_NAME
        LOGGER.warning(
            "Challenge %s has tags mapped to multiple modules: %s",
            challenge_id,
            ", ".join(sorted(module_names)),
        )
    else:
        module_name = next(iter(module_names))

    module_id = _ensure_named_row(connection, ModuleModel, module_name)

    # In commits with native Modules support, set Challenges.module_id so
    # CTFd's own administration and challenge APIs see the assignment.
    if HAS_NATIVE_CHALLENGE_MODULE_FIELD:
        challenge_table = Challenges.__table__
        connection.execute(
            challenge_table.update()
            .where(challenge_table.c.id == challenge_id)
            .values(module_id=module_id)
        )

    connection.execute(
        delete(managed_table).where(
            managed_table.c.challenge_id == challenge_id
        )
    )
    connection.execute(
        managed_table.insert().values(
            challenge_id=challenge_id,
            module_id=module_id,
        )
    )


def _clear_ctfd_caches():
    from CTFd.cache import clear_challenges

    clear_challenges()
    try:
        from CTFd.cache import clear_accessible_module_ids
    except ImportError:
        # CTFd 3.8.0 predates native module-access cache helpers.
        return
    clear_accessible_module_ids()


def _sync_database_state(connection, settings):
    _sync_configuration(connection, settings)

    user_ids = [
        row[0] for row in connection.execute(select(Users.__table__.c.id))
    ]
    if get_config("user_mode", default="teams") == "teams":
        team_ids = {
            row[0]
            for row in connection.execute(
                select(Users.__table__.c.team_id)
                .where(Users.__table__.c.team_id.isnot(None))
                .distinct()
            )
        }

        # Include teams that have a Team Custom Field entry as well, even if
        # they are not returned by the current user-membership query.
        entry_table = UserFieldEntries.__table__
        if "team_id" in entry_table.c and "type" in entry_table.c:
            team_ids.update(
                row[0]
                for row in connection.execute(
                    select(entry_table.c.team_id)
                    .where(
                        entry_table.c.type == "team",
                        entry_table.c.team_id.isnot(None),
                    )
                    .distinct()
                )
            )

        for team_id in sorted(team_ids):
            _sync_team_connection(connection, team_id, settings)
    else:
        for user_id in user_ids:
            _sync_user_connection(connection, user_id, settings)

    challenge_ids = [
        row[0] for row in connection.execute(select(Challenges.__table__.c.id))
    ]
    for challenge_id in challenge_ids:
        _sync_challenge_connection(connection, challenge_id, settings)

    return user_ids, challenge_ids


def _sync_everything():
    settings = _load_settings()

    if db.engine.dialect.name in ("mysql", "mariadb"):
        # Hold the advisory lock and run the transaction on the SAME
        # connection. This also works when SQLAlchemy's pool has size 1.
        with db.engine.connect() as connection:
            acquired = None
            try:
                with connection.begin():
                    acquired = connection.execute(
                        text("SELECT GET_LOCK(:lock_name, :timeout)"),
                        {
                            "lock_name": SYNC_LOCK_NAME,
                            "timeout": SYNC_LOCK_TIMEOUT_SECONDS,
                        },
                    ).scalar()

                    if acquired != 1:
                        raise RuntimeError(
                            "Could not acquire the CTFd Campus Audiences "
                            "synchronization lock within {} seconds".format(
                                SYNC_LOCK_TIMEOUT_SECONDS
                            )
                        )

                    user_ids, challenge_ids = _sync_database_state(
                        connection, settings
                    )
            finally:
                if acquired == 1:
                    try:
                        # Use begin() as a context manager instead of Connection.commit().
                        # CTFd 3.8.0's SQLAlchemy connection API may not expose commit().
                        with connection.begin():
                            released = connection.execute(
                                text("SELECT RELEASE_LOCK(:lock_name)"),
                                {"lock_name": SYNC_LOCK_NAME},
                            ).scalar()

                        if released != 1:
                            LOGGER.warning(
                                "MySQL did not confirm release of synchronization "
                                "lock %s",
                                SYNC_LOCK_NAME,
                            )
                    except Exception:
                        # Closing the connection also releases a MySQL named lock.
                        LOGGER.exception(
                            "Failed to explicitly release synchronization lock %s",
                            SYNC_LOCK_NAME,
                        )
    else:
        with db.engine.begin() as connection:
            user_ids, challenge_ids = _sync_database_state(connection, settings)

    _clear_ctfd_caches()
    LOGGER.info(
        "Campus synchronization completed: %d users, %d challenges (native modules: %s)",
        len(user_ids),
        len(challenge_ids),
        HAS_NATIVE_AUDIENCES_AND_MODULES and HAS_NATIVE_CHALLENGE_MODULE_FIELD,
    )
    return len(user_ids), len(challenge_ids)


def _team_field_event_context(connection, target, settings):
    if getattr(target, "type", None) != "team":
        return None

    team_id = getattr(target, "team_id", None)
    field_id = getattr(target, "field_id", None)
    if team_id is None or field_id is None:
        return None

    team_field_name = settings.get(
        "team_field", settings.get("user_field", "campus")
    )
    field_name = connection.execute(
        select(Fields.__table__.c.name).where(
            Fields.__table__.c.id == field_id,
            Fields.__table__.c.type == "team",
        )
    ).first()
    if field_name is None or field_name[0] != team_field_name:
        return None

    return team_id, field_id


def _team_field_manually_changed(mapper, connection, target):
    settings = _load_settings()
    context = _team_field_event_context(connection, target, settings)
    if context is None or get_config("user_mode", default="teams") != "teams":
        return

    team_id, field_id = context
    managed_table = CampusManagedTeamFieldEntry.__table__
    entries = UserFieldEntries.__table__
    managed = connection.execute(
        select(managed_table.c.entry_id, managed_table.c.is_manual).where(
            managed_table.c.team_id == team_id,
            managed_table.c.field_id == field_id,
        )
    ).first()

    value = _clean_custom_field_value(getattr(target, "value", None))
    if value is None:
        # An explicitly emptied field is eligible for automatic derivation.
        if managed is not None and managed[0] == target.id:
            connection.execute(
                delete(managed_table).where(
                    managed_table.c.team_id == team_id,
                    managed_table.c.field_id == field_id,
                )
            )
    else:
        if managed is not None and not managed[1] and managed[0] != target.id:
            # Remove the previous auto-generated row when the administrator
            # enters a separate manual Team Custom Field value.
            connection.execute(
                delete(entries).where(entries.c.id == managed[0])
            )

        if managed is None:
            connection.execute(
                managed_table.insert().values(
                    team_id=team_id,
                    field_id=field_id,
                    entry_id=target.id,
                    generated_value=None,
                    is_manual=True,
                )
            )
        else:
            connection.execute(
                managed_table.update()
                .where(
                    managed_table.c.team_id == team_id,
                    managed_table.c.field_id == field_id,
                )
                .values(
                    entry_id=target.id,
                    generated_value=None,
                    is_manual=True,
                )
            )

    _sync_team_connection(connection, team_id, settings)
    _clear_ctfd_caches()


def _team_field_deleted(mapper, connection, target):
    settings = _load_settings()
    context = _team_field_event_context(connection, target, settings)
    if context is None or get_config("user_mode", default="teams") != "teams":
        return

    team_id, field_id = context
    managed_table = CampusManagedTeamFieldEntry.__table__
    connection.execute(
        delete(managed_table).where(
            managed_table.c.team_id == team_id,
            managed_table.c.field_id == field_id,
            managed_table.c.entry_id == target.id,
        )
    )
    _sync_team_connection(connection, team_id, settings)
    _clear_ctfd_caches()


def _user_field_changed(mapper, connection, target):
    settings = _load_settings()
    user_id = getattr(target, "user_id", None)
    if user_id is None:
        return

    _sync_user_connection(connection, user_id, settings)
    _clear_ctfd_caches()


def _tag_changed(mapper, connection, target):
    if target.challenge_id is not None:
        _sync_challenge_connection(
            connection, target.challenge_id, _load_settings()
        )
        _clear_ctfd_caches()


def _register_model_events():
    global _MODEL_EVENTS_REGISTERED
    if _MODEL_EVENTS_REGISTERED:
        return

    # Mapper hooks run in the same transaction as CTFd's user/tag update.
    for action in ("after_insert", "after_update", "after_delete"):
        event.listen(UserFieldEntries, action, _user_field_changed)
        event.listen(Tags, action, _tag_changed)
        event.listen(Users, action, _user_team_changed)

    # Team fields have their own CTFd mapper, distinct from UserFieldEntries.
    event.listen(TeamFieldEntries, "after_insert", _team_field_manually_changed)
    event.listen(TeamFieldEntries, "after_update", _team_field_manually_changed)
    event.listen(TeamFieldEntries, "after_delete", _team_field_deleted)

    _MODEL_EVENTS_REGISTERED = True


def _current_user_id():
    user = get_current_user()
    return getattr(user, "id", None) if user is not None else None


def _challenge_module_id(challenge_id):
    if HAS_NATIVE_CHALLENGE_MODULE_FIELD:
        row = db.session.execute(
            select(Challenges.__table__.c.module_id).where(
                Challenges.__table__.c.id == challenge_id
            )
        ).first()
        return row[0] if row else None

    row = CampusManagedChallenge.query.filter_by(
        challenge_id=challenge_id
    ).first()
    return row.module_id if row else None


def _can_access_challenge(challenge_id, user_id):
    module_id = _challenge_module_id(challenge_id)
    if module_id is None:
        return True

    if user_id is None:
        return False

    user_mode = get_config("user_mode", default="teams")
    if user_mode == "teams":
        user = Users.query.filter_by(id=user_id).first()
        account_id = user.team_id if user else None
        member_table = TeamAudienceMemberModel.__table__
        account_column = "team_id"
    else:
        account_id = user_id
        member_table = AudienceMemberModel.__table__
        account_column = "user_id"

    if account_id is None:
        return False

    access_table = ModuleAccessModel.__table__
    conditions = [
        member_table.c[account_column] == account_id,
        access_table.c.module_id == module_id,
    ]
    if "team_id" in member_table.c and account_column == "user_id":
        conditions.append(member_table.c.team_id.is_(None))
    if "user_id" in member_table.c and account_column == "team_id":
        conditions.append(member_table.c.user_id.is_(None))

    statement = (
        select(member_table.c.id)
        .select_from(
            member_table.join(
                access_table,
                member_table.c.audience_id == access_table.c.audience_id,
            )
        )
        .where(and_(*conditions))
    )
    return db.session.execute(statement).first() is not None


def _inaccessible_challenge_ids(user_id):
    if HAS_NATIVE_CHALLENGE_MODULE_FIELD:
        challenge_table = Challenges.__table__
        assignments = db.session.execute(
            select(challenge_table.c.id, challenge_table.c.module_id).where(
                challenge_table.c.module_id.isnot(None)
            )
        ).all()
    else:
        assignments = [
            (row.challenge_id, row.module_id)
            for row in CampusManagedChallenge.query.all()
        ]

    if not assignments:
        return set()

    if user_id is None:
        return {row[0] for row in assignments}

    user_mode = get_config("user_mode", default="teams")
    if user_mode == "teams":
        user = Users.query.filter_by(id=user_id).first()
        account_id = user.team_id if user else None
        member_table = TeamAudienceMemberModel.__table__
        account_column = "team_id"
    else:
        account_id = user_id
        member_table = AudienceMemberModel.__table__
        account_column = "user_id"

    if account_id is None:
        return {row[0] for row in assignments}

    access_table = ModuleAccessModel.__table__
    conditions = [member_table.c[account_column] == account_id]
    if "team_id" in member_table.c and account_column == "user_id":
        conditions.append(member_table.c.team_id.is_(None))
    if "user_id" in member_table.c and account_column == "team_id":
        conditions.append(member_table.c.user_id.is_(None))

    statement = (
        select(access_table.c.module_id)
        .select_from(
            access_table.join(
                member_table,
                member_table.c.audience_id == access_table.c.audience_id,
            )
        )
        .where(and_(*conditions))
    )
    allowed_module_ids = {
        row[0] for row in db.session.execute(statement).all()
    }

    return {
        challenge_id
        for challenge_id, module_id in assignments
        if module_id not in allowed_module_ids
    }


def _api_v1_path_parts(resource):
    parts = [part for part in request.path.split("/") if part]
    for index in range(len(parts) - 2):
        if parts[index : index + 3] == ["api", "v1", resource]:
            return parts[index + 3 :]
    return None


def _challenge_api_path_parts():
    return _api_v1_path_parts("challenges")


def _enforce_challenge_access():
    if is_admin():
        return None

    user_id = _current_user_id()

    # Protect challenge details and nested API endpoints.
    tail = _challenge_api_path_parts()
    if tail:
        first = tail[0]
        if first.isdigit():
            if not _can_access_challenge(int(first), user_id):
                abort(404)
        elif first == "attempt" and request.method == "POST":
            body = request.get_json(silent=True)
            if not isinstance(body, dict):
                body = request.form
            challenge_id = body.get("challenge_id")
            if challenge_id:
                try:
                    challenge_id = int(challenge_id)
                except (TypeError, ValueError):
                    return None
                if not _can_access_challenge(challenge_id, user_id):
                    abort(404)

    # CTFd 3.8.0 has standalone hint and solution routes.
    for resource, model in (("hints", Hints), ("solutions", Solutions)):
        resource_tail = _api_v1_path_parts(resource)
        if (
            resource_tail
            and len(resource_tail) == 1
            and resource_tail[0].isdigit()
        ):
            item = model.query.filter_by(id=int(resource_tail[0])).first_or_404()
            challenge_id = getattr(item, "challenge_id", None)
            if challenge_id is None or not _can_access_challenge(
                int(challenge_id), user_id
            ):
                abort(404)

    # Paid hint/solution unlock requests must respect module access as well.
    unlock_tail = _api_v1_path_parts("unlocks")
    if unlock_tail == [] and request.method == "POST":
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            body = {}
        target_type = body.get("type")
        target_id = body.get("target")
        target_model = {"hints": Hints, "solutions": Solutions}.get(target_type)
        if target_model is not None and target_id is not None:
            target = target_model.query.filter_by(id=target_id).first()
            challenge_id = getattr(target, "challenge_id", None) if target else None
            if challenge_id is not None and not _can_access_challenge(
                int(challenge_id), user_id
            ):
                abort(404)

    # Direct challenge and solution file downloads must use the same check.
    if request.endpoint == "views.files":
        file_path = (request.view_args or {}).get("path", "")
        file_record = Files.query.filter_by(location=file_path).first()
        if file_record is not None:
            challenge_id = getattr(file_record, "challenge_id", None)
            if file_record.type == "solution":
                solution = Solutions.query.filter_by(
                    id=getattr(file_record, "solution_id", None)
                ).first()
                challenge_id = solution.challenge_id if solution else None

            if challenge_id and not _can_access_challenge(
                int(challenge_id), user_id
            ):
                abort(404)

    return None


def _filter_challenge_list(response):
    if request.method != "GET" or is_admin():
        return response

    tail = _challenge_api_path_parts()
    if tail is None or tail:
        return response

    payload = response.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return response

    inaccessible = _inaccessible_challenge_ids(_current_user_id())
    if not inaccessible:
        return response

    filtered = []
    for challenge in payload["data"]:
        if not isinstance(challenge, dict) or challenge.get("id") is None:
            filtered.append(challenge)
            continue
        try:
            challenge_id = int(challenge["id"])
        except (TypeError, ValueError):
            continue
        if challenge_id not in inaccessible:
            filtered.append(challenge)

    payload["data"] = filtered
    response.set_data(json.dumps(payload, ensure_ascii=False))
    return response


def _admin_sync():
    try:
        users_count, challenges_count = _sync_everything()
    except Exception:
        LOGGER.exception("Campus synchronization failed")
        db.session.rollback()
        return "Synchronization failed. Check the CTFd logs.", 500

    LOGGER.info(
        "Manual synchronization: %d users, %d challenges",
        users_count,
        challenges_count,
    )
    return redirect(url_for("admin.plugin", plugin=PLUGIN_NAME))


def load(app):
    """Plugin entry point with capability detection for CTFd 3.8.0 and newer."""
    if getattr(app, "_campus_audiences_loaded", False):
        return
    app._campus_audiences_loaded = True

    with app.app_context():
        # Creates plugin-owned tracking tables and fallback tables when needed.
        # Native CTFd tables are already managed by CTFd migrations.
        db.create_all()
        _register_model_events()
        _sync_everything()

    app.before_request(_enforce_challenge_access)
    app.after_request(_filter_challenge_list)

    app.add_url_rule(
        "/admin/plugins/{}/sync".format(PLUGIN_NAME),
        endpoint="ctfd_campus_audiences.sync",
        view_func=admins_only(_admin_sync),
        methods=["POST"],
    )
