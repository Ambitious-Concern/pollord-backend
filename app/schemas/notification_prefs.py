from typing import Dict, Optional

from pydantic import BaseModel, field_validator

from app.core.notification_prefs import NOTIFICATION_KEYS


class NotificationPrefsResponse(BaseModel):
    prefs: Dict[str, bool]


class NotificationPrefsUpdate(BaseModel):
    """Partial update — omitted keys keep their current (or default) value."""
    prefs: Dict[str, bool]

    @field_validator("prefs")
    @classmethod
    def validate_keys(cls, v: Dict[str, bool]) -> Dict[str, bool]:
        invalid = set(v) - set(NOTIFICATION_KEYS)
        if invalid:
            raise ValueError(f"Unknown notification preference(s): {sorted(invalid)}")
        return v
