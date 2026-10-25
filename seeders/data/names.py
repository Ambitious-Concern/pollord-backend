"""Ghanaian name banks — combinatorial (first x last) rather than a fixed
list, since the voter pool alone needs hundreds of unique names/emails."""
from seeders.config import rng

FIRST_NAMES = [
    "Kwame", "Kwabena", "Kwaku", "Yaw", "Kofi", "Kwesi", "Kwadwo", "Kwamena",
    "Ama", "Abena", "Akua", "Yaa", "Afua", "Efua", "Akosua", "Adjoa",
    "Kojo", "Kobina", "Fiifi", "Ekow", "Nana", "Kwabla", "Kwasi", "Yoofi",
    "Aba", "Araba", "Abla", "Esi", "Adjei", "Ato", "Panyin", "Kaakyire",
    "Sena", "Selorm", "Elikem", "Delali", "Mawuli", "Enam", "Edem", "Xorlali",
    "Abena", "Baaba", "Adwoa", "Akorfa", "Dzifa", "Ohemaa", "Serwaa", "Konadu",
]

LAST_NAMES = [
    "Mensah", "Boateng", "Owusu", "Asante", "Osei", "Appiah", "Sarpong", "Adjei",
    "Amponsah", "Frimpong", "Gyasi", "Bonsu", "Arthur", "Danso", "Otoo", "Tetteh",
    "Darko", "Antwi", "Yeboah", "Agyeman", "Nyarko", "Acheampong", "Opoku", "Kusi",
    "Ansah", "Baah", "Duah", "Ofori", "Sarfo", "Twum", "Amoah", "Kyei",
    "Addo", "Aidoo", "Quaye", "Lartey", "Nartey", "Odoi", "Tagoe", "Blankson",
    "Aryee", "Sowah", "Laryea", "Naa Adjeley", "Ashong", "Commey", "Bruce", "Cofie",
]

ORG_NAME_PARTS = [
    ("Golden", "Events"), ("Silver Fox", "Productions"), ("Pinnacle", "Awards Co."),
    ("Heritage", "Ceremonies"), ("Vibrant", "Vote Group"), ("Legacy", "Elections Ltd"),
    ("Crownpoint", "Media"), ("Zenith", "Organizers"), ("Beacon", "Events House"),
    ("Trueline", "Poll Services"), ("Summit", "Gatherings"), ("Coral Reef", "Events"),
]


def full_name(index: int) -> str:
    first = FIRST_NAMES[index % len(FIRST_NAMES)]
    last = LAST_NAMES[(index * 7 + 3) % len(LAST_NAMES)]
    return f"{first} {last}"


def unique_full_name(seen: set) -> str:
    """A random, not-yet-used name — voter pools need real uniqueness for
    the email built from them, unlike the deterministic index-based
    full_name() used for candidates (where repeats across categories are
    fine and even realistic)."""
    for _ in range(1000):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        name = f"{first} {last}"
        if name not in seen:
            seen.add(name)
            return name
    # Pool exhausted (shouldn't happen at our volumes) — fall back to a
    # numbered variant rather than looping forever.
    name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)} {len(seen)}"
    seen.add(name)
    return name


def org_name(index: int) -> str:
    a, b = ORG_NAME_PARTS[index % len(ORG_NAME_PARTS)]
    return f"{a} {b}"


def slugify(text: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in text).strip("-")
