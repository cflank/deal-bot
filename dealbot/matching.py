"""Product matching, storage/condition detection and value scoring."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CONDITION_LABELS = {"new": "全新", "openbox": "开箱", "refurb": "翻新", "used": "二手"}

_CONDITION_PATTERNS = [
    ("used", re.compile(r"\b(used|pre-?owned|second[- ]hand|usag[ée]|d'occasion)\b", re.I)),
    ("refurb", re.compile(r"\b(refurb\w*|renewed|reconditionn\w*|remis [àa] neuf)\b", re.I)),
    ("openbox", re.compile(r"\bopen[- ]?box\b", re.I)),
]

CARRIERS = [
    "rogers", "bell", "telus", "freedom", "fido", "virgin", "koodo", "chatr",
    "public mobile", "lucky mobile", "videotron", "vidéotron", "sasktel", "eastlink",
]

_STORAGE_RE = re.compile(r"(\d+)\s*(gb|tb|go|to)\b", re.I)


@dataclass
class Offer:
    source: str
    title: str
    price: float
    url: str
    condition: str = "new"
    regular_price: float | None = None
    in_stock: bool | None = None
    product: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.product}|{self.source}|{self.url or self.title}"


def _compact(text: str) -> str:
    return re.sub(r"[\s\-_/]+", "", text.lower())


def matches_product(title: str, include: list[str], exclude: list[str]) -> bool:
    compact = _compact(title)
    if not any(_compact(k) in compact for k in include if k.strip()):
        return False
    low = " ".join(title.lower().split())
    return not any(k.lower().strip() in low for k in exclude if k.strip())


def mentions(text: str, words: list[str]) -> bool:
    """Whole-word match for free text such as forum post titles: "gap" must not match "Mega Pack"."""
    def norm(s: str) -> str:
        return " ".join(re.split(r"[\s\-_/]+", s.lower().strip()))
    hay = norm(text)
    return any(re.search(rf"(?<![a-z0-9]){re.escape(norm(w))}(?![a-z0-9])", hay) for w in words if w.strip())


def detect_condition(title: str) -> str | None:
    for cond, pattern in _CONDITION_PATTERNS:
        if pattern.search(title):
            return cond
    return None


def storage_sizes(title: str) -> list[int]:
    """Every storage size mentioned in a title, in GB."""
    return [int(num) * (1024 if unit.lower() in ("tb", "to") else 1) for num, unit in _STORAGE_RE.findall(title)]


def detect_storage(title: str, tiers) -> int | None:
    return next((gb for gb in storage_sizes(title) if gb in tiers), None)


def detect_carrier(title: str) -> str | None:
    low = title.lower()
    for carrier in CARRIERS:
        if re.search(rf"\b{re.escape(carrier)}\b", low):
            return carrier.title()
    return None


def tier_label(tier: int | None) -> str:
    if tier is None:
        return ""
    return f"{tier // 1024}TB" if tier >= 1024 and tier % 1024 == 0 else f"{tier}GB"


@dataclass
class Assessment:
    tier: int | None
    storage_guessed: bool
    msrp: float | None
    pct_of_msrp: float | None
    carrier: str | None
    qualifies: bool
    target: float | None = None

    @property
    def discount_pct(self) -> float | None:
        return None if self.pct_of_msrp is None else 100.0 - self.pct_of_msrp

    @property
    def label(self) -> str:
        return tier_label(self.tier)

    def sort_key(self, price: float):
        return (0, self.pct_of_msrp, price) if self.pct_of_msrp is not None else (1, 0, price)


def _num(v) -> float | None:
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def assess(offer: Offer, product: dict) -> Assessment:
    """Score an offer against a product's reference price and alert rules.

    product["msrp"] is either {"256": 1449, "512": 1749, ...} (priced by
    storage), {"default": 329} (single price) or {} (no reference price).
    """
    msrp_cfg = product.get("msrp") or {}
    tiered = {int(k): _num(v) for k, v in msrp_cfg.items() if str(k).isdigit() and _num(v)}
    tier = None
    guessed = False
    if tiered:
        sizes = storage_sizes(offer.title)
        storage = next((gb for gb in sizes if gb in tiered), None)
        if storage is None and sizes:
            msrp = None   # the title names a size we have no price for: no comparison beats a wrong one
        else:
            # Unknown storage: compare against the cheapest tier so we never overstate a deal.
            tier = storage if storage is not None else min(tiered)
            guessed = storage is None
            msrp = tiered[tier]
    else:
        msrp = _num(msrp_cfg.get("default"))
    # A shop's own list price for exactly this item (set by dealweb for shops it trusts) beats a product-wide one.
    msrp = _num(offer.extra.get("list_price")) or msrp
    pct = offer.price / msrp * 100.0 if msrp else None

    max_pct = _num((product.get("max_pct") or {}).get(offer.condition))
    targets = product.get("target_price") or {}
    target = _num(targets.get(str(tier))) if tier is not None else None
    if target is None:
        target = _num(targets.get("default"))
    # Absolute target prices are for new units; second-hand uses the percentage rule only.
    hit_target = offer.condition == "new" and target is not None and offer.price <= target
    # alert_below: one absolute price for every condition the product accepts.
    below = _num(product.get("alert_below"))
    if below is not None:
        hit_target = hit_target or offer.price <= below
        target = target if target is not None else below
    qualifies = (pct is not None and max_pct is not None and pct <= max_pct) or hit_target

    return Assessment(tier=tier, storage_guessed=guessed, msrp=msrp, pct_of_msrp=pct,
                      carrier=detect_carrier(offer.title), qualifies=qualifies, target=target)
