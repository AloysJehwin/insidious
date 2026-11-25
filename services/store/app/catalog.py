"""Jeffy catalogue: 12 handcrafted Jaipur juttis, generated SVG artwork, CPU-heavy feature implementations."""

from __future__ import annotations

import hashlib
import math
import os
import random
import time
from dataclasses import dataclass, field
from functools import lru_cache

# Feature axes for the similarity model: bridal, festive, everyday, groom, embroidery, mirror, warm, cool, premium
AXES = 9


@dataclass(frozen=True)
class Product:
    id: int
    name: str
    tagline: str
    price: int
    mrp: int
    category: str
    primary: str
    secondary: str
    accent: str
    bg: str
    motif: str  # dots | mirror | floral | zari | block | thread
    vec: tuple[float, ...]
    rating: float
    sales: int
    badge: str = ""
    description: str = ""
    sizes: tuple[int, ...] = field(default=(36, 37, 38, 39, 40, 41))

    @property
    def discount(self) -> int:
        return round(100 * (self.mrp - self.price) / self.mrp) if self.mrp > self.price else 0

    def to_api(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "tagline": self.tagline,
            "price": self.price,
            "mrp": self.mrp,
            "currency": "INR",
            "category": self.category,
            "rating": self.rating,
            "badge": self.badge,
            "image": f"/img/{self.id}.svg",
        }


PRODUCTS: list[Product] = [
    Product(1, "Rani Haar Zardozi Jutti", "Hand-stitched zardozi on velvet", 2499, 3999, "Bridal",
            "#7a1035", "#d4a537", "#f6d77a", "#fbe9ec", "zari", (1, .8, 0, 0, 1, 0, 1, 0, .8), 4.8, 940, "Bestseller",
            "Deep maroon velvet upper with real zardozi (gold thread) work, cushioned leather sole. Made by the "
            "Sharma family of karigars in Johari Bazaar."),
    Product(2, "Marigold Mojari", "Sunny genda-phool yellow", 1299, 1799, "Festive",
            "#f2a007", "#b5420f", "#ffe08a", "#fff4d6", "floral", (0, 1, .4, 0, .6, 0, 1, 0, .3), 4.6, 1210, "",
            "Bright as Diwali morning marigolds. Soft camel-leather lining and a padded heel for long pujas."),
    Product(3, "Sheesh Mahal Mirror Jutti", "Tiny mirrors that catch every diya", 1899, 2599, "Festive",
            "#0f6b6b", "#e7eef0", "#9fe3e0", "#e3f4f2", "mirror", (.4, 1, 0, 0, .7, 1, 0, 1, .5), 4.7, 860, "New",
            "Inspired by Amer Fort's Sheesh Mahal: hand-set mirror work on teal silk with silver dori outlines."),
    Product(4, "Gulabi Nagri Pink Jutti", "A love letter to the Pink City", 999, 1499, "Everyday",
            "#e0567f", "#ffffff", "#ffc2d4", "#ffeef3", "dots", (0, .5, 1, 0, .3, 0, 1, 0, .1), 4.5, 1580, "",
            "Jaipur pink with white polka embroidery. Light enough for everyday, pretty enough for sangeet."),
    Product(5, "Dulha Royal Nagra", "For the groom who walks in like a Maharaja", 3499, 4999, "Groom",
            "#efe2c4", "#b8862b", "#e9c46a", "#f7f1e3", "zari", (.6, .8, 0, 1, 1, 0, .5, 0, 1), 4.9, 520, "Wedding",
            "Ivory raw-silk nagra with antique gold zari and a curled toe. Pairs with sherwanis and bandhgalas."),
    Product(6, "Emerald Bagh Jutti", "Mughal garden in emerald silk", 1599, 2199, "Festive",
            "#0f5c4d", "#e5b84b", "#7cc4a8", "#e6f3ee", "floral", (.2, 1, .2, 0, .8, 0, 0, 1, .5), 4.6, 770, "",
            "Emerald silk with gold-thread bagh (garden) motifs and a cushioned insole for all-night baraats."),
    Product(7, "Kundan Bridal Jutti", "Kundan stones for the big day", 4299, 5999, "Bridal",
            "#b3122e", "#f1c75b", "#ffe9a8", "#fde8e6", "mirror", (1, .7, 0, 0, 1, .8, 1, 0, 1), 4.9, 410, "Bridal",
            "Bridal red with kundan stones and gota patti. Made to match lehengas, built to survive the pheras."),
    Product(8, "Neel Kamal Indigo Jutti", "Sanganeri block print in indigo", 1199, 1599, "Everyday",
            "#26407a", "#e8edf7", "#8fa7dc", "#e9eef8", "block", (0, .3, 1, 0, .2, 0, 0, 1, .2), 4.4, 1320, "",
            "Hand block-printed in Sanganer with natural indigo. Breathable cotton lining, everyday comfort."),
    Product(9, "Phulkari Threadwork Jutti", "Every colour of a Punjabi wedding", 1449, 1999, "Festive",
            "#c2185b", "#ffb300", "#4dd0e1", "#fdeef4", "thread", (.3, 1, .3, 0, 1, 0, 1, .4, .4), 4.7, 990, "",
            "Phulkari-style thread embroidery in five colours. Our most-photographed pair at mehendi functions."),
    Product(10, "Sunehri Kolhapuri", "Sun-gold leather, open toe", 899, 1299, "Everyday",
             "#a8692b", "#f3c26b", "#ffd99a", "#fbf0e1", "dots", (0, .2, 1, .3, .1, 0, 1, 0, .1), 4.3, 1720, "Value",
             "Vegetable-tanned leather with sunehri (golden) piping. Ages beautifully, festival after festival."),
    Product(11, "Diwali Diya Glow Jutti", "Saffron and gold like a row of diyas", 2199, 2999, "Festive",
            "#e8590c", "#ffd43b", "#ffe8a3", "#fff0e0", "zari", (.5, 1, 0, 0, .9, .3, 1, 0, .7), 4.8, 1105,
            "Diwali Edit",
            "Our Diwali edit: saffron velvet, diya-shaped zari motifs and a glow-gold heel tab."),
    Product(12, "Hawa Mahal Pastel Jutti", "Lavender lattice, breezy as Hawa Mahal", 1349, 1899, "Everyday",
            "#8e7cc3", "#ffffff", "#d6ccf2", "#f2eefb", "block", (.2, .6, .8, 0, .4, 0, 0, 1, .3), 4.5, 880, "",
            "Pastel lavender with a jharokha lattice print. Day-wedding favourite."),
]
BY_ID = {p.id: p for p in PRODUCTS}
TOP_SELLERS = sorted(PRODUCTS, key=lambda p: -p.sales)


def get(pid: int) -> Product | None:
    return BY_ID.get(pid)


# ------------------------------------------------------------------------------------------- CPU knobs
def heavy_ms() -> float:
    try:
        return float(os.environ.get("HEAVY_WORK_MS", "40"))
    except ValueError:
        return 40.0


def _knob(name: str, ratio: float) -> float:
    v = os.environ.get(name)
    if v:
        try:
            return float(v)
        except ValueError:
            pass
    return heavy_ms() * ratio


def hd_ms() -> float:
    return _knob("HD_WORK_MS", 0.5)


def live_reviews_ms() -> float:
    return _knob("LIVE_REVIEWS_WORK_MS", 0.5)


def burn_cpu(ms: float, seed: int = 0) -> float:
    """Do real CPU work (float math over the catalogue vectors) for ``ms`` milliseconds of *thread CPU time*.

    Thread CPU time (not wall time) means a request really needs ``ms`` of CPU, so a 1-worker pod saturates and
    queues requests realistically once offered load exceeds 1000/ms requests per second.
    """
    if ms <= 0:
        return 0.0
    end = time.thread_time() + ms / 1000.0
    acc = float(seed)
    vecs = [p.vec for p in PRODUCTS]
    while time.thread_time() < end:
        for a in vecs:
            for b in vecs:
                dot = 0.0
                na = 0.0
                nb = 0.0
                for i in range(AXES):
                    dot += a[i] * b[i]
                    na += a[i] * a[i]
                    nb += b[i] * b[i]
                acc += dot / (math.sqrt(na * nb) + 1e-9)
    return acc


def _cos(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb + 1e-9)


def compute_recommendations(seed_ids: list[int], identity: str | None, k: int = 4,
                            candidates: list[Product] | None = None) -> list[Product]:
    """Personalised recommendations: similarity over the catalogue around the seed products (CPU-heavy).

    ``candidates`` is the catalogue as fetched from the database (defaults to the in-memory seed catalogue).
    """
    burn_cpu(heavy_ms(), seed=len(seed_ids))
    pool = candidates if candidates else PRODUCTS
    by_id = {p.id: p for p in pool}
    seeds = [by_id.get(i) or BY_ID[i] for i in seed_ids if i in by_id or i in BY_ID]
    if seeds:
        profile = tuple(sum(p.vec[i] for p in seeds) / len(seeds) for i in range(AXES))
    else:  # no context: use a stable per-session taste profile
        rnd = random.Random(int(hashlib.md5((identity or "anon").encode()).hexdigest()[:8], 16))
        profile = tuple(rnd.random() for _ in range(AXES))
    exclude = {p.id for p in seeds}
    scored = sorted(
        (p for p in pool if p.id not in exclude), key=lambda p: (-_cos(profile, p.vec) - p.rating / 50, p.id)
    )
    return scored[:k]


def cached_top_sellers(seed_ids: list[int] | None = None, identity: str | None = None, k: int = 4) -> list[Product]:
    exclude = set(seed_ids or [])
    return [p for p in TOP_SELLERS if p.id not in exclude][:k]


# ------------------------------------------------------------------------------------------- reviews
_NAMES = ["Priya, Jaipur", "Ananya, Pune", "Rohit, Delhi", "Meera, Udaipur", "Kavya, Bengaluru", "Arjun, Indore",
          "Sneha, Lucknow", "Farah, Hyderabad", "Ishaan, Jodhpur", "Divya, Chennai", "Neha, Ahmedabad",
          "Vikram, Kota"]
_TEXTS = [
    "Wore these for my sister's sangeet and danced all night. Zero shoe bite!",
    "The embroidery is even prettier in person. Packaging felt like a Diwali gift.",
    "Size chart was spot on. Delivered before Dhanteras as promised.",
    "Soft padding, beautiful colour. My mother ordered a second pair.",
    "Got so many compliments at the wedding. Worth every rupee.",
    "Runs slightly snug for the first day, then perfect.",
    "Gold work is neat, no loose threads. Feels handmade in the best way.",
    "Matched my lehenga perfectly. Ordered the bridal one for my cousin too.",
]


@lru_cache(maxsize=64)
def cached_reviews(pid: int) -> dict:
    rnd = random.Random(pid * 7919)
    p = BY_ID.get(pid)
    items = [
        {"author": rnd.choice(_NAMES), "stars": rnd.choice([5, 5, 5, 4, 4]), "text": rnd.choice(_TEXTS)}
        for _ in range(3)
    ]
    return {"source": "cached", "count": 120 + pid * 37, "avg": p.rating if p else 4.5, "items": items,
            "freshness": "Cached 10 min ago"}


def live_reviews(pid: int) -> dict:
    """Aggregate a large synthetic review corpus on every request (CPU-heavy on purpose)."""
    burn_cpu(live_reviews_ms(), seed=pid)
    base = cached_reviews(pid)
    tick = int(time.time() // 30)
    rnd = random.Random(pid * 104729 + tick)
    count = base["count"] + tick % 50
    items = [
        {"author": rnd.choice(_NAMES), "stars": rnd.choice([5, 5, 5, 4]), "text": rnd.choice(_TEXTS)} for _ in range(4)
    ]
    return {"source": "live", "count": count, "avg": base["avg"], "items": items, "freshness": "Live · updated now"}


# ------------------------------------------------------------------------------------------- artwork
def _shade(hex_color: str, factor: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    if factor < 1:
        r, g, b = (int(c * factor) for c in (r, g, b))
    else:
        r, g, b = (int(c + (255 - c) * (factor - 1)) for c in (r, g, b))
    return f"#{max(0, min(255, r)):02x}{max(0, min(255, g)):02x}{max(0, min(255, b)):02x}"


_UPPER = ("M64 214 C62 176 88 146 136 138 C176 131 214 134 252 150 C284 163 312 176 336 170 "
          "C350 166 360 156 368 150 C372 160 366 176 354 188 C344 198 336 206 326 210 C250 228 140 228 64 214 Z")
_SOLE = "M56 214 C140 230 250 230 330 210 L340 214 C336 226 250 244 150 242 C102 241 66 234 54 226 Z"
_OPENING = "M92 176 C104 158 140 150 176 152 C190 153 196 160 188 168 C168 182 120 186 92 176 Z"
_VAMP = "M196 150 C232 152 270 166 300 182 C310 188 316 196 306 202 C270 210 226 206 200 196 C186 182 186 162 196 150 Z"


def _motif(p: Product, hd: bool) -> str:
    rnd = random.Random(p.id)
    out = []
    n = 26 if hd else 9
    if p.motif in ("dots", "zari", "thread"):
        for i in range(n):
            t = i / max(1, n - 1)
            x = 204 + t * 100 + rnd.uniform(-4, 4)
            y = 158 + t * 30 + rnd.uniform(4, 30) * (1 - t * .6)
            col = p.secondary if p.motif != "thread" else rnd.choice([p.secondary, p.accent, "#ffffff", "#7bd389"])
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{(2.6 if hd else 3.4):.1f}" fill="{col}"/>')
    if p.motif in ("floral", "zari", "thread"):
        petals = 6 if hd else 4
        for cx, cy, r in ((248, 178, 13), (284, 190, 9)) if hd else ((252, 180, 12),):
            for k in range(petals):
                a = 2 * math.pi * k / petals
                out.append(f'<ellipse cx="{cx + math.cos(a) * r * .7:.1f}" cy="{cy + math.sin(a) * r * .7:.1f}" '
                           f'rx="{r * .45:.1f}" ry="{r * .28:.1f}" transform="rotate({math.degrees(a):.0f} '
                           f'{cx + math.cos(a) * r * .7:.1f} {cy + math.sin(a) * r * .7:.1f})" fill="{p.accent}"/>')
            out.append(f'<circle cx="{cx}" cy="{cy}" r="{r * .3:.1f}" fill="{p.secondary}"/>')
    if p.motif == "mirror":
        for i in range(12 if hd else 5):
            x = 210 + (i % 6) * 16 + (8 if (i // 6) % 2 else 0)
            y = 168 + (i // 6) * 16 + (i % 6) * 3
            out.append(f'<circle cx="{x}" cy="{y}" r="5.5" fill="{p.secondary}" stroke="{p.accent}" stroke-width="1.6"/>')
            if hd:
                out.append(f'<circle cx="{x - 1.6}" cy="{y - 1.6}" r="1.6" fill="#ffffff" opacity=".9"/>')
    if p.motif == "block":
        for i in range(10 if hd else 5):
            x = 206 + i * 10
            y = 162 + i * 3.4
            out.append(f'<path d="M{x} {y + 8} l6 -8 l6 8 l-6 8 z" fill="{p.secondary}" opacity=".85"/>')
    # heel band embroidery line
    out.append(f'<path d="M70 206 C150 220 250 220 326 204" fill="none" stroke="{p.secondary}" '
               f'stroke-width="{2.4 if hd else 3}" stroke-dasharray="{"4 4" if hd else "8 6"}"/>')
    return "".join(out)


@lru_cache(maxsize=32)
def _svg_cached(pid: int, hd: bool) -> str:
    p = BY_ID[pid]
    uid = f"p{pid}{'h' if hd else 'l'}"
    dark = _shade(p.primary, 0.62)
    light = _shade(p.primary, 1.25)
    defs = (f'<defs><radialGradient id="bg{uid}" cx="50%" cy="38%" r="75%"><stop offset="0" stop-color="#ffffff"/>'
            f'<stop offset="1" stop-color="{p.bg}"/></radialGradient>'
            f'<linearGradient id="up{uid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{light}"/>'
            f'<stop offset=".55" stop-color="{p.primary}"/><stop offset="1" stop-color="{dark}"/></linearGradient>'
            '</defs>')
    rangoli = []
    for k in range(16 if hd else 8):
        a = 2 * math.pi * k / (16 if hd else 8)
        rangoli.append(f'<circle cx="{200 + math.cos(a) * 118:.1f}" cy="{150 + math.sin(a) * 118:.1f}" r="{5 if hd else 7}" '
                       f'fill="none" stroke="{p.primary}" stroke-opacity=".14" stroke-width="2"/>')
    upper_fill = f"url(#up{uid})" if hd else p.primary
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300" role="img" aria-label="{p.name}">',
        defs,
        f'<rect width="400" height="300" fill="{"url(#bg" + uid + ")" if hd else p.bg}"/>',
        f'<circle cx="200" cy="150" r="118" fill="none" stroke="{p.primary}" stroke-opacity=".10" stroke-width="18"/>',
        "".join(rangoli),
        '<ellipse cx="200" cy="248" rx="150" ry="10" fill="#000" opacity=".08"/>',
        f'<path d="{_SOLE}" fill="{_shade(p.secondary, 0.55) if p.motif != "mirror" else "#6b4a2b"}"/>',
        f'<path d="{_UPPER}" fill="{upper_fill}" stroke="{dark}" stroke-width="2"/>',
        f'<path d="{_VAMP}" fill="{_shade(p.primary, 0.85)}" opacity=".55"/>',
        f'<path d="{_OPENING}" fill="{_shade(p.primary, 0.35)}"/>',
        f'<path d="M92 176 C120 188 168 184 190 168" fill="none" stroke="{p.secondary}" stroke-width="3"/>',
        _motif(p, hd),
        f'<path d="M354 188 C362 180 372 166 368 150" fill="none" stroke="{p.secondary}" stroke-width="2.4"/>',
    ]
    if hd:
        parts.append('<path d="M120 150 C160 140 200 140 236 150" fill="none" stroke="#ffffff" stroke-opacity=".35" '
                     'stroke-width="5" stroke-linecap="round"/>')
        for x, y in ((316, 96), (88, 92), (300, 236)):
            parts.append(f'<path d="M{x} {y - 8} L{x + 2} {y - 2} L{x + 8} {y} L{x + 2} {y + 2} L{x} {y + 8} '
                         f'L{x - 2} {y + 2} L{x - 8} {y} L{x - 2} {y - 2} Z" fill="{p.secondary}" opacity=".8"/>')
    parts.append("</svg>")
    return "".join(parts)


def product_svg(pid: int, hd: bool) -> str:
    """Product artwork. With hd_images on, simulate on-the-fly HD resizing with real CPU work per render."""
    if hd:
        burn_cpu(hd_ms(), seed=pid)
    return _svg_cached(pid, hd)


def grid_svgs(ids: list[int], hd: bool) -> dict[int, str]:
    """Artwork for a product grid. HD grids cost one hd_ms() in total (thumbnails are cheaper than a hero)."""
    if hd:
        burn_cpu(hd_ms(), seed=len(ids))
    return {i: _svg_cached(i, hd) for i in ids}
