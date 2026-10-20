"""Tests for parsing logic. The HTML below is a SYNTHETIC fixture, not a copy of armeps.am."""
from armeps_monitor import find_page_links, has_cpv, matches, parse_tenders

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
