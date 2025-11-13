#!/usr/bin/env python3
"""
Product Radar ~ free GitHub-only version
- Scrapes brand sites with Playwright
- Looks for bestseller/restock/new-arrivals signals
- Emits a dated markdown report
- Compares with prior state to highlight NEW items
- Emails a summary via SMTP
"""

import os
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
from radar_selectors import GENERIC_KEYWORDS, BRAND_HINTS


# ---------- Repo paths ----------
STATE_PATH = Path("data/state.json")
REPORTS_DIR = Path("reports")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

# ---------- Email env ----------
# Page + fetch tuning
DEFAULT_GOTO_TIMEOUT = 35000        # 35s per navigation
DEFAULT_WAIT_AFTER_DOM = 1500       # short settle
RETRIES_PER_URL = 2

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

EMAIL_USER = os.getenv("EMAIL_USER")      # e.g. your Gmail address
EMAIL_PASS = os.getenv("EMAIL_PASS")      # Gmail app password
EMAIL_TO   = os.getenv("EMAIL_TO")        # recipient address

# ---------- Brands loader ----------
def load_brands():
    p = Path("brands.json")
    if not p.exists():
        raise RuntimeError("brands.json not found. Create it at repo root.")
    data = json.loads(p.read_text(encoding="utf-8"))
    return {item["name"]: item["url"] for item in data if item.get("name") and item.get("url")}

# ---------- Helpers ----------
def today_iso_utc():
    return datetime.now(timezone.utc).date().isoformat()

def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}

def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))

def normalise_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

def find_candidate_products(html: str, base_url: str):
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ").lower()

    hits = [kw for kw in GENERIC_KEYWORDS if kw in text]

    products = set()
    for sel in [
        "a[href*='/products/']",
        "a[href*='product']",
        "[class*='product'] a",
        "h2, h3, .product-title, .ProductItem__Title, .card__heading",
        "a:has(img)",
    ]:
        for el in soup.select(sel):
            name = normalise_text(el.get_text())
            if 3 <= len(name) <= 120 and not re.match(r"^(home|shop|cart|menu|search)$", name, re.I):
                products.add(name)

    sample = sorted(products)[:25]
    return sample, hits

def brand_scan(play, brand: str, url: str):
    """
    Visit the site and any brand-specific alt paths.
    Never hang on 'networkidle' ~ use DOMContentLoaded + short settle.
    Block heavy assets to reduce load + chance of timeouts.
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

    # Block heavy/3rd-party requests to speed up loads
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

    def safe_visit(target_url: str):
        """Try visiting a URL with retries; return (products, hits, note)."""
        last_err = None
        for attempt in range(1, RETRIES_PER_URL + 1):
            try:
                page.set_default_navigation_timeout(DEFAULT_GOTO_TIMEOUT)
                page.goto(target_url, wait_until="domcontentloaded", timeout=DEFAULT_GOTO_TIMEOUT)
                page.wait_for_timeout(DEFAULT_WAIT_AFTER_DOM)  # short settle
                html = page.content()
                sample, hits = find_candidate_products(html, target_url)
                return sample, hits, ""
            except PWTimeoutError:
                last_err = f"timeout on attempt {attempt}"
            except Exception as e:
                last_err = f"error: {type(e).__name__}"
        return [], [], last_err or "unknown error"

    found, notes = [], []

    try:
        # Base URL
        sample, hits, warn = safe_visit(url)
        if warn:
            notes.append(f"{warn} at base")
        if hits:
            notes.append(f"Page signals: {', '.join(sorted(set(hits)))}")
        found.extend(sample)

        # Alt paths
        from radar_selectors import BRAND_HINTS  # local import to avoid circulars if any
        for alt in BRAND_HINTS.get(brand, {}).get("alts", []):
            target = url.rstrip("/") + alt
            sample2, hits2, warn2 = safe_visit(target)
            if warn2:
                notes.append(f"{alt} {warn2}")
            if hits2:
                notes.append(f"{alt} signals: {', '.join(sorted(set(hits2)))}")
            found.extend(sample2)

    finally:
        context.close()
        browser.close()

    # De-dup
    dedup, seen = [], set()
    for p in found:
        key = p.lower()
        if key not in seen:
            seen.add(key)
            dedup.append(p)

    return dedup[:30], notes


def format_markdown(date_str, brand, rows, expl_notes, newly_added):
    header = f"{date_str} | {brand}"
    if not rows:
        return f"### {header}\nCould not verify current best-sellers or restocks with confidence.\n"

    md = [f"### {header}", "", "| Product | Category | Notes |", "|---|---|---|"]
    note_text = ", ".join(expl_notes) if expl_notes else "Heuristic selection"
    for name in rows:
        flag = "NEW" if name in newly_added else ""
        md.append(f"| {name} | n/a | {flag or note_text} |")
    md.append("")
    return "\n".join(md)

def diff_new(prev_list, current_list):
    prev_set = {p.lower() for p in (prev_list or [])}
    return [p for p in current_list if p.lower() not in prev_set]

def send_email(subject, body):
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

def main():
    brands = load_brands()
    day = today_iso_utc()
    state = load_state()
    report_sections, email_sections = [], []

    with sync_playwright() as play:
        for brand, url in brands.items():
            print(f"Scanning {brand} -> {url}")
            rows, notes = brand_scan(play, brand, url)
            prev_rows = state.get(brand, [])
            newly = diff_new(prev_rows, rows)

            section_md = format_markdown(day, brand, rows, notes, newly)
            report_sections.append(section_md)

            if newly:
                email_sections.append(f"{brand} ~ new items:\n- " + "\n- ".join(newly))

            state[brand] = rows

    report_md = "\n".join(report_sections).strip() + "\n"
    out_path = REPORTS_DIR / f"{day}.md"
    out_path.write_text(report_md, encoding="utf-8")
    save_state(state)

    subject = f"Product Radar ~ {day}"
    if email_sections:
        body = "New items detected since last run:\n\n" + "\n\n".join(email_sections) + "\n\nFull report is in the repo under reports/."
    else:
        body = f"No new items detected today.\n\nFull report for {day} is in the repo under reports/."
    send_email(subject, body)

    print(f"Wrote {out_path}")

if __name__ == "__main__":
    main()

