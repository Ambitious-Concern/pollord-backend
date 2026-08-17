from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import get_db
from app.models.election import Election
from app.models.event import Event

router = APIRouter(tags=["Sitemap"])

# (path, changefreq, priority) for pages that always exist, independent of DB content.
STATIC_ROUTES = [
    ("/", "weekly", "1.0"),
    ("/explore", "daily", "0.9"),
    ("/pricing", "monthly", "0.8"),
    ("/contact", "monthly", "0.5"),
    ("/privacy", "yearly", "0.3"),
    ("/vote", "monthly", "0.5"),
]


def _url_entry(loc: str, lastmod, changefreq: str, priority: str) -> str:
    parts = [f"    <loc>{escape(loc)}</loc>"]
    if lastmod is not None:
        parts.append(f"    <lastmod>{lastmod.strftime('%Y-%m-%d')}</lastmod>")
    parts.append(f"    <changefreq>{changefreq}</changefreq>")
    parts.append(f"    <priority>{priority}</priority>")
    return "  <url>\n" + "\n".join(parts) + "\n  </url>"


@router.get("/sitemap.xml")
async def sitemap(db: AsyncSession = Depends(get_db)):
    """Public sitemap — static routes plus every publicly-visible election/event.

    No auth required; matches the same visibility rules as GET /api/v1/elections/public
    (visibility == "public", not a draft) and the "published" filter used for events.
    """
    base = settings.FRONTEND_URL.rstrip("/")
    entries = [_url_entry(f"{base}{path}", None, changefreq, priority) for path, changefreq, priority in STATIC_ROUTES]

    elections = await db.execute(
        select(Election.election_id, Election.updated_at).where(
            Election.visibility == "public",
            Election.status != "draft",
        )
    )
    for election_id, updated_at in elections.all():
        entries.append(_url_entry(f"{base}/public/elections/{election_id}", updated_at, "weekly", "0.7"))

    events = await db.execute(
        select(Event.event_id, Event.updated_at).where(Event.status == "published")
    )
    for event_id, updated_at in events.all():
        entries.append(_url_entry(f"{base}/public/events/{event_id}", updated_at, "weekly", "0.7"))

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries)
        + "\n</urlset>\n"
    )
    return Response(content=xml, media_type="application/xml")
