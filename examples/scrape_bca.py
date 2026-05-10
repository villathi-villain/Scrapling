"""Scrape the BCA Directory across every contractor category.

Two endpoint families:
  - GetCRSCompaniesByGrade  (params: workhead, title, grade)
  - GetBLSCompaniesByGrade  (params: classCD, description)

Pagination is session-stateful — `?page=N` paginates whatever filter the
session most recently set, so we use a fresh requests.Session() per group.

For CRS we prefer `grade=All` (union of all tiers for that workhead) when it
exists, falling back to the only available grade. BLS classes are scraped
individually since each classCD is its own group.
"""

import csv
import json
import re
import time
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://www.bca.gov.sg"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

CATEGORIES = [
    ("registered-contractors", "construction"),
    ("registered-contractors", "construction-related"),
    ("registered-contractors", "mechanical-electrical"),
    ("registered-contractors", "regulatory"),
    ("registered-contractors", "trade"),
    ("licensed-builders", "general-builder"),
    ("licensed-builders", "specialist-builder"),
    ("fm-registry", "facilities-management"),
    ("fm-registry", "housekeeping-cleansing-desilting-conservancy-service"),
    ("fm-registry", "landscaping"),
    ("fm-registry", "pest-control"),
    ("suppliers-registry", "supply"),
]


def index_url(contractor_type: str, trade_type: str) -> str:
    return (
        f"{BASE}/eBACS/BCA_DIRECTORY/Filter/FilterWorkHeadType"
        f"?contractorType={contractor_type}&tradeType={trade_type}"
    )


def list_groups(contractor_type: str, trade_type: str) -> list[dict]:
    """Parse a category index and return one listing URL per group.

    For CRS pages, a "group" is a workhead — we prefer `grade=All`, else
    the only grade available. For BLS pages, a "group" is a classCD.
    """
    r = requests.get(index_url(contractor_type, trade_type),
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    text = r.text
    crs_links = re.findall(
        r'href="(/eBACS/BCA_DIRECTORY/Filter/GetCRSCompaniesByGrade\?[^"]+)"',
        text,
    )
    bls_links = re.findall(
        r'href="(/eBACS/BCA_DIRECTORY/Filter/GetBLSCompaniesByGrade\?[^"]+)"',
        text,
    )

    groups: list[dict] = []

    # CRS: group by workhead; prefer grade=All
    by_workhead: dict[str, list[dict]] = {}
    for href in dict.fromkeys(crs_links):
        href = unescape(href)
        q = parse_qs(urlparse(href).query)
        workhead = q.get("workhead", [""])[0]
        title = q.get("title", [""])[0]
        grade = q.get("grade", [""])[0]
        by_workhead.setdefault(workhead, []).append({
            "workhead": workhead,
            "title": title,
            "grade": grade,
            "url": BASE + href,
        })
    for workhead, items in by_workhead.items():
        chosen = next((i for i in items if i["grade"] == "All"), items[0])
        chosen["kind"] = "CRS"
        chosen["contractor_type"] = contractor_type
        chosen["trade_type"] = trade_type
        groups.append(chosen)

    # BLS: each classCD is its own group
    seen_class = set()
    for href in dict.fromkeys(bls_links):
        href = unescape(href)
        q = parse_qs(urlparse(href).query)
        class_cd = q.get("classCD", [""])[0]
        desc = q.get("description", [""])[0]
        if class_cd in seen_class:
            continue
        seen_class.add(class_cd)
        groups.append({
            "kind": "BLS",
            "contractor_type": contractor_type,
            "trade_type": trade_type,
            "workhead": class_cd,
            "title": desc,
            "grade": "",
            "url": BASE + href,
        })

    return groups


def parse_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = []
    for sec in soup.select("section.company-card-wrapper .company-card"):
        title = sec.select_one("a.title")
        uen = sec.select_one(".uen")
        addr = sec.select_one(".address")
        tel = sec.select_one(".tel")
        fax = sec.select_one(".fax")

        def clean(el):
            if not el:
                return ""
            return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()

        uen_val = clean(uen)
        if uen_val.lower().startswith("uen:"):
            uen_val = uen_val[4:].strip()

        tel_link = sec.select_one('.tel a[href^="tel:"]')
        tel_val = (
            tel_link.get_text(strip=True)
            if tel_link
            else clean(tel).replace("NIL", "").strip()
        )

        fax_val = clean(fax)
        if fax_val.upper() == "NIL":
            fax_val = ""

        detail_href = title.get("href") if title and title.has_attr("href") else ""
        cards.append({
            "name": title.get_text(strip=True) if title else "",
            "uen": uen_val,
            "address": clean(addr),
            "tel": tel_val,
            "fax": fax_val,
            "detail_url": (BASE + detail_href) if detail_href else "",
        })
    return cards


def parse_last_page(html: str) -> int:
    pages = [1]
    for m in re.finditer(r"page=(\d+)", html):
        pages.append(int(m.group(1)))
    return max(pages)


def page_url_for(kind: str, page: int) -> str:
    if kind == "BLS":
        return (
            f"{BASE}/eBACS/BCA_DIRECTORY/Filter/GetBLSCompaniesByGrade?page={page}"
        )
    return (
        f"{BASE}/eBACS/BCA_DIRECTORY/Filter/GetCRSCompaniesByGrade?page={page}"
    )


def log(msg: str):
    print(msg, flush=True)


def scrape_group(g: dict, partial_dir: Path) -> list[dict]:
    label = f"[{g['contractor_type']}/{g['trade_type']}] {g['workhead']} {g['title']}"
    if g.get("grade"):
        label += f" ({g['grade']})"

    # Skip if already cached (resume support)
    safe = (
        f"{g['contractor_type']}__{g['trade_type']}__{g['workhead']}"
        .replace("/", "_").replace(" ", "_")
    )
    cache = partial_dir / f"{safe}.json"
    if cache.exists():
        try:
            cached = json.loads(cache.read_text())
            log(f"  {label}  CACHED ({len(cached)} rows)")
            return cached
        except Exception:
            pass

    log(f"  {label}")
    s = requests.Session()
    s.headers.update(HEADERS)
    r = s.get(g["url"], timeout=30)
    r.raise_for_status()
    last = parse_last_page(r.text)
    rows = parse_page(r.text)
    log(f"    page 1/{last}  +{len(rows)}")
    for p in range(2, last + 1):
        rp = s.get(page_url_for(g["kind"], p), timeout=30)
        rp.raise_for_status()
        rows.extend(parse_page(rp.text))
        if p % 25 == 0 or p == last:
            log(f"    page {p}/{last}  total {len(rows)}")
        time.sleep(0.1)
    for c in rows:
        c["contractor_type"] = g["contractor_type"]
        c["trade_type"] = g["trade_type"]
        c["workhead"] = g["workhead"]
        c["workhead_title"] = g["title"]
        c["grade"] = g.get("grade", "")
    cache.write_text(json.dumps(rows, ensure_ascii=False))
    return rows


def main():
    out_dir = Path(__file__).parent / "bca_output"
    out_dir.mkdir(exist_ok=True)
    partial_dir = out_dir / "partials"
    partial_dir.mkdir(exist_ok=True)

    log("Discovering groups across all categories…")
    groups: list[dict] = []
    for ct, tt in CATEGORIES:
        gs = list_groups(ct, tt)
        log(f"  {ct}/{tt}: {len(gs)} group(s)")
        groups.extend(gs)
    log(f"Total groups: {len(groups)}")

    all_rows: list[dict] = []
    for i, g in enumerate(groups, 1):
        log(f"[{i}/{len(groups)}]")
        try:
            all_rows.extend(scrape_group(g, partial_dir))
        except Exception as e:
            log(f"    !! failed: {e}")

    (out_dir / "contractors_full.json").write_text(
        json.dumps(all_rows, indent=2, ensure_ascii=False)
    )

    csv_path = out_dir / "contractors.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "name", "uen", "address", "tel", "fax",
            "contractor_type", "trade_type",
            "workhead", "workhead_title", "grade", "detail_url",
        ])
        for c in all_rows:
            w.writerow([
                c["name"], c["uen"], c["address"], c["tel"], c["fax"],
                c["contractor_type"], c["trade_type"],
                c["workhead"], c["workhead_title"], c["grade"], c["detail_url"],
            ])

    # Unique-by-UEN flat company list
    uniq: dict[str, dict] = {}
    for c in all_rows:
        if not c["uen"]:
            continue
        if c["uen"] not in uniq:
            uniq[c["uen"]] = {
                "name": c["name"],
                "uen": c["uen"],
                "address": c["address"],
                "tel": c["tel"],
                "fax": c["fax"],
                "detail_url": c["detail_url"],
                "categories": [],
            }
        uniq[c["uen"]]["categories"].append(
            f"{c['contractor_type']}/{c['trade_type']}/{c['workhead']}"
            + (f"|{c['grade']}" if c["grade"] else "")
        )

    uniq_csv = out_dir / "contractors_unique.csv"
    with uniq_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "name", "uen", "address", "tel", "fax",
            "category_count", "categories", "detail_url",
        ])
        for c in uniq.values():
            w.writerow([
                c["name"], c["uen"], c["address"], c["tel"], c["fax"],
                len(c["categories"]),
                " | ".join(c["categories"]),
                c["detail_url"],
            ])

    print(
        f"\nWrote {csv_path}  ({len(all_rows)} listing rows)"
        f"\nWrote {uniq_csv}  ({len(uniq)} unique companies)"
        f"\nWrote {out_dir/'contractors_full.json'}"
    )


if __name__ == "__main__":
    main()
