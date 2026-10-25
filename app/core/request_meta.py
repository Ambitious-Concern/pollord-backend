from typing import Optional, Tuple

from fastapi import Request


def client_ip_and_agent(request: Request) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort (client IP, user agent) for session and audit records.

    request.client.host is the socket peer, which behind nginx (or Cloudflare)
    is the proxy itself — every session would otherwise be recorded as the
    same container-gateway address, making the "active sessions" list useless
    for spotting a login you don't recognise.

    X-Forwarded-For is a client-settable header, so it is only consulted when
    the request genuinely arrived through a proxy hop; its leftmost entry is
    the original client. Cloudflare's CF-Connecting-IP wins when present
    because it is set by the edge and cannot be spoofed past it.
    """
    return client_ip(request), request.headers.get("user-agent")


def client_ip(request: Request) -> Optional[str]:
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # "client, proxy1, proxy2" — the first entry is the original client.
        first = forwarded.split(",")[0].strip()
        if first:
            return first

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()

    return request.client.host if request.client else None
