"""Scrape vendor (brand) details from https://findgroupbuy.com.

The site is a Bubble.io app whose internal ElasticSearch endpoints use
encrypted payloads, but its **public Data API** is open for the `brand`
type and returns all 91 brands with full contact info in a single call.

We also render the Bishan estate page (with full lazy-scroll) to mark which
brands are listed there.
"""

import csv
import json
import re
from pathlib import Path

from scrapling.fetchers import Fetcher
from playwright.sync_api import sync_playwright

BASE = "https://findgroupbuy.com"


def fetch_brands():
    r = Fetcher.get(f"{BASE}/api/1.1/obj/brand?limit=200")
    if r.status != 200:
        raise RuntimeError(f"brand API {r.status}: {r.body[:300]!r}")
    body = r.body if isinstance(r.body, str) else r.body.decode()
    return json.loads(body)["response"]["results"]


def fetch_estate_brand_names(slug: str) -> set[str]:
    """Render an estate's discover page (scrolling to load all cards) and
    return the set of brand names rendered on it."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 3000})
        page = ctx.new_page()
        page.goto(
            f"{BASE}/discover/{slug}", wait_until="networkidle", timeout=45000
        )
        page.wait_for_timeout(2000)
        for _ in range(15):
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(400)
        page.wait_for_timeout(1500)
        text = page.evaluate("document.body.innerText")
        browser.close()

    # Strip the page header (everything up to and including the
    # "N brand(s) available for groupbuy in your area" line).
    text = re.sub(
        r"(?s).*?\d+\s+brand\(s\) available for groupbuy in your area[^\n]*\n",
        "",
        text,
        count=1,
    )

    # Each card chunk is delimited by "arrow_forward" on its own line.
    # Format: Category \n Name \n <description...> \n N product(s)
    chunks = [c.strip() for c in re.split(r"\narrow_forward\n", text) if c.strip()]
    names = set()
    for c in chunks:
        m = re.search(r"(.*)\n\d+ product\(s\)\s*$", c, flags=re.DOTALL)
        if not m:
            continue
        head = m.group(1).strip()
        lines = [ln.strip() for ln in head.splitlines() if ln.strip()]
        if len(lines) >= 2:
            # Structure: Category, Name, <description lines>
            names.add(lines[1])
    return names


def to_str(v) -> str:
    if v is None or isinstance(v, bool):
        return ""
    return str(v).strip()


def normalize_url(v) -> str:
    u = to_str(v)
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    return u


def main():
    out_dir = Path(__file__).parent / "findgroupbuy_output"
    out_dir.mkdir(exist_ok=True)

    print("Fetching brands via public Data API…")
    brands = fetch_brands()
    print(f"  {len(brands)} brands")

    print("Rendering Bishan page to mark on-Bishan brands…")
    bishan_names = fetch_estate_brand_names("bishan")
    print(f"  {len(bishan_names)} brand names parsed from Bishan page")

    (out_dir / "brands_full.json").write_text(
        json.dumps(brands, indent=2, ensure_ascii=False)
    )

    csv_path = out_dir / "vendors.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "name",
            "category",
            "in_bishan",
            "live",
            "dtc",
            "email",
            "whatsapp",
            "website",
            "facebook",
            "instagram",
            "wa_group",
            "wa_group_admin",
            "slug",
            "page_url",
            "description",
        ])
        for b in brands:
            name = to_str(b.get("Name"))
            slug = to_str(b.get("Slug"))
            page_url = f"{BASE}/brand/{slug}" if slug else ""
            w.writerow([
                name,
                to_str(b.get("Category")),
                "yes" if name in bishan_names else "",
                b.get("Live", ""),
                b.get("DTC", ""),
                to_str(b.get("Email")),
                to_str(b.get("WhatsApp Number")),
                normalize_url(b.get("Website URL")),
                normalize_url(b.get("Facebook URL")),
                normalize_url(b.get("Instagram URL")),
                normalize_url(b.get("Whatsapp Group")),
                to_str(b.get("WA Group Admin Line")),
                slug,
                page_url,
                to_str(b.get("Description")).replace("\n", " "),
            ])

    bishan_count = sum(1 for b in brands if to_str(b.get("Name")) in bishan_names)
    print(f"Wrote {csv_path}  ({len(brands)} rows, {bishan_count} marked in_bishan)")
    print(f"Wrote {out_dir/'brands_full.json'}")


if __name__ == "__main__":
    main()
