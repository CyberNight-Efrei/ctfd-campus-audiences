import json
import logging
import os

from flask import abort, redirect, request, url_for
from sqlalchemy import delete, event, select

from CTFd.models import (
    Challenges,
    Fields,
    Files,
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

# CTFd 3.8.9 does not ship native Audience/Module models. These plugin-owned
# tables provide the same concepts without patching CTFd's core schema.
class CampusAudience(db.Model):
    __tablename__ = "campus_audiences"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True)


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
        db.UniqueConstraint("audience_id", "user_id", name="uq_campus_audience_user"),
    )


class CampusModule(db.Model):
    __tablename__ = "campus_modules"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True)


class CampusModuleAudienceAccess(db.Model):
    __tablename__ = "campus_module_audience_access"

    id = db.Column(db.Integer, primary_key=True)
    module_id = db.Column(
        db.Integer, db.ForeignKey("campus_modules.id", ondelete="CASCADE"), nullable=False
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


class CampusModuleChallenge(db.Model):
    __tablename__ = "campus_module_challenges"

    id = db.Column(db.Integer, primary_key=True)
    module_id = db.Column(
        db.Integer, db.ForeignKey("campus_modules.id", ondelete="CASCADE"), nullable=False
    )
    challenge_id = db.Column(
        db.Integer,
        db.ForeignKey("challenges.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )


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

    connection.execute(table.insert().values(name=name))
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
    module_names.update(
        name for name in settings.get("module_to_audiences", {})
        if isinstance(name, str) and name.strip()
    )
    module_names.add(CONFLICT_MODULE_NAME)

    audience_names = set(campus_to_audience.values())
    for names in module_to_audiences.values():
        audience_names.update(names)

    for audience_name in sorted(audience_names):
        _ensure_named_row(connection, CampusAudience, audience_name)

    for module_name in sorted(module_names):
        _ensure_named_row(connection, CampusModule, module_name)

    access_table = CampusModuleAudienceAccess.__table__
    connection.execute(delete(access_table))

    for module_name in sorted(module_names):
        if module_name == CONFLICT_MODULE_NAME:
            continue
        configured_audiences = module_to_audiences.get(
            _normalise(module_name), [module_name]
        )
        module_id = _ensure_named_row(connection, CampusModule, module_name)

        for audience_name in configured_audiences:
            audience_id = _ensure_named_row(
                connection, CampusAudience, audience_name
            )
            connection.execute(
                access_table.insert().values(
                    module_id=module_id, audience_id=audience_id
                )
            )


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

    membership_table = CampusAudienceMember.__table__
    connection.execute(
        delete(membership_table).where(membership_table.c.user_id == user_id)
    )

    if audience_name:
        audience_id = _ensure_named_row(
            connection, CampusAudience, audience_name
        )
        connection.execute(
            membership_table.insert().values(
                user_id=user_id, audience_id=audience_id
            )
        )


def _sync_challenge_connection(connection, challenge_id, settings):
    tag_to_module = _normalised_mapping(settings.get("tag_to_module", {}))
    tags = Tags.__table__
    rows = connection.execute(
        select(tags.c.value).where(tags.c.challenge_id == challenge_id)
    ).all()

    module_names = {
        tag_to_module[normalised_tag]
        for row in rows
        if (normalised_tag := _normalise(row[0])) in tag_to_module
    }

    if not module_names:
        connection.execute(
            delete(CampusModuleChallenge.__table__).where(
                CampusModuleChallenge.challenge_id == challenge_id
            )
        )
        return

    # A challenge with tags mapped to multiple modules is deliberately denied
    # to regular users instead of accidentally exposing it to either campus.
    if len(module_names) > 1:
        module_name = CONFLICT_MODULE_NAME
        LOGGER.warning(
            "Challenge %s has tags mapped to multiple modules: %s",
            challenge_id,
            ", ".join(sorted(module_names)),
        )
    else:
        module_name = next(iter(module_names))

    module_id = _ensure_named_row(connection, CampusModule, module_name)
    assignment_table = CampusModuleChallenge.__table__
    connection.execute(
        delete(assignment_table).where(
            assignment_table.c.challenge_id == challenge_id
        )
    )
    connection.execute(
        assignment_table.insert().values(
            module_id=module_id, challenge_id=challenge_id
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
        "Campus synchronization completed: %d users, %d challenges",
        len(user_ids),
        len(challenge_ids),
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


def _can_access_challenge(challenge_id, user_id):
    assignment = CampusModuleChallenge.query.filter_by(
        challenge_id=challenge_id
    ).first()
    if assignment is None:
        return True

    if user_id is None:
        return False

    membership = (
        db.session.query(CampusModuleAudienceAccess.id)
        .join(
            CampusAudienceMember,
            CampusAudienceMember.audience_id
            == CampusModuleAudienceAccess.audience_id,
        )
        .filter(
            CampusModuleAudienceAccess.module_id == assignment.module_id,
            CampusAudienceMember.user_id == user_id,
        )
        .first()
    )
    return membership is not None


def _inaccessible_challenge_ids(user_id):
    assignments = CampusModuleChallenge.query.all()
    if not assignments:
        return set()

    if user_id is None:
        return {item.challenge_id for item in assignments}

    allowed_module_ids = {
        row[0]
        for row in (
            db.session.query(CampusModuleAudienceAccess.module_id)
            .join(
                CampusAudienceMember,
                CampusAudienceMember.audience_id
                == CampusModuleAudienceAccess.audience_id,
            )
            .filter(CampusAudienceMember.user_id == user_id)
            .all()
        )
    }

    return {
        item.challenge_id
        for item in assignments
        if item.module_id not in allowed_module_ids
    }


def _challenge_api_path_parts():
    parts = [part for part in request.path.split("/") if part]
    for index in range(len(parts) - 2):
        if parts[index : index + 3] == ["api", "v1", "challenges"]:
            return parts[index + 3 :]
    return None


def _enforce_challenge_access():
    if is_admin():
        return None

    user_id = _current_user_id()

    # Protect details and every nested challenge API endpoint (hints, files,
    # solves, solution, tags, ratings, requirements, etc.).
    tail = _challenge_api_path_parts()
    if tail:
        first = tail[0]
        if first.isdigit():
            if not _can_access_challenge(int(first), user_id):
                abort(404)
        elif first == "attempt" and request.method == "POST":
            body = request.get_json(silent=True) or request.form
            challenge_id = body.get("challenge_id")
            if challenge_id:
                try:
                    challenge_id = int(challenge_id)
                except (TypeError, ValueError):
                    return None
                if not _can_access_challenge(challenge_id, user_id):
                    abort(404)

    # Challenge and solution files can be downloaded directly by URL. Apply
    # the same access check even when the client already has a file URL.
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

    payload["data"] = [
        challenge
        for challenge in payload["data"]
        if not isinstance(challenge, dict)
        or challenge.get("id") is None
        or int(challenge["id"]) not in inaccessible
    ]
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
    """CTFd plugin entry point. Compatible with the CTFd 3.8.9 plugin API."""
    if getattr(app, "_campus_audiences_loaded", False):
        return
    app._campus_audiences_loaded = True

    # Plugins are loaded after CTFd initialized its database. create_all()
    # creates only the plugin's missing tables; no CTFd core migration is needed.
    with app.app_context():
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
