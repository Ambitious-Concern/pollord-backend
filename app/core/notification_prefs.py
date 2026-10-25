from typing import Dict, Optional

# Mirrors the frontend's NotificationKey union exactly (pollord-frontend's
# settings-data.ts NOTIFICATION_GROUPS) — these are the "send me an email
# when..." toggles on the Notifications settings tab. Separate from
# NOTIFICATION_TYPES in app/models/notification.py, which is the coarser
# taxonomy for the always-on in-app bell feed; these keys only gate the
# email channel.
DEFAULT_NOTIFICATION_PREFS: Dict[str, bool] = {
    "vote_cast": True,
    "election_started": True,
    "election_ended": True,
    "event_published": True,
    "ticket_purchased": True,
    "payout_requested": True,
    "payout_paid": True,
    "payout_rejected": True,
    "new_member": False,
    "invitation_accepted": False,
}

NOTIFICATION_KEYS = tuple(DEFAULT_NOTIFICATION_PREFS.keys())


def effective_prefs(stored: Optional[dict]) -> Dict[str, bool]:
    """Merge a user's stored (possibly partial, possibly null) preferences
    dict over the defaults. Takes a plain dict rather than a User so it
    works identically from the sync Celery session used by the
    election-auto-close job."""
    if not stored:
        return dict(DEFAULT_NOTIFICATION_PREFS)
    return {**DEFAULT_NOTIFICATION_PREFS, **{k: v for k, v in stored.items() if k in DEFAULT_NOTIFICATION_PREFS}}


def wants_email(stored: Optional[dict], key: str) -> bool:
    return effective_prefs(stored)[key]
