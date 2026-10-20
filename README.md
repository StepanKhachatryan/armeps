# ARMEPS procurement monitor

Checks [armeps.am](https://armeps.am/epps/quickSearchAction.do) on a schedule and reports
procurement announcements that match your keywords. It only reports each one once.

## How it works
1. Runs the site's own quick search (the home-page form: `POST /epps/quickSearchAction.do` with
   `searchSelect=1` "CfTs" and `searchInput=<keyword>`) once per keyword, following pagination
   up to `max_pages`. Any extra URLs in `search_urls` are fetched too.
2. Collects every link to a tender workspace (`/epps/cft/prepareViewCfTWS.do?resourceId=…`) along with the text of its table row.
3. Keeps the tenders whose text contains any of your `keywords` and none of your `exclude_keywords`.
   With `fetch_details: true`, it also matches against each tender's full detail page.
4. Drops tenders whose detail page lists a CPV code from `exclude_cpv`
   (the detail page shows them as `CPV Codes: 30211220-[en] …`).
5. Adds the IDs it has reported to `data/seen.json`, so the next run reports only new tenders.

## Setup
1. Edit `config.yaml`. Put keywords in Armenian, plus English/Russian if needed.
   To track a narrower search, run it on armeps.am in your browser and paste the resulting
   URL into `search_urls`. That only works if the search is a GET request, meaning the
   filters show up in the address bar.
2. Push to GitHub. The workflow in `.github/workflows/armeps.yml` runs twice a day.
   You can also start it by hand from **Actions → ARMEPS monitor → Run workflow**.
3. When it finds new matches, it opens a GitHub **Issue**. GitHub emails you about the
   issue if you watch the repository.

## Run locally
```bash
pip install -r requirements.txt
python armeps_monitor.py --dump-html debug_html   # --all re-reports previously seen matches
python -m pytest -q tests
```

## Inspecting pages
Start the workflow with the `inspect` input set to one or more URLs. It prints each page's
forms, links and the text around "CPV" instead of running a check.

## Troubleshooting
If a run finds `0 tender link(s)`, check the `armeps-html` artifact on the workflow run
(or `debug_html/` locally) to see what the site returned. The page layout or URL may have
changed, or the search may need parameters.
