# AURUM — MCX Gold Relative-Value Intelligence (Hack in Hills '26 · Problem 3)

Normalizes GOLDM / GOLDTEN / GOLDGUINEA / GOLDPETAL to ₹/10 g on a 999 basis, carry-adjusts across expiries,
scores spreads with a past-only z-score, filters by liquidity and cost hurdle, and runs a walk-forward backtest
on the contracts actually held. If no edge survives costs, it says so.

## Run locally
```bash
cd backend && python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000          # docs: http://localhost:8000/docs
# new terminal
cd frontend && python -m http.server 5500      # open http://localhost:5500
```
The API boots on **synthetic demo data** so the UI works instantly. For real data: Data tab → upload MCX Bhavcopy CSVs
(Symbol, Date, ExpiryDate, Close, Volume, OpenInterest) or try "Fetch day".

## Deploy backend (Render, free)
1. Push this folder to GitHub.
2. Render → New → Web Service → select repo, **Root Directory** `backend`.
3. Build: `pip install -r requirements.txt` · Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Env var (optional): `CORS=https://your-frontend.vercel.app`
5. Test `https://<service>.onrender.com/api/health`. (Free tier sleeps; open it once before demoing.)

## Deploy frontend (Vercel or Netlify, static)
1. Vercel → New Project → repo, **Root Directory** `frontend`, Framework "Other", no build command.
2. Open the site → **Data** tab → paste the Render URL → Save & reconnect.
   (Or hard-code it: add `<script>window.AURUM_API="https://<service>.onrender.com"</script>` before the main script.)

## Judge demo flow
Dashboard (scrub a date) → click a spread → read the explanation → show a "Quiet by design" day and an ILLIQUID rejection → Backtest → Lifecycle.

## Notes / limits
- Demo data is synthetic; do not present its results as real market findings.
- The live MCX pull is best-effort (endpoint set via `MCX_URL`); uploads of the downloaded Bhavcopy are the reliable path.
- Settlement prices are not executable fills; volume is not depth. Costs/slippage are configurable assumptions.
