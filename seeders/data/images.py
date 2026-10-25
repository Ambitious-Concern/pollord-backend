"""Real Unsplash photos for banners/portraits — every ID below was verified
(HTTP 200) against images.unsplash.com before being added here. "Optimizing"
happens via Unsplash's own imgix query params (width/quality/format/crop)
rather than downloading and re-processing anything ourselves — the CDN
already serves a resized, compressed, modern-format image on request."""

EVENT_BANNER_IDS = [
    "1519671482749-fd09be7ccebf", "1540575467063-178a50c2df87", "1511578314322-379afb476865",
    "1492684223066-81342ee5ff30", "1505373877841-8d25f7d46678", "1531058020387-3be344556be6",
    "1523580494863-6f3031224c94", "1517457373958-b7bdd4587205", "1556761175-5973dc0f32e7",
    "1531482615713-2afd69097998", "1587825140708-dfaf72ae4b04", "1487956382158-bb926046304a",
    "1521737604893-d14cc237f11d", "1524638431109-93d95c968f03", "1470229722913-7c0e2dbbafd3",
    "1543269865-cbf427effbad", "1567443024551-f3e3cc2be870", "1516450360452-9312f5e86fc7",
    "1591115765373-5207764f72e7", "1511795409834-ef04bbd61622", "1475721027785-f74eccf877e2",
    "1522202176988-66273c2fd55f", "1429962714451-bb934ecdc4ec", "1533174072545-7a4b6ad7a6c3",
    "1508997449629-303059a039c0", "1505236858219-8359eb29e329", "1546519638-68e109498ffc",
    "1478147427282-58a87a120781", "1533488765986-dfa2a9939acd", "1500648767791-00dcc994a43e",
    "1517841905240-472988babdf9", "1571019613454-1cb2f99b2d8b", "1524178232363-1fb2b075b655",
    "1560250097-0b93528c311a", "1541534741688-6078c6bfb5c5", "1494790108377-be9c29b29330",
    "1500375592092-40eb2168fd21", "1573497019940-1c28c88b4f3e", "1519241047957-be31d7379a5d",
    "1508214751196-bcfd4ca60f91", "1607746882042-944635dfe10e", "1580489944761-15a19d654956",
    "1583939003579-730e3918a45a", "1552374196-c4e7ffc6e126", "1517245386807-bb43f82c33c4",
    "1544005313-94ddf0286df2",
]

PORTRAIT_IDS = [
    "1633332755192-727a05c4013d", "1552058544-f2b08422138a", "1573496359142-b8d87734a5a2",
    "1531123897727-8f129e1688ce", "1584999734482-0361aecad844", "1595152772835-219674b2a8a6",
    "1519085360753-af0119f7cbe7", "1568602471122-7832951cc4c5", "1607990281513-2c110a25bd8c",
    "1522075469751-3a6694fb2f61", "1502685104226-ee32379fefbe", "1544717305-2782549b5136",
    "1506794778202-cad84cf45f1d",
]


def _optimized(photo_id: str, *, w: int, q: int = 75) -> str:
    return f"https://images.unsplash.com/photo-{photo_id}?w={w}&q={q}&auto=format&fit=crop"


def event_banner(index: int) -> str:
    """1200px wide — banner/hero-sized, matches BannerCropModal's target use."""
    return _optimized(EVENT_BANNER_IDS[index % len(EVENT_BANNER_IDS)], w=1200)


def org_logo(index: int) -> str:
    """Square-cropped, small — logos don't need banner resolution."""
    return _optimized(EVENT_BANNER_IDS[(index * 5 + 2) % len(EVENT_BANNER_IDS)], w=200, q=70)


def portrait(index: int) -> str:
    """Small square — candidate photo / user avatar sized."""
    return _optimized(PORTRAIT_IDS[index % len(PORTRAIT_IDS)], w=300, q=70)
