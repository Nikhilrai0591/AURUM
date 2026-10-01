"""AURUM API - MCX gold relative-value intelligence (FastAPI)."""
import io, os, json
import numpy as np, pandas as pd, requests
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="AURUM API")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS", "*").split(","),
                   allow_methods=["*"], allow_headers=["*"])

# symbol -> (quote base grams, purity)   [from MCX contract specs]
SPEC = {"GOLDM": (10, .995), "GOLDTEN": (10, .999), "GOLDGUINEA": (8, .999), "GOLDPETAL": (1, .999)}
REF, OTHERS = "GOLDM", ["GOLDTEN", "GOLDGUINEA", "GOLDPETAL"]
MCX_URL = os.getenv("MCX_URL", "https://www.mcxindia.com/backpage.aspx/GetDateWiseBhavCopy")
S = {"raw": None, "prep": None, "sp": None, "source": "none"}

def J(df): return json.loads(df.to_json(orient="records", date_format="iso"))

# ---------- data: demo / upload / live fetch ----------
def demo(seed=7):
    rng = np.random.default_rng(seed); rows = []
    days = pd.bdate_range("2025-06-02", "2026-09-29")
    spot = 135000 * np.exp(np.cumsum(rng.normal(3e-4, .008, len(days))))  # Rs/10g, 999
    noise = {"GOLDM": 1.5, "GOLDTEN": 3, "GOLDGUINEA": 6, "GOLDPETAL": 10}
    for k, d in enumerate(days):
        for s, (bg, pur) in SPEC.items():
            if s == "GOLDTEN" and d < pd.Timestamp("2025-09-01"): continue
            exps = []
            for m in range(-1, 6):
                t = d + pd.DateOffset(months=m)
                e = pd.Timestamp(t.year, t.month, 5) if s == "GOLDM" else t + pd.offsets.MonthEnd(0) - pd.Timedelta(days=1)
                if e > d: exps.append(e)
            for n, e in enumerate(sorted(exps)[:4]):
                dte = (e - d).days
                nb = spot[k] * np.exp(.065 / 365 * dte) * (1 + rng.normal(0, noise[s]) / 1e4)
                if s != "GOLDM" and rng.random() < .02: nb *= 1 + rng.choice([-1, 1]) * rng.uniform(15, 35) / 1e4
                base = {"GOLDM": 4000, "GOLDTEN": 500, "GOLDGUINEA": 150, "GOLDPETAL": 300}[s] / (1 + 2.5 * n)
                vol = int(max(0, rng.lognormal(np.log(base + 1), .7)))
                rows.append((d, s, e, nb * bg / 10 * pur / .999, vol, int(vol * rng.uniform(3, 8))))
    return pd.DataFrame(rows, columns=["date", "symbol", "expiry", "close", "volume", "oi"])

def clean(raw):
    r = raw.rename(columns=lambda c: c.strip().lower())
    r = r.rename(columns={"expirydate": "expiry", "openinterest": "oi"})
    r["symbol"] = r["symbol"].astype(str).str.strip()
    r = r[r.symbol.isin(SPEC)].copy()
    r["date"] = pd.to_datetime(r["date"], format="%m/%d/%Y", errors="coerce")  # response Date is MM/DD/YYYY
    r["expiry"] = pd.to_datetime(r["expiry"].astype(str).str.strip(), format="%d%b%Y", errors="coerce")
    for c in ["close", "volume", "oi"]: r[c] = pd.to_numeric(r[c], errors="coerce")
    return r.dropna(subset=["date", "expiry", "close"])[["date", "symbol", "expiry", "close", "volume", "oi"]]

def set_data(df, source):
    df = df.drop_duplicates(["date", "symbol", "expiry"]).sort_values(["date", "symbol", "expiry"]).reset_index(drop=True)
    p = df.copy()
    p["norm"] = p.close / p.symbol.map(lambda s: SPEC[s][0]) * 10 * .999 / p.symbol.map(lambda s: SPEC[s][1])  # Rs/10g @999
    p["dte"] = (p.expiry - p.date).dt.days
    S.update(raw=df, prep=p, sp=build_spreads(p), source=source)

def build_spreads(p):
    out = []
    for d, g in p.groupby("date"):
        R = g[g.symbol == REF]
        if R.empty: continue
        slope = np.polyfit(R.dte, np.log(R.norm), 1)[0] if R.dte.nunique() > 1 else 0.0  # daily carry from curve
        for s in OTHERS:
            for _, b in g[g.symbol == s].iterrows():
                a = R.iloc[(R.dte - b.dte).abs().argsort().iloc[0]]
                if abs(a.dte - b.dte) > 35: continue
                bps = 1e4 * (np.log(b.norm / a.norm) - slope * (b.dte - a.dte))  # carry-adjusted
                out.append(dict(date=d, pair=f"{s}/{REF}", exp_a=a.expiry, exp_b=b.expiry, dte_b=b.dte, bps=bps,
                                na=a.norm, nb=b.norm, va=a.volume, vb=b.volume, oa=a.oi, ob=b.oi))
    return pd.DataFrame(out)

def signals(win=20, z=2.0, minvol=50, minoi=100, cost=1.0, slip=3.0, thin=100):
    sp = S["sp"].sort_values(["pair", "exp_b", "date"]).copy()
    g = sp.groupby(["pair", "exp_b"]).bps
    sp["mu"] = g.transform(lambda s: s.shift(1).rolling(win, min_periods=10).mean())   # past-only baseline
    sp["sd"] = g.transform(lambda s: s.shift(1).rolling(win, min_periods=10).std())
    sp["z"] = (sp.bps - sp.mu) / sp.sd.replace(0, np.nan)
    sp["vmin"], sp["omin"] = np.minimum(sp.va, sp.vb), np.minimum(sp.oa, sp.ob)
    sp["liquid"] = (sp.vmin >= minvol) & (sp.omin >= minoi)
    sp["edge"] = (sp.bps - sp.mu).abs()
    sp["hurdle"] = 4 * cost + 2 * slip * (sp.vmin < thin)   # 4 fills + thin-day slippage, in bps
    sp["signal"] = (sp.z.abs() >= z) & sp.liquid & (sp.edge > sp.hurdle)
    return sp

# ---------- endpoints ----------
@app.on_event("startup")
def _init(): set_data(demo(), "SYNTHETIC DEMO")

@app.get("/api/meta")
def meta():
    p = S["prep"]
    return dict(source=S["source"], start=str(p.date.min().date()), end=str(p.date.max().date()),
                dates=[str(d.date()) for d in sorted(p.date.unique())], spec=SPEC)

@app.post("/api/demo")
def load_demo(): set_data(demo(), "SYNTHETIC DEMO"); return meta()

@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    frames = [clean(pd.read_csv(io.BytesIO(await f.read()))) for f in files]
    df = pd.concat(frames)
    if df.empty: raise HTTPException(400, "No GOLDM/GOLDTEN/GOLDGUINEA/GOLDPETAL rows parsed")
    set_data(df, "MCX BHAVCOPY (upload)"); return meta()

@app.post("/api/fetch")
def fetch(date: str):  # date = YYYY-MM-DD ; best-effort live pull, validated against returned Date
    d = pd.Timestamp(date)
    try:
        r = requests.post(MCX_URL, json={"Date": d.strftime("%Y%m%d"), "InstrumentName": "ALL"},
                          headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}, timeout=25)
        rows = clean(pd.DataFrame(r.json()["d"]["Data"]))
    except Exception as e:
        raise HTTPException(502, f"MCX fetch failed ({e}). Download the Bhavcopy CSV and use Upload instead.")
    if rows.empty or (rows.date != d).any():   # MCX may silently return latest trading day
        raise HTTPException(422, "Returned Date != requested date (holiday/stale). Rejected.")
    old = S["raw"] if S["source"].startswith("MCX") else pd.DataFrame(columns=rows.columns)
    set_data(pd.concat([old, rows]), "MCX BHAVCOPY (live)"); return meta()

@app.get("/api/snapshot")
def snapshot(date: str, win: int = 20, z: float = 2.0, minvol: int = 50, minoi: int = 100, cost: float = 1.0):
    d = pd.Timestamp(date); p = S["prep"]; sp = signals(win, z, minvol, minoi, cost)
    return dict(contracts=J(p[p.date == d]), spreads=J(sp[sp.date == d].sort_values("z", key=abs, ascending=False)))

@app.get("/api/series")
def series(pair: str, exp_b: str, win: int = 20, z: float = 2.0, minvol: int = 50, minoi: int = 100, cost: float = 1.0):
    sp = signals(win, z, minvol, minoi, cost)
    return J(sp[(sp.pair == pair) & (sp.exp_b == pd.Timestamp(exp_b))])

@app.get("/api/backtest")
def backtest(win: int = 20, z_in: float = 2.0, z_out: float = 0.5, hold: int = 10, minvol: int = 50,
             minoi: int = 100, cost: float = 1.0, slip: float = 3.0, thin: int = 100):
    sp = signals(win, z_in, minvol, minoi, cost, slip, thin); T = []
    for (pair, _), g in sp.groupby(["pair", "exp_b"]):
        g = g.reset_index(drop=True); i = 0
        while i < len(g) - 2:
            r = g.loc[i]
            if not (r.signal and r.dte_b > 5): i += 1; continue
            e, dr, j = g.loc[i + 1], -np.sign(r.z), i + 1   # signal at t -> fill at t+1 (no look-ahead)
            while j < len(g) - 2 and (j - i) < hold and abs(g.z[j]) >= z_out and g.dte_b[j] > 2: j += 1
            x = g.loc[min(j + 1, len(g) - 1)]
            gross = dr * 1e4 * (np.log(x.nb / e.nb) - np.log(x.na / e.na))   # on contracts actually held
            c = 4 * cost + 2 * slip * ((e.vmin < thin) + (x.vmin < thin))
            T.append(dict(pair=pair, entry=e.date, exit=x.date, dir="long B / short A" if dr > 0 else "short B / long A",
                          gross=gross, cost=c, net=gross - c, gold=1e4 * np.log(x.na / e.na), z=r.z))
            i = j + 1
    if not T: return dict(trades=[], stats=dict(n=0, verdict="No trades passed the filters - AURUM stays quiet."), equity=[])
    t = pd.DataFrame(T).sort_values("exit"); t["eq"] = t.net.cumsum(); n = len(t)
    tstat = t.net.mean() / (t.net.std() / np.sqrt(n)) if n > 2 and t.net.std() > 0 else 0
    ok = t.net.sum() > 0 and tstat > 2 and n >= 15
    st = dict(n=n, hit=float((t.net > 0).mean()), gross=float(t.gross.sum()), cost=float(t.cost.sum()), net=float(t.net.sum()),
              avg=float(t.net.mean()), tstat=float(tstat), gold_corr=float(t.net.corr(t.gold)) if n > 2 else 0,
              gold_move=float(t.gold.sum()),
              verdict="Edge survives costs on this sample (t>2) - validate out-of-sample before trusting it." if ok else
                      "No persistent edge survives realistic costs. AURUM reports this instead of forcing a trade.")
    return dict(trades=J(t), stats=st, equity=J(t[["exit", "eq"]]))

@app.get("/api/lifecycle")
def lifecycle(minvol: int = 50):
    out = []
    for (s, e), g in S["prep"].groupby(["symbol", "expiry"]):
        lq = g[g.volume >= minvol]
        out.append(dict(symbol=s, expiry=e, first=g.date.min(), liquid_from=lq.date.min() if len(lq) else None,
                        peak=g.loc[g.volume.idxmax()].date, last=g.date.max(), exit_by=e - pd.Timedelta(days=3)))
    return J(pd.DataFrame(out).sort_values(["expiry", "symbol"]))

@app.get("/api/health")
def health(): return {"ok": True}
