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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup, Tag

DETAIL_PATH = "prepareViewCfTWS.do"
NEXT_LINK_TEXTS = {"next", "հաջորդ", "следующая", ">", ">>", "»", "›"}
USER_AGENT = "Mozilla/5.0 (compatible; armeps-monitor/1.0; +https://github.com/)"


@dataclass
class Tender:
    resource_id: str
    url: str
    text: str
    columns: dict[str, str] = field(default_factory=dict)
    detail_text: str = ""

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


class Client:
    def __init__(self, delay: float):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "hy,en;q=0.8,ru;q=0.6"})
        self.delay = delay

    def get(self, url: str) -> str:
        for attempt in range(4):
            try:
                resp = self.session.get(url, timeout=60)
                resp.raise_for_status()
                time.sleep(self.delay)
                return resp.text
            except requests.RequestException as exc:
                if attempt == 3:
                    raise
                wait = 2 ** (attempt + 1)
                print(f"  request failed ({exc}); retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
        raise AssertionError("unreachable")


def crawl(client: Client, start_url: str, max_pages: int, dump_dir: Path | None) -> list[Tender]:
    queue, visited, found = [start_url], set(), {}
    while queue and len(visited) < max_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        print(f"Fetching {url}")
        html = client.get(url)
        if dump_dir:
            dump_dir.mkdir(parents=True, exist_ok=True)
            (dump_dir / f"page_{len(visited)}.html").write_text(html, encoding="utf-8")
        page_tenders = parse_tenders(html, url)
        print(f"  {len(page_tenders)} tender link(s) on page")
        for t in page_tenders:
            found.setdefault(t.resource_id, t)
        queue += [u for u in find_page_links(html, url) if u not in visited]
    return list(found.values())


def load_seen(path: Path) -> set[str]:
    return set(json.loads(path.read_text())) if path.exists() else set()


def write_report(path: Path, items: list[tuple[Tender, list[str]]]) -> None:
    lines = [f"# New ARMEPS announcements ({len(items)})", ""]
    for t, kws in items:
        lines.append(f"- [{t.resource_id}]({t.url}) — {t.title}  ")
        lines.append(f"  _matched: {', '.join(kws)}_")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--seen", default="data/seen.json", help="IDs already reported")
    ap.add_argument("--report", default="new_announcements.md")
    ap.add_argument("--json", default="data/latest_matches.json")
    ap.add_argument("--dump-html", metavar="DIR", help="save fetched pages for debugging")
    ap.add_argument("--all", action="store_true", help="report matches even if seen before")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    keywords = [str(k) for k in cfg.get("keywords") or []]
    exclude = [str(k) for k in cfg.get("exclude_keywords") or []]
    client = Client(float(cfg.get("request_delay", 1.5)))
    dump = Path(args.dump_html) if args.dump_html else None

    tenders: dict[str, Tender] = {}
    for url in cfg["search_urls"]:
        for t in crawl(client, url, int(cfg.get("max_pages", 5)), dump):
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
        if kws:
            new.append((t, kws))

    print(f"New matching announcements: {len(new)}")
    for t, kws in new:
        print(f"  {t.resource_id}: {t.title} [{', '.join(kws)}]\n    {t.url}")

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
