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

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from selectors import GENERIC_KEYWORDS, BRAND_HINTS

# ---------- Repo paths ----------
STATE_PATH = Path("data/state.json")
REPORTS_DIR = Path("reports")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

# ---------- Email env ----------
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

def brand_scan(play, brand, url):
    browser = play.chromium.launch(args=["--no-sandbox"])
    page = browser.new_page()
    found, notes = [], []

    try:
        page.goto(url, timeout=60000)
        page.wait_for_load_state("networkidle")
        html = page.content()
        sample, hits = find_candidate_products(html, url)
        if hits:
            notes.append(f"Page signals: {', '.join(sorted(set(hits)))}")
        found.extend(sample)

        for alt in BRAND_HINTS.get(brand, {}).get("alts", []):
            try:
                target = url.rstrip("/") + alt
                page.goto(target, timeout=60000)
                page.wait_for_load_state("networkidle")
                html2 = page.content()
                sample2, hits2 = find_candidate_products(html2, target)
                if hits2:
                    notes.append(f"{alt} signals: {', '.join(sorted(set(hits2)))}")
                found.extend(sample2)
            except Exception:
                pass
    finally:
        browser.close()

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

