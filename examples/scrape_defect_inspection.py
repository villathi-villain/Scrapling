"""Scrape Singapore defect-inspection competitors' advertised package prices.

Real-world example: this powers the live "Competitive benchmark" on
defectsguru.sg, comparing the top BTO/condo defect-inspection providers against
DefectsGuru's own rate card.

The target sites (a1inspection.sg, uncledefect.sg) sit behind
a JS / bot-wall — plain HTTP clients get an `sgcaptcha` interstitial — so we
fetch with a real browser via `StealthyFetcher` and fall back to the static
`Fetcher`. Run `scrapling install` once to download the browser.

Output: defect_inspection_output/competitor-pricing.json
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from scrapling.fetchers import Fetcher, StealthyFetcher

COMPETITORS = [
    {
        "name": "A1 Inspection",
        "slug": "a1inspection",
        "url": "https://a1inspection.sg/",
        "tier_names": {"1": "Bronze", "3": "Gold", "5": "Diamond"},
        "five_trip_4room": [
            r"diamond[^$]{0,400}?4[\s-]*room[^$]{0,120}?\$\s?(\d{3})",
            r"4[\s-]*room[^$]{0,400}?diamond[^$]{0,120}?\$\s?(\d{3})",
        ],
    },
    {
        "name": "UncleDefect",
        "slug": "uncledefect",
        "url": "https://uncledefect.sg/",
        "tier_names": {"1": "1 Trip", "3": "3 Trips", "5": "5 Trips"},
        "five_trip_4room": [r"5[\s-]*trip[^$]{0,400}?4[\s-]*room[^$]{0,120}?\$\s?(\d{3})"],
    },
]


def fetch_text(url: str) -> str | None:
    """Rendered page text via a real browser, falling back to a static GET."""
    try:
        page = StealthyFetcher.fetch(url, headless=True, network_idle=True, timeout=60_000)
        if page is not None and page.status == 200:
            return page.get_all_text(ignore_tags=("script", "style"))
    except Exception as exc:
        print(f"  stealth fetch failed for {url}: {exc!r}")

    r = Fetcher.get(url, timeout=30, stealthy_headers=True)
    body = r.body if isinstance(r.body, str) else r.body.decode("utf-8", "ignore")
    if r.status == 200 and len(body) > 800:
        return body
    print(f"  static fetch for {url}: status={r.status} len={len(body)}")
    return None


def first_match(patterns: list[str], text: str) -> int | None:
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            return int(m.group(1))
    return None


def cheapest_price(text: str) -> int | None:
    vals = [int(v) for v in re.findall(r"\$\s?(\d{2,4})\b", text)]
    vals = [v for v in vals if 60 <= v <= 1500]
    return min(vals) if vals else None


def main() -> None:
    out_dir = Path(__file__).parent / "defect_inspection_output"
    out_dir.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    competitors = []
    for cfg in COMPETITORS:
        print(f"Scraping {cfg['name']} ({cfg['url']}) …")
        text = fetch_text(cfg["url"])
        comparable = first_match(cfg["five_trip_4room"], text) if text else None
        from_price = cheapest_price(text) if text else None
        print(f"  from=${from_price} 5trip/4room={comparable}")
        competitors.append(
            {
                "name": cfg["name"],
                "slug": cfg["slug"],
                "url": cfg["url"],
                "tier_names": cfg["tier_names"],
                "from": from_price,
                "comparable_5trip_4room": comparable,
                "scraped_at": now if text else None,
            }
        )

    out = out_dir / "competitor-pricing.json"
    out.write_text(json.dumps({"generated_at": now, "competitors": competitors}, indent=2))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
