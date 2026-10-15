"""External data: EIA, GDELT, Apify (async run, retry, cost), Serper (credits), URL normalization."""
import html
import io
import logging
import re
import time
import zipfile
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

import config
import cost_tracker
import pricing
import storage

log = logging.getLogger("data_sources")

# ---- Apify settings -------------------------------------------------------
APIFY_ACTOR = "topsail~compliant-commodity-intel"
APIFY_BASE = "https://api.apify.com/v2"
# Verify these input fields against the actor's input schema in the Apify console.
APIFY_SOURCE_CONFIG = {
    "sources": [
        {"url": "https://oilprice.com/", "commodity": "oil"},
        {"url": "https://www.worldoil.com/news/", "commodity": "oil"},
        {"url": "https://www.rigzone.com/news/", "commodity": "oil"},
    ],
    "maxItemsPerSource": 20,
}
APIFY_RETRY_ATTEMPTS = 3
APIFY_BACKOFF_SECONDS = 5
APIFY_RETRY_STATUSES = {429, 500, 502, 503, 504}
APIFY_RUN_TIME_LIMIT_S = 300
APIFY_POLL_SECONDS = 5

TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "cmpid", "igshid"}


class ApifyUnavailable(Exception):
    pass


class _Retryable(Exception):
    pass


def normalize_url(url):
    """Lowercase host, drop fragment, tracking parameters and trailing slash."""
    parts = urlsplit((url or "").strip())
    query = [(k, v) for k, v in parse_qsl(parts.query)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def domain_of(url):
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _article(url, title, published_at="", snippet="", source=None, **extra):
    url = normalize_url(url)
    return {"url": url, "title": (title or "").strip(), "published_at": published_at or "",
            "snippet": (snippet or "").strip(), "source": source or domain_of(url), **extra}


# ---- EIA ------------------------------------------------------------------
EIA_BASE = "https://api.eia.gov/v2"
# (route, series id). Verify against the live API; shapes change.
EIA_SERIES = {
    "brent": ("petroleum/pri/spt", "RBRTE", "daily"),
    "wti": ("petroleum/pri/spt", "RWTC", "daily"),
    "crude_stocks": ("petroleum/stoc/wstk", "WCESTUS1", "weekly"),
}


def _eia_series(route, series, frequency, length=10):
    params = {
        "api_key": config.get("EIA_API_KEY"), "frequency": frequency, "data[0]": "value",
        "facets[series][]": series, "sort[0][column]": "period", "sort[0][direction]": "desc", "length": length,
    }
    resp = requests.get(f"{EIA_BASE}/{route}/data/", params=params, timeout=30)
    resp.raise_for_status()
    rows = resp.json().get("response", {}).get("data", [])
    return [(r["period"], float(r["value"])) for r in rows if r.get("value") not in (None, "")]


def fetch_eia_data():
    """Return {name: [(period, value), ...]} newest first. Empty dict on failure (logged as degraded)."""
    data, errors = {}, []
    try:
        config.require("EIA_API_KEY")
        for name, (route, series, freq) in EIA_SERIES.items():
            try:
                data[name] = _eia_series(route, series, freq)
            except (requests.RequestException, KeyError, ValueError) as exc:
                errors.append(f"{name}: {exc}")
    except RuntimeError as exc:
        errors.append(str(exc))
    storage.log_job("fetch_eia_data", "degraded" if errors or not data else "success", "; ".join(errors))
    return data


# ---- GDELT ----------------------------------------------------------------
# The DOC API answers 429 persistently from some networks, so this reads GDELT's static 15-minute GKG files
# instead (no key, no rate limit). Each file is a few MB zipped; the last GDELT_FILES files cover one cycle
# plus overlap, and URL dedupe removes repeats.
GDELT_LASTUPDATE = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"
GDELT_FILE_URL = "http://data.gdeltproject.org/gdeltv2/{ts}.gkg.csv.zip"
GDELT_FILES = 2
GDELT_THEMES = re.compile(r"(^|;)(ECON_OILPRICE|ENV_OIL|ENV_NATURALGAS|ENV_GAS|WB_\d+_OIL[A-Z_]*|"
                          r"WB_\d+_NATURAL_GAS[A-Z_]*|ENERGY[A-Z_]*|WB_\d+_ENERGY[A-Z_]*)[,;]", re.I)
GDELT_KEYWORDS = re.compile(r"\b(oil|crude|opec\+?|brent|wti|lng|natural gas|refiner\w*|petroleum|diesel|"
                            r"gasoline|pipeline|drill\w*|offshore|shale|barrel|tanker|energy)\b", re.I)
GDELT_TITLE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.S)


def _parse_gkg(raw, ts):
    """GKG 2.0 columns used: 3 source domain, 4 url, 8 themes, 26 extras (holds the page title)."""
    out = []
    for line in raw.splitlines():
        cols = line.split("\t")
        if len(cols) < 27:
            continue
        title = GDELT_TITLE.search(cols[26])
        if not title or not GDELT_THEMES.search(cols[8] + ";"):
            continue
        title = html.unescape(title.group(1)).strip()
        if GDELT_KEYWORDS.search(title) and cols[4].startswith("http"):
            out.append(_article(cols[4], title, ts, source=cols[3] or None, origin="gdelt"))
    return out


def fetch_gdelt():
    """Oil and gas related stories from the latest GDELT GKG files. Logs 'degraded' if nothing could be read."""
    errors, articles = [], []
    try:
        resp = requests.get(GDELT_LASTUPDATE, timeout=30)
        resp.raise_for_status()
        latest = re.search(r"/(\d{14})\.gkg\.csv\.zip", resp.text).group(1)
    except (requests.RequestException, AttributeError) as exc:
        storage.log_job("fetch_gdelt", "degraded", f"lastupdate: {exc}")
        return []
    stamp = datetime.strptime(latest, "%Y%m%d%H%M%S")
    read = 0
    for n in range(GDELT_FILES):
        ts = (stamp - timedelta(minutes=15 * n)).strftime("%Y%m%d%H%M%S")
        try:
            z = requests.get(GDELT_FILE_URL.format(ts=ts), timeout=120)
            z.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
                raw = zf.read(zf.namelist()[0]).decode("utf-8", "replace")
            articles += _parse_gkg(raw, ts)
            read += 1
        except (requests.RequestException, zipfile.BadZipFile, KeyError, IndexError) as exc:
            errors.append(f"{ts}: {exc}")
    storage.log_job("fetch_gdelt", "success" if read else "degraded",
                    f"{len(articles)} items from {read} files" + (f"; {'; '.join(errors)}" if errors else ""))
    return articles


# ---- Apify ----------------------------------------------------------------

def _normalize_apify_items(items):
    """Map actor output to the common article shape. Field names are guesses: confirm in the console."""
    out = []
    for it in items:
        url = it.get("url") or it.get("link")
        title = it.get("title") or it.get("headline")
        if not url or not title:
            continue
        out.append(_article(url, title, it.get("publishedAt") or it.get("date") or it.get("published", ""),
                            it.get("summary") or it.get("description") or it.get("text", "")[:500],
                            source=domain_of(url), origin="apify",
                            byline=it.get("author") or it.get("byline", ""),
                            section=it.get("section") or it.get("category", "")))
    return out


def _apify_cost(run, n_results):
    usd = run.get("usageTotalUsd") if run else None
    if usd is None:
        return pricing.apify_fallback_cost(n_results), True
    return float(usd), False


def _run_apify_once():
    token = config.get("APIFY_TOKEN")
    try:
        resp = requests.post(f"{APIFY_BASE}/acts/{APIFY_ACTOR}/runs", params={"token": token},
                             json=APIFY_SOURCE_CONFIG, timeout=30)
    except requests.RequestException as exc:
        raise _Retryable(str(exc))
    if resp.status_code in APIFY_RETRY_STATUSES:
        raise _Retryable(f"start failed: HTTP {resp.status_code}")
    if not resp.ok:
        raise ApifyUnavailable(f"start failed: HTTP {resp.status_code} {resp.text[:200]}")
    run = resp.json()["data"]
    run_id = run["id"]

    deadline = time.monotonic() + APIFY_RUN_TIME_LIMIT_S
    while run.get("status") in ("READY", "RUNNING", "ABORTING"):
        if time.monotonic() > deadline:
            requests.post(f"{APIFY_BASE}/actor-runs/{run_id}/abort", params={"token": token}, timeout=30)
            time.sleep(APIFY_POLL_SECONDS)  # let usage settle
            run = _get_run(run_id, token) or run
            usd, est = _apify_cost(run, 0)
            cost_tracker.record_apify(run_id, usd, est, "ABORTED_TIMEOUT")
            raise _Retryable("run exceeded time limit and was aborted")
        time.sleep(APIFY_POLL_SECONDS)
        run = _get_run(run_id, token) or run

    if run.get("status") != "SUCCEEDED":
        usd, est = _apify_cost(run, 0)
        cost_tracker.record_apify(run_id, usd, est, run.get("status", "UNKNOWN"))
        raise _Retryable(f"run ended with status {run.get('status')}")

    ds = requests.get(f"{APIFY_BASE}/datasets/{run['defaultDatasetId']}/items",
                      params={"token": token, "format": "json"}, timeout=60)
    ds.raise_for_status()
    items = ds.json()
    usd, est = _apify_cost(run, len(items))
    cost_tracker.record_apify(run_id, usd, est, "SUCCEEDED")
    return items


def _get_run(run_id, token):
    try:
        r = requests.get(f"{APIFY_BASE}/actor-runs/{run_id}", params={"token": token}, timeout=30)
        return r.json()["data"] if r.ok else None
    except (requests.RequestException, ValueError, KeyError):
        return None


def fetch_apify_commodity_news():
    """Run the actor with retry and backoff. Returns [] and logs 'degraded' if it stays unavailable."""
    try:
        config.require("APIFY_TOKEN")
    except RuntimeError as exc:
        storage.log_job("fetch_apify_commodity_news", "degraded", str(exc))
        return []
    last_error = ""
    for attempt in range(APIFY_RETRY_ATTEMPTS):
        try:
            items = _normalize_apify_items(_run_apify_once())
            storage.log_job("fetch_apify_commodity_news", "success", f"{len(items)} items")
            return items
        except _Retryable as exc:
            last_error = str(exc)
            log.warning("Apify attempt %d failed: %s", attempt + 1, exc)
            if attempt < APIFY_RETRY_ATTEMPTS - 1:
                time.sleep(APIFY_BACKOFF_SECONDS * 2 ** attempt)
        except (ApifyUnavailable, requests.RequestException, ValueError, KeyError) as exc:
            last_error = str(exc)
            break
    storage.log_job("fetch_apify_commodity_news", "degraded", last_error)
    return []


# ---- Serper ---------------------------------------------------------------

def serper_news(query, num=10):
    """News search. Keep num <= 10 (more costs 2 credits). Returns result list; raises on failure."""
    config.require("SERPER_API_KEY")
    resp = requests.post("https://google.serper.dev/news",
                         headers={"X-API-KEY": config.get("SERPER_API_KEY")},
                         json={"q": query, "num": num}, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    credits = body.get("credits") or (1 if num <= 10 else 2)
    cost_tracker.record_serper(credits)
    return body.get("news", [])
