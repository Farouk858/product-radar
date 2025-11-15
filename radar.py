#!/usr/bin/env python3
"""
Product Radar ~ GitHub-only version with tables + links + best pick

- Reads brands from brands.json
- Scrapes each brand with Playwright
- For each product we store: name, url, score, status
- Compares against previous state to find NEW products
- Writes a markdown report under reports/YYYY-MM-DD.md
- Sends an email where each brand is a table:
    | Product | Link | New? | Score | Status |
  plus a "Best pick" line per brand
"""

import os
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Tuple, Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

from radar_selectors import GENERIC_KEYWORDS, BRAND_HINTS

# ---------- Repo paths ----------
STATE_PATH = Path("data/state.json")
REPORTS_DIR = Path("reports")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

# ---------- Email env ----------
EMAIL_USER = os.getenv("EMAIL_USER")      # your Gmail address
EMAIL_PASS = os.getenv("EMAIL_PASS")      # Gmail app password
EMAIL_TO   = os.getenv("EMAIL_TO")        # recipient address

# ---------- Page tuning ----------
DEFAULT_GOTO_TIMEOUT = 35000        # 35s per navigation
DEFAULT_WAIT_AFTER_DOM = 1500       # ms
RETRIES_PER_URL = 2

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------- Brands loader ----------
def load_brands() -> Dict[str, str]:
    p = Path("brands.json")
    if not p.exists():
        raise RuntimeError("brands.json not found. Create it at repo root.")
    data = json.loads(p.read_text(encoding="utf-8"))
    return {item["name"]: item["url"] for item in data if item.get("name") and item.get("url")}


# ---------- State helpers ----------
def today_iso_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def load_state() -> Dict[str, List[Dict[str, Any]]]:
    """
    Load previous state.

    Old versions stored a list of product names (strings) per brand.
    New version stores list of dicts: {name, url, score, status}.
    This function upgrades old state into the new format automatically.
    """
    if not STATE_PATH.exists():
        return {}
    raw = json.loads(STATE_PATH.read_text())
    fixed: Dict[str, List[Dict[str, Any]]] = {}
    for brand, rows in raw.items():
        if rows and isinstance(rows[0], str):
            fixed[brand] = [{"name": r, "url": "", "score": 0.0, "status": "unknown"} for r in rows]
        else:
            # ensure dicts have all keys
            fixed_rows: List[Dict[str, Any]] = []
            for r in rows or []:
                fixed_rows.append({
                    "name": r.get("name", ""),
                    "url": r.get("url", ""),
                    "score": float(r.get("score", 0.0)),
                    "status": r.get("status", "unknown"),
                })
            fixed[brand] = fixed_rows
    return fixed


def save_state(state: Dict[str, List[Dict[str, Any]]]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ---------- Scraping helpers ----------
def normalise_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def product_key(prod: Dict[str, Any]) -> Tuple[str, str]:
    return (
        prod.get("name", "").lower(),
        prod.get("url", "").lower(),
    )


def find_candidate_products(html: str, base_url: str, collection_hint: str | None = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Parse HTML and extract candidate products as dicts:
    {name, url, score, status}

    Score heuristic:
    - base 1
    - +2 if collection_hint suggests "new" or "drop"
    - +3 if collection_hint suggests "best", "popular"
    - +3 if product name contains generic keywords

    Status heuristic:
    - "sold out" in name -> sold out
    - "only ... left" in name -> low stock
    """
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ").lower()

    hits = [kw for kw in GENERIC_KEYWORDS if kw in text]

    # Collection-level score bump
    base_score = 1.0
    if collection_hint:
        hint_l = collection_hint.lower()
        if any(k in hint_l for k in ["best", "popular", "bestseller"]):
            base_score += 3.0
        if any(k in hint_l for k in ["new", "latest", "drop", "arrivals"]):
            base_score += 2.0

    products: Dict[Tuple[str, str], Dict[str, Any]] = {}

    selectors = [
        "a[href*='/products/']",
        "a[href*='product']",
        "[class*='product'] a",
        "h2, h3, .product-title, .ProductItem__Title, .card__heading",
        "a:has(img)",
    ]

    for sel in selectors:
        for el in soup.select(sel):
            name = normalise_text(el.get_text())
            if not (3 <= len(name) <= 120):
                continue
            if re.match(r"^(home|shop|cart|menu|search)$", name, re.I):
                continue

            # Find a URL for this product
            href = ""
            if el.name == "a" and el.get("href"):
                href = el["href"]
            else:
                a_parent = el.find_parent("a")
                if a_parent and a_parent.get("href"):
                    href = a_parent["href"]

            if href:
                href = urljoin(base_url, href)

            # Status heuristic based on name text
            lower_name = name.lower()
            status = "available"
            if "sold out" in lower_name:
                status = "sold out"
            elif "only" in lower_name and "left" in lower_name:
                status = "low stock"

            # Score this product
            score = base_score
            if any(kw in lower_name for kw in GENERIC_KEYWORDS):
                score += 3.0

            key = (name.lower(), href.lower())
            existing = products.get(key)
            if existing:
                if score > existing["score"]:
                    existing["score"] = score
            else:
                products[key] = {
                    "name": name,
                    "url": href,
                    "score": score,
                    "status": status,
                }

    return list(products.values())[:25], hits


def brand_scan(play, brand: str, url: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Visit base URL and any brand-specific alt paths.
    Uses DOMContentLoaded + short settle, blocks heavy assets, retries on failure.
    """
    browser = play.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    context = browser.new_context(
        user_agent=UA_DESKTOP,
        locale="en-GB",
        timezone_id="Europe/London",
        viewport={"width": 1366, "height": 2000},
    )

    def block_resources(route):
        req = route.request
        url_l = req.url.lower()
        if any(url_l.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".mp4", ".webm", ".woff", ".woff2", ".ttf")):
            return route.abort()
        if any(host in url_l for host in ("doubleclick.net", "googletagmanager.com", "analytics", "facebook.net", "tiktokcdn")):
            return route.abort()
        return route.continue_()

    context.route("**/*", block_resources)
    page = context.new_page()

    def safe_visit(target_url: str, hint: str | None) -> Tuple[List[Dict[str, Any]], List[str], str]:
        last_err = None
        for attempt in range(1, RETRIES_PER_URL + 1):
            try:
                page.set_default_navigation_timeout(DEFAULT_GOTO_TIMEOUT)
                page.goto(target_url, wait_until="domcontentloaded", timeout=DEFAULT_GOTO_TIMEOUT)
                page.wait_for_timeout(DEFAULT_WAIT_AFTER_DOM)
                html = page.content()
                sample, hits = find_candidate_products(html, target_url, collection_hint=hint)
                return sample, hits, ""
            except PWTimeoutError:
                last_err = f"timeout on attempt {attempt}"
            except Exception as e:
                last_err = f"error: {type(e).__name__}"
        return [], [], last_err or "unknown error"

    found: List[Dict[str, Any]] = []
    notes: List[str] = []

    try:
        # Base URL
        sample, hits, warn = safe_visit(url, hint=None)
        if warn:
            notes.append(f"{warn} at base")
        if hits:
            notes.append(f"Page signals: {', '.join(sorted(set(hits)))}")
        found.extend(sample)

        # Alt collection paths
        for alt in BRAND_HINTS.get(brand, {}).get("alts", []):
            target = url.rstrip("/") + alt
            sample2, hits2, warn2 = safe_visit(target, hint=alt)
            if warn2:
                notes.append(f"{alt} {warn2}")
            if hits2:
                notes.append(f"{alt} signals: {', '.join(sorted(set(hits2)))}")
            found.extend(sample2)

    finally:
        context.close()
        browser.close()

    # De-dup by (name,url)
    dedup: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    for p in found:
        key = product_key(p)
        if key not in seen:
            seen.add(key)
            dedup.append(p)

    return dedup[:30], notes


# ---------- Reporting helpers ----------
def format_markdown(date_str: str, brand: str, rows: List[Dict[str, Any]], expl_notes: List[str], newly_added: List[Dict[str, Any]]) -> str:
    header = f"{date_str} | {brand}"
    if not rows:
        reason = "; ".join(expl_notes) or "No reliable signals found"
        return f"### {header}\nCould not verify current best-sellers or restocks with confidence ~ {reason}.\n"

    md = [f"### {header}", "", "| Product | Link | New? | Score | Status |", "|---|---|---|---|---|"]
    note_text = ", ".join(expl_notes) if expl_notes else "Heuristic selection"
    new_keys = {product_key(p) for p in newly_added}

    # Sort rows by score descending
    sorted_rows = sorted(rows, key=lambda p: p.get("score", 0.0), reverse=True)

    for p in sorted_rows:
        name = p["name"]
        url = p.get("url") or ""
        score = f"{p.get('score', 0.0):.1f}"
        status = p.get("status", "unknown")
        is_new = "✅" if product_key(p) in new_keys else ""
        link = url if url else ""
        md.append(f"| {name} | {link} | {is_new} | {score} | {status} |")

    md.append("")
    md.append(f"_Notes: {note_text}_")
    md.append("")
    return "\n".join(md)


def diff_new(prev_list: List[Any], current_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    prev_keys = set()
    for item in prev_list or []:
        if isinstance(item, str):
            prev_keys.add((item.lower(), ""))
        else:
            prev_keys.add(product_key(item))
    return [p for p in current_list if product_key(p) not in prev_keys]


def choose_best(products: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    if not products:
        return None
    return max(products, key=lambda p: p.get("score", 0.0))


def send_email(subject: str, body: str) -> None:
    if not (EMAIL_USER and EMAIL_PASS and EMAIL_TO):
        print("Email credentials not set ~ skipping email send.")
        return
    import smtplib
    from email.mime.text import MIMEText
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)


# ---------- Main ----------
def main():
    brands = load_brands()
    day = today_iso_utc()
    state = load_state()
    report_sections: List[str] = []
    email_sections: List[str] = []

    with sync_playwright() as play:
        for brand, url in brands.items():
            print(f"Scanning {brand} -> {url}")
            rows, notes = brand_scan(play, brand, url)

            prev_rows = state.get(brand, [])
            newly = diff_new(prev_rows, rows)
            best = choose_best(newly)

            # Markdown section for full report file
            section_md = format_markdown(day, brand, rows, notes, newly)
            report_sections.append(section_md)

            # Email section ~ markdown-style table with links
            sorted_rows = sorted(rows, key=lambda p: p.get("score", 0.0), reverse=True)
            new_keys = {product_key(p) for p in newly}

            lines: List[str] = []
            lines.append(f"{brand}")
            lines.append("")
            lines.append("| Product | Link | New? | Score | Status |")
            lines.append("|---|---|---|---|---|")

            for p_row in sorted_rows:
                name = p_row["name"]
                url = p_row.get("url") or ""
                score = f"{p_row.get('score', 0.0):.1f}"
                status = p_row.get("status", "unknown")
                is_new = "✅" if product_key(p_row) in new_keys else ""
                link = url if url else ""
                lines.append(f"| {name} | {link} | {is_new} | {score} | {status} |")

            if best:
                best_url = best.get("url") or ""
                best_score = f"{best.get('score', 0.0):.1f}"
                best_status = best.get("status", "unknown")
                lines.append("")
                lines.append(f"Best pick: {best['name']} ({best_url}) · Score {best_score} · Status {best_status}")

            email_sections.append("\n".join(lines))

            # Update state
            state[brand] = rows

    # Write markdown report file
    report_md = "\n\n".join(report_sections).strip() + "\n"
    out_path = REPORTS_DIR / f"{day}.md"
    out_path.write_text(report_md, encoding="utf-8")
    save_state(state)

    # Build email body
    subject = f"Product Radar ~ {day}"
    body = (
        "Daily Product Radar\n\n"
        + "\n\n".join(email_sections)
        + "\n\nLegend:\n"
          "- New? column shows ✅ for products first seen since the previous report.\n"
          "- Score is a heuristic based on how prominently the site surfaces the product "
          "(new arrivals / bestsellers / badges / keywords).\n\n"
        "Full markdown report is stored in the repo under reports/.\n"
    )
    send_email(subject, body)

    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
