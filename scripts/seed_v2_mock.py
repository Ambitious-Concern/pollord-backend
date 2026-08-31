"""
Seed the database with data matching the v2 mock (pollord-frontend's
src/components/v2/shared/listings-data.ts) — for wiring the v2 public pages
to the real backend instead of the static mock.

Ports the mock's 6 listings (5 elections + 1 ticketed event), their
categories/nominees, and (for the event) ticket types, plus casts a modest
number of real encrypted votes per nominee so live results/leaderboards have
something to show. Idempotent by title — safe to re-run, skips anything that
already exists.

Run inside the backend container (has the same DATABASE_URL as the app):
    docker compose exec app python scripts/seed_v2_mock.py

Or locally against the dev DB:
    DATABASE_URL=postgresql+asyncpg://pollard:pollard@localhost:5432/pollard \
        .venv/Scripts/python.exe scripts/seed_v2_mock.py
"""
import asyncio
import hashlib
import sys
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.core.security import hash_password
from app.db.base import async_session_maker
from app.models.election import Candidate, Category, Election
from app.models.event import Event, TicketType
from app.models.organization import Organization, OrganizationMember
from app.models.user import Role, User, UserRole
from app.models.vote import Vote
from app.repositories.user_repository import UserRepository
from app.services.cryptography_service import CryptographyService

NOMINEE_NAME_POOL = [
    'Kwame Mensah', 'Ama Owusu', 'Yaw Boateng', 'Efua Asante', 'Kojo Appiah',
    'Abena Sarpong', 'Kofi Adjei', 'Akosua Frimpong', 'Yaa Asantewaa', 'Kwabena Otoo',
    'Adjoa Danso', 'Kwesi Amponsah', 'Afia Nyarko', 'Kwaku Gyasi', 'Esi Bonsu',
    'Fiifi Arthur', 'Adwoa Serwaa', 'Kobina Tetteh', 'Abena Konadu', 'Nana Yeboah',
    'Aba Darko', 'Kwame Antwi',
]

NOMINEE_PHOTOS = [
    'https://images.pexels.com/photos/16152597/pexels-photo-16152597.jpeg?auto=compress&cs=tinysrgb&w=100',
    'https://images.pexels.com/photos/12311572/pexels-photo-12311572.jpeg?auto=compress&cs=tinysrgb&w=100',
    'https://images.pexels.com/photos/36322504/pexels-photo-36322504.jpeg?auto=compress&cs=tinysrgb&w=100',
    'https://images.pexels.com/photos/6311668/pexels-photo-6311668.jpeg?auto=compress&cs=tinysrgb&w=200',
    'https://images.pexels.com/photos/38670596/pexels-photo-38670596.jpeg?auto=compress&cs=tinysrgb&w=200',
    'https://images.pexels.com/photos/38165826/pexels-photo-38165826.jpeg?auto=compress&cs=tinysrgb&w=200',
]

TICKET_TIER_META = [
    ('General Admission', 'Standard entry with access to all main sessions.', 50),
    ('VIP', 'Priority seating, a welcome pack, and a reserved table.', 120),
    ('VVIP', 'Front-row seating, backstage access, and a meet-and-greet.', 250),
]


def hash_string(value: str) -> int:
    """Mirror listings-data.ts's deterministic string hash (same formula,
    without JS's 32-bit overflow — Python ints don't overflow, so we mask
    manually to match)."""
    h = 0
    for ch in value:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    return abs(h)


def nominees_for(category_label: str, top_votes: int) -> list[dict]:
    h = hash_string(category_label)
    count = 3 + (h % 3)  # 3-5 nominees, same as the mock
    result = []
    for i in range(count):
        name = NOMINEE_NAME_POOL[(h + i * 7) % len(NOMINEE_NAME_POOL)]
        photo = NOMINEE_PHOTOS[(h + i * 3) % len(NOMINEE_PHOTOS)]
        mock_votes = max(2, round(top_votes / (1.4 ** i)) + ((i * 5) % 7))
        # Scaled down from the mock's display-only numbers (which assume a
        # rendered chart, not real DB rows) to a realistic seed vote count.
        votes = max(1, round(mock_votes / 15))
        result.append({"name": name, "photo": photo, "votes": votes})
    return result


def with_category_nominees(names: list[str], top_votes: int) -> list[dict]:
    categories = []
    for i, label in enumerate(names):
        budget = max(20, round(top_votes / (1.15 ** i)) + ((i * 7919) % 11))
        categories.append({"label": label, "nominees": nominees_for(label, budget)})
    return categories


def ticket_types_for(seed: str) -> list[dict]:
    h = hash_string(seed)
    types = []
    for i, (name, description, base_price) in enumerate(TICKET_TIER_META):
        types.append({
            "name": name,
            "description": description,
            "price": base_price + (h % 20) + i * 5,
            "quantity_sold": 200 + ((h + i * 53) % 1200),
            "quantity_available": max(0, 150 + ((h + i * 29) % 400) - i * 40),
        })
    return types


LISTINGS = [
    {
        "slug": "etrc", "type": "election", "status": "live",
        "title": "ETRC",
        "image": "https://images.pexels.com/photos/1550340/pexels-photo-1550340.jpeg?auto=compress&cs=tinysrgb&w=600",
        "description": "A single-choice election run end to end on Pollord, from nomination to live, verifiable results.",
        "venue": "Accra Sports Stadium Annex", "lat": 5.6037, "lng": -0.187, "tag": "Election",
        "top_votes": 210,
        "categories": ["President", "Vice President", "General Secretary", "Assistant Secretary",
                       "Financial Secretary", "Treasurer", "Organizing Secretary", "Public Relations Officer",
                       "Women's Commissioner", "Youth Organizer", "Welfare Officer", "Sports Director",
                       "Chaplain", "Auditor", "Membership Secretary", "Disciplinary Committee Chair",
                       "Education Officer", "Legal Adviser", "Publicity Secretary", "Trustee",
                       "Ex-Officio Member", "Regional Representative"],
        "organiser": "ETRC",
    },
    {
        "slug": "stool-awards", "type": "election", "status": "completed",
        "title": "Stool Awards",
        "image": "https://images.pexels.com/photos/35755225/pexels-photo-35755225.jpeg?auto=compress&cs=tinysrgb&w=600",
        "description": "Traditional leadership recognition awards decided by public vote, with results verified live.",
        "venue": "Manhyia Palace Grounds", "lat": 6.6885, "lng": -1.6244, "tag": "Ceremony",
        "top_votes": 390,
        "categories": ["Best Chief of the Year", "Outstanding Queen Mother", "Most Progressive Community",
                       "Cultural Preservation Award", "Development Chief Award", "Peace and Unity Award",
                       "Best Traditional Council", "Youth Empowerment Chief", "Heritage Ambassador Award",
                       "Community Impact Award", "Best Durbar Organisation", "Traditional Leadership Excellence",
                       "Chieftaincy Reform Award", "Best Sub-Chief", "Women in Chieftaincy Award",
                       "Rural Development Award", "Environmental Stewardship Chief", "Education Advocacy Award",
                       "Best Palace Administration", "Lifetime Service to Tradition"],
        "organiser": "Kumasi Traditional Council",
    },
    {
        "slug": "ac-academy-awards", "type": "election", "status": "completed",
        "title": "AC Academy Awards",
        "image": "https://images.pexels.com/photos/29229896/pexels-photo-29229896.jpeg?auto=compress&cs=tinysrgb&w=600",
        "description": "Cohort excellence awards for AC Academy's IT training program, voted by peers and mentors.",
        "venue": "CCB Auditorium", "lat": 5.65, "lng": -0.187, "tag": "Ceremony",
        "top_votes": 145,
        "categories": ["Most Dedicated Worker", "Best Final Project", "Most Improved Student",
                       "Outstanding Teamwork", "Best Presentation", "Rising Star Award",
                       "Coding Excellence Award", "Best Mentee", "Most Innovative Solution",
                       "Perfect Attendance", "Team Player Award", "Best Debugging Skills",
                       "Fastest Learner", "Community Contributor", "Best Portfolio", "Leadership Award",
                       "Creative Problem Solver", "Most Helpful Peer", "Excellence in Design",
                       "Top Performer", "Best Code Review Partner", "Hackathon Champion"],
        "organiser": "AC Academy",
    },
    {
        "slug": "homecoming-gala", "type": "election", "status": "completed",
        "title": "Homecoming Gala",
        "image": "https://images.pexels.com/photos/6532375/pexels-photo-6532375.jpeg?auto=compress&cs=tinysrgb&w=600",
        "description": "Homecoming royalty and category awards decided entirely by student and alumni votes.",
        "venue": "Grand Arena Accra", "lat": 5.5913, "lng": -0.2508, "tag": "Gala",
        "top_votes": 315,
        "categories": ["Homecoming King", "Homecoming Queen", "Best Dressed", "Most Popular",
                       "Class Spirit Award", "Best Performance", "Alumni Choice Award",
                       "Most Likely to Succeed", "Best Smile", "Life of the Party", "Best Dance Crew",
                       "Reunion Spirit Award", "Golden Alumni Award", "Best Throwback Photo",
                       "Most School Pride", "Best Speech", "Outstanding Volunteer",
                       "Best Table Decoration", "Photogenic Award", "Legacy Award"],
        "organiser": "Legon Alumni Association",
    },
    {
        "slug": "best-dressed-awards", "type": "election", "status": "completed",
        "title": "Best Dressed Awards",
        "image": "https://images.pexels.com/photos/19837893/pexels-photo-19837893.jpeg?auto=compress&cs=tinysrgb&w=600",
        "description": "Style and elegance awards, judged entirely by public vote.",
        "venue": "La Palm Royal Beach Hotel", "lat": 5.5605, "lng": -0.1697, "tag": "Fashion Show",
        "top_votes": 110,
        "categories": ["Best Dressed Male", "Best Dressed Female", "Most Elegant", "Best Traditional Wear",
                       "Best Red Carpet Look", "Style Icon of the Year", "Best Accessories",
                       "Trendsetter Award", "Best Suit", "Best Gown", "Most Creative Outfit",
                       "Best Hairstyle", "Best Makeup Look", "Fashion Forward Award",
                       "Best Colour Coordination", "Most Confident Walk", "Best Vintage Look",
                       "Emerging Designer Award", "Best Group Outfit Theme", "People's Choice Style Award"],
        "organiser": "Silver Fox Events",
    },
    {
        "slug": "tech-fair-2026", "type": "event", "status": "upcoming",
        "title": "Tech Fair 2026",
        "image": "https://images.pexels.com/photos/35138560/pexels-photo-35138560.jpeg?auto=compress&cs=tinysrgb&w=600",
        "description": "A hands-on technology exhibition and fair, with ticketed entry and live demos.",
        "venue": "Kumasi Trade Fair Centre", "lat": 6.6666, "lng": -1.6163, "tag": "Exhibition",
        "top_votes": 88,
        "categories": ["Best Startup Pitch", "Most Innovative Hardware", "Best AI Application",
                       "People's Choice Award", "Best Student Project", "Outstanding Robotics Demo",
                       "Best Fintech Solution", "Green Tech Innovation Award", "Best UI/UX Design",
                       "Rising Developer Award", "Best Cybersecurity Solution", "Most Scalable Idea",
                       "Best Hardware Hack", "Community Impact Tech Award", "Best Mobile App",
                       "Excellence in Data Science", "Best Open Source Project", "Most Disruptive Idea",
                       "Best Demo Booth", "Judges' Choice Award"],
        "organiser": "Ghana Tech Collective",
    },
]

ELECTION_STATUS_MAP = {"live": "active", "completed": "completed", "upcoming": "scheduled"}


async def get_or_create_organiser(db, org_name: str) -> Organization:
    """One seed user + org per organiser, idempotent by org name."""
    result = await db.execute(select(Organization).where(Organization.name == org_name))
    existing = result.scalar_one_or_none()
    if existing:
        return existing

    slug = org_name.lower().replace(" ", "-").replace("'", "")
    email = f"seed+{slug}@pollord.dev"
    user_repo = UserRepository(User, db)
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user:
        user = User(
            email=email,
            password_hash=hash_password("SeedUser@1234"),
            full_name=f"{org_name} (seed)",
            email_verified=True,
            account_status="active",
        )
        db.add(user)
        await db.flush()

    org = Organization(name=org_name, is_verified=True, owner_id=user.user_id)
    db.add(org)
    await db.flush()
    db.add(OrganizationMember(org_id=org.org_id, user_id=user.user_id, role="owner"))
    await user_repo.grant_roles_by_name(user.user_id, ["Election Administrator", "Event Organizer"])
    await db.flush()
    return org


async def cast_seed_votes(db, crypto: CryptographyService, category_id, election_id, event_id, candidate_id, count: int):
    cast_at = datetime.now(timezone.utc).isoformat()
    for _ in range(count):
        encrypted = crypto.encrypt_vote_data([str(candidate_id)])
        signature = crypto.sign_vote(encrypted, cast_at)
        voter_hash = hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest()
        db.add(Vote(
            category_id=category_id,
            election_id=election_id,
            event_id=event_id,
            voter_hash=voter_hash,
            vote_data=encrypted,
            vote_signature=signature,
            count=1,
        ))


async def seed_listing(db, crypto: CryptographyService, listing: dict) -> None:
    result = await db.execute(select(Election).where(Election.title == listing["title"]))
    if listing["type"] == "election" and result.scalar_one_or_none():
        print(f"  skip (exists): {listing['title']}")
        return
    if listing["type"] == "event":
        result = await db.execute(select(Event).where(Event.title == listing["title"]))
        if result.scalar_one_or_none():
            print(f"  skip (exists): {listing['title']}")
            return

    org = await get_or_create_organiser(db, listing["organiser"])
    now = datetime.now(timezone.utc)

    if listing["type"] == "election":
        if listing["status"] == "live":
            start, end = now - timedelta(days=3), now + timedelta(days=14)
        elif listing["status"] == "completed":
            start, end = now - timedelta(days=30), now - timedelta(days=5)
        else:
            start, end = now + timedelta(days=7), now + timedelta(days=21)

        parent = Election(
            title=listing["title"],
            description=listing["description"],
            start_datetime=start,
            end_datetime=end,
            status=ELECTION_STATUS_MAP[listing["status"]],
            created_by=org.owner_id,
            banner_image_url=listing["image"],
            visibility="public",
            require_verification=False,
            venue=listing["venue"],
            latitude=listing["lat"],
            longitude=listing["lng"],
            tag=listing["tag"],
        )
        db.add(parent)
        await db.flush()
        election_id, event_id = parent.election_id, None
    else:
        event_date = (now + timedelta(days=16)).date()
        parent = Event(
            title=listing["title"],
            description=listing["description"],
            event_date=event_date,
            event_time=time(10, 0),
            location=listing["venue"],
            category=listing["tag"],
            latitude=listing["lat"],
            longitude=listing["lng"],
            banner_image_url=listing["image"],
            status="published",
            created_by=org.owner_id,
        )
        db.add(parent)
        await db.flush()
        election_id, event_id = None, parent.event_id

        for tier in ticket_types_for(listing["slug"]):
            db.add(TicketType(
                event_id=parent.event_id,
                type_name=tier["name"],
                description=tier["description"],
                price=Decimal(tier["price"]),
                quantity_available=tier["quantity_available"],
                quantity_sold=tier["quantity_sold"],
                max_per_user=5,
            ))

    categories = with_category_nominees(listing["categories"], listing["top_votes"])
    for order, cat in enumerate(categories):
        category = Category(
            election_id=election_id,
            event_id=event_id,
            name=cat["label"],
            election_type="single_choice",
            display_order=order,
        )
        db.add(category)
        await db.flush()

        for c_order, nominee in enumerate(cat["nominees"]):
            candidate_id = uuid.uuid4()
            short_code = str(candidate_id).replace("-", "")[-4:].upper()
            db.add(Candidate(
                candidate_id=candidate_id,
                category_id=category.category_id,
                election_id=election_id,
                event_id=event_id,
                name=nominee["name"],
                short_code=short_code,
                image_url=nominee["photo"],
                display_order=c_order,
            ))
            if listing["status"] != "upcoming":
                await cast_seed_votes(
                    db, crypto, category.category_id, election_id, event_id,
                    candidate_id, nominee["votes"],
                )

    await db.flush()
    print(f"  seeded: {listing['title']} ({len(categories)} categories)")


async def main():
    crypto = CryptographyService()
    async with async_session_maker() as db:
        print("Seeding v2 mock data...")
        for listing in LISTINGS:
            await seed_listing(db, crypto, listing)
        await db.commit()
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
