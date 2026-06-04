# OrcaTax — Deployment Guide

## Files

- `index.html` — Frontend (deploy to GitHub Pages)
- `orcatax-server.py` — Backend (deploy to Railway)
- `requirements.txt` — Python dependencies
- `Procfile` — Railway start command

---

## Step 1: Get a Free Etherscan API Key

1. Go to https://etherscan.io/register and create a free account
2. Go to https://etherscan.io/myapikey and create a new API key
3. Copy the key — you'll need it for Railway

The free tier gives 5 API calls/second and 100,000 calls/day — more than enough.
**Without this key, only Lightchain transactions will work** (other chains will show 0 results).

---

## Step 2: Deploy Backend to Railway

1. Go to https://railway.app
2. Create a new project → Deploy from GitHub
3. Connect `Keiko-Dev-LCAI/orcatax` repo
4. Railway will auto-detect Python from requirements.txt + Procfile
5. Add environment variables:
   - `ETHERSCAN_API_KEY` = your Etherscan API key from Step 1
6. Deploy — Railway gives you a URL like `orcatax-production.up.railway.app`

---

## Step 3: Update Frontend with Backend URL

In `index.html`, find this line (~line 601):
```
const BACKEND = 'https://orcatax-production.up.railway.app';
```
Replace with your actual Railway URL.

---

## Step 4: Deploy Frontend to GitHub Pages

1. Create repo `orcatax` under Keiko-Dev-LCAI
2. Push `index.html` to the repo root
3. Go to Settings → Pages → Source: Deploy from branch → main
4. Site will be at: `keiko-dev-lcai.github.io/orcatax`

---

## Step 5: Optional — Custom Domain (orcatax.xyz)

1. Register `orcatax.xyz` on Namecheap or similar
2. Point DNS to GitHub Pages (or Cloudflare Pages)

---

## API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/api/health` | GET | Server status, shows if Etherscan key is configured |
| `/api/lcai-price` | GET | Current LCAI price from CoinGecko |
| `/api/pnl` | POST | Free P&L summary (up to 3 wallets) |
| `/api/report` | POST | Full paid tax report (requires LCAI payment tx) |

### POST /api/pnl body:
```json
{
  "wallets": ["0x..."],
  "chains": ["eth", "lightchain", "bsc", "polygon", "arbitrum"],
  "year": 2024,
  "limit": 20
}
```

### POST /api/report body:
```json
{
  "wallets": ["0x..."],
  "chains": ["eth", "lightchain"],
  "year": 2024,
  "method": "fifo",
  "tx_hash": "0x... (Lightchain payment tx)",
  "payer": "0x... (user wallet)",
  "cex_csv": "optional CSV text",
  "cex_type": "auto|kraken|coinbase|binance|bitmart"
}
```
Returns: ZIP file containing `OrcaTax-Report-{year}.csv` and `OrcaTax-Report-{year}.pdf`

---

## Privacy Notes

- No wallet addresses or transaction data is ever logged or stored
- All processing is stateless — data in, report out, nothing saved
- Payment tx hash is stored in memory only (to prevent replay attacks)
- Server restarts clear all in-memory state
