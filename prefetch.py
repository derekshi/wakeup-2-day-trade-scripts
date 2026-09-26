#!/usr/bin/env python3
"""Parallel data prefetch for runner-quick-analysis.

Usage: python3 prefetch.py TICKER [--no-chart] [--no-docs]

Fetches in parallel (threads, per-request timeouts + retries) and writes
/workspace/runner-cache/TICKER-<YYYY-MM-DD>/ (box-local date) containing
summary.json plus the raw files:
  sa_overview.json / sa_history.json  StockAnalysis.com quote + daily OHLCV (primary)
  finviz.html                          Finviz quote page (float, short float, rel vol, news)
  nasdaq_short_interest.json / finra_short_interest.json / sa_statistics.json
                                       short interest with settlement dates; split history
  yahoo_5m.json                        Yahoo v8 5-minute bars, last 2 sessions (optional)
  sec_submissions.json / sec_companyfacts.json, docs/*.txt (stripped filings)
  daily_bars.json                      bars handed to chart.py
SEC cache: /workspace/runner-cache/sec-json/<CIK>/ (15-minute TTL) and
           /workspace/runner-cache/sec-docs/<CIK>/<accession>/ (immutable filings)
Reference cache: /workspace/runner-cache/ref/company_tickers*.json (refreshed >24h).
Every summary value is {"value", "source", "as_of"[, "note"|"error"]}; missing
data is null with an error note. Nothing is estimated or invented here.
Prints the summary.json path on stdout; progress on stderr.
"""
import concurrent.futures as cf
import gzip, html, json, os, re, shutil, subprocess, sys, threading, time, random, zlib
import urllib.request, urllib.error
from datetime import datetime, date, timedelta, timezone
from html.parser import HTMLParser

CACHE = "/workspace/runner-cache"
REF = os.path.join(CACHE, "ref")
SEC_UA = os.environ.get("SEC_USER_AGENT") or "RunnerAnalyst research contact@example.com"  # set SEC_USER_AGENT to "Name/Org your@email"
SEC_JSON_CACHE = os.path.join(CACHE, "sec-json")
SEC_DOC_CACHE = os.path.join(CACHE, "sec-docs")
SEC_JSON_TTL = 15 * 60
BUA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
HERE = os.path.dirname(os.path.abspath(__file__))
DOC_FORMS = re.compile(r"^(8-K|6-K|424B\d|S-1|S-3|F-1|F-3|S-4|F-4|10-Q|10-K|20-F|425|"
                       r"PREM14A|DEFM14A|PRER14A|DEF 14A|PRE 14A|PRE 14C|DEF 14C|EFFECT|SC 13D)")
FIN_FORMS = re.compile(r"^(S-1|S-3|F-1|F-3|424B|EFFECT|S-4|F-4)")
MAX_DOCS = 12
DOC_CHARS = 250_000
T0 = time.time()


def log(msg):
    print(f"[prefetch {time.time()-T0:5.1f}s] {msg}", file=sys.stderr, flush=True)


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def F(value, source, as_of=None, note=None, error=None):
    d = {"value": value, "source": source, "as_of": as_of}
    if note: d["note"] = note
    if error: d["error"] = error
    return d


def MISSING(source, error):
    return F(None, source, None, error=error)


# ---------------------------------------------------------------- HTTP
_sec_lock = threading.Lock()
_sec_last = [0.0]


def http_get(url, ua=BUA, timeout=12, retries=2, accept="*/*", sec=False):
    """GET with gzip, timeout and retries. Returns (bytes, final_url)."""
    err = None
    for i in range(retries + 1):
        if sec:  # stay well under SEC's 10 req/s
            with _sec_lock:
                w = 0.12 - (time.time() - _sec_last[0])
                if w > 0: time.sleep(w)
                _sec_last[0] = time.time()
        req = urllib.request.Request(url, headers={
            "User-Agent": ua, "Accept": accept, "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "en-US,en;q=0.9"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                b = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
                if enc == "gzip" or b[:2] == b"\x1f\x8b": b = gzip.decompress(b)
                elif enc == "deflate": b = zlib.decompress(b)
                return b, r.geturl()
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
            if e.code in (400, 401, 403, 404, 410): break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        if i < retries: time.sleep(0.6 * (2 ** i) + random.uniform(0, 0.3))
    raise RuntimeError(f"{err} for {url}")


def get_json(url, **kw):
    b, _ = http_get(url, accept="application/json,*/*", **kw)
    return json.loads(b.decode("utf-8"))


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(data, (dict, list)): json.dump(data, f, indent=1, default=str)
        else: f.write(data)


# ---------------------------------------------------------------- helpers
def num(s):
    if s is None: return None
    s = str(s).replace(",", "").replace("$", "").replace("%", "").strip()
    if not s or s in ("-", "n/a", "N/A"): return None
    m = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
    try:
        return float(s[:-1]) * m[s[-1].upper()] if s[-1].upper() in m else float(s)
    except ValueError:
        return None


class _Strip(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.out = []; self.skip = 0
    def handle_starttag(self, tag, a):
        if tag in ("script", "style", "head"): self.skip += 1
        elif tag in ("p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table"): self.out.append("\n")
        elif tag in ("td", "th"): self.out.append(" | ")
    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self.skip: self.skip -= 1
    def handle_data(self, d):
        if not self.skip: self.out.append(d.replace("\r", " ").replace("\n", " "))


def strip_html(raw):
    raw = re.sub(r"(?is)<ix:header>.*?</ix:header>", " ", raw)  # iXBRL hidden facts
    p = _Strip()
    try:
        p.feed(raw); p.close(); t = "".join(p.out)
    except Exception:
        t = re.sub(r"<[^>]+>", " ", raw)
    t = t.replace("\xa0", " ")
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n(?:[ |]*\n)+", "\n\n", t)
    return t.strip()


def devalue(arr):
    """Decode a SvelteKit/devalue flat array (StockAnalysis __data.json nodes)."""
    cache = {}
    specials = {-1: None, -2: None, -3: None, -4: None, -5: None, -6: 0.0}
    def r(i):
        if i in specials: return specials[i]
        if i in cache: return cache[i]
        v = arr[i]
        if isinstance(v, dict):
            o = {}; cache[i] = o
            for k, j in v.items(): o[k] = r(j)
            return o
        if isinstance(v, list):
            if v and isinstance(v[0], str) and v[0] in ("Date", "Set", "Map", "BigInt", "RegExp", "Object", "null"):
                return v[1] if len(v) > 1 else None
            o = []; cache[i] = o; o.extend(r(j) for j in v); return o
        cache[i] = v; return v
    return r(0)


def sa_data(path):
    """Fetch stockanalysis.com/<path>/__data.json, following SvelteKit redirects
    (e.g. /stocks/x/ -> /etf/x/ or /quote/otc/X/). Returns (merged dict, page url)."""
    url = f"https://stockanalysis.com/{path.strip('/')}/__data.json"
    for _ in range(3):
        d = get_json(url, timeout=10)
        if d.get("type") == "redirect":
            url = "https://stockanalysis.com" + d["location"].rstrip("/") + "/__data.json"
            continue
        merged = {}
        for n in d.get("nodes") or []:
            if n and n.get("type") == "data" and n.get("data"):
                o = devalue(n["data"])
                if isinstance(o, dict): merged.update(o)
        return merged, url.replace("__data.json", "")
    raise RuntimeError("too many redirects")


# ---------------------------------------------------------------- SEC
def ref_file(name, max_age=86400):
    os.makedirs(REF, exist_ok=True)
    p = os.path.join(REF, name)
    fresh = os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age
    if not fresh:
        try:
            b, _ = http_get(f"https://www.sec.gov/files/{name}", ua=SEC_UA, sec=True, timeout=15)
            json.loads(b)  # validate before replacing the cache
            tmp = p + ".tmp"; open(tmp, "wb").write(b); os.replace(tmp, p)
            log(f"refreshed ref/{name}")
        except Exception as e:
            if not os.path.exists(p): raise
            log(f"ref/{name} refresh failed ({e}); using stale copy")
    return json.load(open(p)), datetime.fromtimestamp(os.path.getmtime(p)).astimezone().isoformat(timespec="seconds")


def resolve_cik(ticker):
    tk, asof = ref_file("company_tickers.json")
    cands = {ticker, ticker.replace(".", "-"), ticker.replace("-", ".")}
    hit = next((v for v in tk.values() if v["ticker"].upper() in cands), None)
    exch = None
    try:
        ex, _ = ref_file("company_tickers_exchange.json")
        fi = ex["fields"]
        for row in ex["data"]:
            r = dict(zip(fi, row))
            if str(r["ticker"]).upper() in cands: exch = r.get("exchange"); break
    except Exception as e:
        log(f"exchange map unavailable: {e}")
    if not hit: return None, None, exch, asof
    return str(hit["cik_str"]).zfill(10), hit["title"], exch, asof


def to_et(z):
    """EDGAR acceptanceDateTime (UTC, 'Z') -> 'YYYY-MM-DD HH:MM ET'."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromisoformat(z.replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
    except Exception:
        return z


def doc_url(cik, acc, doc):
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"


def parse_filings(sub, cik):
    r = sub["filings"]["recent"]
    out = []
    for i in range(len(r["form"])):
        out.append({"form": r["form"][i], "filed": r["filingDate"][i],
                    "accepted": to_et(r.get("acceptanceDateTime", [None] * (i + 1))[i]),
                    "report_date": (r.get("reportDate") or [""] * (i + 1))[i] or None,
                    "items": (r.get("items") or [""] * (i + 1))[i] or None,
                    "accession": r["accessionNumber"][i],
                    "primary_doc": r["primaryDocument"][i],
                    "description": (r.get("primaryDocDescription") or [""] * (i + 1))[i] or None,
                    "url": doc_url(cik, r["accessionNumber"][i], r["primaryDocument"][i])})
    return out


FACTS = {  # label: [(taxonomy, tag), ...] in preference order
    "shares_outstanding": [("dei", "EntityCommonStockSharesOutstanding")],
    "cash": [("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
             ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
             ("us-gaap", "Cash"), ("ifrs-full", "CashAndCashEquivalents")],
    "short_term_investments": [("us-gaap", "ShortTermInvestments")],
    "operating_cash_flow": [("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
                            ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
                            ("ifrs-full", "CashFlowsFromUsedInOperatingActivities")],
    "net_income_loss": [("us-gaap", "NetIncomeLoss"), ("us-gaap", "ProfitLoss"),
                        ("ifrs-full", "ProfitLoss")],
    "revenue": [("us-gaap", "Revenues"), ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
                ("ifrs-full", "Revenue")],
}
DEBT_TAGS = ["LongTermDebt", "LongTermDebtCurrent", "LongTermDebtNoncurrent", "DebtCurrent",
             "ConvertibleNotesPayable", "ConvertibleNotesPayableCurrent", "ConvertibleDebtNoncurrent",
             "NotesPayable", "NotesPayableCurrent", "ShortTermBorrowings", "LoansPayableCurrent",
             "NotesPayableRelatedPartiesClassifiedCurrent"]
GOOD_FORMS = ("10-Q", "10-K", "20-F", "40-F", "10-Q/A", "10-K/A", "20-F/A")


def _days(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def pick_facts(cf_json, latest_period):
    facts = cf_json.get("facts", {})
    src = "SEC XBRL companyfacts"
    out = {}

    def rows(tax, tag):
        u = (facts.get(tax, {}).get(tag) or {}).get("units") or {}
        for unit, vals in u.items():
            for v in vals:
                if v.get("form") in GOOD_FORMS:
                    yield dict(v, unit=unit, tag=f"{tax}:{tag}")

    def fmt(v, note=None):
        return F(v["val"], src + f" ({v['tag']}, {v['form']} filed {v['filed']})",
                 v["end"], note=note or (f"period {v['start']} to {v['end']}" if v.get("start") else f"as of {v['end']}"))

    for label, tags in FACTS.items():
        best = None
        for tax, tag in tags:
            rs = list(rows(tax, tag))
            if not rs: continue
            rs.sort(key=lambda x: (x["end"], x["filed"]))
            cand = rs[-1]
            if best is None or cand["end"] > best[-1]["end"]:
                best = rs
        if not best:
            out[label] = MISSING(src, "tag not reported in 10-Q/10-K XBRL"); continue
        last = best[-1]
        # staleness: cover-page shares can post-date the period; everything else must reach the latest period
        if label != "shares_outstanding" and latest_period and last["end"] < latest_period:
            out[label] = MISSING(src, f"latest value ({last['val']} at {last['end']}, {last['tag']}) is older than the latest 10-Q/10-K period {latest_period}; treated as stale/not reported")
            continue
        if label in ("operating_cash_flow", "net_income_loss", "revenue"):
            # latest-period values: longest (YTD) and the ~3-month quarter (direct or derived)
            same_end = [x for x in best if x["end"] == last["end"] and x.get("start")]
            ytd = max(same_end, key=lambda x: _days(x["start"], x["end"])) if same_end else last
            q = [x for x in same_end if 80 <= _days(x["start"], x["end"]) <= 100]
            d = {"ytd_or_latest": fmt(ytd)}
            if q:
                d["latest_quarter"] = fmt(q[-1])
            elif ytd.get("start") and _days(ytd["start"], ytd["end"]) > 100:
                prev = [x for x in best if x.get("start") == ytd["start"] and x["end"] < ytd["end"]
                        and x["end"] > (date.fromisoformat(ytd["end"]) - timedelta(days=100)).isoformat()]
                if prev:
                    p = max(prev, key=lambda x: x["end"])
                    d["latest_quarter"] = F(ytd["val"] - p["val"], src + f" (derived: {ytd['tag']} YTD to {ytd['end']} minus YTD to {p['end']})",
                                            ytd["end"], note=f"derived quarter {p['end']} to {ytd['end']}")
                else:
                    d["latest_quarter"] = MISSING(src, "no quarterly value and no prior YTD value to difference")
            out[label] = d
        else:
            out[label] = fmt(last)
    # debt: every current debt-like tag that reaches the latest period
    debt = {}
    for tag in DEBT_TAGS:
        rs = sorted(rows("us-gaap", tag), key=lambda x: (x["end"], x["filed"]))
        if not rs: continue
        last = rs[-1]
        if latest_period and last["end"] < latest_period:
            continue  # stale tag = not reported now
        debt[tag] = fmt(last)
    out["debt"] = debt if debt else MISSING(src, f"no debt tags reported for the latest period {latest_period} (may mean no debt or untagged; check the 10-Q balance sheet)")
    return out


class _Links(HTMLParser):
    def __init__(self): super().__init__(); self.links = []
    def handle_starttag(self, tag, a):
        if tag == "a":
            h = dict(a).get("href") or ""
            if h: self.links.append(h)


def exhibit_urls(cik, acc):
    """Press-release exhibits (EX-99.x) of an 8-K/6-K via the filing index page."""
    b, _ = http_get(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/index.json",
                    ua=SEC_UA, sec=True, timeout=10, retries=1)
    items = json.loads(b)["directory"]["item"]
    names = [i["name"] for i in items if re.search(r"(ex|dex)[-_]?99|exhibit99|ex991|ex99", i["name"], re.I)
             and re.search(r"\.(htm|html|txt)$", i["name"], re.I)]
    return [doc_url(cik, acc, n) for n in sorted(names)[:2]]


def _sec_json_cached(cik, name, url, timeout):
    """Load a SEC JSON response for 15 minutes, then refetch it.

    The mtime is the fetch time.  A fresh cache intentionally avoids a request;
    after the TTL the normal SEC request path runs again so new filings appear.
    """
    d = os.path.join(SEC_JSON_CACHE, cik)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    fresh = os.path.exists(p) and time.time() - os.path.getmtime(p) < SEC_JSON_TTL
    if fresh:
        try:
            obj = json.load(open(p, encoding="utf-8"))
            log(f"SEC {name} cache hit ({time.time() - os.path.getmtime(p):.0f}s old)")
            return obj
        except Exception as e:
            log(f"SEC {name} cache invalid; refetching: {e}")
    b, _ = http_get(url, ua=SEC_UA, sec=True, timeout=timeout)
    obj = json.loads(b.decode("utf-8"))
    tmp = p + ".tmp"
    with open(tmp, "wb") as f:
        f.write(b)
    os.replace(tmp, p)
    log(f"SEC {name} fetched and cached")
    return obj


def _hardlink_or_copy(src, dst):
    """Materialize a persistent cached file at the day's compatible path."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _cached_doc_body(path):
    """Read a cached combined filing, excluding its generated header."""
    with open(path, encoding="utf-8", errors="replace") as f:
        content = f.read()
    return content.split("\n\n", 1)[1] if "\n\n" in content else content


DILUTION_TERMS = {
    "ATM / at-the-market": r"at[- ]the[- ]market",
    "equity line / purchase agreement": r"equity line|committed equity|(?:common )?stock purchase agreement|ELOC",
    "convertible notes/preferred": r"convertible (?:promissory )?note|convertible debenture|convertible preferred|series [a-z] (?:convertible )?preferred",
    "pre-funded warrants": r"pre-funded warrant",
    "warrants": r"\bwarrants?\b",
    "registered direct / PIPE": r"registered direct|private placement|securities purchase agreement",
    "reverse split": r"reverse (?:stock )?split",
    "resale registration": r"selling (?:stock|share)holders?",
    "going concern": r"going concern",
    "Nasdaq/NYSE deficiency": r"minimum bid price|listing rule 5550|deficiency",
}


def sec_block(ticker, outdir, want_docs=True):
    res = {}
    try:
        cik, title, exch, asof = resolve_cik(ticker)
    except Exception as e:
        return {"error": f"CIK lookup failed: {e}"}
    res["cik"] = F(cik, "SEC company_tickers.json (cached ref)", asof,
                   error=None if cik else "ticker not in SEC company_tickers.json (OTC non-reporting, foreign, new listing or ticker change?)")
    res["sec_company_name"] = F(title, "SEC company_tickers.json", asof)
    res["sec_exchange"] = F(exch, "SEC company_tickers_exchange.json", asof)
    if not cik: return res
    with cf.ThreadPoolExecutor(2) as ex:
        fs = ex.submit(_sec_json_cached, cik, "sec_submissions.json",
                       f"https://data.sec.gov/submissions/CIK{cik}.json", 12)
        ff = ex.submit(_sec_json_cached, cik, "sec_companyfacts.json",
                       f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", 20)
        try:
            sub = fs.result(); save(os.path.join(outdir, "sec_submissions.json"), sub)
        except Exception as e:
            sub = None; res["filings_error"] = f"submissions fetch failed: {e}"
        try:
            cfj = ff.result(); save(os.path.join(outdir, "sec_companyfacts.json"), cfj)
        except Exception as e:
            cfj = None; res["facts_error"] = f"companyfacts fetch failed: {e}"
    log("SEC submissions/companyfacts done")
    today = date.today()
    filings = parse_filings(sub, cik) if sub else []
    if sub:
        res["sec_profile"] = F({"name": sub.get("name"), "sic": sub.get("sicDescription"),
                                "state": sub.get("stateOfIncorporation"), "fiscal_year_end": sub.get("fiscalYearEnd"),
                                "exchanges": sub.get("exchanges"), "tickers": sub.get("tickers"),
                                "former_names": sub.get("formerNames"), "category": sub.get("category")},
                               "SEC submissions JSON", now_iso())
        d30 = (today - timedelta(days=30)).isoformat(); d365 = (today - timedelta(days=365)).isoformat()
        slim = lambda f: {k: f[k] for k in ("form", "filed", "accepted", "items", "report_date", "description", "url")}
        res["filings_30d"] = F([slim(f) for f in filings if f["filed"] >= d30], "SEC submissions JSON", now_iso())
        res["filings_12m"] = F([slim(f) for f in filings if f["filed"] >= d365 and not f["form"].startswith("4")],
                               "SEC submissions JSON", now_iso(), note="Form 4/4-A omitted here; counted in form4_count_12m")
        res["form4_count_12m"] = sum(1 for f in filings if f["filed"] >= d365 and f["form"] in ("4", "4/A"))
        if sub["filings"].get("files"):
            res["filings_note"] = "older filings paginated in submissions 'files'; recent list covers the latest ~1000 filings"
    periodic = [f for f in filings if f["form"] in GOOD_FORMS and f.get("report_date")]
    latest_period = periodic[0]["report_date"] if periodic else None
    res["latest_periodic"] = F({"form": periodic[0]["form"], "period": latest_period, "filed": periodic[0]["filed"],
                                "url": periodic[0]["url"]} if periodic else None, "SEC submissions JSON", now_iso(),
                               error=None if periodic else "no 10-Q/10-K/20-F in recent filings")
    if cfj:
        res["financials"] = pick_facts(cfj, latest_period)
        log("XBRL facts picked")
    # filings since the last periodic report that may change the share count
    if latest_period:
        orig = [f for f in periodic if not f["form"].endswith("/A")] or periodic
        since = orig[0]["filed"]
        res["financing_filings_since_last_periodic"] = F(
            [{"form": f["form"], "filed": f["filed"], "items": f["items"], "url": f["url"]} for f in filings
             if f["filed"] >= since and (FIN_FORMS.match(f["form"]) or (f["form"].startswith("8-K") and f["items"] and "3.02" in f["items"]))],
            "SEC submissions JSON", now_iso(), note=f"since {orig[0]['form']} filed {since}; XBRL share counts do not reflect these")
    # documents
    docs = []
    if want_docs and filings:
        d90 = (today - timedelta(days=90)).isoformat()
        pick = [f for f in filings if f["filed"] >= d90 and DOC_FORMS.match(f["form"])][:MAX_DOCS]
        ddir = os.path.join(outdir, "docs"); os.makedirs(ddir, exist_ok=True)

        def grab(f):
            name = f"{f['filed']}_{re.sub(r'[^A-Za-z0-9]+', '', f['form'])}_{f['accession']}.txt"
            path = os.path.join(ddir, name)
            persistent = os.path.join(SEC_DOC_CACHE, cik, f["accession"], name)
            # Accession numbers are immutable.  Avoid even the exhibit-index
            # request when this combined filing text is already cached.
            if os.path.isfile(persistent) and os.path.getsize(persistent) > 0:
                try:
                    _hardlink_or_copy(persistent, path)
                    text = _cached_doc_body(persistent)
                    meta = {}
                    mp = persistent + ".meta.json"
                    try: meta = json.load(open(mp, encoding="utf-8"))
                    except Exception: pass
                    hits = {k: len(re.findall(pat, text, re.I)) for k, pat in DILUTION_TERMS.items()}
                    return {"form": f["form"], "filed": f["filed"], "accepted": f["accepted"], "items": f["items"],
                            "url": f["url"], "file": path, "chars": len(text),
                            "truncated": meta.get("truncated", len(text) >= DOC_CHARS),
                            "exhibits": meta.get("exhibits", []),
                            "term_hits": {k: v for k, v in hits.items() if v}, "error": None}
                except Exception as e:
                    log(f"cached SEC doc unreadable {f['accession']}; refetching: {e}")
            urls = [f["url"]]
            if f["form"].startswith(("8-K", "6-K")):
                try: urls += exhibit_urls(cik, f["accession"])
                except Exception as e: log(f"exhibit index failed {f['accession']}: {e}")
            parts, errs = [], []
            for u in urls:
                try:
                    b, _ = http_get(u, ua=SEC_UA, sec=True, timeout=15, retries=1)
                    raw = b.decode("utf-8", "replace")
                    t = strip_html(raw) if re.search(r"\.(htm|html)$", u, re.I) or "<html" in raw[:2000].lower() else raw
                    parts.append(f"===== {u} =====\n{t}")
                except Exception as e:
                    errs.append(str(e))
            text = "\n\n".join(parts)
            truncated = len(text) > DOC_CHARS
            text = text[:DOC_CHARS]
            hdr = f"FORM {f['form']} | filed {f['filed']} | accepted {f['accepted']} | items {f['items']}\nURL {f['url']}\n\n"
            if parts:
                os.makedirs(os.path.dirname(persistent), exist_ok=True)
                tmp = persistent + ".tmp"
                save(tmp, hdr + text)
                os.replace(tmp, persistent)
                mt = persistent + ".meta.json.tmp"
                save(mt, {"exhibits": urls[1:], "truncated": truncated, "chars": len(text)})
                os.replace(mt, persistent + ".meta.json")
                _hardlink_or_copy(persistent, path)
            hits = {k: len(re.findall(pat, text, re.I)) for k, pat in DILUTION_TERMS.items()}
            return {"form": f["form"], "filed": f["filed"], "accepted": f["accepted"], "items": f["items"],
                    "url": f["url"], "file": path if parts else None, "chars": len(text), "truncated": truncated,
                    "exhibits": urls[1:], "term_hits": {k: v for k, v in hits.items() if v},
                    "error": "; ".join(errs) or None}
        with cf.ThreadPoolExecutor(6) as ex:
            docs = list(ex.map(grab, pick))
        log(f"SEC docs ready (persistent cache): {sum(1 for d in docs if d['file'])}/{len(docs)}")
    res["documents_90d"] = F(docs, "SEC EDGAR archives (stripped text)", now_iso(),
                             note=f"8-K/6-K include EX-99 press-release exhibits; capped at {MAX_DOCS} most recent; text capped at {DOC_CHARS} chars")
    res["_filings_all"] = filings
    return res


# ---------------------------------------------------------------- StockAnalysis
def sa_block(ticker, outdir):
    t = ticker.lower().replace(".", "-")
    res = {}
    try:
        ov, page = sa_data(f"stocks/{t}")
        save(os.path.join(outdir, "sa_overview.json"), ov)
    except Exception as e:
        return {"error": f"StockAnalysis overview failed: {e}"}
    info = ov.get("info") or {}
    if not info.get("symbol") and not info.get("ticker"):
        return {"error": f"StockAnalysis has no page for {ticker} ({page}): unknown symbol, delisted, or unsupported listing"}
    q = info.get("quote") or {}
    src = f"StockAnalysis.com {page}"
    asof = q.get("u")
    res["page"] = page
    res["name"] = F(info.get("nameFull") or info.get("name"), src, now_iso())
    res["exchange"] = F(info.get("exchange"), src, now_iso())
    res["asset_type"] = F(f"{info.get('type')}/{info.get('subtype')}", src, now_iso())
    if not q:
        res["quote_error"] = "no quote object on page"
    else:
        res["price"] = F(q.get("p"), src, asof, note="regular-session last/close; may be delayed")
        res["change"] = F(q.get("c"), src, asof)
        res["change_pct"] = F(q.get("cp"), src, asof)
        res["prev_close"] = F(q.get("cl"), src, asof)
        res["open"] = F(q.get("o"), src, asof); res["high"] = F(q.get("h"), src, asof); res["low"] = F(q.get("l"), src, asof)
        res["volume"] = F(q.get("v"), src, asof)
        res["trading_date"] = F(q.get("td"), src, asof)
        res["market_status"] = F(q.get("ms"), src, now_iso())
        res["range_52w"] = F({"low": q.get("l52"), "high": q.get("h52")}, src, asof)
        if q.get("e") and q.get("ep") is not None:
            res["extended"] = F({"session": q.get("es"), "price": q.get("ep"), "change": q.get("ec"),
                                 "change_pct": q.get("ecp")}, src, q.get("eu"))
        else:
            res["extended"] = MISSING(src, "no premarket/after-hours print shown")
    res["market_cap"] = F(num(ov.get("marketCap")), src, asof, note=f"page text '{ov.get('marketCap')}' (SA shares x price)") if ov.get("marketCap") not in (None, "n/a") else MISSING(src, "market cap not shown")
    res["shares_out"] = F(num(ov.get("sharesOut")), src, asof, note=f"page text '{ov.get('sharesOut')}' (S&P Global data)") if ov.get("sharesOut") not in (None, "n/a") else MISSING(src, "shares not shown")
    for k in ("revenue", "netIncome", "earningsDate"):
        if ov.get(k) not in (None, "n/a"): res[k] = F(ov.get(k), src, now_iso(), note="S&P Global, TTM" if k != "earningsDate" else None)
    res["perf_reference_prices"] = F(ov.get("changes"), src, asof, note="reference closes 1w/1m/3m/6m/YTD/1y ago")
    news = ((ov.get("news") or {}).get("data")) or []
    def _t(x):
        if x and re.match(r"\d{4}-\d\d-\d\dT", x): return to_et(x)
        return x
    res["news"] = F([{"time": _t(n.get("time")), "title": n.get("title"), "source": n.get("source"), "url": n.get("url"),
                      "type": n.get("type")} for n in news[:15]], src, now_iso())
    ch = (ov.get("chart") or {}).get("data") or []
    if ch:
        pts = [p for p in ch if p.get("c") is not None]
        res["intraday_1m_closes"] = F({"points": len(pts), "first": pts[0] if pts else None, "last": pts[-1] if pts else None,
                                       "raw_file": "sa_overview.json (chart.data)"}, src + " (overview chart)", asof,
                                      note="close-only 1-minute points for the last regular session; no OHLC/volume, so not a bar source")
    # daily history
    try:
        hi, hpage = sa_data(page.replace("https://stockanalysis.com", "").strip("/") + "/history")
        save(os.path.join(outdir, "sa_history.json"), hi)
        h = hi.get("data") or {}
        bars = [b for b in (h.get("data") or []) if all(b.get(k) is not None for k in ("o", "h", "l", "c", "t"))]
        bars.sort(key=lambda b: b["t"])
        res["daily_bars"] = F({"count": len(bars), "first": bars[0]["t"] if bars else None, "last": bars[-1]["t"] if bars else None},
                              f"StockAnalysis.com {hpage} (data: {h.get('source')})", h.get("updated"))
        res["_bars"] = [{"date": b["t"], "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"],
                         "volume": b.get("v") or 0} for b in bars]
        res["_bars_source"] = "StockAnalysis.com" + (" (S&P Global data)" if h.get("source") == "spg" else f" (data: {h.get('source') or 'n/a'})")
    except Exception as e:
        res["daily_bars"] = MISSING("StockAnalysis.com history", f"{e}")
    return res


# ---------------------------------------------------------------- Finviz
FV_KEYS = ["Market Cap", "Shs Outstand", "Shs Float", "Short Float", "Short Ratio", "Short Interest",
           "Rel Volume", "Avg Volume", "Volume", "Price", "Change %", "Prev Close", "Insider Own",
           "Inst Own", "Earnings", "ATR (14)", "52W High", "52W Low", "Perf Week", "Perf Month",
           "Index", "Option/Short", "Cash/sh", "Enterprise Value", "Sales", "Income", "Employees", "IPO"]


def finviz_block(ticker, outdir):
    url = f"https://finviz.com/quote.ashx?t={ticker.replace('.', '-')}&p=d"
    try:
        b, final = http_get(url, timeout=12)
    except Exception as e:
        return {"error": f"Finviz fetch failed: {e}"}
    t = b.decode("utf-8", "replace")
    save(os.path.join(outdir, "finviz.html"), t)
    fetched = now_iso()
    cells = [html.unescape(re.sub(r"<[^>]+>", "", x)).strip()
             for x in re.findall(r'<td[^>]*class="[^"]*snapshot-td2[^"]*"[^>]*>(.*?)</td>', t, re.S)]
    kv = {}
    for k, v in zip(cells[0::2], cells[1::2]):
        kv.setdefault(k, v)
    if not kv:
        return {"error": "Finviz page had no snapshot table (ticker not covered, blocked, or layout change)"}
    src = f"Finviz {url}"
    note = "Finviz page shows no timestamp; free data is delayed. as_of = fetch time"
    res = {}
    for k in FV_KEYS:
        if k in kv:
            v = kv[k]
            parsed = num(v.split()[0]) if v and k not in ("Earnings", "Index", "Option/Short", "IPO") else None
            d = F({"text": v, "number": parsed}, src, fetched, note=note)
            if k in ("Short Float", "Short Interest", "Short Ratio"):
                d["note"] = "Finviz shows no settlement date; see summary.short_interest (Nasdaq/FINRA settlement date, split check, cross-check)"
            res[k] = d
    # news with carried-forward dates
    news, cur = [], None
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", t[t.find('id="news-table"'):] if 'id="news-table"' in t else "", re.S)[:60]:
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        a = re.search(r'<a[^>]*class="tab-link-news"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', row, re.S)
        if not tds or not a: continue
        stamp = " ".join(html.unescape(re.sub(r"<[^>]+>", "", tds[0])).split())
        m = re.match(r"(\w{3}-\d{2}-\d{2}|Today)\s+(.*)", stamp)
        if m:
            cur = date.today().strftime("%b-%d-%y") if m.group(1) == "Today" else m.group(1); tm = m.group(2)
        else:
            tm = stamp
        srcm = re.search(r"<span>\(([^)]+)\)</span>", row)
        href = html.unescape(a.group(1))
        if href.startswith("/"): href = "https://finviz.com" + href
        news.append({"date": cur, "time_et": tm, "title": " ".join(html.unescape(re.sub(r"<[^>]+>", "", a.group(2))).split()),
                     "source": srcm.group(1) if srcm else None, "url": href})
    res["news"] = F(news[:25], src, fetched, note="dates are Finviz display dates (ET), MMM-DD-YY")
    return res


# ---------------------------------------------------------------- Yahoo
def yahoo_chart(ticker, params, timeout=10):
    err = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            d = get_json(f"https://{host}/v8/finance/chart/{ticker.replace('.', '-')}?{params}", timeout=timeout, retries=1)
            r = ((d.get("chart") or {}).get("result") or [None])[0]
            if r: return r
            err = f"empty result: {(d.get('chart') or {}).get('error')}"
        except Exception as e:
            err = str(e)
    raise RuntimeError(err)


def yahoo_5m(ticker, outdir):
    try:
        r = yahoo_chart(ticker, "interval=5m&range=5d&includePrePost=true")
    except Exception as e:
        return MISSING("Yahoo v8 chart 5m", f"optional intraday fetch failed: {e}")
    off = int(r["meta"].get("gmtoffset") or 0)
    q = r["indicators"]["quote"][0]
    bars = []
    for i, ts in enumerate(r.get("timestamp") or []):
        if q["close"][i] is None: continue
        lt = datetime.fromtimestamp(ts + off, tz=timezone.utc).replace(tzinfo=None)
        bars.append({"t_et": lt.strftime("%Y-%m-%d %H:%M"), "o": q["open"][i], "h": q["high"][i], "l": q["low"][i],
                     "c": q["close"][i], "v": q["volume"][i]})
    days = sorted({b["t_et"][:10] for b in bars})[-2:]
    bars = [b for b in bars if b["t_et"][:10] in days]
    save(os.path.join(outdir, "yahoo_5m.json"), {"source": "Yahoo v8 chart interval=5m includePrePost", "tz": r["meta"].get("exchangeTimezoneName"), "bars": bars})
    sess = {}
    for dd in days:
        bs = [b for b in bars if b["t_et"][:10] == dd]
        reg = [b for b in bs if "09:30" <= b["t_et"][11:] < "16:00"]
        sess[dd] = {"bars": len(bs), "first_bar_et": bs[0]["t_et"][11:], "last_bar_et": bs[-1]["t_et"][11:],
                    "high": max(b["h"] for b in bs if b["h"] is not None), "low": min(b["l"] for b in bs if b["l"] is not None),
                    "regular_open": reg[0]["o"] if reg else None, "regular_close": reg[-1]["c"] if reg else None,
                    "volume_total": sum(b["v"] or 0 for b in bs)}
    return F({"sessions": sess, "raw_file": "yahoo_5m.json", "times": "ET (exchange time)"},
             "Yahoo Finance v8 chart (unofficial), interval=5m, includePrePost", bars[-1]["t_et"] + " ET" if bars else None,
             note="optional; for tying the move to release timestamps")


def yahoo_daily(ticker):
    r = yahoo_chart(ticker, "interval=1d&range=6mo&events=div%2Csplit")
    off = int(r["meta"].get("gmtoffset") or 0); q = r["indicators"]["quote"][0]
    bars = []
    for i, ts in enumerate(r.get("timestamp") or []):
        if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]): continue
        d = datetime.fromtimestamp(ts + off, tz=timezone.utc).strftime("%Y-%m-%d")
        bars.append({"date": d, "open": q["open"][i], "high": q["high"][i], "low": q["low"][i], "close": q["close"][i], "volume": q["volume"][i] or 0})
    return bars, r["meta"]


# ---------------------------------------------------------------- Short interest
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_DATE = rf"((?:{MONTHS}) \d{{1,2}}, \d{{4}})"
_CS = r"shares of (?:our |the Company(?:'|’)s |the registrant(?:'|’)s )?(?:Class A )?common stock"
_PAR = r"(?:,? \$?[\d.]+ par value(?: per share)?)?"
SHARE_PATS = [  # dated total-outstanding statements only (never "to be outstanding after this offering")
    rf"As of {_DATE},? there (?:were|was) ([\d,]{{4,}}) {_CS}{_PAR},?(?: issued and)? outstanding",
    rf"As of {_DATE},? there were outstanding ([\d,]{{4,}}) {_CS}",
    rf"As of {_DATE},? (?:the Company|we|the registrant|the Registrant) had ([\d,]{{4,}}) {_CS}{_PAR},?(?: issued and)? outstanding",
    rf"([\d,]{{4,}}) {_CS}{_PAR},?(?: were| that were| issued and)? outstanding (?:as of|on|at) {_DATE}",
]
SHARE_FORMS = re.compile(r"^(S-1|S-3|F-1|F-3|424B|10-Q|10-K|8-K|20-F|DEF 14|PRE 14)")


def http_post_json(url, payload, timeout=15, retries=1):
    err = None
    for i in range(retries + 1):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={
            "User-Agent": BUA, "Accept": "application/json", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
            if e.code in (400, 401, 403, 404): break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        if i < retries: time.sleep(0.8)
    raise RuntimeError(f"{err} for {url}")


def _mdy(s):
    return datetime.strptime(s, "%m/%d/%Y").date().isoformat()


def nasdaq_si(ticker, outdir):
    """Nasdaq quote short-interest API (FINRA data; Nasdaq-listed only). Newest first."""
    url = f"https://api.nasdaq.com/api/quote/{ticker.replace('.', '-')}/short-interest?assetClass=stocks"
    req = urllib.request.Request(url, headers={"User-Agent": BUA, "Accept": "application/json, text/plain, */*",
                                               "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/",
                                               "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=12) as r:
        b = r.read()
        if b[:2] == b"\x1f\x8b": b = gzip.decompress(b)
    d = json.loads(b.decode("utf-8"))
    save(os.path.join(outdir, "nasdaq_short_interest.json"), d)
    rows = ((((d.get("data") or {}).get("shortInterestTable")) or {}).get("rows")) or []
    if not rows:
        raise RuntimeError(d.get("message") or "no rows")
    out = [{"settlement_date": _mdy(x["settlementDate"]), "shares": num(x.get("interest")),
            "adv": num(x.get("avgDailyShareVolume")), "dtc": x.get("daysToCover")} for x in rows]
    out.sort(key=lambda x: x["settlement_date"], reverse=True)
    return out, "https://www.nasdaq.com/market-activity/stocks/%s/short-interest" % ticker.lower()


def finra_si(ticker, outdir):
    """FINRA consolidated equity short interest (public API, no key; all exchange-listed). Newest first."""
    start = (date.today() - timedelta(days=120)).isoformat()
    d = http_post_json("https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest", {
        "limit": 50, "compareFilters": [{"compareType": "EQUAL", "fieldName": "symbolCode", "fieldValue": ticker}],
        "dateRangeFilters": [{"fieldName": "settlementDate", "startDate": start, "endDate": date.today().isoformat()}]})
    save(os.path.join(outdir, "finra_short_interest.json"), d)
    if not d:
        raise RuntimeError("no FINRA rows in the last 120 days")
    out = [{"settlement_date": x["settlementDate"], "shares": x.get("currentShortPositionQuantity"),
            "adv": x.get("averageDailyVolumeQuantity"), "dtc": x.get("daysToCoverQuantity"),
            "split_flag": x.get("stockSplitFlag"), "revision_flag": x.get("revisionFlag"),
            "market": x.get("marketClassCode")} for x in d]
    out.sort(key=lambda x: x["settlement_date"], reverse=True)
    return out, "FINRA API consolidatedShortInterest"


def _bday_back(d):
    while d.weekday() >= 5: d -= timedelta(days=1)
    return d


def finra_inferred_settlement(today=None):
    """Latest FINRA short-interest settlement date likely already published, from the standard
    schedule (15th and month-end, rolled back to a business day; published ~8 business days later).
    Exchange holidays are not modeled, so this is labeled "inferred"."""
    today = today or date.today()
    cands = []
    for k in range(0, 3):
        y, m = today.year, today.month - k
        while m <= 0: m += 12; y -= 1
        first_next = date(y + (m == 12), m % 12 + 1, 1)
        cands += [_bday_back(date(y, m, 15)), _bday_back(first_next - timedelta(days=1))]
    def pub(d):
        n = 0
        while n < 8:
            d += timedelta(days=1)
            if d.weekday() < 5: n += 1
        return d
    ok = [c for c in cands if pub(c) <= today]
    return max(ok).isoformat() if ok else None


def split_history(ticker, outdir):
    """Splits from Yahoo (full list, events=split) and StockAnalysis statistics (last split).
    factor = new shares per old share (1:150 reverse -> 1/150)."""
    splits, errs, stats = [], [], None
    try:
        r = yahoo_chart(ticker, "interval=1d&range=2y&events=split")
        for v in ((r.get("events") or {}).get("splits") or {}).values():
            off = int(r["meta"].get("gmtoffset") or 0)
            dd = datetime.fromtimestamp(int(v["date"]) + off, tz=timezone.utc).date().isoformat()
            splits.append({"date": dd, "factor": v["numerator"] / v["denominator"],
                           "text": f"{v['numerator']:g}:{v['denominator']:g}", "source": "Yahoo v8 chart events=split"})
    except Exception as e:
        errs.append(f"Yahoo splits: {e}")
    try:
        stats, page = sa_data(f"stocks/{ticker.lower().replace('.', '-')}/statistics")
        save(os.path.join(outdir, "sa_statistics.json"), stats)
        kv = {x.get("id"): x for x in ((stats.get("stockSplits") or {}).get("data") or [])}
        sd, rt = (kv.get("lastSplitDate") or {}).get("hover"), (kv.get("splitRatio") or {}).get("value")
        if sd and rt and sd != "n/a" and ":" in str(rt):
            dd = datetime.strptime(sd, "%B %d, %Y").date().isoformat()
            a, b = (float(x) for x in rt.split(":"))
            hit = next((s for s in splits if abs(_days(s["date"], dd)) <= 3 and abs(s["factor"] - a / b) < 1e-9), None)
            if hit: hit["source"] += f" + StockAnalysis ({sd})"
            else: splits.append({"date": dd, "factor": a / b, "text": rt, "source": f"StockAnalysis.com {page}statistics"})
    except Exception as e:
        errs.append(f"StockAnalysis statistics: {e}")
    splits.sort(key=lambda s: s["date"])
    return splits, stats, errs


def sec_doc_share_counts(docs):
    """Dated 'N shares of common stock outstanding as of DATE' statements in downloaded filings."""
    out = []
    for d in docs or []:
        if not d.get("file") or not SHARE_FORMS.match(d["form"]): continue
        try: text = open(d["file"], encoding="utf-8").read()
        except Exception: continue
        text = re.sub(r"\s+", " ", text)
        for i, p in enumerate(SHARE_PATS):
            for m in re.finditer(p, text, re.I):
                g1, g2 = m.group(1), m.group(2)
                dt_s, n_s = (g2, g1) if i == 3 else (g1, g2)
                try: asof = datetime.strptime(dt_s, "%B %d, %Y").date().isoformat()
                except ValueError: continue
                n = num(n_s)
                if not n or n < 1000: continue
                out.append({"shares": int(n), "as_of": asof, "form": d["form"], "filed": d["filed"], "url": d["url"],
                            "class_a": "class a" in m.group(0).lower(), "quote": m.group(0)[:200]})
    out.sort(key=lambda x: (x["as_of"], x["filed"]), reverse=True)
    seen, uniq = set(), []
    for x in out:
        if (x["shares"], x["as_of"]) not in seen: seen.add((x["shares"], x["as_of"])); uniq.append(x)
    return uniq


def latest_spike(bars):
    """Most recent big up-day in the last 60 daily bars: high >= +30% or close >= +25% vs the prior
    close, or volume >= 5x the median of the prior 20 sessions."""
    hit = None
    for i in range(max(1, len(bars) - 60), len(bars)):
        b, pc = bars[i], bars[i - 1]["close"]
        vols = sorted(x["volume"] for x in bars[max(0, i - 20):i] if x.get("volume"))
        med = vols[len(vols) // 2] if vols else 0
        why = []
        if pc and b["high"] / pc - 1 >= 0.30: why.append(f"high +{(b['high'] / pc - 1) * 100:.0f}% vs prior close")
        elif pc and b["close"] / pc - 1 >= 0.25: why.append(f"close +{(b['close'] / pc - 1) * 100:.0f}%")
        if med and b.get("volume") and b["volume"] >= 5 * med: why.append(f"volume {b['volume'] / med:.0f}x 20-day median")
        if why: hit = {"date": b["date"], "why": "; ".join(why)}
    return hit


def si_sources(ticker, outdir):
    """Network part (runs in the prefetch pool)."""
    res = {}
    with cf.ThreadPoolExecutor(3) as ex:
        fn, ff, fs = ex.submit(nasdaq_si, ticker, outdir), ex.submit(finra_si, ticker, outdir), ex.submit(split_history, ticker, outdir)
        for k, f in (("nasdaq", fn), ("finra", ff)):
            try:
                rows, s = f.result(); res[k] = F(rows[:6], s, rows[0]["settlement_date"], note="newest first; shares as reported on each settlement date (not split-adjusted)")
            except Exception as e:
                res[k] = MISSING("Nasdaq short-interest API" if k == "nasdaq" else "FINRA API consolidatedShortInterest", str(e))
        splits, stats, errs = fs.result()
        res["splits"] = F(splits, "Yahoo v8 events=split + StockAnalysis statistics", now_iso(), error="; ".join(errs) or None)
        res["_sa_stats"] = stats
    return res


def _sa_stat(stats, sect, key):
    for x in ((stats or {}).get(sect) or {}).get("data") or []:
        if x.get("id") == key: return num(x.get("hover")), x.get("value")
    return None, None


def short_interest_block(summary, src_res, bars, bars_partial=False):
    """Combine Nasdaq/FINRA/Finviz short interest with split history, float and volume.
    Every number is reported or computed from reported numbers (math shown in 'math')."""
    today = date.today()
    fv = summary.get("finviz") or {}
    fvn = lambda k: ((fv.get(k) or {}).get("value") or {}).get("number")
    fvt = lambda k: ((fv.get(k) or {}).get("value") or {}).get("text")
    fin = {k: {"text": fvt(k), "number": fvn(k)} for k in ("Short Float", "Short Interest", "Short Ratio", "Shs Float", "Shs Outstand", "Avg Volume")}
    nas, fra = src_res.get("nasdaq") or {}, src_res.get("finra") or {}
    splits = ((src_res.get("splits") or {}).get("value")) or []
    stats = src_res.get("_sa_stats")
    notes, flags, math = [], [], []
    # 1. pick the settlement record: Nasdaq first, FINRA second, then Finviz + inferred date
    rec, label, source = None, "reported", None
    if nas.get("value"):
        rec, source = nas["value"][0], "Nasdaq/FINRA"
    if fra.get("value"):
        fr = fra["value"][0]
        if rec is None or fr["settlement_date"] > rec["settlement_date"]:
            rec, source = fr, "FINRA"
        if nas.get("value"):
            n0 = next((x for x in nas["value"] if x["settlement_date"] == fr["settlement_date"]), None)
            if n0 and n0["shares"] != fr["shares"]:
                notes.append(f"Nasdaq {n0['shares']:,.0f} vs FINRA {fr['shares']:,.0f} for {fr['settlement_date']} (FINRA revision?)")
            elif n0:
                notes.append(f"Nasdaq and FINRA agree for {fr['settlement_date']} ({fr['shares']:,.0f})")
    if rec is None and fin["Short Interest"]["number"]:
        sd = finra_inferred_settlement(today)
        rec = {"settlement_date": sd, "shares": fin["Short Interest"]["number"], "adv": None, "dtc": fin["Short Ratio"]["number"]}
        label, source = "inferred", "Finviz (settlement date inferred from FINRA schedule)"
        notes.append(f"Finviz shows {fin['Short Interest']['text']} with no date; Nasdaq and FINRA had no rows, so the date is inferred")
    if rec is None or not rec.get("shares") or not rec.get("settlement_date"):
        return MISSING("Nasdaq / FINRA / Finviz", "no short-interest data from any source")
    sd, raw = rec["settlement_date"], float(rec["shares"])
    # 2. splits after the settlement date -> adjust the share count
    after = [s for s in splits if s["date"] > sd]
    if label == "inferred" and after:  # Finviz already split-adjusts its short interest (seen on WHLR 1:9)
        notes.append(f"split {' x '.join(s['text'] for s in after)} after the inferred date; Finviz figure not adjusted again")
        after = []
    factor = 1.0
    for s in after: factor *= s["factor"]
    adj = raw * factor
    if after:
        math.append(f"split adjustment: {raw:,.0f} reported on {sd} x {' x '.join(s['text'] for s in after)} "
                    f"(split(s) on {', '.join(s['date'] for s in after)}) = {adj:,.0f}")
    elif splits and splits[-1]["date"] > (date.fromisoformat(sd) - timedelta(days=90)).isoformat():
        notes.append(f"last split {splits[-1]['text']} on {splits[-1]['date']} is before the {sd} settlement, so the count is already post-split")
    rs_warn = [x for x in (summary.get("cross_checks") or []) if "reverse-split" in x]
    if rs_warn and not after and not any(s["date"] > (today - timedelta(days=120)).isoformat() for s in splits):
        notes.append("SEC filings mention a reverse split; no split after the settlement date found in Yahoo/StockAnalysis, so no adjustment applied (confirm effective date)")
    # 3. Finviz cross-check (Finviz rounds to 0.01M)
    fsi = fin["Short Interest"]["number"]
    if fsi and label == "reported":
        tol = max(0.03 * fsi, 6000)
        if abs(fsi - adj) <= tol: notes.append(f"Finviz {fin['Short Interest']['text']} matches" + (" the split-adjusted count" if after else f" {sd}"))
        elif after and abs(fsi - raw) <= max(0.03 * raw, 6000): notes.append(f"Finviz {fin['Short Interest']['text']} = unadjusted pre-split count (Finviz not split-adjusted)")
        else: notes.append(f"Finviz {fin['Short Interest']['text']} differs from {source} {sd} ({adj:,.0f}); Finviz may be on another settlement date")
    # 4. reference share count: latest dated SEC statement (split-adjusted if it predates a split)
    docs = ((summary.get("sec") or {}).get("documents_90d") or {}).get("value") or []
    counts = sec_doc_share_counts(docs)
    ref = None
    if counts:
        c = counts[0]
        f2 = 1.0; post = [s for s in splits if s["date"] > c["as_of"]]
        for s in post: f2 *= s["factor"]
        ref = {"value": c["shares"] * f2, "reported_value": c["shares"],
               "source": f"SEC {c['form']} filed {c['filed']} ({'Class A ' if c['class_a'] else ''}shares as of {c['as_of']})"
               + (f" x {' x '.join(s['text'] for s in post)} split" if post else ""),
               "url": c["url"], "as_of": c["as_of"], "adjusted_by_splits": bool(post),
               "split_adjustments": post}
    xb = (((summary.get("sec") or {}).get("financials") or {}).get("shares_outstanding")) or {}
    if not ref and xb.get("value") and xb.get("as_of") and _days(xb["as_of"], today.isoformat()) <= 120 \
            and not any(s["date"] > xb["as_of"] for s in splits):
        ref = {"value": xb["value"], "source": f"SEC XBRL cover ({xb['as_of']})", "as_of": xb["as_of"],
               "adjusted_by_splits": False, "split_adjustments": []}
    def stale_vs_ref(v, who):
        if not ref or not v: return None
        r = v / ref["value"]
        for s in splits:
            if s["date"] >= (today - timedelta(days=365)).isoformat() and s["factor"] < 1 and r >= 0.6 / s["factor"]:
                return f"{who} shares {v:,.0f} look pre-split ({r:.1f}x the {ref['source']} count; {s['text']} split {s['date']})"
        if abs(r - 1) > 0.3:
            return f"{who} shares {v:,.0f} differ {100 * (r - 1):+.0f}% from {ref['source']} {ref['value']:,.0f}"
        return None
    fv_st = stale_vs_ref(fin["Shs Outstand"]["number"], "Finviz")
    sa_so, _ = _sa_stat(stats, "shares", "sharesout")
    if sa_so is None: sa_so = ((summary.get("stockanalysis") or {}).get("shares_out") or {}).get("value")
    sa_fl, _ = _sa_stat(stats, "shares", "float")
    sa_st = stale_vs_ref(sa_so, "StockAnalysis")
    for x in (fv_st, sa_st):
        if x: notes.append(x)
    # float: Finviz unless it looks stale/inconsistent, then StockAnalysis, else none
    flt = None
    if fin["Shs Float"]["number"] and not fv_st:
        flt = {"value": fin["Shs Float"]["number"], "source": f"Finviz Shs Float {fin['Shs Float']['text']}"}
    elif sa_fl and not sa_st:
        flt = {"value": sa_fl, "source": "StockAnalysis float (S&P Global)"}
    if not flt: notes.append("no current float that passes the staleness check; % of float omitted")
    # shares out: SEC dated count, else Finviz, else StockAnalysis (each only if not flagged)
    so = dict(ref) if ref else None
    if not so and fin["Shs Outstand"]["number"] and not fv_st:
        so = {"value": fin["Shs Outstand"]["number"], "source": f"Finviz Shs Outstand {fin['Shs Outstand']['text']}"}
    if not so and sa_so and not sa_st:
        so = {"value": sa_so, "source": "StockAnalysis shares out"}
    pf = adj / flt["value"] * 100 if flt else None
    ps = adj / so["value"] * 100 if so else None
    if pf is not None: math.append(f"% of float = {adj:,.0f} / {flt['value']:,.0f} ({flt['source']}) = {pf:.2f}%")
    if ps is not None: math.append(f"% of shares out = {adj:,.0f} / {so['value']:,.0f} ({so['source']}) = {ps:.2f}%")
    # 5. days to cover from the last 30 full sessions of split-adjusted daily bars
    b = list(bars or [])
    if bars_partial and b: b = b[:-1]
    for s in splits:
        pre = [x for x in b if x["date"] < s["date"]]; post_ = [x for x in b if x["date"] >= s["date"]]
        if pre and post_ and s["factor"] < 1 and post_[0]["open"] / pre[-1]["close"] >= 0.5 / s["factor"]:
            notes.append(f"daily bars look unadjusted for the {s['text']} split on {s['date']}; days to cover uses post-split bars only")
            b = post_
    last30 = b[-30:]
    adv = sum(x["volume"] for x in last30) / len(last30) if last30 else None
    dtc = adj / adv if adv else None
    if dtc is not None:
        math.append(f"days to cover = {adj:,.0f} / {adv:,.0f} (avg volume of {len(last30)} sessions {last30[0]['date']}..{last30[-1]['date']}, "
                    f"split-adjusted daily bars) = {dtc:.2f}")
        if rec.get("dtc") is not None:
            math.append(f"{source} days to cover at settlement: {rec['dtc']:g}" + (f" (ADV then {rec['adv']:,.0f}, as reported"
                        + ("; Nasdaq/FINRA floor this at 1" if rec["dtc"] <= 1 else "") + ")" if rec.get("adv") else ""))
    # 6. staleness
    age = _days(sd, today.isoformat())
    if age > 20: flags.append(f"{age} days old")
    sp = latest_spike(bars or [])
    if sp and sp["date"] > sd: flags.append(f"pre-spike, likely stale (spike {sp['date']}: {sp['why']})")
    if label == "inferred": flags.append("settlement date inferred")
    val = {"shares_short": round(adj), "shares_short_reported": raw, "settlement_date": sd, "settlement_date_label": label,
           "split_adjusted": bool(after), "split_ratio": " x ".join(s["text"] for s in after) or None, "split_factor": factor,
           "splits_after_settlement": after, "pct_float": round(pf, 2) if pf is not None else None, "float_used": flt,
           "pct_shares_out": round(ps, 2) if ps is not None else None, "shares_out_used": so,
           "days_to_cover": round(dtc, 2) if dtc is not None else None, "adv_30d": round(adv) if adv else None,
           "days_to_cover_at_settlement": rec.get("dtc"), "adv_at_settlement": rec.get("adv"),
           "age_days": age, "latest_spike": sp, "flags": flags, "finviz": fin,
           "sec_share_counts": counts[:3], "math": math, "notes": notes}
    return F(val, source, sd, note="shares short as of the settlement date; adjusted by splits after that date; "
             "% float / days to cover computed here from current float and 30-session average volume")


def verified_shares_node(short_interest):
    """Expose the SEC-dated, split-adjusted count selected for short-interest math."""
    v = (short_interest or {}).get("value") if isinstance(short_interest, dict) else None
    so = v.get("shares_out_used") if isinstance(v, dict) else None
    if not isinstance(so, dict) or so.get("value") is None or not str(so.get("source") or "").startswith("SEC "):
        return MISSING("verified SEC shares outstanding", "no dated SEC share count passed the split/staleness checks")
    out = F(so["value"], so.get("source"), so.get("as_of"),
             note="selected by short_interest after SEC date and split/staleness checks")
    for k in ("reported_value", "adjusted_by_splits", "split_adjustments", "url"):
        if k in so:
            out[k] = so[k]
    return out


# ---------------------------------------------------------------- derived
def derive(summary, filings):
    """Preliminary Quick Read inputs. Mechanical presence checks only."""
    today = date.today()
    d3 = (today - timedelta(days=7)).isoformat(); d90 = (today - timedelta(days=90)).isoformat()
    flags = []
    for f in filings:
        if f["filed"] < d90: break
        fm = f["form"]
        if re.match(r"^(S-1|S-3|F-1|F-3)(/A)?$", fm): flags.append(f"{fm} filed {f['filed']}")
        elif fm.startswith("424B"): flags.append(f"{fm} filed {f['filed']}")
        elif fm == "EFFECT": flags.append(f"EFFECT {f['filed']}")
        elif fm.startswith("8-K") and f.get("items") and "3.02" in f["items"]: flags.append(f"8-K item 3.02 (unregistered sale) {f['filed']}")
    term = {}
    for d in (summary.get("sec", {}).get("documents_90d") or {}).get("value") or []:
        for k, v in (d.get("term_hits") or {}).items():
            if k in ("warrants",): continue
            term.setdefault(k, []).append(f"{d['form']} {d['filed']}")
    strong = [k for k in term if k in ("ATM / at-the-market", "equity line / purchase agreement", "convertible notes/preferred",
                                        "pre-funded warrants", "registered direct / PIPE", "resale registration")]
    recent_fin = [x for x in flags if any(s in x for s in ("424B", "S-1", "F-1", "3.02"))]
    if (strong and flags) or len(recent_fin) >= 2: level = "High"
    elif strong or flags: level = "Moderate"
    else: level = "Low (nothing found in 90-day presence check)"
    recent8k = [{"form": f["form"], "filed": f["filed"], "accepted": f["accepted"], "items": f["items"], "url": f["url"]}
                for f in filings if f["filed"] >= d3 and (f["form"].startswith("8-K") or f["form"].startswith("6-K"))]
    heads = []
    for n in ((summary.get("finviz", {}).get("news") or {}).get("value") or [])[:8]:
        heads.append(f"{n['date']} {n['time_et']} ET · {n['title']} ({n['source']})")
    return {
        "dilution_flag_preliminary": F({"level": level, "filings_90d": flags, "doc_term_hits": term},
                                       "derived from SEC submissions + downloaded filing text (presence check)", now_iso(),
                                       note="PRELIMINARY: keyword/form presence only, not a reading of terms; 'warrants' hits ignored as boilerplate-prone"),
        "recent_8k_6k_7d": F(recent8k, "SEC submissions JSON", now_iso(), note="last 7 calendar days; accepted times in ET"),
        "recent_headlines": F(heads, "Finviz quote page news", now_iso()),
    }


# ---------------------------------------------------------------- main
def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args or "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__); sys.exit(0 if args else 1)
    ticker = args[0].strip().lstrip("$").upper()
    want_chart = "--no-chart" not in sys.argv
    want_docs = "--no-docs" not in sys.argv
    outdir = os.path.join(CACHE, f"{ticker}-{date.today().isoformat()}")
    os.makedirs(outdir, exist_ok=True)
    summary = {"ticker": ticker, "generated_at": now_iso(), "cache_dir": outdir,
               "conventions": "each value = {value, source, as_of}; null value + error = missing; nothing estimated",
               "timings_s": {}}
    tm = summary["timings_s"]

    def timed(name, fn, *a):
        s = time.time()
        try:
            return fn(*a)
        finally:
            tm[name] = round(time.time() - s, 2); log(f"{name} finished in {tm[name]}s")

    chart_res = {}
    bars_ready = threading.Event()
    bars_holder = {}

    def chart_job():
        bars_ready.wait(40)
        b = bars_holder.get("bars")
        if not b:
            chart_res.update(MISSING("chart.py", "no daily bars from StockAnalysis or Yahoo")); return
        p = os.path.join(outdir, "daily_bars.json")
        save(p, {"ticker": ticker, "source": bars_holder["source"], "name": bars_holder.get("name"), "bars": b})
        script = next((c for c in (os.path.join(HERE, "chart.py"), "/workspace/runner-watch/chart.py") if os.path.exists(c)), None)
        if not script:
            chart_res.update(MISSING("chart.py", "chart.py not found")); return
        try:
            r = subprocess.run([sys.executable, script, ticker, "--bars", p], capture_output=True, text=True, timeout=60)
            out = r.stdout.strip().splitlines()
            if r.returncode == 0 and out:
                chart_res.update(F(out[0], f"chart.py from {bars_holder['source']}", b[-1]["date"], note=r.stderr.strip()[-300:]))
            else:
                chart_res.update(MISSING("chart.py", f"exit {r.returncode}: {r.stderr.strip()[-400:]}"))
        except Exception as e:
            chart_res.update(MISSING("chart.py", str(e)))

    with cf.ThreadPoolExecutor(8) as ex:
        f_sec = ex.submit(timed, "sec", sec_block, ticker, outdir, want_docs)
        f_sa = ex.submit(timed, "stockanalysis", sa_block, ticker, outdir)
        f_fv = ex.submit(timed, "finviz", finviz_block, ticker, outdir)
        f_y5 = ex.submit(timed, "yahoo_5m", yahoo_5m, ticker, outdir)
        f_ch = ex.submit(timed, "chart", chart_job) if want_chart else None
        f_si = ex.submit(timed, "short_interest_sources", si_sources, ticker, outdir)
        sa = f_sa.result()
        sa_bars, sa_bsrc = sa.pop("_bars", None), sa.pop("_bars_source", None)
        if sa_bars and len(sa_bars) >= 20:
            # during a live session the history page may not have today's bar yet: append the
            # quote's O/H/L/last as a clearly labeled partial bar (never fabricated)
            qd = (sa.get("trading_date") or {}).get("value")
            vals = [(sa.get(k) or {}).get("value") for k in ("open", "high", "low", "price", "volume")]
            if qd and qd > sa_bars[-1]["date"] and None not in vals[:4]:
                sa_bars.append({"date": qd, "open": vals[0], "high": vals[1], "low": vals[2], "close": vals[3],
                                "volume": vals[4] or 0})
                sa_bsrc += f" + {qd} partial bar from SA quote"
                sa["daily_bars_note"] = f"appended partial bar for {qd} from the SA quote (history page lagged)"
            bars_holder.update(bars=sa_bars, source=sa_bsrc, name=(sa.get("name") or {}).get("value"))
        else:
            if sa_bars: sa["daily_bars_note"] = f"only {len(sa_bars)} StockAnalysis bars; using Yahoo fallback"
            try:
                yb, ym = yahoo_daily(ticker)
                bars_holder.update(bars=yb, source="Yahoo Finance v8 (fallback)", name=ym.get("shortName"))
                sa["daily_bars_fallback"] = F({"count": len(yb), "first": yb[0]["date"], "last": yb[-1]["date"]},
                                              "Yahoo Finance v8 chart 1d (fallback)", yb[-1]["date"])
            except Exception as e:
                sa["daily_bars_fallback"] = MISSING("Yahoo Finance v8 chart 1d (fallback)", str(e))
        bars_ready.set()
        summary["stockanalysis"] = sa
        # quote fallback: Yahoo meta if SA quote missing
        if not sa.get("price") or sa["price"].get("value") is None:
            try:
                ym = yahoo_chart(ticker, "interval=1d&range=5d&includePrePost=true")["meta"]
                t = datetime.fromtimestamp(ym.get("regularMarketTime", 0)).astimezone().isoformat(timespec="seconds")
                summary["yahoo_quote_fallback"] = {
                    "price": F(ym.get("regularMarketPrice"), "Yahoo v8 meta (fallback)", t),
                    "prev_close": F(ym.get("chartPreviousClose"), "Yahoo v8 meta (fallback)", t),
                    "volume": F(ym.get("regularMarketVolume"), "Yahoo v8 meta (fallback)", t)}
            except Exception as e:
                summary["yahoo_quote_fallback"] = MISSING("Yahoo v8 meta (fallback)", str(e))
        summary["finviz"] = f_fv.result()
        summary["yahoo_5m"] = f_y5.result()
        sec = f_sec.result()
        filings = sec.pop("_filings_all", [])
        summary["sec"] = sec
        if f_ch: f_ch.result()
        si_res = f_si.result()
    summary["chart"] = chart_res if want_chart else MISSING("chart.py", "skipped (--no-chart)")
    summary["quick_read_inputs"] = derive(summary, filings)
    # cross-check a few numbers between sources (no reconciliation, just flags)
    xc = []
    try:
        sp = summary["stockanalysis"]["price"]["value"]; fp = summary["finviz"]["Price"]["value"]["number"]
        if sp and fp and abs(sp - fp) / sp > 0.02: xc.append(f"price differs: SA {sp} vs Finviz {fp}")
    except Exception: pass
    try:
        ss = summary["stockanalysis"]["shares_out"]["value"]; fs = summary["finviz"]["Shs Outstand"]["value"]["number"]
        xs = summary["sec"]["financials"]["shares_outstanding"]["value"]
        xc.append(f"shares outstanding: SA {ss:,.0f} · Finviz {fs:,.0f} · SEC cover {xs:,.0f} ({summary['sec']['financials']['shares_outstanding']['as_of']})")
    except Exception: pass
    # reverse split after the SEC cover-page share count -> that count is pre-split
    try:
        so = summary["sec"]["financials"]["shares_outstanding"]
        docs = summary["sec"]["documents_90d"]["value"]
        rs = [d for d in docs if d["filed"] >= (so["as_of"] or "") and d["form"].startswith(("8-K", "6-K", "PRE 14C", "DEF 14C"))
              and (d.get("term_hits") or {}).get("reverse split") and (d.get("items") is None or any(i in (d["items"] or "") for i in ("3.03", "5.03", "8.01", "7.01")))]
        if rs:
            xc.append("WARNING: reverse-split-related filing(s) after the SEC cover share count date (%s): %s. Check whether a split "
                      "took effect (ratio, effective date); if so, the SEC count is pre-split, so don't use it unadjusted." % (
                          so["as_of"], ", ".join(f"{d['form']} {d['filed']}" for d in rs)))
    except Exception: pass
    summary["cross_checks"] = xc
    # short interest (needs Finviz, SEC docs, cross_checks and daily bars)
    try:
        summary["short_interest"] = short_interest_block(summary, si_res, bars_holder.get("bars"),
                                                         "partial bar" in (bars_holder.get("source") or ""))
    except Exception as e:
        summary["short_interest"] = MISSING("short_interest_block", f"{type(e).__name__}: {e}")
    summary["shares_verified"] = verified_shares_node(summary["short_interest"])
    summary["short_interest_sources"] = {k: v for k, v in si_res.items() if not k.startswith("_")}
    tm["total"] = round(time.time() - T0, 2)
    p = os.path.join(outdir, "summary.json")
    save(p, summary)
    log(f"done in {tm['total']}s -> {p}")
    print(p)


if __name__ == "__main__":
    main()
