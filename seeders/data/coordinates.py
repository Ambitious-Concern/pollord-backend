"""Approximate real-world coordinates for the Ghanaian places copy_bank's
VENUES/PLACES draw from, so seeded elections/events land on plausible map
pins instead of null geo fields. Jittered per row so multiple rows at the
same place don't stack on one exact pixel."""
from seeders.config import rng

PLACE_COORDS = {
    "Accra": (5.6037, -0.1870),
    "Kumasi": (6.6885, -1.6244),
    "Tema": (5.6698, -0.0166),
    "Takoradi": (4.8845, -1.7554),
    "Cape Coast": (5.1053, -1.2466),
    "Ho": (6.6115, 0.4708),
    "Sunyani": (7.3399, -2.3268),
    "Koforidua": (6.0940, -0.2591),
    "Tamale": (9.4034, -0.8424),
    "Bolgatanga": (10.7856, -0.8514),
    "Techiman": (7.5911, -1.9400),
    "Obuasi": (6.2025, -1.6704),
    "Winneba": (5.3511, -0.6231),
    "Nkawkaw": (6.5500, -0.7667),
    "Wa": (10.0601, -2.5099),
    "Ashaiman": (5.6947, -0.0322),
    "Madina": (5.6833, -0.1667),
    "Kasoa": (5.5333, -0.4167),
}

# Every VENUES entry in copy_bank.py is a real Accra/Kumasi venue — mapped
# by hand since venue names don't share a substring with PLACE_COORDS keys.
VENUE_PLACE = {
    "Accra International Conference Centre": "Accra",
    "Grand Arena Accra": "Accra",
    "Kumasi Trade Fair Centre": "Kumasi",
    "La Palm Royal Beach Hotel": "Accra",
    "Alisa Hotel": "Accra",
    "Movenpick Ambassador Hotel": "Accra",
    "Kempinski Hotel Gold Coast City": "Accra",
    "CCB Auditorium": "Accra",
    "National Theatre of Ghana": "Accra",
    "Perez Dome": "Accra",
    "Fiesta Royale Hotel": "Accra",
    "Best Western Plus Atlantic Hotel": "Accra",
}


def coords_for_place(place: str) -> tuple[float, float]:
    lat, lng = PLACE_COORDS.get(place, PLACE_COORDS["Accra"])
    return (
        round(lat + rng.uniform(-0.01, 0.01), 6),
        round(lng + rng.uniform(-0.01, 0.01), 6),
    )


def coords_for_venue(venue: str) -> tuple[float, float]:
    return coords_for_place(VENUE_PLACE.get(venue, "Accra"))
