"""A bounded, shared pool of nominee identities (name + photo) that
elections and events both draw candidates from — a real pageant/awards
contestant plausibly turns up in more than one show, so candidate rows
reuse identities from one fixed pool instead of every category slot
minting a brand-new, never-repeated name.

One NomineePicker instance is built once per seeding run and threaded
through both seed_elections and seed_events, so the wraparound reuse
genuinely spans elections *and* events, not just within one domain."""
from seeders.data import images, names

N_IDENTITIES = 150


class NomineeIdentity:
    __slots__ = ("name", "image_url")

    def __init__(self, name: str, image_url: str):
        self.name = name
        self.image_url = image_url


def build_identity_pool() -> list[NomineeIdentity]:
    seen: set = set()
    return [
        NomineeIdentity(names.unique_full_name(seen), images.portrait(i))
        for i in range(N_IDENTITIES)
    ]


class NomineePicker:
    """Hands out identities in sequence, wrapping around the pool — the
    wraparound is what produces realistic cross-election/event reuse."""

    def __init__(self, pool: list[NomineeIdentity]):
        self._pool = pool
        self._i = 0

    def next(self) -> NomineeIdentity:
        identity = self._pool[self._i % len(self._pool)]
        self._i += 1
        return identity
