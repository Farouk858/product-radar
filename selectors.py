# selectors.py

GENERIC_KEYWORDS = [
    "bestseller", "best seller", "best-selling",
    "back in stock", "restock", "restocked",
    "most popular", "popular",
    "trending",
    "new arrivals", "new in", "just dropped",
]

# Optional brand-specific paths that often surface signals
BRAND_HINTS = {
    "Corteiz": {
        "alts": [],
    },
    "Represent": {
        "alts": ["/collections/new-arrivals", "/collections/bestsellers"],
    },
    "Supreme": {
        "alts": ["/shop/new", "/shop/all"],
    },
    "Palace": {
        "alts": ["/collections/new", "/collections/all"],
    },
    "Aimé Leon Dore": {
        "alts": ["/collections/new-arrivals", "/collections/menswear"],
    },
    "Jaded London": {
        "alts": ["/collections/new-in", "/collections/bestsellers"],
    },
}

