from datetime import datetime

from pydantic import BaseModel


class PublicLaunchStatus(BaseModel):
    gate_enabled: bool
    launch_at: datetime
