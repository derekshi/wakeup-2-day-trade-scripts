#!/usr/bin/env python3
"""Stage-1 "first look" snapshot for runner-quick-analysis (stdlib only, <0.5 s).

Usage: python3 snapshot.py <path/to/summary.json | TICKER> [--spike]

Builds a ready-to-send markdown message ONLY from prefetch.py facts (no judgments):
freshness stamp, header, price / move, extended hours, volume + relative volume,
float, shares outstanding, market cap, short interest (Nasdaq/FINRA settlement date, split-adjusted),
latest SEC filings (flagging S-1/S-3/424B*/8-K/F-1/6-K/reverse split) and latest
headlines. Every figure names its source; missing fields are skipped.
The last stdout line is always "CHART: <png path>" or "CHART: none".
TICKER finds today's newest /workspace/runner-cache/<TICKER>-<date>/summary.json
(falls back to the newest older one, marked "cached").
--spike instead prints the largest 5-minute moves from yahoo_5m.json (ET + PT),
for the Stage-2 Quick Read timing check (not part of the Stage-1 message).
"""
import glob, json, os, re, sys
from datetime import date, datetime, timedelta

try:
    from zoneinfo import ZoneInfo
    ET, PT = ZoneInfo("America/New_York"), ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover
    ET = PT = None

CACHE = "/workspace/runner-cache"
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
ITEMS = {"1.01": "material agreement", "1.02": "agreement terminated", "1.03": "bankruptcy",
         "2.01": "acquisition/disposition", "2.02": "results", "2.03": "new debt obligation",
         "2.04": "debt acceleration", "2.05": "exit/restructuring costs", "2.06": "impairment",
         "3.01": "listing deficiency/transfer", "3.02": "unregistered equity sale",
         "3.03": "change to holders' rights", "4.01": "auditor change", "4.02": "non-reliance",
         "5.01": "change in control", "5.02": "officer/director change", "5.03": "charter/bylaw amendment",
         "5.07": "shareholder vote", "7.01": "Reg FD", "8.01": "other events"}
FLAG_FORM = re.compile(r"^(S-1|S-3|F-1|424B|8-K|6-K)")


def g(d, *path):
    """Safe nested get; returns None for missing."""
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def val(d, *path):
    """Value of a {value, source, as_of} node, or None."""
    return g(d, *path, "value")


def src(d, *path):
    s = g(d, *path, "source") or ""
    return s.split(" http")[0].strip()


def big(n, money=False):
    if n is None:
        return None
    a = abs(n)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            s = f"{n / div:,.2f}{suf}"
            break
    else:
        s = f"{n:,.0f}"
    return ("$" + s) if money else s


def px(p):
    if p is None:
        return None
    return f"${p:,.4f}" if abs(p) < 1 else f"${p:,.2f}"


def pct(p):
    return None if p is None else f"{p:+.2f}%".replace("-", "−")


def fmt_day(dt, with_year=False):
    s = f"{MON[dt.month - 1]} {dt.day}"
    return s + (f", {dt.year}" if with_year or dt.year != date.today().year else "")


def fmt_hm(dt):
    return dt.strftime("%I:%M %p").lstrip("0")


def et_pt(dt_et, day=True):
    """'Sep 26, 1:44 PM ET / 10:44 AM PT' from an ET-naive or aware datetime."""
    if ET:
        if dt_et.tzinfo is None:
            dt_et = dt_et.replace(tzinfo=ET)
        p = dt_et.astimezone(PT)
    else:
        p = dt_et - timedelta(hours=3)
    s = f"{fmt_hm(dt_et)} ET / {fmt_hm(p)} PT"
    return (fmt_day(dt_et) + ", " + s) if day else s


def parse_et(s):
    """Parse the time formats prefetch stores (SA news, SA quote as_of, Finviz news)."""
    if not s:
        return None
    s = s.strip()
    for f, cut in (("%Y-%m-%d %H:%M", " ET"), ("%b %d, %Y, %I:%M %p", (" EDT", " EST", " ET"))):
        t = s
        for c in (cut if isinstance(cut, tuple) else (cut,)):
            if t.endswith(c):
                t = t[: -len(c)]
        try:
            return datetime.strptime(t.strip(), f)
        except ValueError:
            pass
    return None


def find_summary(arg):
    if os.path.isfile(arg):
        return arg, False
    if os.path.isdir(arg) and os.path.isfile(os.path.join(arg, "summary.json")):
        return os.path.join(arg, "summary.json"), False
    t = arg.strip().lstrip("$").upper()
    today = os.path.join(CACHE, f"{t}-{date.today().isoformat()}", "summary.json")
    if os.path.isfile(today):
        return today, False
    c = sorted(glob.glob(os.path.join(CACHE, f"{t}-????-??-??", "summary.json")))
    if c:
        return c[-1], True
    sys.exit(f"snapshot.py: no summary.json for {arg} (run prefetch.py {t} first)")


# ------------------------------------------------------------------ sections
def stamp(s, cached):
    sa = g(s, "stockanalysis") or {}
    as_of = g(sa, "price", "as_of")
    dt = parse_et(as_of)
    status = val(sa, "market_status")
    parts = []
    if dt:
        lbl = "close" if status == "closed" else "last trade"
        parts.append(f"Data as of {dt.strftime('%a')} {et_pt(dt)} ({lbl})")
    elif as_of:
        parts.append(f"Data as of {as_of}")
    ext = g(sa, "extended")
    edt = parse_et(g(ext, "as_of")) if val(sa, "extended") else None
    if edt:
        sess = ((val(sa, "extended") or {}).get("session") or "extended hours").lower()
        if parts:
            parts[-1] += f"; {sess} to {fmt_hm(edt)} ET"
    gen = g(s, "generated_at")
    try:
        gdt = datetime.fromisoformat(gen)
        if ET:
            gdt = gdt.astimezone(PT)
        parts.append(f"fetched {gdt.strftime('%a')} {fmt_day(gdt)}, {fmt_hm(gdt)} PT" + (" (cached, not today)" if cached else ""))
    except Exception:
        pass
    srcs = []
    if val(sa, "price") is not None:
        srcs.append("StockAnalysis.com (may be delayed)")
    elif val(s, "yahoo_quote_fallback", "price") is not None:
        srcs.append("Yahoo (fallback, may be delayed)")
    if isinstance(g(s, "finviz"), dict) and any(isinstance(v, dict) and v.get("value") is not None for v in s["finviz"].values()):
        srcs.append("Finviz")
    if val(s, "sec", "cik"):
        srcs.append("SEC EDGAR")
    if srcs:
        parts.append("sources: " + ", ".join(srcs))
    return "*" + " · ".join(parts) + "*" if parts else None


def quote_lines(s):
    sa = g(s, "stockanalysis") or {}
    fv = g(s, "finviz") or {}
    L = []
    price, psrc = val(sa, "price"), src(sa, "price")
    chg, cp = val(sa, "change"), val(sa, "change_pct")
    if price is None and val(s, "yahoo_quote_fallback", "price") is not None:
        price, psrc = val(s, "yahoo_quote_fallback", "price"), src(s, "yahoo_quote_fallback", "price")
        pc = val(s, "yahoo_quote_fallback", "prev_close")
        if pc:
            chg, cp = price - pc, (price - pc) / pc * 100
    if price is None and val(fv, "Price") is not None:
        price, psrc = (val(fv, "Price") or {}).get("number"), "Finviz"
        cp = (val(fv, "Change %") or {}).get("number")
    if price is not None:
        line = f"**Price:** {px(price)}"
        if cp is not None:
            line += f" {'📈' if cp >= 0 else '📉'} {pct(cp)}"
            if chg is not None:
                c = f"${abs(chg):,.4f}" if abs(price) < 1 else f"${abs(chg):,.2f}"
                line += f" ({'+' if chg >= 0 else '−'}{c})"
        dt = parse_et(g(sa, "price", "as_of"))
        td = val(sa, "trading_date")
        if val(sa, "market_status") == "closed" and (dt or td):
            d0 = dt or datetime.strptime(td, "%Y-%m-%d")
            line += f" · last session, {d0.strftime('%a')} {fmt_day(d0)} close"
        elif dt:
            line += f" · as of {et_pt(dt, day=False)}"
        hi, lo = val(sa, "high"), val(sa, "low")
        if hi is not None and lo is not None:
            line += f" · range {px(lo)}–{px(hi)}"
        L.append(line + f" · {psrc}")
    ext = val(sa, "extended")
    if isinstance(ext, dict) and ext.get("price") is not None:
        line = f"**{ext.get('session') or 'Extended hours'}:** {px(ext['price'])}"
        if ext.get("change_pct") is not None:
            line += f" {'📈' if ext['change_pct'] >= 0 else '📉'} {pct(ext['change_pct'])}"
        edt = parse_et(g(sa, "extended", "as_of"))
        if edt:
            line += f" · {et_pt(edt, day=False)}"
        L.append(line + f" · {src(sa, 'extended')}")
    vol, vsrc = val(sa, "volume"), src(sa, "volume")
    if vol is None and val(fv, "Volume") is not None:
        vol, vsrc = (val(fv, "Volume") or {}).get("number"), "Finviz"
    rv = (val(fv, "Rel Volume") or {}).get("number")
    av = (val(fv, "Avg Volume") or {}).get("number")
    bits = []
    if vol is not None:
        bits.append(f"{big(vol)} ({vsrc})")
    if rv is not None:
        bits.append(f"rel. volume {rv:g}×" + (f" vs avg {big(av)}" if av else "") + " (Finviz)")
    if bits:
        L.append("**Volume:** " + " · ".join(bits))
    fl = val(fv, "Shs Float")
    if isinstance(fl, dict) and fl.get("number") is not None:
        L.append(f"**Float:** {big(fl['number'])} (Finviz)")
    def iso_day(raw):
        try:
            return fmt_day(datetime.strptime(raw, "%Y-%m-%d"))
        except Exception:
            return raw

    def verified_share_text(node):
        source = node.get("source") or "SEC"
        m = re.match(r"SEC\s+(.+?)\s+filed\s+\d{4}-\d{2}-\d{2}", source)
        form = m.group(1) if m else source.removeprefix("SEC ")
        text = f"{big(node['value'])} (SEC {form}, as of {iso_day(node.get('as_of'))}"
        if node.get("adjusted_by_splits"):
            adj = node.get("split_adjustments") or []
            for split in adj:
                text += f", adjusted {split.get('text')} for {iso_day(split.get('date'))} reverse split"
        return text + ")"

    def disagreement_note(verified):
        # Keep the report concise: mention only the first materially stale source.
        candidates = (("StockAnalysis", val(sa, "shares_out")),
                      ("Finviz", (val(fv, "Shs Outstand") or {}).get("number")
                       if isinstance(val(fv, "Shs Outstand"), dict) else None))
        for name, n in candidates:
            if n is not None and verified and abs(n / verified - 1) > 0.20:
                return f" ⚠️ {name} {big(n)} differs {(n / verified - 1) * 100:+.0f}% from verified {big(verified)} (stale)"
        return ""

    sh = []
    verified_node = g(s, "shares_verified") or {}
    verified = val(s, "shares_verified")
    if verified is not None:
        sh.append(verified_share_text(dict(verified_node)) + disagreement_note(verified))
    else:
        # A single fallback only; do not print a chain of stale filings/sources.
        so = val(sa, "shares_out")
        if so is not None:
            sh.append(f"{big(so)} (StockAnalysis.com / S&P Global)")
        else:
            sec_so = g(s, "sec", "financials", "shares_outstanding")
            if val(sec_so) is not None:
                sh.append(f"{big(val(sec_so))} (SEC cover, as of {iso_day(g(sec_so, 'as_of'))})")
            else:
                fso = val(fv, "Shs Outstand")
                if isinstance(fso, dict) and fso.get("number") is not None:
                    sh.append(f"{big(fso['number'])} (Finviz)")
    if sh:
        L.append("**Shares outstanding:** " + " · ".join(sh))
    mc = val(sa, "market_cap")
    price_for_mc = val(sa, "price")
    if mc is not None and verified is not None and price_for_mc:
        implied_shares = mc / price_for_mc
        if abs(implied_shares / verified - 1) > 0.20:
            verified_mc = verified * price_for_mc
            L.append(f"**Market cap:** ~{big(verified_mc, money=True)} (verified {big(verified)} shares × {px(price_for_mc)}) — StockAnalysis's {big(mc, money=True)} figure is stale")
        else:
            L.append(f"**Market cap:** {big(mc, money=True)} ({src(sa, 'market_cap')})")
    elif mc is not None:
        L.append(f"**Market cap:** {big(mc, money=True)} ({src(sa, 'market_cap')})")
    elif isinstance(val(fv, "Market Cap"), dict) and val(fv, "Market Cap").get("number") is not None:
        L.append(f"**Market cap:** {big(val(fv, 'Market Cap')['number'], money=True)} (Finviz)")
    si = short_interest_line(s)
    if si:
        L.append(si)
    return L


def short_interest_line(s):
    """Short interest from prefetch's short_interest block (Nasdaq/FINRA settlement date,
    split-adjusted). Falls back to raw Finviz, marked undated. None only if no data at all."""
    v = val(s, "short_interest")
    if isinstance(v, dict) and v.get("shares_short") is not None:
        t = f"**Short interest:** {big(v['shares_short'])} shares"
        if v.get("pct_float") is not None:
            t += f" ({v['pct_float']:.1f}% of float)"
        if v.get("pct_shares_out") is not None:
            t += f" · {v['pct_shares_out']:.1f}% of shares out"
        if v.get("days_to_cover") is not None:
            t += f" · days to cover {v['days_to_cover']:.2f} (30-day avg vol)"
        try:
            sd = fmt_day(datetime.strptime(v["settlement_date"], "%Y-%m-%d"))
        except Exception:
            sd = v.get("settlement_date")
        lab = " (inferred)" if v.get("settlement_date_label") == "inferred" else ""
        t += f" · settled {sd}{lab} ({src(s, 'short_interest')})"
        if v.get("split_adjusted"):
            t += f" · split-adjusted {v.get('split_ratio')} (reported {v['shares_short_reported']:,.0f} pre-split)"
        fl = []
        if (v.get("age_days") or 0) > 20:
            fl.append(f"{v['age_days']} days old")
        sp = v.get("latest_spike")
        if sp and sp.get("date", "") > (v.get("settlement_date") or ""):
            fl.append(f"pre-spike, likely stale (spike {fmt_day(datetime.strptime(sp['date'], '%Y-%m-%d'))})")
        if fl:
            t += " · ⚠️ " + "; ".join(fl)
        return t
    fv = g(s, "finviz") or {}
    si, sf = val(fv, "Short Interest"), val(fv, "Short Float")
    if isinstance(si, dict) and si.get("number") is not None:
        t = f"**Short interest:** {si['text']} shares"
        if isinstance(sf, dict) and sf.get("number") is not None:
            t += f" ({sf['text']} of float)"
        return t + " · Finviz, no settlement date ⚠️ unverified, possibly stale"
    return None


def filing_lines(s):
    fl = val(s, "sec", "filings_12m") or val(s, "sec", "filings_30d") or []
    if not fl:
        return []
    hits = set(g(s, "quick_read_inputs", "dilution_flag_preliminary", "value", "doc_term_hits", "reverse split") or [])
    for x in g(s, "cross_checks") or []:
        if "reverse-split" in x:
            hits.update(re.findall(r"((?:8-K|6-K|PRE 14C|DEF 14C)\S* \d{4}-\d{2}-\d{2})", x))
    groups = []  # collapse consecutive identical non-flagged forms (e.g. many 144s)
    for f in fl:
        fm = f.get("form") or "?"
        if groups and groups[-1][0]["form"] == fm and not FLAG_FORM.match(fm):
            groups[-1].append(f)
        else:
            groups.append([f])
        if len(groups) > 1:
            groups.pop()
            break
    today = date.today()
    out = []
    for grp in groups:
        f = grp[0]
        fm = f["form"]
        try:
            d1 = datetime.strptime(f["filed"], "%Y-%m-%d")
        except Exception:
            continue
        rs = fm.startswith(("8-K", "6-K", "PRE 14", "DEF 14")) and f"{fm} {f['filed']}" in hits
        flag = bool(FLAG_FORM.match(fm)) or rs
        name = f"[{fm}]({f['url']})" if (f.get("url") or "").startswith("http") else fm
        line = ("⚠️ " if flag else "") + f"**{name}**" + (f" ×{len(grp)}" if len(grp) > 1 else "")
        if len(grp) > 1:
            d0 = datetime.strptime(grp[-1]["filed"], "%Y-%m-%d")
            line += f" · {fmt_day(d0)}–{fmt_day(d1)}"
        else:
            line += f" · {fmt_day(d1)}"
            acc = f.get("accepted")
            if acc and (today - d1.date()).days <= 7:
                a = parse_et(acc)
                if a:
                    line += f" {et_pt(a, day=False)}"
        desc = []
        if f.get("items") and fm.startswith("8-K"):
            its = [i.strip() for i in f["items"].split(",") if i.strip() and i.strip() != "9.01"]
            desc.append("items " + ", ".join(f"{i} {ITEMS[i]}" if i in ITEMS else i for i in its))
        elif fm.startswith("424B"):
            desc.append("prospectus")
        elif re.match(r"^(S-1|F-1|S-3|F-3)", fm):
            desc.append("registration statement")
        elif fm.startswith("6-K"):
            desc.append("foreign-issuer report")
        if rs:
            desc.append("mentions reverse split")
        if desc:
            line += " · " + "; ".join(desc)
        out.append("- " + line)
    return out


def headline_lines(s):
    items = []
    for n in val(s, "stockanalysis", "news") or []:
        dt = parse_et(n.get("time"))
        items.append((dt, n.get("title"), n.get("source"), n.get("url")))
    for n in (val(s, "finviz", "news") or []):
        try:
            dt = datetime.strptime(f"{n['date']} {n['time_et']}", "%b-%d-%y %I:%M%p")
        except Exception:
            dt = None
        items.append((dt, n.get("title"), n.get("source"), n.get("url")))
    seen, out = set(), []
    items = [i for i in items if i[1]]
    items.sort(key=lambda i: i[0] or datetime.min, reverse=True)
    for dt, title, source, url in items:
        k = re.sub(r"[^a-z0-9]", "", title.lower())[:40]
        if k in seen:
            continue
        seen.add(k)
        title = title.replace("[", "(").replace("]", ")")
        t = f"[{title}]({url})" if (url or "").startswith("http") else title
        when = et_pt(dt) if dt else "time n/a"
        out.append(f"- {when} — {t}" + (f" ({source})" if source else ""))
        if len(out) == 1:
            break
    return out


def snapshot(s, cached=False):
    t = s.get("ticker") or "?"
    name = val(s, "stockanalysis", "name") or val(s, "sec", "sec_company_name")
    out = []
    st = stamp(s, cached)
    if st:
        out.append(st)
    out.append(f"⏳ **${t}" + (f" · {name}" if name else "") + "** — first look (full analysis coming)")
    out.append("")
    out += ["- " + x for x in quote_lines(s)]
    fl = filing_lines(s)
    if fl:
        out += ["", "**Latest filing** (SEC EDGAR; Form 4s omitted):"] + fl
    hl = headline_lines(s)
    if hl:
        out += ["", "**Latest headline** (StockAnalysis.com + Finviz news):"] + hl
    ch = val(s, "chart")
    out += ["", f"CHART: {ch if ch and os.path.isfile(ch) else 'none'}"]
    return "\n".join(out)


def spike(s):
    """Largest 5-minute moves in the latest session of yahoo_5m.json (facts only)."""
    p = os.path.join(s.get("cache_dir") or "", "yahoo_5m.json")
    try:
        bars = json.load(open(p))["bars"]
    except Exception as e:
        return f"5-min bars unavailable ({e})"
    if not bars:
        return "5-min bars unavailable (empty)"
    day = bars[-1]["t_et"][:10]
    prev_c = None
    rows = []
    for b in bars:
        if b["t_et"][:10] == day and prev_c:
            rows.append(((b["h"] - prev_c) / prev_c * 100, (b["c"] - prev_c) / prev_c * 100, b))
        prev_c = b["c"]
    if not rows:
        return "5-min bars: no bars for the latest session"
    out = [f"Yahoo 5-min bars, session {day} (times ET / PT; unofficial source):"]
    for lab, key in (("biggest 5-min jump (high vs prior close)", 0), ("biggest 5-min close-to-close gain", 1)):
        r = max(rows, key=lambda r: r[key])
        dt = datetime.strptime(r[2]["t_et"], "%Y-%m-%d %H:%M")
        out.append(f"- {lab}: {r[key]:+.1f}% at {et_pt(dt, day=False)} (bar H {px(r[2]['h'])} C {px(r[2]['c'])}, vol {big(r[2]['v'])})")
    vb = sorted((r[2] for r in rows), key=lambda b: b["v"] or 0, reverse=True)[:3]
    out.append("- top-volume bars: " + "; ".join(
        f"{et_pt(datetime.strptime(b['t_et'], '%Y-%m-%d %H:%M'), day=False)} vol {big(b['v'])} C {px(b['c'])}" for b in vb))
    return "\n".join(out)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args or "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__)
        sys.exit(0 if args else 1)
    path, cached = find_summary(args[0])
    with open(path) as fh:
        s = json.load(fh)
    print(spike(s) if "--spike" in sys.argv else snapshot(s, cached))


if __name__ == "__main__":
    main()
