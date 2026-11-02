"""Seed realistic abuse alerts spanning all types, severities, and statuses."""
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.abuse_alert import AbuseAlert, AlertSeverity, AlertStatus, AlertType
from app.models.user import User

_IPS = [
    "185.220.101.47",  # known Tor exit
    "45.142.212.100",  # VPN range
    "103.21.244.0",
    "91.108.4.0",
    "198.51.100.23",
    "203.0.113.44",
    "192.0.2.17",
    "62.210.180.229",
    "94.102.49.190",
    "176.10.104.240",
    "195.206.105.217",
    "89.234.157.254",
]

_PATHS = [
    "/api/v1/auth/login",
    "/api/v1/auth/otp/verify",
    "/api/v1/elections/{id}/vote",
    "/api/v1/payments/initiate",
    "/api/v1/users/me",
    "/api/v1/auth/refresh",
    "/api/v1/organizations",
    "/api/v1/payouts/request",
]

_ALERTS: list[dict] = [
    # CRITICAL — need immediate attention
    {
        "alert_type": AlertType.DDOS,
        "severity": AlertSeverity.CRITICAL,
        "source_ip": "185.220.101.47",
        "target_path": "/api/v1/elections/{id}/vote",
        "detail": {"requests_per_second": 3400, "peak_rps": 5800, "origin_asn": "AS16276", "unique_ips": 147},
        "status": AlertStatus.INVESTIGATING,
        "offset_hours": 2,
    },
    {
        "alert_type": AlertType.BRUTE_FORCE,
        "severity": AlertSeverity.CRITICAL,
        "source_ip": "45.142.212.100",
        "target_path": "/api/v1/auth/login",
        "detail": {"attempts": 2840, "window_minutes": 10, "unique_passwords": 2838, "accounts_targeted": 1},
        "status": AlertStatus.NEW,
        "offset_hours": 1,
    },
    {
        "alert_type": AlertType.PAYMENT_FRAUD,
        "severity": AlertSeverity.CRITICAL,
        "source_ip": "94.102.49.190",
        "target_path": "/api/v1/payments/initiate",
        "detail": {"failed_charges": 48, "window_minutes": 5, "card_bins_tried": 31, "total_amount_attempted": 9600},
        "status": AlertStatus.NEW,
        "offset_hours": 0,
    },
    # HIGH
    {
        "alert_type": AlertType.OTP_ABUSE,
        "severity": AlertSeverity.HIGH,
        "source_ip": "103.21.244.0",
        "target_path": "/api/v1/auth/otp/verify",
        "detail": {"otp_requests": 340, "window_minutes": 15, "phone_numbers_targeted": 12},
        "status": AlertStatus.INVESTIGATING,
        "offset_hours": 5,
    },
    {
        "alert_type": AlertType.VOTING_ANOMALY,
        "severity": AlertSeverity.HIGH,
        "source_ip": "62.210.180.229",
        "target_path": "/api/v1/elections/{id}/vote",
        "detail": {"votes_from_ip": 89, "election_id": "elec_abc123", "time_span_seconds": 42},
        "status": AlertStatus.INVESTIGATING,
        "offset_hours": 8,
    },
    {
        "alert_type": AlertType.SUSPICIOUS_IP,
        "severity": AlertSeverity.HIGH,
        "source_ip": "176.10.104.240",
        "target_path": "/api/v1/auth/login",
        "detail": {"tor_exit_node": True, "country": "NL", "failed_logins": 22},
        "status": AlertStatus.NEW,
        "offset_hours": 3,
    },
    {
        "alert_type": AlertType.RATE_LIMIT,
        "severity": AlertSeverity.HIGH,
        "source_ip": "195.206.105.217",
        "target_path": "/api/v1/organizations",
        "detail": {"requests": 1200, "window_seconds": 60, "limit": 100},
        "status": AlertStatus.RESOLVED,
        "offset_hours": 24,
    },
    # MEDIUM
    {
        "alert_type": AlertType.BRUTE_FORCE,
        "severity": AlertSeverity.MEDIUM,
        "source_ip": "91.108.4.0",
        "target_path": "/api/v1/auth/login",
        "detail": {"attempts": 85, "window_minutes": 30, "accounts_targeted": 3},
        "status": AlertStatus.RESOLVED,
        "offset_hours": 48,
    },
    {
        "alert_type": AlertType.RATE_LIMIT,
        "severity": AlertSeverity.MEDIUM,
        "source_ip": "198.51.100.23",
        "target_path": "/api/v1/users/me",
        "detail": {"requests": 420, "window_seconds": 60, "limit": 60},
        "status": AlertStatus.DISMISSED,
        "offset_hours": 72,
    },
    {
        "alert_type": AlertType.OTP_ABUSE,
        "severity": AlertSeverity.MEDIUM,
        "source_ip": "203.0.113.44",
        "target_path": "/api/v1/auth/otp/verify",
        "detail": {"otp_requests": 65, "window_minutes": 20, "same_number": True},
        "status": AlertStatus.RESOLVED,
        "offset_hours": 36,
    },
    {
        "alert_type": AlertType.SUSPICIOUS_IP,
        "severity": AlertSeverity.MEDIUM,
        "source_ip": "192.0.2.17",
        "target_path": "/api/v1/payouts/request",
        "detail": {"vpn_detected": True, "country_mismatch": True, "registered_country": "GH", "request_country": "RU"},
        "status": AlertStatus.DISMISSED,
        "offset_hours": 96,
    },
    {
        "alert_type": AlertType.VOTING_ANOMALY,
        "severity": AlertSeverity.MEDIUM,
        "source_ip": "89.234.157.254",
        "target_path": "/api/v1/elections/{id}/vote",
        "detail": {"votes_from_ip": 14, "election_id": "elec_def456", "time_span_seconds": 180},
        "status": AlertStatus.RESOLVED,
        "offset_hours": 120,
    },
    # LOW
    {
        "alert_type": AlertType.RATE_LIMIT,
        "severity": AlertSeverity.LOW,
        "source_ip": "185.220.101.47",
        "target_path": "/api/v1/auth/refresh",
        "detail": {"requests": 95, "window_seconds": 60, "limit": 60},
        "status": AlertStatus.DISMISSED,
        "offset_hours": 144,
    },
    {
        "alert_type": AlertType.SUSPICIOUS_IP,
        "severity": AlertSeverity.LOW,
        "source_ip": "45.142.212.100",
        "target_path": "/api/v1/users/me",
        "detail": {"new_country": True, "usual_country": "GH", "current_country": "DE"},
        "status": AlertStatus.DISMISSED,
        "offset_hours": 168,
    },
    {
        "alert_type": AlertType.RATE_LIMIT,
        "severity": AlertSeverity.LOW,
        "source_ip": "103.21.244.0",
        "target_path": "/api/v1/organizations",
        "detail": {"requests": 72, "window_seconds": 60, "limit": 60},
        "status": AlertStatus.RESOLVED,
        "offset_hours": 200,
    },
]

_N_TARGET = len(_ALERTS)


async def seed_abuse_alerts(db: AsyncSession) -> None:
    result = await db.execute(select(func.count()).select_from(AbuseAlert))
    existing = result.scalar_one()
    if existing >= _N_TARGET:
        return

    # pick a resolver for resolved/dismissed alerts
    admin_result = await db.execute(select(User).limit(1))
    admin = admin_result.scalar_one_or_none()

    now = datetime.now(timezone.utc)

    for spec in _ALERTS:
        created = now - timedelta(hours=spec["offset_hours"], minutes=random.randint(0, 59))
        status: AlertStatus = spec["status"]

        resolved_at = None
        resolved_by = None
        if status in (AlertStatus.RESOLVED, AlertStatus.DISMISSED):
            resolved_at = created + timedelta(hours=random.randint(1, 4))
            if admin:
                resolved_by = admin.user_id

        db.add(AbuseAlert(
            alert_type=spec["alert_type"],
            severity=spec["severity"],
            source_ip=spec["source_ip"],
            target_path=spec["target_path"],
            detail=spec["detail"],
            status=status,
            created_at=created,
            resolved_at=resolved_at,
            resolved_by=resolved_by,
        ))

    await db.flush()
    print(f"  seeded {_N_TARGET} abuse alerts")
