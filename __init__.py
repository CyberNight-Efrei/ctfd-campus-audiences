import json
import logging
import os

from flask import abort, redirect, request, url_for
from sqlalchemy import and_, delete, event, select

from CTFd.models import (
    Challenges,
    Fields,
    Files,
    Hints,
    Solutions,
    Tags,
    UserFieldEntries,
    Users,
    db,
)
from CTFd.utils.decorators import admins_only
from CTFd.utils.user import get_current_user, is_admin

PLUGIN_NAME = "ctfd_campus_audiences"
CONFLICT_MODULE_NAME = "__conflicting_tag_mapping__"
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
    ModuleModel = CampusModule
    ModuleAccessModel = CampusModuleAudienceAccess


HAS_NATIVE_CHALLENGE_MODULE_FIELD = (
    hasattr(Challenges, "module_id")
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


def _normalise(value):
    if not isinstance(value, str):
        return None
    value = value.strip().casefold()
    return value or None


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

    # Remove only access links previously created by this plugin.
    old_links = connection.execute(
        select(
            managed_table.c.module_id,
            managed_table.c.audience_id,
            managed_table.c.created_by_plugin,
        )
    ).all()
    for old_link in old_links:
        if old_link[2]:
            connection.execute(
                delete(access_table).where(
                    and_(
                        access_table.c.module_id == old_link[0],
                        access_table.c.audience_id == old_link[1],
                    )
                )
            )
    connection.execute(delete(managed_table))

    # Rebuild desired links. Existing links not created by the plugin are kept.
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
                values = {
                    "module_id": module_id,
                    "audience_id": audience_id,
                }
                connection.execute(access_table.insert().values(**values))

            connection.execute(
                managed_table.insert().values(
                    module_id=module_id,
                    audience_id=audience_id,
                    created_by_plugin=created_by_plugin,
                )
            )


def _delete_user_membership(connection, user_id, audience_id):
    membership_table = AudienceMemberModel.__table__
    conditions = [
        membership_table.c.user_id == user_id,
        membership_table.c.audience_id == audience_id,
    ]
    if "team_id" in membership_table.c:
        conditions.append(membership_table.c.team_id.is_(None))
    connection.execute(delete(membership_table).where(and_(*conditions)))


def _sync_user_connection(connection, user_id, settings):
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
            _delete_user_membership(connection, user_id, old_membership[0])

    connection.execute(
        delete(managed_table).where(managed_table.c.user_id == user_id)
    )

    if not audience_name:
        return

    audience_id = _ensure_named_row(
        connection, AudienceModel, audience_name
    )
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


def _sync_everything():
    settings = _load_settings()
    with db.engine.begin() as connection:
        _sync_configuration(connection, settings)

        user_ids = [
            row[0] for row in connection.execute(select(Users.__table__.c.id))
        ]
        for user_id in user_ids:
            _sync_user_connection(connection, user_id, settings)

        challenge_ids = [
            row[0] for row in connection.execute(select(Challenges.__table__.c.id))
        ]
        for challenge_id in challenge_ids:
            _sync_challenge_connection(connection, challenge_id, settings)

    LOGGER.info(
        "Campus synchronization completed: %d users, %d challenges (native modules: %s)",
        len(user_ids),
        len(challenge_ids),
        HAS_NATIVE_AUDIENCES_AND_MODULES and HAS_NATIVE_CHALLENGE_MODULE_FIELD,
    )
    return len(user_ids), len(challenge_ids)


def _user_field_changed(mapper, connection, target):
    if target.user_id is not None:
        _sync_user_connection(connection, target.user_id, _load_settings())


def _tag_changed(mapper, connection, target):
    if target.challenge_id is not None:
        _sync_challenge_connection(
            connection, target.challenge_id, _load_settings()
        )


def _register_model_events():
    global _MODEL_EVENTS_REGISTERED
    if _MODEL_EVENTS_REGISTERED:
        return

    # Mapper hooks run in the same transaction as CTFd's user/tag update.
    for action in ("after_insert", "after_update", "after_delete"):
        event.listen(UserFieldEntries, action, _user_field_changed)
        event.listen(Tags, action, _tag_changed)

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

    member_table = AudienceMemberModel.__table__
    access_table = ModuleAccessModel.__table__
    conditions = [
        member_table.c.user_id == user_id,
        access_table.c.module_id == module_id,
    ]
    if "team_id" in member_table.c:
        conditions.append(member_table.c.team_id.is_(None))

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

    member_table = AudienceMemberModel.__table__
    access_table = ModuleAccessModel.__table__
    conditions = [member_table.c.user_id == user_id]
    if "team_id" in member_table.c:
        conditions.append(member_table.c.team_id.is_(None))

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
