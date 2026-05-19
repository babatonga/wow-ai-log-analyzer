"""Admin-only endpoints: settings, invites, user management."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, status
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.core.errors import ForbiddenError, NotFoundError
from app.deps import AdminUser, SessionDep
from app.models import AppSetting, Invite, Report, User, UserRole
from app.schemas.system import (
    ContainerOut,
    LocalAiConfigPatch,
    LocalAiModelFile,
    LocalAiStatusOut,
    SimcStatusOut,
    SystemStatusOut,
)
from app.schemas.talent_finder import EncounterMap
from app.services import docker_control, local_ai_supervisor_service as supervisor, simc_service
from app.services import talent_finder_service
from app.core.errors import UpstreamError
from app.schemas.user import (
    AdminSettingsOut,
    AdminSettingsUpdate,
    AdminUserUpdate,
    InviteIn,
    InviteOut,
    UserOut,
)
from app.services import auth_service

router = APIRouter()


# --- App settings -------------------------------------------------------------


def _settings_value(rows: list[AppSetting], key: str, default: object) -> object:
    for r in rows:
        if r.key == key:
            return (r.value or {}).get("value", (r.value or {}).get("enabled", default))
    return default


_VALID_REASONING_EFFORT = {"", "minimal", "low", "medium", "high"}


def _normalize_reasoning_effort(raw: object) -> str | None:
    """Coerce a stored / submitted reasoning_effort into the canonical form.

    Returns ``None`` when the value is empty / missing / invalid. Returns the
    lowercased string otherwise. Centralised so the GET + PATCH endpoints
    agree on what counts as ``"unset"``.
    """
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if not value or value not in _VALID_REASONING_EFFORT:
        return None
    return value or None


@router.get("/settings", response_model=AdminSettingsOut)
async def read_settings(session: SessionDep, _: AdminUser) -> AdminSettingsOut:
    rows = (await session.execute(select(AppSetting))).scalars().all()
    return AdminSettingsOut(
        allow_registration=bool(_settings_value(rows, "allow_registration", settings.allow_registration)),
        ai_provider=str(_settings_value(rows, "ai_provider", settings.ai_provider)),
        ai_model=str(_settings_value(rows, "ai_model", settings.ai_model)),
        openai_reasoning_effort=_normalize_reasoning_effort(
            _settings_value(rows, "openai_reasoning_effort", settings.openai_reasoning_effort)
        ),
    )


async def _upsert_setting(session, key: str, value: dict) -> None:
    stmt = pg_insert(AppSetting).values(key=key, value=value)
    stmt = stmt.on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": stmt.excluded.value})
    await session.execute(stmt)


@router.patch("/settings", response_model=AdminSettingsOut)
async def update_settings(
    payload: AdminSettingsUpdate, session: SessionDep, admin: AdminUser
) -> AdminSettingsOut:
    if payload.allow_registration is not None:
        await _upsert_setting(session, "allow_registration", {"enabled": payload.allow_registration})
    ai_provider_changed = payload.ai_provider is not None
    if ai_provider_changed:
        await _upsert_setting(session, "ai_provider", {"value": payload.ai_provider})
    if payload.ai_model is not None:
        await _upsert_setting(session, "ai_model", {"value": payload.ai_model})
    if payload.openai_reasoning_effort is not None:
        # Empty string explicitly clears the override → falls back to OpenAI's
        # default (no reasoning). Anything else is validated against the
        # canonical set; junk values are silently coerced to "" so a typo
        # in a curl request can't poison the cache.
        normalized = _normalize_reasoning_effort(payload.openai_reasoning_effort) or ""
        await _upsert_setting(session, "openai_reasoning_effort", {"value": normalized})
    await session.commit()

    # When the provider toggles, align the local-ai container *and* its
    # supervisor's desired-running flag. Two layers because they answer
    # different questions:
    #
    #   docker_control.ensure_local_ai → flips the whole container on/off
    #     (no-op when ADMIN_DOCKER_CONTROL=false or local-ai container
    #     was never created).
    #   supervisor.ensure_running     → tells the running supervisor to
    #     spawn or terminate llama-server inside the container (no-op
    #     when supervisor is unreachable, e.g. just-started container).
    #
    # docker_control runs first so the container is up by the time the
    # supervisor call lands.
    if ai_provider_changed:
        target_running = payload.ai_provider == "local"
        try:
            await docker_control.ensure_local_ai(target_running)
        except Exception:  # noqa: BLE001
            logger = __import__("logging").getLogger(__name__)
            logger.exception("docker_control.ensure_local_ai failed")
        await supervisor.ensure_running(target_running)
    return await read_settings(session, admin)


# --- Invites ------------------------------------------------------------------


@router.get("/invites", response_model=list[InviteOut])
async def list_invites(session: SessionDep, _: AdminUser) -> list[InviteOut]:
    rows = (
        await session.execute(select(Invite).order_by(Invite.created_at.desc()))
    ).scalars().all()
    return [InviteOut.model_validate(r) for r in rows]


@router.post("/invites", response_model=InviteOut, status_code=status.HTTP_201_CREATED)
async def create_invite(payload: InviteIn, session: SessionDep, admin: AdminUser) -> InviteOut:
    invite = await auth_service.create_invite(
        session, email=payload.email, inviter=admin, locale=payload.locale
    )
    await session.commit()
    return InviteOut.model_validate(invite)


@router.delete("/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(invite_id: uuid.UUID, session: SessionDep, _: AdminUser) -> None:
    await auth_service.revoke_invite(session, invite_id)
    await session.commit()


# --- Users --------------------------------------------------------------------


@router.get("/users", response_model=list[UserOut])
async def list_users(session: SessionDep, _: AdminUser) -> list[UserOut]:
    rows = (await session.execute(select(User).order_by(User.created_at.desc()))).scalars().all()
    return [UserOut.model_validate(u) for u in rows]


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID, payload: AdminUserUpdate, session: SessionDep, _: AdminUser
) -> UserOut:
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise NotFoundError("User not found.")
    if payload.role is not None:
        user.role = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active
    await session.commit()
    return UserOut.model_validate(user)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID, session: SessionDep, admin: AdminUser
) -> None:
    """Hard-delete a user along with all of their data.

    Mirrors ``DELETE /users/me``: owned reports cascade to fights/players/
    analyses, the WCL connection cascades on the user row. Two safeguards:

    - admins cannot delete themselves (use ``/users/me`` instead, which
      also tears down the auth session cleanly);
    - the *last* remaining admin cannot be deleted, so the instance never
      ends up with zero admins.
    """
    if user_id == admin.id:
        raise ForbiddenError(
            "Admins cannot delete their own account here. Use the profile "
            "page to delete your own account."
        )
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise NotFoundError("User not found.")
    if user.role == UserRole.admin:
        admin_count = (
            await session.execute(
                select(func.count(User.id)).where(User.role == UserRole.admin)
            )
        ).scalar_one()
        if admin_count <= 1:
            raise ForbiddenError(
                "Cannot delete the last remaining admin — promote another "
                "user to admin first."
            )
    await session.execute(delete(Report).where(Report.owner_user_id == user_id))
    await session.flush()
    await session.delete(user)
    await session.commit()


# --- System (docker control, opt-in) -----------------------------------------


def _to_container_out(info: docker_control.ContainerInfo) -> ContainerOut:
    return ContainerOut(
        name=info.name,
        service=info.service,
        image=info.image,
        status=info.status,
        health=info.health,
        started_at=info.started_at,
        finished_at=info.finished_at,
        is_local_ai=info.is_local_ai,
    )


@router.get("/system", response_model=SystemStatusOut)
async def read_system_status(_: AdminUser) -> SystemStatusOut:
    """Return ``enabled=False`` (no error) when ADMIN_DOCKER_CONTROL is off,
    so the frontend can hide the System card without surfacing a scary
    network error to the admin."""
    if not settings.admin_docker_control:
        return SystemStatusOut(
            enabled=False,
            project=settings.docker_compose_project,
            containers=[],
        )
    try:
        items = await docker_control.list_stack_containers()
    except Exception as exc:  # noqa: BLE001
        # Fail soft — admin sees an empty list with a clear server log line.
        logger = __import__("logging").getLogger(__name__)
        logger.exception("docker list_stack_containers failed: %s", exc)
        return SystemStatusOut(
            enabled=True,
            project=settings.docker_compose_project,
            containers=[],
        )
    return SystemStatusOut(
        enabled=True,
        project=settings.docker_compose_project,
        containers=[_to_container_out(i) for i in items],
    )


@router.post("/system/containers/{name}/restart", response_model=ContainerOut)
async def restart_container(name: str, _: AdminUser) -> ContainerOut:
    return _to_container_out(await docker_control.restart(name))


@router.post("/system/containers/{name}/start", response_model=ContainerOut)
async def start_container(name: str, _: AdminUser) -> ContainerOut:
    return _to_container_out(await docker_control.start(name))


@router.post("/system/containers/{name}/stop", response_model=ContainerOut)
async def stop_container(name: str, _: AdminUser) -> ContainerOut:
    return _to_container_out(await docker_control.stop(name))


# --- Local-AI supervisor (model download / switch / cache cleanup) ---------
#
# These endpoints are thin wrappers over the local-ai sidecar's management
# API. They return ``reachable=False`` (rather than 502) when the
# supervisor isn't up so the admin UI can render the card with a clear
# "container not running" hint instead of an alert dialog.


@router.get("/local-ai/status", response_model=LocalAiStatusOut)
async def read_local_ai_status(_: AdminUser) -> LocalAiStatusOut:
    try:
        data = await supervisor.get_status()
    except UpstreamError:
        return LocalAiStatusOut(reachable=False)
    return LocalAiStatusOut(reachable=True, **data)


@router.patch("/local-ai/config", response_model=LocalAiStatusOut)
async def patch_local_ai_config(
    payload: LocalAiConfigPatch, _: AdminUser
) -> LocalAiStatusOut:
    data = await supervisor.patch_config(
        config=payload.config.model_dump() if payload.config else None,
        desired_running=payload.desired_running,
    )
    return LocalAiStatusOut(reachable=True, **data)


@router.post("/local-ai/start", response_model=LocalAiStatusOut)
async def start_local_ai(_: AdminUser) -> LocalAiStatusOut:
    data = await supervisor.start_inference()
    return LocalAiStatusOut(reachable=True, **data)


@router.post("/local-ai/stop", response_model=LocalAiStatusOut)
async def stop_local_ai(_: AdminUser) -> LocalAiStatusOut:
    data = await supervisor.stop_inference()
    return LocalAiStatusOut(reachable=True, **data)


@router.get("/local-ai/models", response_model=list[LocalAiModelFile])
async def list_local_ai_models(_: AdminUser) -> list[LocalAiModelFile]:
    try:
        rows = await supervisor.list_models()
    except UpstreamError:
        return []
    return [LocalAiModelFile(**r) for r in rows]


@router.delete("/local-ai/models/{filename}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_local_ai_model(filename: str, _: AdminUser) -> None:
    await supervisor.delete_model(filename)


# --- SimC sidecar -----------------------------------------------------------
#
# Two endpoints: status (reachability + reported version + container row)
# and a one-shot "pull latest + recreate" for tracking the upstream
# simulationcraftorg/simc image. The upstream image is rebuilt daily
# with the current binary and DBC data, so a single button click is
# all the maintenance this needs in practice.


@router.get("/simc/status", response_model=SimcStatusOut)
async def read_simc_status(_: AdminUser) -> SimcStatusOut:
    banner = ""
    reachable = False
    try:
        info = await simc_service.ping_version()
        banner = info.get("banner") or ""
        reachable = True
    except UpstreamError:
        reachable = False

    container_out: ContainerOut | None = None
    if settings.admin_docker_control:
        try:
            row = await docker_control.list_stack_containers()
            simc_row = next((c for c in row if c.service == "simc"), None)
            if simc_row is not None:
                container_out = _to_container_out(simc_row)
        except UpstreamError:
            container_out = None

    return SimcStatusOut(
        reachable=reachable,
        build_banner=banner,
        base_url=settings.simc_base_url,
        container=container_out,
    )


@router.post("/simc/update", response_model=SimcStatusOut)
async def update_simc_sidecar(_: AdminUser) -> SimcStatusOut:
    """Pull the latest simc sidecar image and recreate the container.

    Requires ADMIN_DOCKER_CONTROL + the docker socket mount on the
    backend container. The recreate step preserves compose labels and
    network attachment so the worker keeps reaching it as ``simc:8090``.
    """
    info = await docker_control.pull_and_recreate("simc")
    container_out = _to_container_out(info)
    # Probe the version banner once more so the response reflects the
    # *new* build the admin just rolled forward to.
    banner = ""
    reachable = False
    try:
        v = await simc_service.ping_version()
        banner = v.get("banner") or ""
        reachable = True
    except UpstreamError:
        reachable = False
    return SimcStatusOut(
        reachable=reachable,
        build_banner=banner,
        base_url=settings.simc_base_url,
        container=container_out,
    )


# --- Talent-Finder: encounter mapping -----------------------------------------


@router.get("/talent-finder/encounter-map", response_model=EncounterMap)
async def read_talent_finder_encounter_map(
    session: SessionDep, _: AdminUser
) -> EncounterMap:
    """Current (fight_profile → encounter) mapping the talent-finder mines.

    Returns an empty map (all entries ``None``) when the admin hasn't
    configured anything yet — that's a valid state, the user-facing
    talent-finder endpoint will then reject runs with a clear error.
    """
    return await talent_finder_service.read_encounter_map(session)


@router.put("/talent-finder/encounter-map", response_model=EncounterMap)
async def update_talent_finder_encounter_map(
    payload: EncounterMap, session: SessionDep, _: AdminUser
) -> EncounterMap:
    """Replace the entire (fight_profile → encounter) mapping.

    PUT semantics: the full map in the body is what gets stored. To
    clear a profile, send ``null`` for that field. The admin form is
    expected to read the current map first, mutate locally, and PUT
    back — no PATCH endpoint by design (the map is tiny).
    """
    await talent_finder_service.write_encounter_map(session, payload)
    await session.commit()
    return await talent_finder_service.read_encounter_map(session)

