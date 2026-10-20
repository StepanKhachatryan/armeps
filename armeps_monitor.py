#!/usr/bin/env python3
"""Monitor ARMEPS (armeps.am) for procurement announcements matching keywords.

Fetches the search/results page(s), extracts every link to a Call-for-Tender
workspace (``/epps/cft/prepareViewCfTWS.do?resourceId=...``), filters them by
keywords from ``config.yaml`` and reports only those not seen before.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup, Tag

HOME_URL = "https://armeps.am/epps/home.do"
QUICK_SEARCH_URL = "https://armeps.am/epps/quickSearchAction.do"
# Advanced CfT search form (prepareAdvancedSearch.do?type=cft, form "searchCfTWorkspaceForm").
ADVANCED_SEARCH_URL = "https://armeps.am/epps/viewCFTSAction.do"
# Options of the home page's quick-search form: 1 = "ՄՀ-ներ" (calls for tender), 2 = organisation.
SEARCH_SELECT_CFT = "1"
DETAIL_PATH = "prepareViewCfTWS.do"
# Switches the session's interface language to English, so detail-page labels are predictable.
ENGLISH_URL = "https://armeps.am/epps/changeLanguage.do?language=en&url="
# Detail-page labels (English interface, plus the Armenian deadline label as fallback).
DEADLINE_LABELS = [
    "Time-limit for receipt of tenders or requests to participate",
    "Ժամանակի սահմանափակում հայտերի ստացման կամ մասնակցության հայտերի համար",
]
PUBLISHED_LABELS = ["Date of Publication/Invitation"]
YEREVAN_TZ = timezone(timedelta(hours=4))  # Armenia: UTC+4, no daylight saving time
NEXT_LINK_TEXTS = {"next", "հաջորդ", "следующая", ">", ">>", "»", "›"}
USER_AGENT = "Mozilla/5.0 (compatible; armeps-monitor/1.0; +https://github.com/)"


@dataclass
class Tender:
    resource_id: str
    url: str
    text: str
    columns: dict[str, str] = field(default_factory=dict)
    detail_text: str = ""
    deadline: str = ""
    published: str = ""

    @property
    def title(self) -> str:
        return self.text[:200]


def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def resource_id_of(url: str) -> str | None:
    values = parse_qs(urlparse(url).query).get("resourceId")
    return values[0] if values else None


def header_for(table: Tag | None) -> list[str]:
    if table is None:
        return []
    for tr in table.find_all("tr"):
        ths = tr.find_all("th")
        if ths:
            return [normalize(th.get_text(" ")) for th in ths]
    return []


def parse_tenders(html: str, base_url: str) -> list[Tender]:
    """Extract tenders from a results page by locating links to CfT workspaces."""
    soup = BeautifulSoup(html, "html.parser")
    tenders: dict[str, Tender] = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"])
        if DETAIL_PATH not in url:
            continue
        rid = resource_id_of(url)
        if not rid or rid in tenders:
            continue
        row = a.find_parent("tr")
        if row is not None:
            cells = [normalize(td.get_text(" ")) for td in row.find_all(["td", "th"], recursive=False)]
            headers = header_for(row.find_parent("table"))
            columns = dict(zip(headers, cells)) if len(headers) == len(cells) else {}
            text = " | ".join(c for c in cells if c)
        else:
            columns, text = {}, normalize(a.get_text(" "))
        tenders[rid] = Tender(resource_id=rid, url=url, text=text, columns=columns)
    return list(tenders.values())


def find_page_links(html: str, base_url: str) -> list[str]:
    """Return pagination links (numbered or 'next') pointing back at the same action."""
    soup = BeautifulSoup(html, "html.parser")
    path = urlparse(base_url).path
    links = []
    for a in soup.find_all("a", href=True):
        label = normalize(a.get_text(" ")).lower()
        url = urljoin(base_url, a["href"])
        if urlparse(url).path != path:
            continue
        if label.isdigit() or label in NEXT_LINK_TEXTS:
            links.append(url)
    return links


def matches(text: str, keywords: list[str], exclude: list[str]) -> list[str]:
    folded = text.casefold()
    if any(x.casefold() in folded for x in exclude):
        return []
    return [k for k in keywords if k.casefold() in folded]


def find_date(text: str, labels: list[str]) -> datetime | None:
    """Return the 'dd/mm/yyyy [HH:MM]' date that follows any of `labels`, in Yerevan time."""
    for label in labels:
        m = re.search(re.escape(label) + r"\s*:?\s*(\d{2}/\d{2}/\d{4})(?:\s+(\d{2}:\d{2}))?", text)
        if m:
            return datetime.strptime(f"{m[1]} {m[2] or '23:59'}", "%d/%m/%Y %H:%M").replace(tzinfo=YEREVAN_TZ)
    return None


def months_ago(now: datetime, months: int) -> datetime:
    """`now` shifted back by calendar months (day clamped to the month's length)."""
    y, m = divmod(now.year * 12 + now.month - 1 - months, 12)
    m += 1
    days = [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return now.replace(year=y, month=m, day=min(now.day, days[m - 1]))


def date_filter(text: str, now: datetime, max_age_months: int | None) -> str | None:
    """Return why the tender should be skipped based on its dates, or None to keep it."""
    deadline = find_date(text, DEADLINE_LABELS)
    if deadline and deadline < now:
        return f"deadline passed ({deadline:%d/%m/%Y %H:%M})"
    published = find_date(text, PUBLISHED_LABELS)
    if max_age_months and published and published < months_ago(now, max_age_months):
        return f"published {published:%d/%m/%Y}, older than {max_age_months} months"
    return None


def has_cpv(text: str, codes: list[str]) -> list[str]:
    """Return the CPV codes (8-digit, optionally with '-N' check digit) present in text."""
    return [c for c in codes if re.search(rf"(?<!\d){re.escape(c)}(?!\d)", text)]


class Client:
    def __init__(self, delay: float):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "hy,en;q=0.8,ru;q=0.6"})
        self.delay = delay

    def get(self, url: str, data: dict | None = None) -> str:
        """GET `url`, or POST `data` to it when given."""
        for attempt in range(4):
            try:
                if data is None:
                    resp = self.session.get(url, timeout=60)
                else:
                    resp = self.session.post(url, data=data, timeout=60)
                resp.raise_for_status()
                print(f"  HTTP {resp.status_code}, {len(resp.text)} chars, final URL {resp.url}")
                time.sleep(self.delay)
                return resp.text
            except requests.RequestException as exc:
                if attempt == 3:
                    raise
                wait = 2 ** (attempt + 1)
                print(f"  request failed ({exc}); retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
        raise AssertionError("unreachable")


def crawl(client: Client, start_url: str, max_pages: int, dump_dir: Path | None,
          form: dict | None = None) -> list[Tender]:
    """Fetch `start_url` (POSTing `form` if given), then follow pagination links."""
    queue, visited, found = [start_url], set(), {}
    while queue and len(visited) < max_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        first = len(visited) == 1
        print(f"Fetching {url}" + (f" with {form}" if form and first else ""))
        html = client.get(url, form if first else None)
        if dump_dir:
            dump_dir.mkdir(parents=True, exist_ok=True)
            (dump_dir / f"page_{len(visited)}.html").write_text(html, encoding="utf-8")
        page_tenders = parse_tenders(html, url)
        print(f"  {len(page_tenders)} tender link(s) on page")
        for t in page_tenders[:3]:
            print(f"    e.g. {t.resource_id}: {t.text[:160]}")
        if not page_tenders:
            print("  --- page content (first 4000 chars) ---")
            print(html[:4000])
            print("  --- end of page content ---")
        for t in page_tenders:
            found.setdefault(t.resource_id, t)
        queue += [u for u in find_page_links(html, url) if u not in visited]
    return list(found.values())


def inspect(client: Client, url: str) -> None:
    """Print the forms (action, method, fields) and procurement-related links on a page."""
    print(f"===== INSPECT {url}")
    try:
        html = client.get(url)
    except requests.RequestException as exc:
        print(f"  error: {exc}")
        return
    soup = BeautifulSoup(html, "html.parser")
    print(f"  title: {normalize(soup.title.get_text()) if soup.title else ''}")
    tenders = parse_tenders(html, url)
    print(f"  tender links: {len(tenders)}")
    for t in tenders[:5]:
        print(f"    {t.resource_id}: {t.text[:200]}")
    text = normalize(soup.get_text(" "))
    for m in re.finditer(r"CPV", text):
        print(f"  CPV context: ...{text[max(0, m.start() - 80):m.end() + 150]}...")
    if os.environ.get("INSPECT_FULL_TEXT"):
        main = text[text.find("CPV") - 1500:] if "CPV" in text else text
        print(f"  FULL TEXT: {main[:5000]}")
    for form in soup.find_all("form"):
        print(f"  FORM action={form.get('action')!r} method={form.get('method')!r} name={form.get('name')!r}")
        for el in form.find_all(["input", "select", "textarea"]):
            line = f"    {el.name} name={el.get('name')!r} type={el.get('type')!r} value={el.get('value')!r}"
            if el.name == "select":
                opts = [(o.get("value"), normalize(o.get_text())) for o in el.find_all("option")][:15]
                line += f" options={opts}"
            print(line)
    seen = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        if ".do" in href and href not in seen:
            seen.add(href)
            print(f"  LINK {normalize(a.get_text(' '))[:80]!r} -> {href}")


def load_seen(path: Path) -> set[str]:
    return set(json.loads(path.read_text())) if path.exists() else set()


def write_report(path: Path, items: list[tuple[Tender, list[str]]]) -> None:
    lines = [f"# New ARMEPS announcements ({len(items)})", ""]
    for t, kws in items:
        lines.append(f"- [{t.resource_id}]({t.url}) — {t.title}  ")
        lines.append(f"  published: {t.published} · deadline: **{t.deadline}** · _matched: {', '.join(kws)}_")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--seen", default="data/seen.json", help="IDs already reported")
    ap.add_argument("--report", default="new_announcements.md")
    ap.add_argument("--json", default="data/latest_matches.json")
    ap.add_argument("--dump-html", metavar="DIR", help="save fetched pages for debugging")
    ap.add_argument("--all", action="store_true", help="report matches even if seen before")
    ap.add_argument("--inspect", nargs="+", metavar="URL", help="print forms and links of pages, then exit")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    keywords = [str(k) for k in cfg.get("keywords") or []]
    exclude = [str(k) for k in cfg.get("exclude_keywords") or []]
    exclude_cpv = [str(c).split("-")[0].strip() for c in cfg.get("exclude_cpv") or []]
    max_age = cfg.get("max_age_months")
    now = datetime.now(YEREVAN_TZ)
    client = Client(float(cfg.get("request_delay", 1.5)))
    dump = Path(args.dump_html) if args.dump_html else None
    if args.inspect:
        client.get(HOME_URL)
        for url in args.inspect:
            inspect(client, url)
        return 0

    max_pages = int(cfg.get("max_pages", 5))
    tenders: dict[str, Tender] = {}
    # Tenders returned by the site's own keyword search, mapped to the keywords that found them.
    found_by: dict[str, set[str]] = {}
    if (adv := cfg.get("advanced_search")) is not None:
        client.get(HOME_URL)
        client.get(ENGLISH_URL)
        form = {k: "" if v is None else str(v) for k, v in adv.items()}
        for t in crawl(client, ADVANCED_SEARCH_URL, max_pages, dump, form):
            tenders.setdefault(t.resource_id, t)
    if cfg.get("quick_search", False):
        client.get(HOME_URL)  # establishes the session cookie the search form expects
        client.get(ENGLISH_URL)
        for kw in keywords:
            form = {"searchSelect": SEARCH_SELECT_CFT, "searchInput": kw}
            for t in crawl(client, QUICK_SEARCH_URL, max_pages, dump, form):
                tenders.setdefault(t.resource_id, t)
                found_by.setdefault(t.resource_id, set()).add(kw)
    for url in cfg.get("search_urls") or []:
        for t in crawl(client, url, max_pages, dump):
            tenders.setdefault(t.resource_id, t)
    print(f"Total distinct tenders found: {len(tenders)}")
    if not tenders:
        print("WARNING: no tender links found; the page layout may have changed. "
              "Run with --dump-html to inspect.", file=sys.stderr)

    seen_path = Path(args.seen)
    seen = load_seen(seen_path)
    new: list[tuple[Tender, list[str]]] = []
    for t in tenders.values():
        if t.resource_id in seen and not args.all:
            continue
        haystack = t.text
        if cfg.get("fetch_details"):
            t.detail_text = normalize(BeautifulSoup(client.get(t.url), "html.parser").get_text(" "))
            haystack += " " + t.detail_text
        kws = matches(haystack, keywords, exclude)
        excluded = any(x.casefold() in haystack.casefold() for x in exclude)
        if not kws and t.resource_id in found_by and not excluded:
            kws = sorted(found_by[t.resource_id])
        if not kws:
            continue
        # CPV codes and dates are only on the detail page.
        if not t.detail_text:
            t.detail_text = normalize(BeautifulSoup(client.get(t.url), "html.parser").get_text(" "))
        if bad := has_cpv(f"{t.text} {t.detail_text}", exclude_cpv):
            print(f"  skipping {t.resource_id}: excluded CPV {', '.join(bad)}")
            continue
        if reason := date_filter(t.detail_text, now, max_age):
            print(f"  skipping {t.resource_id}: {reason}")
            continue
        d = find_date(t.detail_text, DEADLINE_LABELS)
        pub = find_date(t.detail_text, PUBLISHED_LABELS)
        t.deadline = f"{d:%d/%m/%Y %H:%M}" if d else "not found"
        t.published = f"{pub:%d/%m/%Y %H:%M}" if pub else "not found"
        if kws:
            new.append((t, kws))

    print(f"New matching announcements: {len(new)}")
    for t, kws in new:
        print(f"  {t.resource_id}: {t.title} [{', '.join(kws)}] deadline {t.deadline}\n    {t.url}")

    Path(args.json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json).write_text(json.dumps(
        [dict(asdict(t), matched=kws) for t, kws in new], ensure_ascii=False, indent=2), encoding="utf-8")
    if new:
        write_report(Path(args.report), new)
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    seen_path.write_text(json.dumps(sorted(seen | {t.resource_id for t, _ in new}), indent=0))

    if gh_out := os.environ.get("GITHUB_OUTPUT"):
        with open(gh_out, "a") as fh:
            fh.write(f"new_count={len(new)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
