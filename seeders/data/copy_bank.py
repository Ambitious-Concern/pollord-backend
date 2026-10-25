"""Title/category/venue pools for elections and events — genres combine with
places/schools/orgs to produce enough distinct, plausible titles without
hand-writing every single one (see seeders/config.py for the target counts)."""
from seeders.config import rng

PLACES = [
    "Accra", "Kumasi", "Tema", "Takoradi", "Cape Coast", "Ho", "Sunyani",
    "Koforidua", "Tamale", "Bolgatanga", "Techiman", "Obuasi", "Winneba",
    "Nkawkaw", "Wa", "Ashaiman", "Madina", "Kasoa",
]

SCHOOLS = [
    "Achimota School", "Legon", "KNUST", "Wesley Girls'", "Prempeh College",
    "Mfantsipim", "Aburi Girls'", "Adisadel College", "St. Louis SHS",
    "Holy Child School", "GIJ", "UPSA", "Ashesi University", "UCC",
    "Accra Academy", "Presec Legon",
]

ORGS = [
    "Ghana Traders Union", "National Nurses Association", "Bar Association",
    "Teachers' Guild", "Chamber of Commerce", "Youth Coalition",
    "Photographers Guild", "Musicians Union", "Real Estate Association",
    "Fashion Designers Guild", "Tech Founders Network", "Farmers Cooperative",
]

VENUES = [
    "Accra International Conference Centre", "Grand Arena Accra",
    "Kumasi Trade Fair Centre", "La Palm Royal Beach Hotel",
    "Alisa Hotel", "Movenpick Ambassador Hotel", "Kempinski Hotel Gold Coast City",
    "CCB Auditorium", "National Theatre of Ghana", "Perez Dome",
    "Fiesta Royale Hotel", "Best Western Plus Atlantic Hotel",
]

ELECTION_GENRES = [
    {
        "genre": "student_council",
        "titles": ["{school} Student Council Election {year}", "{school} SRC Elections {year}"],
        "categories": [
            "President", "Vice President", "General Secretary", "Financial Secretary",
            "Organizing Secretary", "Women's Commissioner", "Public Relations Officer", "Treasurer",
        ],
        "tag": "Student Election",
    },
    {
        "genre": "pageant",
        "titles": ["Miss {place} {year}", "{place} Beauty Pageant {year}", "Mr & Miss {school} {year}"],
        "categories": [
            "Overall Queen", "Best in Talent", "Best National Costume", "Miss Congeniality",
            "People's Choice", "Best Catwalk", "Miss Photogenic",
        ],
        "tag": "Pageant",
    },
    {
        "genre": "awards",
        "titles": ["{org} Excellence Awards {year}", "{place} Achievers Awards {year}"],
        "categories": [
            "Best Performer", "Rising Star", "Outstanding Leadership", "Community Impact Award",
            "Innovator of the Year", "Lifetime Achievement", "People's Choice Award",
        ],
        "tag": "Awards",
    },
    {
        "genre": "chieftaincy",
        "titles": ["{place} Traditional Council Awards {year}", "{place} Chieftaincy Honours {year}"],
        "categories": [
            "Best Chief of the Year", "Outstanding Queen Mother", "Development Chief Award",
            "Heritage Ambassador", "Peace and Unity Award",
        ],
        "tag": "Ceremony",
    },
    {
        "genre": "alumni",
        "titles": ["{school} Alumni Homecoming {year}", "{school} Old Students Association Election {year}"],
        "categories": [
            "Homecoming King", "Homecoming Queen", "Best Dressed", "Alumni Choice Award",
            "Class Spirit Award", "Golden Alumni Award",
        ],
        "tag": "Alumni",
    },
    {
        "genre": "association",
        "titles": ["{org} Executive Elections {year}", "{org} National Council Election {year}"],
        "categories": [
            "President", "Vice President", "General Secretary", "Treasurer",
            "Public Relations Officer", "Legal Adviser",
        ],
        "tag": "Association",
    },
]

EVENT_GENRES = [
    {
        "genre": "tech",
        "titles": ["{place} Tech Fair {year}", "{org} Innovation Summit {year}"],
        "tag": "Exhibition",
        "voting_categories": ["Best Startup Pitch", "Best AI Application", "People's Choice Award"],
    },
    {
        "genre": "gala",
        "titles": ["{org} Annual Gala {year}", "{place} Charity Gala Night {year}"],
        "tag": "Gala",
        "voting_categories": ["Best Dressed", "Philanthropist of the Year"],
    },
    {
        "genre": "concert",
        "titles": ["{place} Music Festival {year}", "{org} Live in Concert {year}"],
        "tag": "Concert",
        "voting_categories": [],
    },
    {
        "genre": "conference",
        "titles": ["{org} Leadership Conference {year}", "{place} Business Summit {year}"],
        "tag": "Conference",
        "voting_categories": [],
    },
    {
        "genre": "cultural",
        "titles": ["{place} Cultural Festival {year}", "{org} Heritage Day {year}"],
        "tag": "Festival",
        "voting_categories": ["Best Cultural Display", "Best Traditional Attire"],
    },
    {
        "genre": "sports",
        "titles": ["{place} Sports Gala {year}", "{org} Inter-Company Games {year}"],
        "tag": "Sports",
        "voting_categories": [],
    },
]

TICKET_TIERS = [
    ("Regular", "General admission with access to all main sessions.", 30, 40),
    ("VIP", "Priority seating, a welcome pack, and a reserved table.", 80, 150),
    ("VVIP", "Front-row seating, backstage access, and a meet-and-greet.", 200, 350),
    ("Table of 10", "Reserved table for 10 guests.", 1500, 3000),
]


def _fill(template: str, year: int) -> str:
    return (
        template
        .replace("{place}", rng.choice(PLACES))
        .replace("{school}", rng.choice(SCHOOLS))
        .replace("{org}", rng.choice(ORGS))
        .replace("{year}", str(year))
    )


def unique_titles(genres: list[dict], count: int, year: int, seen: set) -> list[tuple[str, dict]]:
    """count (title, genre) pairs, no duplicate titles within this run."""
    out = []
    attempts = 0
    while len(out) < count and attempts < count * 50:
        attempts += 1
        genre = rng.choice(genres)
        title = _fill(rng.choice(genre["titles"]), year)
        if title in seen:
            continue
        seen.add(title)
        out.append((title, genre))
    return out
