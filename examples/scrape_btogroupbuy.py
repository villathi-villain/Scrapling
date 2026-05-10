"""Scrape vendor (brand) details from https://btogroupbuy.sg/discover.

The page is a React SPA backed by a public Supabase REST API. We bypass the
SPA entirely and call the same endpoint the browser does, which returns the
full vendor list with category + deal data in one shot.
"""

import csv
import json
from pathlib import Path

from scrapling.fetchers import Fetcher

SUPABASE_URL = "https://kawfrclvoumigrjgxxki.supabase.co"
SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imthd2"
    "ZyY2x2b3VtaWdyamd4eGtpIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTI1NjcwMjAsImV4cCI6"
    "MjA2ODE0MzAyMH0.u4eM4dm60j9zoS88aiVzG5E5mT_He8gbnOPmSqXHgZE"
)

HEADERS = {
    "apikey": SUPABASE_ANON_KEY,
    "authorization": f"Bearer {SUPABASE_ANON_KEY}",
    "accept-profile": "public",
    "accept": "application/json",
    "referer": "https://btogroupbuy.sg/",
}


def fetch_json(path: str):
    url = f"{SUPABASE_URL}{path}"
    r = Fetcher.get(url, headers=HEADERS)
    if r.status != 200:
        raise RuntimeError(f"{r.status} for {url}: {r.body[:300]!r}")
    body = r.body if isinstance(r.body, str) else r.body.decode()
    return json.loads(body)


def fetch_brands():
    return fetch_json("/rest/v1/brands?select=*,categories(*)&order=name.asc")


def fetch_deals():
    return fetch_json(
        "/rest/v1/deals?select=*,brands(*),categories(*),deal_options(*)"
        "&order=created_at.desc"
    )


def main():
    out_dir = Path(__file__).parent / "btogroupbuy_output"
    out_dir.mkdir(exist_ok=True)

    print("Fetching brands…")
    brands = fetch_brands()
    print(f"  {len(brands)} brands")

    print("Fetching deals…")
    deals = fetch_deals()
    print(f"  {len(deals)} deals")

    deals_by_brand: dict[str, list] = {}
    for d in deals:
        deals_by_brand.setdefault(d["brand_id"], []).append(d)

    # Full JSON dump (everything, including base64 logos)
    (out_dir / "brands_full.json").write_text(
        json.dumps(brands, indent=2, ensure_ascii=False)
    )
    (out_dir / "deals_full.json").write_text(
        json.dumps(deals, indent=2, ensure_ascii=False)
    )

    # Slim CSV that's actually useful — vendor contact + deal counts
    csv_path = out_dir / "vendors.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "name",
            "category",
            "description",
            "website",
            "whatsapp",
            "page_url",
            "active_deal_count",
            "deal_titles",
        ])
        for b in brands:
            name = (b.get("name") or "").strip()
            slug = name.lower().replace(" ", "-").replace("&", "").replace("--", "-")
            page_url = f"https://btogroupbuy.sg/brand/{slug}-{b['id']}"
            cat = (b.get("categories") or {}).get("name", "")
            bdeals = deals_by_brand.get(b["id"], [])
            w.writerow([
                name,
                cat,
                b.get("description") or "",
                b.get("website") or "",
                b.get("whatsapp") or "",
                page_url,
                len(bdeals),
                " | ".join(d.get("title", "") for d in bdeals),
            ])
    print(f"Wrote {csv_path}")
    print(f"Wrote {out_dir/'brands_full.json'}")
    print(f"Wrote {out_dir/'deals_full.json'}")


if __name__ == "__main__":
    main()
