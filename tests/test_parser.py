"""Tests for parsing logic. The HTML below is a SYNTHETIC fixture, not a copy of armeps.am."""
from datetime import datetime

from armeps_monitor import (YEREVAN_TZ, date_filter, find_date, find_page_links, has_cpv,
                             matches, months_ago, parse_tenders, DEADLINE_LABELS, PUBLISHED_LABELS)

BASE = "https://armeps.am/epps/quickSearchAction.do"
HTML = """
<table>
  <tr><th>Title</th><th>Authority</th><th>Deadline</th></tr>
  <tr><td><a href="/epps/cft/prepareViewCfTWS.do?resourceId=111">Համակարգիչների ձեռքբերում</a></td>
      <td>Ministry A</td><td>01/11/2026</td></tr>
  <tr><td><a href="/epps/cft/prepareViewCfTWS.do?resourceId=222">Road repair works</a></td>
      <td>Municipality B</td><td>05/11/2026</td></tr>
</table>
<a href="quickSearchAction.do?d-1-p=2">2</a> <a href="quickSearchAction.do?d-1-p=2">Next</a>
<a href="/epps/other.do">3</a>
"""


def test_parse_tenders():
    ts = parse_tenders(HTML, BASE)
    assert [t.resource_id for t in ts] == ["111", "222"]
    assert ts[0].url == "https://armeps.am/epps/cft/prepareViewCfTWS.do?resourceId=111"
    assert ts[0].columns["Authority"] == "Ministry A"


def test_matches_case_insensitive_and_exclude():
    ts = parse_tenders(HTML, BASE)
    assert matches(ts[0].text, ["համակարգիչ"], []) == ["համակարգիչ"]
    assert matches(ts[1].text, ["ROAD"], []) == ["ROAD"]
    assert matches(ts[1].text, ["road"], ["repair"]) == []


def test_page_links_same_action_only():
    links = find_page_links(HTML, BASE)
    assert set(links) == {"https://armeps.am/epps/quickSearchAction.do?d-1-p=2"}


def test_has_cpv_whole_code_only():
    assert has_cpv("CPV: 71351540-0 Geophysical", ["71351540"]) == ["71351540"]
    assert has_cpv("CPV 71351540", ["71351540"]) == ["71351540"]
    assert has_cpv("CPV 713515401", ["71351540"]) == []
    assert has_cpv("CPV 71351500-8", ["71351540"]) == []


def test_has_cpv_armeps_detail_format():
    # Format seen on armeps.am tender detail pages (resourceId=9308573).
    text = ("Procedure: Request for Quotations CPV Codes: 30211170-[en] մինիհամակարգչային սարքեր "
            "30211220-[en] սեղանի համակարգիչներ 71351540-[en] Inclusion of e-Auctions: No")
    assert has_cpv(text, ["71351540"]) == ["71351540"]
    assert has_cpv(text, ["30211170", "99999999"]) == ["30211170"]


# Excerpt of armeps.am detail-page text (resourceId=11084262, English interface).
DETAIL = ("Estimated value (AMD): Time-limit for receipt of tenders or requests to participate: "
          "26/03/2025 09:30 Deadline for dispatching invitations: Request for clarifications (from-to) : "
          "17/03/2025 09:58 - 21/03/2025 09:30 Tenders Opening Date: 26/03/2025 09:30 "
          "Date of Publication/Invitation : 17/03/2025 12:28 Date of Awarding:")


def at(s):
    return datetime.strptime(s, "%d/%m/%Y %H:%M").replace(tzinfo=YEREVAN_TZ)


def test_find_dates_real_format():
    assert find_date(DETAIL, DEADLINE_LABELS) == at("26/03/2025 09:30")
    assert find_date(DETAIL, PUBLISHED_LABELS) == at("17/03/2025 12:28")
    hy = "Ժամանակի սահմանափակում հայտերի ստացման կամ մասնակցության հայտերի համար: 05/11/2026 10:00"
    assert find_date(hy, DEADLINE_LABELS) == at("05/11/2026 10:00")


def test_date_filter():
    assert date_filter(DETAIL, at("20/03/2025 10:00"), 3) is None
    assert "deadline passed" in date_filter(DETAIL, at("26/03/2025 09:31"), 3)
    later = DETAIL.replace("26/03/2025 09:30 Deadline", "26/03/2026 09:30 Deadline")
    assert "older than 3 months" in date_filter(later, at("18/06/2025 00:00"), 3)
    assert date_filter(later, at("16/06/2025 00:00"), 3) is None
    assert date_filter("no dates here", at("01/01/2026 00:00"), 3) is None


def test_months_ago():
    assert months_ago(at("31/05/2026 10:00"), 3) == at("28/02/2026 10:00")
    assert months_ago(at("20/01/2026 10:00"), 3) == at("20/10/2025 10:00")
