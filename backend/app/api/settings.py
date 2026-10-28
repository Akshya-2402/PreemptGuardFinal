from fastapi import APIRouter, HTTPException

from app.core.config import settings_store
from app.core.database import database
from app.schemas import SettingsUpdate, validate_settings_update

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
def get_settings():
    return settings_store.get().model_dump()


@router.put("")
def update_settings(request: SettingsUpdate):
    try:
        candidate = validate_settings_update(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    values = candidate.model_dump()
    for key, value in values.items():
        database.execute(
            "INSERT INTO app_settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
    return settings_store.update(request.model_dump(exclude_none=True)).model_dump()
