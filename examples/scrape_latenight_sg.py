"""Scrape late-night Singapore Bars / Clubs / Bistros / Restaurants from Google Maps.

Pipeline
--------
1. For each seed query, drive Google Maps search, scroll the result feed,
   and harvest each result's place URL.
2. Visit each unique place URL and scrape the side panel:
   name, category, address, phone, website, weekly hours.
3. Filter to category in {bar, pub, lounge, club, nightclub, bistro,
   brasserie, restaurant, gastropub, izakaya, diner, eatery} AND closing time
   between 00:30 and 05:30 on at least one day (or "Open 24 hours").
4. For each kept place with a website, fetch the homepage (and a /contact
   page if linked) and mine emails.
5. Output:
   - latenight_sg_output/raw.json       (all scraped places, resumable cache)
   - latenight_sg_output/places.csv     (final filtered + enriched rows)

Notes
-----
- Google Maps caps each search at ~120 results, so coverage is broadened
  via multiple seed queries; results are de-duped by Maps CID.
- Email is best-effort: many F&B venues don't publish one. Expect partial
  coverage. mailto: links and on-page email regex matches are mined; junk
  patterns (noreply, wixpress, sentry…) are filtered.
- The script is resumable: re-running picks up from raw.json. Delete the
  file to start fresh.
"""

from __future__ import annotations

import csv
import json
import re
import time
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from playwright.sync_api import Page, sync_playwright

from scrapling.fetchers import Fetcher

OUT_DIR = Path(__file__).parent / "latenight_sg_output"
OUT_DIR.mkdir(exist_ok=True)

QUERIES = [
    "bars Singapore",
    "cocktail bars Singapore",
    "pubs Singapore",
    "nightclubs Singapore",
    "late night bistro Singapore",
    "bistros Singapore",
    "late night restaurant Singapore",
    "24 hour restaurant Singapore",
]

CATEGORY_KEEP = re.compile(
    r"\b(bar|pub|tavern|lounge|club|night\s*club|bistro|brasserie|"
    r"restaurant|diner|izakaya|gastropub|eatery)\b",
    re.IGNORECASE,
)

DAY_PREFIXES = (
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
)


# ---------------------------------------------------------------------------
# Google Maps driving
# ---------------------------------------------------------------------------

def maps_search_url(query: str) -> str:
    return f"https://www.google.com/maps/search/{quote(query)}/?hl=en-SG"


def dismiss_consent(page: Page) -> None:
    """Google occasionally shows a consent interstitial. Click through it."""
    for sel in [
        'button[aria-label*="Accept all"]',
        'button[aria-label*="Reject all"]',
        'button:has-text("Accept all")',
        'button:has-text("Reject all")',
        'form[action*="consent"] button',
    ]:
        try:
            btn = page.locator(sel)
            if btn.count():
                btn.first.click(timeout=2000)
                page.wait_for_timeout(600)
                return
        except Exception:
            pass


def collect_place_urls(page: Page, query: str, max_scrolls: int = 80) -> list[str]:
    page.goto(maps_search_url(query), wait_until="domcontentloaded", timeout=45000)
    dismiss_consent(page)

    # Either the results feed appears, or Maps jumps straight to a single place.
    try:
        page.wait_for_selector('div[role="feed"], h1', timeout=15000)
    except Exception:
        return []

    if "/maps/place/" in page.url and not page.locator('div[role="feed"]').count():
        return [page.url]

    feed = page.locator('div[role="feed"]').first
    last = 0
    end_seen = False
    for _ in range(max_scrolls):
        try:
            feed.evaluate("(el) => el.scrollTo(0, el.scrollHeight)")
        except Exception:
            break
        page.wait_for_timeout(1100)
        count = page.locator('a.hfpxzc').count()
        if page.get_by_text("You've reached the end of the list").count():
            end_seen = True
            break
        if count == last:
            page.wait_for_timeout(1500)
            count = page.locator('a.hfpxzc').count()
            if count == last:
                break
        last = count

    urls = []
    for a in page.locator('a.hfpxzc').all():
        href = a.get_attribute('href') or ''
        if "/maps/place/" in href:
            urls.append(href)
    print(f"  [{query}] feed={last} end_marker={end_seen} urls={len(urls)}")
    return urls


def place_key(url: str) -> str:
    """Stable id for de-dup: Maps CID (the !1s segment), else the slug."""
    m = re.search(r"!1s([^!]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/place/([^/]+)/", url)
    return m.group(1) if m else url


def expand_hours(page: Page) -> None:
    btn = page.locator('button[data-item-id="oh"]').first
    if btn and btn.count():
        try:
            btn.click(timeout=2000)
            page.wait_for_timeout(450)
        except Exception:
            pass


def extract_hours_rows(page: Page) -> list[str]:
    """Pull each weekday's row text from the expanded hours table.

    Strips Material-icon ligature glyphs (e.g. \\ue14d) that follow the time
    string in the rendered DOM.
    """
    rows = page.evaluate(
        """
        () => {
          const days = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"];
          const out = [];
          const trs = document.querySelectorAll('tr');
          for (const tr of trs) {
            const t = (tr.innerText || '').trim();
            if (!t) continue;
            for (const d of days) {
              if (t.startsWith(d)) { out.push(t.replace(/\\s+/g,' ').trim()); break; }
            }
          }
          return out;
        }
        """
    ) or []
    # Strip private-use icon glyphs and collapse whitespace
    cleaned = []
    for r in rows:
        r2 = re.sub(r'[-]', '', r)
        r2 = re.sub(r'\s+', ' ', r2).strip()
        if r2:
            cleaned.append(r2)
    return cleaned


def scrape_place(page: Page, url: str) -> dict | None:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"    ! goto failed: {e}")
        return None
    try:
        page.wait_for_selector('h1', timeout=15000)
    except Exception:
        return None
    page.wait_for_timeout(1100)

    def _safe(fn, default=""):
        try:
            return fn() or default
        except Exception:
            return default

    name = _safe(lambda: page.locator('h1').first.inner_text(timeout=2000).strip())

    permanently_closed = False
    try:
        permanently_closed = page.get_by_text("Permanently closed").count() > 0
    except Exception:
        pass

    address = _safe(lambda: (
        (page.locator('button[data-item-id="address"]').first.get_attribute('aria-label') or '')
        .replace('Address: ', '').strip()
    ))
    if not address:
        address = _safe(lambda: page.locator('button[data-item-id="address"]').first.inner_text().strip())

    phone = ""
    try:
        b = page.locator('button[data-item-id^="phone:tel:"]').first
        if b.count():
            aria = (b.get_attribute('aria-label') or '').strip()
            mp = re.search(r'(\+?\d[\d\s\-]{6,}\d)', aria)
            if mp:
                phone = mp.group(1).strip()
            if not phone:
                phone = (b.get_attribute('data-item-id') or '').replace('phone:tel:', '').strip()
            # SG locale strips the +65 prefix from local numbers; restore it
            if phone and not phone.lstrip().startswith('+'):
                digits_only = re.sub(r'\D', '', phone)
                if len(digits_only) == 8:
                    phone = f"+65 {phone}"
    except Exception:
        pass

    website = _safe(lambda: page.locator('a[data-item-id="authority"]').first.get_attribute('href') or '')

    category = _safe(lambda: page.locator('button.DkEaL').first.inner_text().strip())
    if not category:
        # Fallback: aria-label of the first button next to the title block
        category = _safe(lambda: (page.locator('button[jsaction*="category"]').first.inner_text() or '').strip())

    expand_hours(page)
    rows = []
    try:
        rows = extract_hours_rows(page) or []
    except Exception:
        pass

    return {
        "name": name,
        "category": category,
        "address": address,
        "phone": phone,
        "website": website,
        "hours_raw": " | ".join(rows),
        "hours_rows": rows,
        "permanently_closed": permanently_closed,
        "gmaps_url": page.url,
    }


# ---------------------------------------------------------------------------
# Hours parsing + late-night filter
# ---------------------------------------------------------------------------

TIME_RE = re.compile(r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>AM|PM)?", re.IGNORECASE)
SEP_RE = re.compile(r"\s*[–—\-]\s*")  # en / em dash / hyphen
DAY_PREFIX_RE = re.compile(
    r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*",
    re.IGNORECASE,
)


def to_minutes(t: str) -> int | None:
    """'1 AM' -> 60, '12:30 AM' -> 30, '11:30 PM' -> 23*60+30. Returns None if unparseable."""
    m = TIME_RE.search(t)
    if not m:
        return None
    h = int(m.group('h'))
    mn = int(m.group('m') or 0)
    ap = (m.group('ap') or '').upper()
    if ap == 'AM' and h == 12:
        h = 0
    elif ap == 'PM' and h != 12:
        h += 12
    return h * 60 + mn


def latest_close_minutes(rows: list[str]) -> int | None:
    """Across the week, the latest close time as minutes from start-of-open-day-midnight.

    A close that happens after midnight (close <= open) is returned as 24*60 + close,
    so '6 PM-2 AM' -> 26*60 = 1560, which beats '6 PM-11 PM' -> 23*60 = 1380.
    Returns the sentinel -1 if any day says 'Open 24 hours'.
    """
    best: int | None = None
    for row in rows:
        body = DAY_PREFIX_RE.sub('', row).strip()
        low = body.lower()
        if 'open 24 hours' in low or low == '24 hours':
            return -1
        if 'closed' in low and not SEP_RE.search(body):
            continue
        for rng in re.split(r'[,/;]\s*', body):
            if not SEP_RE.search(rng):
                continue
            left, right = SEP_RE.split(rng, maxsplit=1)
            open_m = to_minutes(left)
            close_m = to_minutes(right)
            if open_m is None or close_m is None:
                continue
            effective = close_m if close_m > open_m else (24 * 60 + close_m)
            if best is None or effective > best:
                best = effective
    return best


def passes_filter(place: dict) -> bool:
    if place.get('permanently_closed'):
        return False
    cat = place.get('category') or ''
    if not CATEGORY_KEEP.search(cat):
        return False
    rows = place.get('hours_rows') or []
    if not rows:
        return False
    lc = latest_close_minutes(rows)
    if lc is None:
        return False
    if lc == -1:
        return True  # 24h venue
    # Closes between 00:30 and 05:30 on the day after open => 24*60+30 .. 24*60+330
    return (24 * 60 + 30) <= lc <= (24 * 60 + 330)


# ---------------------------------------------------------------------------
# Email enrichment
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
JUNK_EMAIL_RE = re.compile(
    r"(noreply|no-reply|donotreply|wixpress|sentry|example|godaddy|"
    r"wordpress|cloudflare|squarespace|email\.com$|@2x|@3x)",
    re.IGNORECASE,
)


def find_emails(html: str) -> list[str]:
    found: set[str] = set()
    for m in re.finditer(r'mailto:([^"\'>?\s]+)', html):
        addr = m.group(1).split('?')[0].strip()
        if EMAIL_RE.fullmatch(addr):
            found.add(addr)
    for m in EMAIL_RE.finditer(html):
        found.add(m.group(0))
    # filter junk + image-asset look-alikes
    out = []
    for e in found:
        if JUNK_EMAIL_RE.search(e):
            continue
        if re.search(r"\.(png|jpe?g|gif|svg|webp)$", e, re.IGNORECASE):
            continue
        out.append(e)
    return out


def fetch_html(url: str) -> str | None:
    try:
        r = Fetcher.get(url, timeout=15, follow_redirects=True)
        if r.status >= 400:
            return None
        body = r.body
        return body if isinstance(body, str) else body.decode(errors='ignore')
    except Exception:
        return None


def best_email_for(website: str) -> str:
    if not website:
        return ''
    parsed = urlparse(website)
    if not parsed.scheme:
        website = 'https://' + website
        parsed = urlparse(website)
    base = f"{parsed.scheme}://{parsed.netloc}"

    # 1) Try homepage first
    home = fetch_html(website) or ''
    candidates = find_emails(home)

    # 2) Try common contact paths
    if not candidates:
        for path in ('/contact', '/contact-us', '/contact/', '/contact-us/', '/about', '/reservations'):
            html = fetch_html(urljoin(base, path))
            if html:
                candidates = find_emails(html)
                if candidates:
                    break

    # 3) Discover a contact link from homepage and follow it
    if not candidates and home:
        seen = set()
        for m in re.finditer(r'href=["\']([^"\']+)["\']', home, flags=re.IGNORECASE):
            href = m.group(1)
            if 'contact' not in href.lower():
                continue
            absu = urljoin(base, href)
            if absu in seen:
                continue
            seen.add(absu)
            html = fetch_html(absu)
            if html:
                candidates = find_emails(html)
                if candidates:
                    break

    if not candidates:
        return ''

    domain = parsed.netloc.lower().removeprefix('www.')
    same_domain = [e for e in candidates if e.lower().endswith('@' + domain)]
    pool = same_domain or candidates
    # Prefer common business inboxes
    for prefix in ('hello', 'info', 'contact', 'reservations', 'bookings', 'enquiries'):
        for e in pool:
            if e.lower().startswith(prefix + '@'):
                return e
    return pool[0]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    raw_path = OUT_DIR / "raw.json"
    csv_path = OUT_DIR / "places.csv"

    seen: dict[str, dict] = {}
    if raw_path.exists():
        try:
            cached = json.loads(raw_path.read_text())
            seen = {p['_key']: p for p in cached if p.get('_key')}
            print(f"Resuming from cache: {len(seen)} places already scraped")
        except Exception:
            seen = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1366, "height": 900},
            locale="en-SG",
            timezone_id="Asia/Singapore",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()

        # Phase 1: harvest URLs from each query
        all_urls: list[tuple[str, str]] = []
        for q in QUERIES:
            try:
                for u in collect_place_urls(page, q):
                    all_urls.append((u, q))
            except Exception as e:
                print(f"  ! query failed [{q}]: {e}")

        # Dedupe (prefer the first source query that introduced each key)
        keys_seen = set(seen.keys())
        queue: list[tuple[str, str]] = []
        for url, q in all_urls:
            k = place_key(url)
            if k in keys_seen:
                continue
            keys_seen.add(k)
            queue.append((url, q))
        print(f"\nNew places to scrape: {len(queue)} (cached: {len(seen)})")

        # Phase 2: scrape each place panel
        for i, (url, q) in enumerate(queue, 1):
            k = place_key(url)
            print(f"  [{i}/{len(queue)}] {k[:36]}…")
            try:
                p_obj = scrape_place(page, url)
            except Exception as e:
                print(f"    ! scrape err: {e}")
                p_obj = None
            if not p_obj:
                continue
            p_obj['_key'] = k
            p_obj['source_query'] = q
            seen[k] = p_obj
            if i % 20 == 0:
                raw_path.write_text(
                    json.dumps(list(seen.values()), indent=2, ensure_ascii=False)
                )
            time.sleep(0.5)

        browser.close()

    raw_path.write_text(
        json.dumps(list(seen.values()), indent=2, ensure_ascii=False)
    )
    print(f"\nWrote {raw_path} ({len(seen)} places total)")

    # Phase 3: filter + email enrichment
    kept = [p for p in seen.values() if passes_filter(p)]
    print(f"\n{len(kept)} places match category + late-night filter "
          f"(of {len(seen)} scraped)")

    print("Enriching websites with email…")
    for i, p_obj in enumerate(kept, 1):
        if 'email' in p_obj:
            continue
        site = p_obj.get('website') or ''
        try:
            p_obj['email'] = best_email_for(site) if site else ''
        except Exception:
            p_obj['email'] = ''
        if i % 10 == 0:
            print(f"  [{i}/{len(kept)}]")

    raw_path.write_text(
        json.dumps(list(seen.values()), indent=2, ensure_ascii=False)
    )

    # CSV
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "name", "category", "address", "phone", "email", "website",
            "hours_raw", "source_query", "gmaps_url",
        ])
        for p_obj in kept:
            w.writerow([
                p_obj.get('name', ''),
                p_obj.get('category', ''),
                p_obj.get('address', ''),
                p_obj.get('phone', ''),
                p_obj.get('email', ''),
                p_obj.get('website', ''),
                p_obj.get('hours_raw', ''),
                p_obj.get('source_query', ''),
                p_obj.get('gmaps_url', ''),
            ])
    with_email = sum(1 for p in kept if p.get('email'))
    print(f"Wrote {csv_path} ({len(kept)} rows, {with_email} with email)")


if __name__ == "__main__":
    main()
