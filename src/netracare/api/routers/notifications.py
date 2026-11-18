from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query

from netracare.api.deps import DbSession, SettingsDep
from netracare.api.schemas.common import Page, Schema
from netracare.api.security import CurrentUserDep
from netracare.domain.enums import NotificationChannel, NotificationStatus
from netracare.services.notification_service import NotificationService

router = APIRouter(tags=["notifications"])


class NotificationOut(Schema):
    id: str
    channel: NotificationChannel
    to_patient_id: str
    template: str
    payload_json: dict[str, Any]
    status: NotificationStatus
    created_at: datetime


@router.get("/notifications")
def list_notifications(
    user: CurrentUserDep,
    db: DbSession,
    settings: SettingsDep,
    patient_id: Annotated[str | None, Query(max_length=36)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[NotificationOut]:
    page = NotificationService(db, settings.tz).list_for(user, patient_id, limit, offset)
    return Page[NotificationOut](
        items=[NotificationOut.model_validate(n) for n in page.items],
        total=page.total,
        limit=limit,
        offset=offset,
    )
