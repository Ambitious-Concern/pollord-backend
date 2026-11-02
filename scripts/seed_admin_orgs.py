"""
Seed a varied set of organizations for the admin Organizations page UI demo.
Mix of verified / pending, different industries and member counts.
Idempotent by org name — safe to re-run.

Run inside the dev environment:
    DATABASE_URL=postgresql+asyncpg://pollard:pollard@localhost:55432/pollard \
        .venv/Scripts/python.exe scripts/seed_admin_orgs.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.core.security import hash_password
from app.db.base import async_session_maker
from app.models.organization import Organization, OrganizationMember
from app.models.user import User

ORG_SEEDS = [
    {
        "name": "Accra Tech Hub",
        "industry": "Technology",
        "description": "Connecting Ghana's developer ecosystem through hackathons and meetups.",
        "email": "hello@accratechhub.com",
        "phone": "+233 30 270 1234",
        "website": "https://accratechhub.com",
        "address": "5 Liberation Road, Accra",
        "is_verified": True,
        "kyc_front": "/uploads/kyc/seed_front_1.pdf",
        "kyc_back": "/uploads/kyc/seed_back_1.pdf",
        "members": [
            {"name": "Kwame Mensah", "email": "kwame@accratechhub.com", "role": "owner"},
            {"name": "Ama Owusu", "email": "ama@accratechhub.com", "role": "admin"},
            {"name": "Yaw Boateng", "email": "yaw@accratechhub.com", "role": "member"},
        ],
    },
    {
        "name": "University of Ghana SRC",
        "industry": "Education",
        "description": "Student Representative Council of the University of Ghana.",
        "email": "src@ug.edu.gh",
        "phone": "+233 30 250 0374",
        "website": "https://ug.edu.gh/src",
        "address": "University of Ghana, Legon, Accra",
        "is_verified": True,
        "kyc_front": "/uploads/kyc/seed_front_2.pdf",
        "kyc_back": "/uploads/kyc/seed_back_2.pdf",
        "members": [
            {"name": "Efua Asante", "email": "efua@ug.edu.gh", "role": "owner"},
            {"name": "Kojo Appiah", "email": "kojo@ug.edu.gh", "role": "admin"},
            {"name": "Abena Sarpong", "email": "abena@ug.edu.gh", "role": "editor"},
            {"name": "Kofi Adjei", "email": "kofi@ug.edu.gh", "role": "member"},
        ],
    },
    {
        "name": "Ghana Youth Parliament",
        "industry": "Civic & NGO",
        "description": "Youth-led advocacy platform running national policy debates and elections.",
        "email": "info@ghanayouthparliament.org",
        "phone": "+233 24 456 7890",
        "website": "https://ghanayouthparliament.org",
        "address": "Parliament House Annex, Accra",
        "is_verified": False,
        "kyc_front": "/uploads/kyc/seed_front_3.pdf",
        "kyc_back": None,
        "members": [
            {"name": "Akosua Frimpong", "email": "akosua@ghanayouthparl.org", "role": "owner"},
            {"name": "Yaa Boateng", "email": "yaa@ghanayouthparl.org", "role": "admin"},
        ],
    },
    {
        "name": "Tema Business Association",
        "industry": "Commerce",
        "description": "Trade association representing SMEs and manufacturers in the Tema industrial zone.",
        "email": "admin@temabusiness.gh",
        "phone": "+233 30 320 5566",
        "website": None,
        "address": "Community 1, Tema",
        "is_verified": True,
        "kyc_front": "/uploads/kyc/seed_front_4.pdf",
        "kyc_back": "/uploads/kyc/seed_back_4.pdf",
        "members": [
            {"name": "Kwabena Otoo", "email": "kwabena@temabusiness.gh", "role": "owner"},
        ],
    },
    {
        "name": "West Africa Entertainment Group",
        "industry": "Entertainment",
        "description": "Promoter of music concerts, award shows, and cultural events across West Africa.",
        "email": "events@waeg.co",
        "phone": "+233 55 789 0011",
        "website": "https://waeg.co",
        "address": "East Legon, Accra",
        "is_verified": False,
        "kyc_front": None,
        "kyc_back": None,
        "members": [
            {"name": "Adjoa Danso", "email": "adjoa@waeg.co", "role": "owner"},
            {"name": "Kwesi Amponsah", "email": "kwesi@waeg.co", "role": "admin"},
            {"name": "Afia Nyarko", "email": "afia@waeg.co", "role": "member"},
        ],
    },
    {
        "name": "Kumasi United FC",
        "industry": "Sports",
        "description": "Football club running player awards, supporter elections, and community leagues.",
        "email": "office@kumasiunitedfc.com",
        "phone": "+233 32 245 0099",
        "website": "https://kumasiunitedfc.com",
        "address": "Kumasi Sports Stadium, Kumasi",
        "is_verified": False,
        "kyc_front": "/uploads/kyc/seed_front_6.pdf",
        "kyc_back": "/uploads/kyc/seed_back_6.pdf",
        "members": [
            {"name": "Kwaku Gyasi", "email": "kwaku@kumasifc.com", "role": "owner"},
            {"name": "Esi Bonsu", "email": "esi@kumasifc.com", "role": "admin"},
        ],
    },
    {
        "name": "Global Health Ghana",
        "industry": "Healthcare",
        "description": "Non-profit driving community health campaigns and medical staff recognition awards.",
        "email": "programs@globalhealth.gh",
        "phone": "+233 24 900 0077",
        "website": "https://globalhealth.gh",
        "address": "Cantonments, Accra",
        "is_verified": True,
        "kyc_front": "/uploads/kyc/seed_front_7.pdf",
        "kyc_back": "/uploads/kyc/seed_back_7.pdf",
        "members": [
            {"name": "Fiifi Arthur", "email": "fiifi@globalhealth.gh", "role": "owner"},
            {"name": "Adwoa Serwaa", "email": "adwoa@globalhealth.gh", "role": "admin"},
        ],
    },
    {
        "name": "Digital Ghana Foundation",
        "industry": "Technology",
        "description": "Bridging the digital divide through coding bootcamps and ICT competitions.",
        "email": "hello@digitalghana.org",
        "phone": None,
        "website": "https://digitalghana.org",
        "address": None,
        "is_verified": False,
        "kyc_front": None,
        "kyc_back": None,
        "members": [
            {"name": "Kobina Tetteh", "email": "kobina@digitalghana.org", "role": "owner"},
        ],
    },
]


async def get_or_create_user(db, email: str, full_name: str) -> User:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user:
        return user
    user = User(
        email=email,
        password_hash=hash_password("SeedOrg@1234"),
        full_name=full_name,
        email_verified=True,
        account_status="active",
    )
    db.add(user)
    await db.flush()
    return user


async def seed():
    async with async_session_maker() as db:
        created = 0
        skipped = 0

        for seed in ORG_SEEDS:
            result = await db.execute(
                select(Organization).where(Organization.name == seed["name"])
            )
            if result.scalar_one_or_none():
                skipped += 1
                continue

            owner_seed = seed["members"][0]
            owner = await get_or_create_user(db, owner_seed["email"], owner_seed["name"])

            org = Organization(
                name=seed["name"],
                description=seed.get("description"),
                industry=seed.get("industry"),
                email=seed.get("email"),
                phone=seed.get("phone"),
                website=seed.get("website"),
                address=seed.get("address"),
                is_verified=seed["is_verified"],
                kyc_document_front=seed.get("kyc_front"),
                kyc_document_back=seed.get("kyc_back"),
                owner_id=owner.user_id,
            )
            db.add(org)
            await db.flush()

            for m in seed["members"]:
                user = await get_or_create_user(db, m["email"], m["name"])
                db.add(OrganizationMember(
                    org_id=org.org_id,
                    user_id=user.user_id,
                    role=m["role"],
                ))

            await db.flush()
            created += 1
            print(f"  + {seed['name']} ({'verified' if seed['is_verified'] else 'pending'})")

        await db.commit()
        print(f"\nDone — {created} created, {skipped} already existed.")


if __name__ == "__main__":
    asyncio.run(seed())
