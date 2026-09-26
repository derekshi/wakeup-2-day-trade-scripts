#!/usr/bin/env python3
"""Scorecard image for a runner quick analysis (runner-quick-analysis).

Usage: python3 scorecard.py --json card.json [out.png]
Renders a mobile-first dark-theme summary card (800x900 logical px, saved at
2x = 1600x1800) AFTER the analyst has written the report, because dilution risk,
catalyst strength and the verdict are research judgments, not prefetch data.
Layout, top to bottom: ticker, company, price and % change; three big rating
tiles (DILUTION RISK, CATALYST, CASH RUNWAY) filled/bordered in their level
colour; secondary stats (market cap, float, rel. volume, short interest, next
catalyst, verdict); footer with source and as-of. Long text wraps or shrinks
to fit, so nothing is cut off.

card.json keys (all optional except ticker; missing values show "n/a"):
  ticker, company, price, pct_change (last session, number or "-3.33%"),
  as_of ("Fri Sep 25 close"), market_cap, float, rel_volume, short_interest,
  dilution_risk   (Low | Moderate | High | Very High),
  catalyst_strength (Strong | Moderate | Weak | Unclear),
  runway          ({"text": "...", "level": "green|yellow|orange|red" or
                    Low/Moderate/High/Very High risk} or plain text/months),
  next_catalyst, verdict, source (default "StockAnalysis.com"),
  date (YYYY-MM-DD, used in the default file name; default today).
Colour scale (same as the report emoji): green = Low risk / Strong catalyst /
runway >24 mo or cash-flow positive; yellow = Moderate / 12-24 mo; orange =
High / Weak / 6-12 mo; red = Very High / Unclear / <6 mo.
Default output: /workspace/runner-charts/<TICKER>-card-<date>.png
Never crashes on bad or missing fields; emoji are stripped (matplotlib fonts
render them as boxes), a backslash-escaped dollar shows as "$" (mathtext is
off), and the arrow is drawn as a shape.
"""
import json, os, re, sys, time
from datetime import date

T0 = time.time()
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Polygon

W, H = 800, 900          # logical px (figure px at dpi 100); saved at 2x
BG, PANEL, LINE = "#0f131a", "#161b24", "#2a3140"
TXT, SUB, DIM = "#f2f4f8", "#9aa3b2", "#5d6675"
UP, DOWN, FLAT = "#22c55e", "#ef4444", "#9aa3b2"
LEVEL_COLORS = ["#22c55e", "#eab308", "#f97316", "#ef4444"]  # green yellow orange red
LEVEL_TINTS = ["#12301f", "#332b0c", "#3a2210", "#3a1515"]   # dark fills for tiles
NA = "n/a"
M = 36                   # outer text margin

EMOJI_RE = re.compile("[\U0001F000-\U0001FFFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3]")


def clean(v, default=NA):
    """String for display: strips emoji, collapses spaces, n/a when empty."""
    if v is None:
        return default
    if isinstance(v, float):
        v = f"{v:g}"
    s = EMOJI_RE.sub("", str(v)).replace("\\$", "$")
    s = re.sub(r"\s+", " ", s).strip(" ·|-")
    return s if s and s.lower() not in ("none", "null", "nan") else default


def num(v):
    try:
        if isinstance(v, (int, float)):
            return float(v)
        s = clean(v, "").replace("\u2212", "-").replace("$", "").replace(",", "").replace("%", "").strip()
        return float(s) if s else None
    except (ValueError, TypeError):
        return None


def level_idx(value, kind):
    """0..3 (green..red) or None."""
    s = clean(value, "").lower()
    if not s:
        return None
    for i, c in enumerate(("green", "yellow", "orange", "red")):
        if c in s:
            return i
    if kind == "catalyst":
        table = [("very strong", 0), ("strong", 0), ("moderate", 1), ("medium", 1),
                 ("very weak", 3), ("weak", 2), ("unclear", 3), ("none", 3), ("unverified", 3)]
    else:
        table = [("very high", 3), ("very low", 0), ("low", 0), ("moderate", 1),
                 ("medium", 1), ("high", 2)]
    if kind == "runway":
        if "positive" in s or "profitable" in s or "not needed" in s:
            return 0
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:\+)?\s*(?:mo|month)", s)
        if m:
            mo = float(m.group(1))
            return 0 if mo > 24 else 1 if mo >= 12 else 2 if mo >= 6 else 3
    for k, i in table:
        if k in s:
            return i
    return None


def fmt_price(v):
    n = num(v)
    if n is None:
        return clean(v)
    return f"${n:,.2f}" if n >= 1 else f"${n:,.4f}"


class Card:
    def __init__(self):
        self.fig = plt.figure(figsize=(W / 100, H / 100), dpi=100, facecolor=BG)
        self.ax = self.fig.add_axes([0, 0, 1, 1])
        self.ax.set_xlim(0, W); self.ax.set_ylim(H, 0); self.ax.axis("off")
        self.r = self.fig.canvas.get_renderer()
        self.family = "DejaVu Sans"

    def width(self, t):
        bb = t.get_window_extent(renderer=self.r)
        return bb.width  # figure px == data px (dpi 100, axes = figure)

    def text(self, x, y, s, size, color=TXT, weight="normal", ha="left", va="baseline",
             maxw=None, minsize=8, style="normal"):
        t = self.ax.text(x, y, s, fontsize=size, color=color, weight=weight, ha=ha, va=va,
                         family=self.family, style=style, parse_math=False)
        if maxw:
            while self.width(t) > maxw and size > minsize:
                size -= 0.5; t.set_fontsize(size)
            if self.width(t) > maxw:  # still too wide: ellipsize
                while len(s) > 4 and self.width(t) > maxw:
                    s = s[:-2]; t.set_text(s.rstrip() + "…")
        return t

    def wrap(self, s, size, maxw, maxlines=None, weight="bold", style="normal"):
        """Greedy word wrap at a fixed size; prefers breaking at ' · ' / ';'.
        Returns (lines, fits) where fits is False if a word or the line count overflows."""
        probe = self.ax.text(0, 0, "", fontsize=size, family=self.family, weight=weight,
                             style=style, parse_math=False)
        def fits(x):
            probe.set_text(x); return self.width(probe) <= maxw
        chunks = [p.strip() for p in re.split(r"\s+·\s+|;\s*", s) if p.strip()] or [s]
        lines, cur, ok = [], "", True
        for ch in chunks:
            cand = f"{cur} · {ch}" if cur else ch
            if fits(cand):
                cur = cand; continue
            if cur:
                lines.append(cur)
            cur = ""
            for w in ch.split():  # chunk itself too long: wrap by words
                cand = f"{cur} {w}".strip()
                if fits(cand) or not cur:
                    cur = cand
                    if not fits(cand):
                        ok = False
                else:
                    lines.append(cur); cur = w
        if cur:
            lines.append(cur)
        probe.remove()
        if maxlines and len(lines) > maxlines:
            ok = False
        return lines, ok

    def fit_block(self, s, sizes, maxw, maxlines, weight="bold", style="normal"):
        """Largest size in `sizes` at which `s` wraps into <= maxlines lines without
        overflow. Returns (size, lines); the last resort ellipsizes the final line."""
        for sz in sizes:
            lines, ok = self.wrap(s, sz, maxw, maxlines, weight, style)
            if ok:
                return sz, lines
        sz = sizes[-1]
        lines, _ = self.wrap(s, sz, maxw, None, weight, style)
        lines = lines[:maxlines]
        if len(lines) == maxlines:
            lines[-1] = lines[-1].rstrip(" ·,;") + " …"
        return sz, lines

    def rbox(self, x, y, w, h, color, r=8, alpha=1.0, ec="none", lw=0):
        p = FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}",
                           fc=color, ec=ec, lw=lw, alpha=alpha, mutation_aspect=1)
        self.ax.add_patch(p)
        return p


PT = 100 / 72  # logical px per point


def render(d, out):
    c = Card()
    ax = c.ax
    c.rbox(10, 10, W - 20, H - 20, PANEL, r=22, ec=LINE, lw=1.2)

    # ---- header: ticker + price on one line, company + % change below
    ticker = clean(d.get("ticker"), "?").upper().lstrip("$")
    company = clean(d.get("company") or d.get("company_name") or d.get("name"))
    price = fmt_price(d.get("price"))
    pct = num(d.get("pct_change"))
    as_of = clean(d.get("as_of"))

    t_pr = c.text(W - M, 86, price, 46, weight="bold", ha="right", maxw=330, minsize=26)
    c.text(M, 86, ticker, 60, weight="bold", maxw=W - 2 * M - c.width(t_pr) - 24, minsize=30)
    if pct is None:
        pcol, ptxt = FLAT, NA
    else:
        pcol = UP if pct > 0 else DOWN if pct < 0 else FLAT
        ptxt = f"{'+' if pct > 0 else '−' if pct < 0 else ''}{abs(pct):,.{2 if abs(pct) < 100 else 1}f}%"
    t_pct = c.text(W - M, 136, ptxt, 36, color=pcol, weight="bold", ha="right", maxw=300, minsize=20)
    wp = c.width(t_pct)
    if pct is not None and pct != 0:
        ax_ = W - M - wp - 42   # arrow left of the %
        cy = 123
        pts = ([(ax_, cy + 14), (ax_ + 30, cy + 14), (ax_ + 15, cy - 14)] if pct > 0 else
               [(ax_, cy - 14), (ax_ + 30, cy - 14), (ax_ + 15, cy + 14)])
        ax.add_patch(Polygon(pts, closed=True, fc=pcol, ec="none"))
        wp += 42
    cw = W - 2 * M - wp - 24
    sz, lines = c.fit_block(company, [25, 23, 21, 19], cw, 1, weight="normal")
    if not c.wrap(company, sz, cw, 1, weight="normal")[1]:
        sz, lines = c.fit_block(company, [19, 18, 17, 16, 15, 14], cw, 2, weight="normal")
    for j, ln in enumerate(lines):
        c.text(M, 132 - (len(lines) - 1 - j) * sz * PT * 1.15 + (6 if len(lines) > 1 else 0),
               ln, sz, color=SUB)
    c.text(W - M, 166, f"last session · {as_of}" if as_of != NA else "last session",
           15, color=SUB, ha="right", maxw=W - 2 * M)

    # ---- rating tiles (dominant element)
    rw = d.get("runway")
    if isinstance(rw, dict):
        rw_text = clean(rw.get("text"))
        rw_idx = level_idx(rw.get("level"), "runway")
        rw_text = re.sub(r"^n/?a\s*[·:;,(/-]+\s*", "", rw_text, flags=re.I) or NA
        if rw_text.endswith(")") and rw_text.count(")") > rw_text.count("("):
            rw_text = rw_text[:-1].rstrip() or NA   # drop only an unbalanced ")"
        if rw_idx is None:
            rw_idx = level_idx(rw.get("text"), "runway")
    else:
        rw_text = clean(rw) if not isinstance(rw, (int, float)) else f"~{rw:g} months"
        rw_idx = level_idx(rw_text if not isinstance(rw, (int, float)) else f"{rw} months", "runway")
    dil = clean(d.get("dilution_risk")); cat = clean(d.get("catalyst_strength"))
    cap1 = lambda v: v if v == NA else v[:1].upper() + v[1:]
    rows = [("DILUTION RISK", dil.upper() if dil != NA else NA, level_idx(dil, "risk"),
             ("Low", "Mod", "High", "V.High")),
            ("CATALYST", cat.upper() if cat != NA else NA, level_idx(cat, "catalyst"),
             ("Strong", "Mod", "Weak", "Unclear")),
            ("CASH RUNWAY", cap1(rw_text), rw_idx, (">24m", "12–24", "6–12", "<6m"))]
    ty, th, tg = 184, 134, 10
    tx, tw = 22, W - 44
    for i, (lab, val, idx, ticks) in enumerate(rows):
        y = ty + i * (th + tg)
        col = LEVEL_COLORS[idx] if idx is not None else SUB
        tint = LEVEL_TINTS[idx] if idx is not None else "#1d2330"
        tile = c.rbox(tx, y, tw, th, tint, r=18, ec=col, lw=3.5)
        bar = plt.Rectangle((tx, y), 14, th, fc=col, ec="none")   # solid accent bar
        c.ax.add_patch(bar); bar.set_clip_path(tile)
        ix = tx + 40
        c.text(ix, y + 36, lab, 20, color=col, weight="bold")
        # mini 4-step scale, top right
        sw, sh, gap = 50, 11, 6
        sx = tx + tw - 22 - 4 * sw - 3 * gap
        for k in range(4):
            on = idx == k
            c.rbox(sx + k * (sw + gap), y + 18, sw, sh, LEVEL_COLORS[k], r=4,
                   alpha=1.0 if on else 0.22)
            c.text(sx + k * (sw + gap) + sw / 2, y + 44, ticks[k], 10,
                   color=TXT if on else DIM, ha="center", weight="bold" if on else "normal")
        vw = tw - 40 - 26
        vcol = TXT if idx is not None else SUB
        sz, lines = c.fit_block(val, [56, 52, 48, 44, 40, 37], vw, 1)
        if not c.wrap(val, sz, vw, 1)[1]:
            sz, lines = c.fit_block(val, [32, 30, 28, 25, 22], vw, 2)
        if len(lines) == 1:
            c.text(ix, y + th - 24, lines[0], sz, color=vcol, weight="bold")
        else:
            lh = sz * PT * 1.08
            for j, ln in enumerate(lines):
                c.text(ix, y + th - 18 - (len(lines) - 1 - j) * lh, ln, sz, color=vcol, weight="bold")

    # ---- footer (drawn first so the stats block knows how much room it has)
    fy = H - 24
    fw = W - 2 * M
    src = clean(d.get("source"), "StockAnalysis.com")
    foot = f"Source: {src}" + (f" · as of {as_of}" if as_of != NA else "")
    t_na = c.text(W - M, fy, "Not investment advice", 12.5, color=SUB, ha="right", style="italic")
    sw_ = fw - c.width(t_na) - 20
    sz, flines = c.fit_block(foot, [12.5, 12, 11.5, 11, 10.5, 10], sw_, 1, weight="normal")
    if not c.wrap(foot, sz, sw_, 1, weight="normal")[1]:
        sz, flines = c.fit_block(foot, [11.5, 11, 10.5, 10, 9.5, 9], sw_, 2, weight="normal")
    for j, ln in enumerate(flines):
        c.text(M, fy - (len(flines) - 1 - j) * sz * PT * 1.2, ln, sz, color=SUB)
    fdiv = fy - (len(flines) - 1) * sz * PT * 1.2 - 24
    ax.plot([M, W - M], [fdiv, fdiv], color=LINE, lw=1.2)

    # ---- secondary stats: 2x2 grid, then next catalyst and verdict. Laid out as a
    # list of text ops, shrunk until the block fits between the tiles and the footer.
    y0 = ty + 3 * th + 2 * tg + 14
    ybot = fdiv - 10                  # footer divider minus padding
    rv = d.get("rel_volume")
    rvn = num(rv)
    rv_s = f"{rvn:.2f}x" if rvn is not None and not re.search(r"[a-wyz]", str(rv), re.I) else clean(rv)
    facts = [("MARKET CAP", clean(d.get("market_cap"))), ("FLOAT", clean(d.get("float"))),
             ("REL. VOLUME", rv_s), ("SHORT INTEREST", clean(d.get("short_interest")))]
    colw = (W - 2 * M - 16) / 2
    fw = W - 2 * M
    nc = clean(d.get("next_catalyst"))
    vd = clean(d.get("verdict"))

    def layout(k, maxl):
        ops, y = [], y0                 # op: (x, y, text, size, color, weight, style)
        ls = 13.5 * k                   # label size
        for r in range(2):
            rowh = 0
            for cidx in range(2):
                lab, v = facts[r * 2 + cidx]
                x = M + cidx * (colw + 16)
                ops.append((x, y + ls * PT, lab, ls, SUB, "bold", "normal"))
                sz, lines = c.fit_block(v, [s_ * k for s_ in (20, 18, 16)], colw - 8, 2)
                yy = y + ls * PT - sz * PT * 0.2 + 6
                for ln in lines:
                    yy += sz * PT * 1.2
                    ops.append((x, yy, ln, sz, TXT, "bold", "normal"))
                rowh = max(rowh, yy - y)
            y += rowh + 10 * k
        ops.append(("line", y))
        y += 8 * k
        for lab, v, wt, st in (("NEXT CATALYST", nc, "bold", "normal"),
                               ("VERDICT", vd, "normal", "italic")):
            y += ls * PT + 4
            ops.append((M, y, lab, ls, SUB, "bold", "normal"))
            sz, lines = c.fit_block(v, [s_ * k for s_ in (17, 16, 15, 14)], fw, maxl, wt, st)
            for ln in lines:
                y += sz * PT * 1.2
                ops.append((M, y, ln, sz, TXT, wt, st))
            y += 10 * k
        return ops, y - 10 * k

    tries = [(k, 3) for k in (1.0, 0.95, 0.9, 0.85)] + \
            [(k, 2) for k in (1.0, 0.9, 0.8, 0.75, 0.7)] + [(0.7, 1)]
    for k, maxl in tries:
        ops, yend = layout(k, maxl)
        if yend <= ybot:
            break
    for op in ops:
        if op[0] == "line":
            ax.plot([M, W - M], [op[1], op[1]], color=LINE, lw=1.2)
        else:
            x, y, t, sz, col, wt, st = op
            c.text(x, y, t, sz, color=col, weight=wt, style=st)

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    c.fig.savefig(out, dpi=200, facecolor=BG)
    plt.close(c.fig)


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help") or "--json" not in argv:
        print(__doc__); sys.exit(0 if argv and argv[0] in ("-h", "--help") else 1)
    i = argv.index("--json")
    path = argv[i + 1] if i + 1 < len(argv) else None
    rest = argv[:i] + argv[i + 2:]
    try:
        d = json.load(open(path, encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError("card.json must be an object")
    except Exception as e:
        print(f"scorecard.py: cannot read {path}: {e}", file=sys.stderr); sys.exit(2)
    tk = re.sub(r"[^A-Z0-9.\-]", "", clean(d.get("ticker"), "CARD").upper()) or "CARD"
    dt = clean(d.get("date"), "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dt):
        dt = date.today().isoformat()
    out = rest[0] if rest else f"/workspace/runner-charts/{tk}-card-{dt}.png"
    try:
        render(d, out)
    except Exception as e:
        print(f"scorecard.py: render failed: {type(e).__name__}: {e}", file=sys.stderr); sys.exit(3)
    print(out)
    print(f"scorecard.py: rendered in {time.time() - T0:.2f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
