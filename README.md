# Product Radar ~ zero-cost automation

Daily GitHub Action scans brand shops with Playwright, writes `reports/YYYY-MM-DD.md`, keeps `data/state.json` for diffs, and emails a compact summary of *new* items. Add or remove brands using browser forms ~ no code edits.

## How to set up

1) **Create repo secrets**  
Settings → Secrets and variables → Actions → New repository secret
- `EMAIL_USER` ~ your Gmail address
- `EMAIL_PASS` ~ a Gmail **App Password**
  - Google Account → Security → 2-Step Verification → App passwords → create for “Mail” on “Other”
  - Copy the 16-character password
- `EMAIL_TO` ~ the destination address

2) **Commit this repo structure**  
All files in place ~ the workflows will run.

3) **Run manually once**  
Go to **Actions → Product Radar ~ daily → Run workflow**.  
First run seeds `data/state.json` and writes `reports/<today>.md`.

4) **Daily schedule**  
The cron in `.github/workflows/radar.yml` is `0 8 * * *` ~ 08:00 UTC.  
Change if you want a different time.

## GUI to manage brands

- Add a brand: **Issues → New issue → “Add a brand”**  
- Remove a brand: **Issues → New issue → “Remove a brand”**  
The `brands.yml` workflow validates and updates `brands.json`, commits, and closes the issue.

## Notes

- This is a heuristic scraper. Improve results by extending `selectors.py` with brand-specific selectors or extra paths.
- If Gmail blocks SMTP, ensure 2-Step Verification is on and you used an **App Password**.
- Everything runs on GitHub infra ~ no local installs needed.

