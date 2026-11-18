from __future__ import annotations

from fastapi import APIRouter, Response, status

from netracare.api.deps import ClockDep, DbSession, SettingsDep, ThrottleDep
from netracare.api.schemas.auth import (
    LoginRequest,
    MeResponse,
    RefreshRequest,
    RegisterRequest,
    TokenPairOut,
)
from netracare.api.security import CurrentUserDep
from netracare.services.access import AccessPolicy
from netracare.services.auth_service import AuthService, RegisterPatient, TokenPair

router = APIRouter(prefix="/auth", tags=["auth"])


def _out(pair: TokenPair) -> TokenPairOut:
    return TokenPairOut(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        token_type=pair.token_type,
        expires_in=pair.expires_in,
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(
    body: RegisterRequest, db: DbSession, settings: SettingsDep, clock: ClockDep, t: ThrottleDep
) -> TokenPairOut:
    svc = AuthService(db, settings, clock, t)
    return _out(svc.register_patient(RegisterPatient(**body.model_dump())))


@router.post("/login")
def login(
    body: LoginRequest, db: DbSession, settings: SettingsDep, clock: ClockDep, t: ThrottleDep
) -> TokenPairOut:
    pair, _ = AuthService(db, settings, clock, t).login(body.email, body.password)
    return _out(pair)


@router.post("/refresh")
def refresh(
    body: RefreshRequest, db: DbSession, settings: SettingsDep, clock: ClockDep, t: ThrottleDep
) -> TokenPairOut:
    return _out(AuthService(db, settings, clock, t).refresh(body.refresh_token))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    body: RefreshRequest, db: DbSession, settings: SettingsDep, clock: ClockDep, t: ThrottleDep
) -> Response:
    AuthService(db, settings, clock, t).logout(body.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me")
def me(user: CurrentUserDep, db: DbSession) -> MeResponse:
    return MeResponse(
        user_id=user.id,
        email=user.email,
        role=user.role,
        display_name=user.display_name,
        doctor_id=user.doctor_id,
        patient_ids=sorted(AccessPolicy(db).patient_ids_for(user)),
    )
