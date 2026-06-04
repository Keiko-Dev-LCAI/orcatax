#!/usr/bin/env python3
"""
OrcaTax Backend
Port: Railway dynamic

Endpoints:
  GET  /api/lcai-price   — Current LCAI price in USD (from CoinGecko)
  POST /api/pnl          — Free P&L summary for up to 3 wallets
  POST /api/report       — Full paid tax report (verifies LCAI payment, returns ZIP)
  GET  /api/health       — Health check

Privacy: No wallet addresses, transactions, or any user data is ever logged or stored.
All data is fetched on demand, computed in memory, returned, and discarded.
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import json, os, threading, time, csv, io, zipfile, re
from urllib.parse import urlparse, parse_qs
import urllib.request as _ur
from datetime import datetime, timezone

PORT = int(os.environ.get("PORT", 8195))

# Optional API key for Etherscan V2 (covers ETH, BSC, Polygon, Arbitrum via chainid param)
# Get a free key at https://etherscan.io/register — set as ETHERSCAN_API_KEY env var on Railway
ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY", "").strip()

# ════════════════════════════════════════════════════════════════════════
# CHAIN CONFIGURATION
# ════════════════════════════════════════════════════════════════════════

# Etherscan V2 API supports multiple chains via chainid parameter
ETHERSCAN_V2_BASE = "https://api.etherscan.io/v2/api"

CHAIN_CONFIG = {
    "eth": {
        "name": "Ethereum",
        "chain_id": 1,
        "native": "ETH",
        "native_cg_id": "ethereum",
        "explorer_api": ETHERSCAN_V2_BASE,
        "explorer_chain_id": 1,
        "rpc": "https://eth.llamarpc.com",
    },
    "bsc": {
        "name": "BNB Chain",
        "chain_id": 56,
        "native": "BNB",
        "native_cg_id": "binancecoin",
        "explorer_api": ETHERSCAN_V2_BASE,
        "explorer_chain_id": 56,
        "rpc": "https://bsc-dataseed.binance.org",
    },
    "polygon": {
        "name": "Polygon",
        "chain_id": 137,
        "native": "MATIC",
        "native_cg_id": "matic-network",
        "explorer_api": ETHERSCAN_V2_BASE,
        "explorer_chain_id": 137,
        "rpc": "https://polygon-rpc.com",
    },
    "arbitrum": {
        "name": "Arbitrum",
        "chain_id": 42161,
        "native": "ETH",
        "native_cg_id": "ethereum",
        "explorer_api": ETHERSCAN_V2_BASE,
        "explorer_chain_id": 42161,
        "rpc": "https://arb1.arbitrum.io/rpc",
    },
    "lightchain": {
        "name": "Lightchain",
        "chain_id": 9200,
        "native": "LCAI",
        "native_cg_id": "lightchain-ai",
        "explorer_api": "https://mainnet.lightscan.app/api",
        "explorer_chain_id": None,   # Lightscan uses its own format
        "rpc": "https://rpc.mainnet.lightchain.ai",
    },
}

FEE_WALLET = "0x6518fD26a7aD2Fe1bA80De5f279Ee59F55C0A9bA"
LCAI_RPC   = "https://rpc.mainnet.lightchain.ai"

# Anti-replay: track used payment tx hashes in memory
_used_tx_hashes = set()
_used_tx_lock   = threading.Lock()

# ════════════════════════════════════════════════════════════════════════
# COINGECKO — PRICE LOOKUP
# ════════════════════════════════════════════════════════════════════════

# Cache: (coin_id, date_str "dd-mm-yyyy") → price_usd
_price_cache      = {}
_price_cache_lock = threading.Lock()

# Cache: contract address → coingecko coin id
_cg_id_cache      = {}
_cg_id_lock       = threading.Lock()

# Current LCAI price cache
_lcai_price_cache = {"price": None, "ts": 0}
_lcai_lock        = threading.Lock()
LCAI_TTL          = 120  # seconds


def _cg_get(path: str, timeout: int = 10) -> dict:
    """Make a CoinGecko free-tier API call."""
    url = "https://api.coingecko.com/api/v3" + path
    req = _ur.Request(url, headers={"User-Agent": "OrcaTax/1.0"})
    with _ur.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get_lcai_price() -> float:
    """Return current LCAI price in USD, cached for 2 minutes."""
    with _lcai_lock:
        cached = _lcai_price_cache
        if cached["price"] and time.time() - cached["ts"] < LCAI_TTL:
            return cached["price"]

    try:
        data = _cg_get("/coins/lightchain-ai?localization=false&tickers=false&community_data=false&developer_data=false&sparkline=false")
        price = data["market_data"]["current_price"]["usd"]
        with _lcai_lock:
            _lcai_price_cache["price"] = price
            _lcai_price_cache["ts"]    = time.time()
        return price
    except Exception as e:
        print(f"  [CoinGecko] LCAI price error: {e}")
        with _lcai_lock:
            if _lcai_price_cache["price"]:
                return _lcai_price_cache["price"]
        return 0.004  # fallback


def get_historical_price(coin_id: str, date_str: str) -> float | None:
    """
    Get price of coin_id on date_str ("dd-mm-yyyy") in USD.
    Returns None if unavailable. Results are cached in memory.
    """
    key = (coin_id, date_str)
    with _price_cache_lock:
        if key in _price_cache:
            return _price_cache[key]

    try:
        data = _cg_get(f"/coins/{coin_id}/history?date={date_str}&localization=false", timeout=12)
        price = data.get("market_data", {}).get("current_price", {}).get("usd")
        if price:
            with _price_cache_lock:
                _price_cache[key] = float(price)
            return float(price)
    except Exception as e:
        pass

    return None


def get_coin_id_by_contract(platform: str, contract_addr: str) -> str | None:
    """
    Look up CoinGecko coin ID by contract address on a given platform.
    Platforms: ethereum, binance-smart-chain, polygon-pos, arbitrum-one, lightchain
    """
    cache_key = (platform, contract_addr.lower())
    with _cg_id_lock:
        if cache_key in _cg_id_cache:
            return _cg_id_cache[cache_key]

    try:
        data = _cg_get(f"/coins/{platform}/contract/{contract_addr.lower()}", timeout=10)
        coin_id = data.get("id")
        if coin_id:
            with _cg_id_lock:
                _cg_id_cache[cache_key] = coin_id
            return coin_id
    except Exception:
        pass

    with _cg_id_lock:
        _cg_id_cache[cache_key] = None
    return None


# Map chain keys to CoinGecko platform identifiers
CG_PLATFORM = {
    "eth":        "ethereum",
    "bsc":        "binance-smart-chain",
    "polygon":    "polygon-pos",
    "arbitrum":   "arbitrum-one",
    "lightchain": "lightchain",
}


def ts_to_date(timestamp: int | str) -> str:
    """Convert Unix timestamp to 'dd-mm-yyyy' for CoinGecko."""
    dt = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
    return dt.strftime("%d-%m-%Y")


def ts_to_ymd(timestamp: int | str) -> str:
    """Convert Unix timestamp to 'YYYY-MM-DD' for display."""
    dt = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def ts_year(timestamp: int | str) -> int:
    """Get year from Unix timestamp."""
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).year


# ════════════════════════════════════════════════════════════════════════
# EXPLORER API — TRANSACTION FETCHING
# ════════════════════════════════════════════════════════════════════════

def _explorer_fetch(chain: str, action: str, address: str, page: int = 1, offset: int = 200) -> list:
    """
    Fetch transaction list from an Etherscan-compatible explorer API.
    action: 'txlist' (normal transactions) or 'tokentx' (ERC-20 transfers)
    Returns list of tx dicts, or empty list on failure.
    """
    cfg      = CHAIN_CONFIG[chain]
    base_url = cfg["explorer_api"]
    chain_id = cfg.get("explorer_chain_id")

    params = (
        f"module=account&action={action}"
        f"&address={address}"
        f"&startblock=0&endblock=99999999"
        f"&page={page}&offset={offset}&sort=asc"
    )

    # Add API key for Etherscan-based chains
    if chain != "lightchain" and ETHERSCAN_API_KEY:
        params += f"&apikey={ETHERSCAN_API_KEY}"

    if chain_id:
        url = f"{base_url}?chainid={chain_id}&{params}"
    else:
        url = f"{base_url}?{params}"

    try:
        req = _ur.Request(url, headers={"User-Agent": "OrcaTax/1.0"})
        with _ur.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        if data.get("status") == "1":
            return data.get("result", [])
        # "No transactions found" returns status 0 — that's fine, just empty
        msg = data.get("message", "")
        if "No transactions" in msg or "No records" in msg:
            return []
        if "API Key" in msg or "Missing" in msg:
            print(f"  [Explorer/{chain}] {action}: API key required — set ETHERSCAN_API_KEY env var")
            return []
        # Real error — log but don't crash
        print(f"  [Explorer/{chain}] {action}: status={data.get('status')} msg={msg}")
        return []
    except Exception as e:
        print(f"  [Explorer/{chain}] {action}: error {e}")
        return []


def _rpc_call(rpc_url: str, method: str, params: list) -> any:
    """Simple JSON-RPC call."""
    try:
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = _ur.Request(rpc_url, data=payload, headers={"Content-Type": "application/json"})
        with _ur.urlopen(req, timeout=10) as r:
            return json.loads(r.read()).get("result")
    except Exception as e:
        print(f"  [RPC] {method}: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════
# P&L CALCULATION ENGINE
# ════════════════════════════════════════════════════════════════════════

def _fetch_wallet_transactions(wallet: str, chain: str, year: int) -> list:
    """
    Fetch all transactions for a wallet on a chain, filtered to the given year.
    Returns a list of normalized tx dicts.
    """
    wallet_lc = wallet.lower()
    txs       = []

    # 1. Native coin transfers (ETH, BNB, MATIC, LCAI)
    normal_txs = _explorer_fetch(chain, "txlist", wallet)
    native_sym = CHAIN_CONFIG[chain]["native"]
    for tx in normal_txs:
        try:
            if int(tx.get("value", "0")) == 0:
                continue   # skip zero-value txs (contract calls)
            if tx.get("isError", "0") != "0":
                continue   # skip failed txs

            ts = int(tx.get("timeStamp", 0))
            if ts_year(ts) != year:
                continue

            txs.append({
                "hash":     tx.get("hash", ""),
                "chain":    chain,
                "wallet":   wallet,
                "date":     ts_ymd(ts),
                "timestamp": ts,
                "token":    native_sym,
                "symbol":   native_sym,
                "contract": "native",
                "amount":   int(tx.get("value", "0")) / 1e18,
                "decimals": 18,
                "is_in":    tx.get("to", "").lower() == wallet_lc,
                "from":     tx.get("from", "").lower(),
                "to":       tx.get("to", "").lower(),
                "type":     "receive" if tx.get("to", "").lower() == wallet_lc else "send",
                "cg_id":    CHAIN_CONFIG[chain]["native_cg_id"],
            })
        except Exception:
            continue

    # 2. ERC-20 token transfers
    token_txs = _explorer_fetch(chain, "tokentx", wallet)
    for tx in token_txs:
        try:
            if tx.get("isError", "0") != "0":
                continue

            ts = int(tx.get("timeStamp", 0))
            if ts_year(ts) != year:
                continue

            decimals = int(tx.get("tokenDecimal", "18"))
            value    = int(tx.get("value", "0")) / (10 ** decimals) if decimals <= 18 else 0
            if value == 0:
                continue

            is_in     = tx.get("to", "").lower() == wallet_lc
            tx_type   = "receive" if is_in else "send"
            contract  = tx.get("contractAddress", "").lower()
            symbol    = tx.get("tokenSymbol", "?")
            name      = tx.get("tokenName", "?")

            txs.append({
                "hash":      tx.get("hash", ""),
                "chain":     chain,
                "wallet":    wallet,
                "date":      ts_ymd(ts),
                "timestamp": ts,
                "token":     symbol,
                "name":      name,
                "symbol":    symbol,
                "contract":  contract,
                "amount":    value,
                "decimals":  decimals,
                "is_in":     is_in,
                "from":      tx.get("from", "").lower(),
                "to":        tx.get("to", "").lower(),
                "type":      tx_type,
                "cg_id":     None,   # looked up lazily
            })
        except Exception:
            continue

    return txs


def ts_ymd(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _get_tx_price_usd(tx: dict, chain: str) -> float | None:
    """Get the USD price per token unit at the time of the transaction."""
    date_str = ts_to_date(tx["timestamp"])

    # If we have a known cg_id, use it directly
    cg_id = tx.get("cg_id")

    if not cg_id and tx.get("contract") and tx["contract"] != "native":
        platform = CG_PLATFORM.get(chain, "ethereum")
        cg_id    = get_coin_id_by_contract(platform, tx["contract"])
        tx["cg_id"] = cg_id

    if not cg_id:
        return None

    return get_historical_price(cg_id, date_str)


def calculate_pnl(transactions: list, method: str = "fifo",
                  realize_crypto_trades: bool = True,
                  dust_threshold_usd: float = 0.0,
                  wallet_based: bool = False) -> dict:
    """
    Calculate realized P&L from a list of normalized transactions.

    method: "fifo" | "lifo" | "hifo"
    realize_crypto_trades: if False, skips gain/loss on crypto→crypto swaps
    dust_threshold_usd: ignore transactions with value below this (e.g. 1.0 = ignore <$1 txs)
    wallet_based: if True, track cost basis separately per wallet rather than pooled

    Returns dict with:
      - total_gain_loss_usd, st_gain_loss_usd (short-term), lt_gain_loss_usd (long-term)
      - total_in_usd, total_out_usd
      - transactions: list of tx dicts with gain_loss_usd and term added
      - remaining_lots: cost_basis_lots still held (for unrealized calc)
      - token_meta: {token_key: {symbol, chain, contract, cg_id}} for held tokens
    """
    # ── Swap detection: find tx hashes with both a send AND receive ──
    # These are crypto→crypto swaps (e.g. ETH sent + LCAI received, same hash)
    if not realize_crypto_trades:
        _hash_sends    = set()
        _hash_receives = set()
        for tx in transactions:
            h = tx.get("hash", "")
            if h:
                if tx["is_in"]:
                    _hash_receives.add(h)
                else:
                    _hash_sends.add(h)
        swap_hashes = _hash_sends & _hash_receives
    else:
        swap_hashes = set()

    cost_basis_lots = {}
    token_meta      = {}
    result_txs      = []

    total_gain_loss = 0.0
    total_in_usd    = 0.0
    total_out_usd   = 0.0
    st_gain_loss    = 0.0   # held < 365 days (short-term)
    lt_gain_loss    = 0.0   # held ≥ 365 days (long-term)

    for tx in transactions:
        # ── Token key: wallet-scoped or global ──
        if wallet_based and tx.get("wallet"):
            token_key = f"{tx['wallet']}:{tx['chain']}:{tx['contract']}"
        else:
            token_key = f"{tx['chain']}:{tx['contract']}"

        # ── Price resolution ──
        # CEX transactions already carry their price from the CSV parser
        if tx.get("chain") == "cex" and tx.get("price_usd") and tx["price_usd"] > 0:
            price_usd = float(tx["price_usd"])
        else:
            price_usd = _get_tx_price_usd(tx, tx["chain"])

        tx_value = (price_usd or 0.0) * tx["amount"]

        # ── Dust filter ──
        if dust_threshold_usd > 0 and 0 < tx_value < dust_threshold_usd:
            continue

        tx_out = {
            "date":          tx["date"],
            "chain":         tx["chain"],
            "type":          tx["type"],
            "token":         tx["symbol"],
            "amount":        f"{tx['amount']:.6f}",
            "price_usd":     price_usd,
            "gain_loss_usd": None,
            "term":          None,
            "hash":          tx["hash"],
        }

        if tx["is_in"]:
            total_in_usd += tx_value
            # BUY / RECEIVE — add to cost basis lots
            if token_key not in cost_basis_lots:
                cost_basis_lots[token_key] = []
            cost_basis_lots[token_key].append({
                "amount":        tx["amount"],
                "cost_per_unit": price_usd or 0.0,
                "date":          tx["date"],
                "timestamp":     tx["timestamp"],
            })
            token_meta[token_key] = {
                "symbol":   tx["symbol"],
                "chain":    tx["chain"],
                "contract": tx.get("contract", ""),
                "cg_id":    tx.get("cg_id"),
            }

        else:
            total_out_usd += tx_value
            lots        = cost_basis_lots.get(token_key, [])
            sell_amount = tx["amount"]
            sell_value  = tx_value
            sell_ts     = tx["timestamp"]

            # Skip gain/loss if this is a crypto→crypto swap and the toggle is off
            is_swap    = tx.get("hash", "") in swap_hashes
            skip_gain  = is_swap and not realize_crypto_trades

            if lots and sell_amount > 0 and not skip_gain:
                if method == "fifo":
                    sorted_lots = sorted(lots, key=lambda x: x["timestamp"])
                elif method == "lifo":
                    sorted_lots = sorted(lots, key=lambda x: -x["timestamp"])
                elif method == "hifo":
                    sorted_lots = sorted(lots, key=lambda x: -x["cost_per_unit"])
                else:
                    sorted_lots = sorted(lots, key=lambda x: x["timestamp"])

                cost_basis   = 0.0
                remaining    = sell_amount
                lot_st_gain  = 0.0
                lot_lt_gain  = 0.0

                for lot in sorted_lots:
                    if remaining <= 0:
                        break
                    if lot["amount"] <= 0:
                        continue
                    used       = min(lot["amount"], remaining)
                    lot_cost   = used * lot["cost_per_unit"]
                    # Proportional proceeds for this lot slice
                    lot_proc   = (sell_value / sell_amount * used) if sell_amount > 0 else 0.0
                    lot_gl     = lot_proc - lot_cost
                    cost_basis += lot_cost
                    remaining  -= used
                    lot["amount"] -= used

                    # Short-term = held < 365 days; long-term = 365+ days
                    days_held = (sell_ts - lot["timestamp"]) / 86400.0
                    if days_held >= 365:
                        lot_lt_gain += lot_gl
                    else:
                        lot_st_gain += lot_gl

                cost_basis_lots[token_key] = [l for l in sorted_lots if l["amount"] > 1e-12]

                gain_loss = sell_value - cost_basis
                total_gain_loss += gain_loss
                st_gain_loss    += lot_st_gain
                lt_gain_loss    += lot_lt_gain
                tx_out["gain_loss_usd"] = gain_loss

                # Term label for this transaction
                if lot_st_gain != 0 and lot_lt_gain != 0:
                    tx_out["term"] = "mixed"
                elif lot_lt_gain != 0:
                    tx_out["term"] = "long"
                else:
                    tx_out["term"] = "short"

            elif not skip_gain and price_usd:
                # No prior lots on record — treat full proceeds as gain (no cost basis)
                gain_loss = sell_value
                total_gain_loss += gain_loss
                st_gain_loss    += gain_loss   # no basis = default short-term
                tx_out["gain_loss_usd"] = gain_loss
                tx_out["term"]          = "short"

        result_txs.append(tx_out)

    return {
        "total_gain_loss_usd": round(total_gain_loss, 2),
        "st_gain_loss_usd":    round(st_gain_loss, 2),
        "lt_gain_loss_usd":    round(lt_gain_loss, 2),
        "total_in_usd":        round(total_in_usd, 2),
        "total_out_usd":       round(total_out_usd, 2),
        "transactions":        result_txs,
        "remaining_lots":      cost_basis_lots,
        "token_meta":          token_meta,
    }


def get_current_price_batch(cg_ids: list) -> dict:
    """
    Fetch current USD prices for a list of CoinGecko IDs in one call.
    Returns {cg_id: price_usd}
    """
    if not cg_ids:
        return {}
    ids_param = ",".join(set(cg_ids))
    try:
        data = _cg_get(f"/simple/price?ids={ids_param}&vs_currencies=usd", timeout=10)
        return {k: v.get("usd", 0) for k, v in data.items()}
    except Exception as e:
        print(f"  [CoinGecko] batch price error: {e}")
        return {}


def build_holdings(remaining_lots: dict, token_meta: dict,
                   dust_threshold_usd: float = 0.0) -> tuple[list, float, float]:
    """
    Build a holdings list from remaining cost basis lots.
    Returns (holdings_list, portfolio_value_usd, unrealized_gain_loss_usd)
    dust_threshold_usd: hide holdings worth less than this amount
    """
    if not remaining_lots:
        return [], 0.0, 0.0

    # Collect all cg_ids we need to price
    cg_ids = []
    for token_key, lots in remaining_lots.items():
        if not lots:
            continue
        meta = token_meta.get(token_key, {})
        cg_id = meta.get("cg_id")
        if cg_id and cg_id != "manual":
            cg_ids.append(cg_id)

    # Batch fetch current prices
    current_prices = get_current_price_batch(cg_ids)

    holdings          = []
    portfolio_value   = 0.0
    unrealized_total  = 0.0

    for token_key, lots in remaining_lots.items():
        if not lots:
            continue

        total_amount = sum(l["amount"] for l in lots)
        if total_amount < 1e-10:
            continue

        total_cost  = sum(l["amount"] * l["cost_per_unit"] for l in lots)
        avg_cost    = total_cost / total_amount if total_amount > 0 else 0

        meta        = token_meta.get(token_key, {})
        symbol      = meta.get("symbol", "?")
        chain       = meta.get("chain", "?")
        cg_id       = meta.get("cg_id")

        # Get current price
        current_price = None
        if cg_id and cg_id != "manual":
            current_price = current_prices.get(cg_id)

        market_value    = (current_price * total_amount) if current_price else None
        unrealized      = (market_value - total_cost) if market_value is not None else None
        roi_pct         = ((market_value - total_cost) / total_cost * 100) if (market_value is not None and total_cost > 0) else None

        # Dust filter on holdings
        if dust_threshold_usd > 0:
            worth = market_value if market_value is not None else total_cost
            if worth < dust_threshold_usd:
                continue

        if market_value is not None:
            portfolio_value  += market_value
            unrealized_total += (unrealized or 0)

        holdings.append({
            "token":          symbol,
            "chain":          chain,
            "balance":        f"{total_amount:.6f}",
            "cost_usd":       round(total_cost, 2),
            "avg_cost_unit":  round(avg_cost, 6),
            "current_price":  current_price,
            "market_value":   round(market_value, 2) if market_value is not None else None,
            "unrealized":     round(unrealized, 2) if unrealized is not None else None,
            "roi_pct":        round(roi_pct, 2) if roi_pct is not None else None,
        })

    # Sort by market value descending, then by cost descending
    holdings.sort(key=lambda h: (h["market_value"] or 0), reverse=True)

    return holdings, round(portfolio_value, 2), round(unrealized_total, 2)


# ════════════════════════════════════════════════════════════════════════
# CEX CSV PARSER
# ════════════════════════════════════════════════════════════════════════

def parse_cex_csv(csv_text: str, cex_type: str, year: int) -> list:
    """
    Parse a CEX CSV export into a list of normalized transaction dicts.
    cex_type: 'kraken' | 'coinbase' | 'binance' | 'bitmart' | 'auto'
    """
    rows  = list(csv.DictReader(io.StringIO(csv_text.strip())))
    txs   = []

    def safe_float(val, default=0.0):
        try:
            v = str(val).replace(",", "").strip()
            return float(v) if v else default
        except (ValueError, TypeError):
            return default

    # ── KRAKEN ────────────────────────────────────────────────────────
    if cex_type == "kraken" or (cex_type == "auto" and rows and "txid" in rows[0]):
        for row in rows:
            try:
                if row.get("type", "").lower() not in ("trade", "buy", "sell"):
                    continue
                raw_time = row.get("time", "")
                dt = _parse_date_flexible(raw_time)
                if not dt or dt.year != year:
                    continue
                pair   = row.get("pair", "")
                vol    = safe_float(row.get("vol"))
                price  = safe_float(row.get("price"))
                tx_type = "sell" if row.get("type", "").lower() == "sell" else "buy"
                token   = pair[:3] if pair else "?"
                txs.append({
                    "hash":      row.get("txid", ""),
                    "chain":     "cex",
                    "date":      dt.strftime("%Y-%m-%d"),
                    "timestamp": int(dt.timestamp()),
                    "token":     token,
                    "symbol":    token,
                    "contract":  "cex:" + token,
                    "amount":    vol,
                    "decimals":  8,
                    "is_in":     tx_type == "buy",
                    "from":      "exchange",
                    "to":        "wallet",
                    "type":      tx_type,
                    "cg_id":     None,
                    "price_usd": price,
                })
            except Exception:
                continue

    # ── COINBASE ─────────────────────────────────────────────────────
    elif cex_type == "coinbase" or (cex_type == "auto" and rows and "Timestamp" in rows[0]):
        for row in rows:
            try:
                tx_type_raw = row.get("Transaction Type", "").lower()
                if tx_type_raw not in ("buy", "sell", "send", "receive"):
                    continue
                raw_time = row.get("Timestamp", "")
                dt = _parse_date_flexible(raw_time)
                if not dt or dt.year != year:
                    continue
                asset  = row.get("Asset", "?")
                qty    = safe_float(row.get("Quantity Transacted"))
                price  = safe_float(row.get("Spot Price at Transaction"))
                is_in  = tx_type_raw in ("buy", "receive")
                txs.append({
                    "hash":      "",
                    "chain":     "cex",
                    "date":      dt.strftime("%Y-%m-%d"),
                    "timestamp": int(dt.timestamp()),
                    "token":     asset,
                    "symbol":    asset,
                    "contract":  "cex:" + asset,
                    "amount":    qty,
                    "decimals":  8,
                    "is_in":     is_in,
                    "from":      "exchange",
                    "to":        "wallet",
                    "type":      "buy" if is_in else "sell",
                    "cg_id":     None,
                    "price_usd": price,
                })
            except Exception:
                continue

    # ── BINANCE ──────────────────────────────────────────────────────
    elif cex_type == "binance" or (cex_type == "auto" and rows and "Date(UTC)" in rows[0]):
        for row in rows:
            try:
                raw_time  = row.get("Date(UTC)", "")
                dt = _parse_date_flexible(raw_time)
                if not dt or dt.year != year:
                    continue
                side  = row.get("Side", "").lower()
                pair  = row.get("Pair", "")
                executed = safe_float(row.get("Executed", "0").replace(pair[:3] if pair else "", "").strip())
                price    = safe_float(row.get("Price"))
                base_asset = pair[:3] if len(pair) >= 6 else pair
                is_in  = side == "buy"
                txs.append({
                    "hash":      "",
                    "chain":     "cex",
                    "date":      dt.strftime("%Y-%m-%d"),
                    "timestamp": int(dt.timestamp()),
                    "token":     base_asset,
                    "symbol":    base_asset,
                    "contract":  "cex:" + base_asset,
                    "amount":    executed,
                    "decimals":  8,
                    "is_in":     is_in,
                    "from":      "exchange",
                    "to":        "wallet",
                    "type":      "buy" if is_in else "sell",
                    "cg_id":     None,
                    "price_usd": price,
                })
            except Exception:
                continue

    # ── GENERIC FALLBACK ─────────────────────────────────────────────
    else:
        # Try to auto-detect from headers
        if rows:
            headers = list(rows[0].keys())
            date_col   = next((h for h in headers if "date" in h.lower() or "time" in h.lower()), None)
            type_col   = next((h for h in headers if "type" in h.lower() or "side" in h.lower()), None)
            amount_col = next((h for h in headers if "amount" in h.lower() or "qty" in h.lower() or "vol" in h.lower() or "quantity" in h.lower()), None)
            asset_col  = next((h for h in headers if "asset" in h.lower() or "symbol" in h.lower() or "coin" in h.lower() or "currency" in h.lower()), None)
            price_col  = next((h for h in headers if "price" in h.lower()), None)

            if date_col and amount_col and asset_col:
                for row in rows:
                    try:
                        dt = _parse_date_flexible(row.get(date_col, ""))
                        if not dt or dt.year != year:
                            continue
                        tx_type_raw = row.get(type_col, "buy").lower() if type_col else "buy"
                        is_in = tx_type_raw in ("buy", "receive", "purchase", "in")
                        asset = row.get(asset_col, "?")
                        amount = safe_float(row.get(amount_col))
                        price  = safe_float(row.get(price_col, 0)) if price_col else None
                        txs.append({
                            "hash":      "",
                            "chain":     "cex",
                            "date":      dt.strftime("%Y-%m-%d"),
                            "timestamp": int(dt.timestamp()),
                            "token":     asset,
                            "symbol":    asset,
                            "contract":  "cex:" + asset,
                            "amount":    amount,
                            "decimals":  8,
                            "is_in":     is_in,
                            "from":      "exchange",
                            "to":        "wallet",
                            "type":      "buy" if is_in else "sell",
                            "cg_id":     None,
                            "price_usd": price,
                        })
                    except Exception:
                        continue

    return txs


def _parse_date_flexible(s: str):
    """Try multiple date formats, return datetime or None."""
    if not s:
        return None
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y",
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s.strip()[:len(fmt)], fmt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


# ════════════════════════════════════════════════════════════════════════
# PAYMENT VERIFICATION
# ════════════════════════════════════════════════════════════════════════

def verify_payment(tx_hash: str, payer: str) -> tuple[bool, str]:
    """
    Verify that tx_hash is a valid LCAI transfer to FEE_WALLET on Lightchain.
    Returns (ok: bool, error_msg: str)
    """
    if not tx_hash or not re.match(r"^0x[0-9a-fA-F]{64}$", tx_hash):
        return False, "Invalid transaction hash format"

    # Anti-replay: check if already used
    with _used_tx_lock:
        if tx_hash.lower() in _used_tx_hashes:
            return False, "This transaction has already been used for a report"

    # Get transaction receipt from Lightchain
    receipt = _rpc_call(LCAI_RPC, "eth_getTransactionReceipt", [tx_hash])
    if not receipt:
        # Try to get the transaction itself
        tx_data = _rpc_call(LCAI_RPC, "eth_getTransactionByHash", [tx_hash])
        if not tx_data:
            return False, "Transaction not found on Lightchain — please wait a moment and try again"
        receipt = {"status": "0x1"}  # assume ok if tx exists (may be pending)

    # Check it succeeded
    status = receipt.get("status", "0x0")
    if status not in ("0x1", 1, "1"):
        return False, "Transaction failed on-chain — no charge, please try again"

    # Get tx details to verify recipient and amount
    tx_data = _rpc_call(LCAI_RPC, "eth_getTransactionByHash", [tx_hash])
    if tx_data:
        to_addr  = tx_data.get("to", "").lower()
        value    = int(tx_data.get("value", "0x0"), 16)
        fee_addr = FEE_WALLET.lower()

        if to_addr != fee_addr:
            return False, f"Transaction was not sent to the OrcaTax fee wallet"

        # Minimum: 0.001 LCAI (very low — just to confirm it's a real payment)
        if value < 1_000_000_000_000_000:   # 0.001 LCAI in wei
            return False, "Transaction value too low — please ensure you sent enough LCAI"

    # Mark as used
    with _used_tx_lock:
        _used_tx_hashes.add(tx_hash.lower())
        # Trim to prevent unbounded growth (keep last 1000)
        if len(_used_tx_hashes) > 1000:
            _used_tx_hashes.clear()

    return True, ""


# ════════════════════════════════════════════════════════════════════════
# REPORT GENERATION — CSV + PDF
# ════════════════════════════════════════════════════════════════════════

def generate_csv(year: int, transactions: list, total_gain_loss: float,
                 wallets: list, chains: list, method: str,
                 st_gain_loss: float = 0.0, lt_gain_loss: float = 0.0) -> bytes:
    """Generate a CSV report as bytes."""
    output = io.StringIO()
    writer = csv.writer(output)

    # Header section
    writer.writerow(["OrcaTax Transaction Report"])
    writer.writerow([f"Tax Year: {year}"])
    writer.writerow([f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"])
    writer.writerow([f"Wallets: {', '.join(wallets)}"])
    writer.writerow([f"Chains: {', '.join(chains)}"])
    writer.writerow([f"Cost Basis Method: {method.upper()}"])
    writer.writerow([f"Short-Term Gains/Losses (held <1 yr): ${st_gain_loss:+.2f}"])
    writer.writerow([f"Long-Term Gains/Losses (held ≥1 yr): ${lt_gain_loss:+.2f}"])
    writer.writerow([f"Net Capital Gain/Loss: ${total_gain_loss:+.2f}"])
    writer.writerow([])

    # DISCLAIMER
    writer.writerow(["DISCLAIMER: This report is for informational purposes only. Not tax advice. Verify with a qualified tax professional."])
    writer.writerow([])

    # Transaction rows
    writer.writerow(["Date", "Chain", "Type", "Token", "Amount", "Price USD", "Gain/Loss USD", "Term", "Tx Hash"])
    for tx in transactions:
        writer.writerow([
            tx.get("date", ""),
            tx.get("chain", ""),
            tx.get("type", ""),
            tx.get("token", ""),
            tx.get("amount", ""),
            f"${tx['price_usd']:.4f}" if tx.get("price_usd") else "N/A",
            f"{tx['gain_loss_usd']:+.2f}" if tx.get("gain_loss_usd") is not None else "N/A",
            tx.get("term") or "—",
            tx.get("hash", ""),
        ])

    return output.getvalue().encode("utf-8")


def generate_pdf(year: int, transactions: list, total_gain_loss: float,
                 wallets: list, chains: list, method: str,
                 total_in: float = 0.0, total_out: float = 0.0,
                 tx_count: int = 0,
                 st_gain_loss: float = 0.0, lt_gain_loss: float = 0.0) -> bytes:
    """Generate a PDF report as bytes using fpdf2.
    Page 1: Clean tax summary cover (like Koinly).
    Page 2+: Full transaction table.
    """
    try:
        from fpdf import FPDF
        from fpdf.enums import XPos, YPos

        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        gen_time  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        sign      = "+" if total_gain_loss >= 0 else ""
        net_label = "Net Gain" if total_gain_loss >= 0 else "Net Loss"

        # ── PAGE 1: TAX SUMMARY COVER ────────────────────────────────────
        pdf.add_page()

        # Logo / title
        pdf.set_font("Helvetica", "B", 22)
        pdf.set_text_color(0, 180, 220)   # cyan-ish
        pdf.cell(0, 14, "OrcaTax", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
        pdf.set_text_color(60, 60, 60)
        pdf.set_font("Helvetica", "", 13)
        pdf.cell(0, 8, f"Crypto Tax Report  |  Tax Year {year}", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
        pdf.ln(10)

        # ── Big net number box ──
        if total_gain_loss >= 0:
            pdf.set_fill_color(230, 255, 240)
            pdf.set_text_color(20, 140, 70)
        else:
            pdf.set_fill_color(255, 235, 235)
            pdf.set_text_color(180, 40, 40)
        pdf.set_font("Helvetica", "B", 28)
        pdf.cell(0, 18, f"{sign}${abs(total_gain_loss):,.2f}", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C", fill=True)
        pdf.set_font("Helvetica", "", 11)
        pdf.cell(0, 8, net_label + " for the Period", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C", fill=True)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(10)

        # ── Summary table ──
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_fill_color(245, 245, 250)
        pdf.cell(0, 8, "Summary", new_x=XPos.LMARGIN, new_y=YPos.NEXT, fill=True)
        pdf.ln(3)

        def _summary_row(label, value, bold=False, color=None):
            pdf.set_font("Helvetica", "B" if bold else "", 10)
            if color:
                pdf.set_text_color(*color)
            pdf.cell(110, 9, label)
            pdf.set_font("Helvetica", "B" if bold else "", 10)
            pdf.cell(0, 9, value, new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="R")
            if color:
                pdf.set_text_color(0, 0, 0)
            # thin separator line
            pdf.set_draw_color(220, 220, 220)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())

        st_sign = "+" if st_gain_loss >= 0 else ""
        lt_sign = "+" if lt_gain_loss >= 0 else ""
        _summary_row(
            f"  Short-Term Gains/Losses  (held < 1 yr)",
            f"{st_sign}${abs(st_gain_loss):,.2f}",
            color=(20, 140, 70) if st_gain_loss >= 0 else (180, 40, 40)
        )
        _summary_row(
            f"  Long-Term Gains/Losses  (held ≥ 1 yr)",
            f"{lt_sign}${abs(lt_gain_loss):,.2f}",
            color=(20, 140, 70) if lt_gain_loss >= 0 else (180, 40, 40)
        )
        _summary_row("Capital Gains / Losses  (Total)",
                     f"{sign}${abs(total_gain_loss):,.2f}",
                     bold=True,
                     color=(20, 140, 70) if total_gain_loss >= 0 else (180, 40, 40))
        _summary_row("Income",     "$0.00")
        _summary_row("Expenses",   "$0.00")
        _summary_row("Trading Fees", "$0.00")
        _summary_row("Total Transactions", str(tx_count))
        _summary_row("Chains Covered", ", ".join(chains) or "—")
        pdf.ln(8)

        # ── Settings block ──
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_fill_color(245, 245, 250)
        pdf.cell(0, 8, "Report Settings", new_x=XPos.LMARGIN, new_y=YPos.NEXT, fill=True)
        pdf.ln(3)

        settings = [
            ("Home Country",      "United States"),
            ("Base Currency",     "USD"),
            ("Cost Basis Method", method.upper()),
            ("Tax Year",          f"Jan 1 - Dec 31, {year}"),
            ("Wallets Tracked",   str(len(wallets))),
            ("Report Generated",  gen_time),
        ]
        pdf.set_font("Helvetica", "", 10)
        for s_label, s_val in settings:
            pdf.set_font("Helvetica", "", 10)
            pdf.cell(110, 8, s_label)
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(0, 8, s_val, new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="R")
            pdf.set_draw_color(220, 220, 220)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(8)

        # ── Wallet list ──
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 7, "Wallets Included:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(80, 80, 80)
        for waddr in wallets:
            pdf.cell(0, 6, waddr, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(8)

        # ── Disclaimer on cover ──
        pdf.set_font("Helvetica", "I", 7)
        pdf.set_text_color(140, 140, 140)
        pdf.multi_cell(0, 4, "DISCLAIMER: This report is generated for informational purposes only and does not constitute tax, legal, or financial advice. Accuracy depends on the completeness of on-chain and CEX data provided. Consult a qualified tax professional before filing. OrcaTax is not responsible for errors or omissions.")
        pdf.set_text_color(0, 0, 0)

        # ── PAGE 2+: TRANSACTION TABLE ───────────────────────────────────
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 10, f"Transaction Detail — {year}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(0, 6, f"Sorted by date  |  Cost basis method: {method.upper()}  |  {len(transactions)} transactions total",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(4)

        # Table header
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(50, 60, 80)
        pdf.set_text_color(255, 255, 255)
        col_widths = [22, 18, 14, 14, 22, 20, 22, 16, 32]
        hdr_labels = ["Date", "Chain", "Type", "Token", "Amount", "Price USD", "Gain/Loss", "Term", "Tx Hash"]
        for cw, hl in zip(col_widths, hdr_labels):
            pdf.cell(cw, 7, hl, border=0, fill=True)
        pdf.ln()
        pdf.set_text_color(0, 0, 0)

        # Rows
        pdf.set_font("Helvetica", "", 7)
        fill = False
        for tx in transactions:
            if fill:
                pdf.set_fill_color(248, 248, 252)
            else:
                pdf.set_fill_color(255, 255, 255)
            gl    = tx.get("gain_loss_usd")
            price = tx.get("price_usd")

            if gl is not None:
                pdf.set_text_color(20, 130, 60) if gl >= 0 else pdf.set_text_color(180, 40, 40)
                gl_str = f"{gl:+,.2f}"
            else:
                pdf.set_text_color(150, 150, 150)
                gl_str = "N/A"

            term = tx.get("term") or "—"
            row = [
                tx.get("date", "")[:10],
                tx.get("chain", ""),
                tx.get("type", ""),
                tx.get("token", "")[:8],
                str(tx.get("amount", ""))[:10],
                f"${price:,.4f}" if price else "—",
                gl_str,
                term,
                tx.get("hash", "")[:10] + ("..." if tx.get("hash") else ""),
            ]
            for i, (cw, cell) in enumerate(zip(col_widths, row)):
                if i == 6 and gl is not None:
                    pass  # gain/loss color already set above
                else:
                    pdf.set_text_color(0, 0, 0)
                pdf.cell(cw, 6, cell, border=0, fill=True)
            pdf.set_text_color(0, 0, 0)
            pdf.ln()
            fill = not fill

        # Footer note
        pdf.ln(6)
        pdf.set_font("Helvetica", "I", 7)
        pdf.set_text_color(140, 140, 140)
        pdf.cell(0, 5, "Generated by OrcaTax | Not tax advice | Verify with a qualified tax professional",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")

        return pdf.output()

    except ImportError:
        print("  [PDF] fpdf2 not available — generating text-based PDF")
        return _generate_text_pdf(year, transactions, total_gain_loss, wallets, chains, method)


def _generate_text_pdf(year, transactions, total_gain_loss, wallets, chains, method) -> bytes:
    """Minimal fallback: text-encoded as PDF using raw PDF syntax."""
    lines = [
        f"OrcaTax Report — Tax Year {year}",
        f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        f"Chains: {', '.join(chains)}",
        f"Cost Basis Method: {method.upper()}",
        f"Net Gain/Loss: ${total_gain_loss:+.2f} USD",
        "",
        "DISCLAIMER: For informational purposes only. Not tax advice.",
        "",
        "Date       | Chain    | Type    | Token | Amount   | Price    | Gain/Loss",
        "-" * 80,
    ]
    for tx in transactions:
        gl    = tx.get("gain_loss_usd")
        price = tx.get("price_usd")
        lines.append(
            f"{tx.get('date','')[:10]:10} | "
            f"{tx.get('chain',''):8} | "
            f"{tx.get('type',''):7} | "
            f"{tx.get('token','')[:6]:6}| "
            f"{str(tx.get('amount',''))[:8]:8} | "
            f"{'$'+f'{price:.4f}' if price else 'N/A':8} | "
            f"{f'{gl:+.2f}' if gl is not None else 'N/A'}"
        )

    text = "\n".join(lines).encode("latin-1", errors="replace")

    # Minimal valid PDF with the text content
    obj1 = b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    obj2 = b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"

    font_stream = b""
    content_lines = []
    for line in lines[:80]:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content_lines.append(f"({safe}) Tj 0 -12 Td".encode())

    stream = b"BT /F1 9 Tf 40 750 Td 12 TL\n" + b"\n".join(content_lines) + b"\nET"
    obj4_str = f"4 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream\nendobj\n"

    obj3 = b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]\n/Contents 4 0 R /Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Courier >> >> >> >>\nendobj\n"

    body  = b"%PDF-1.4\n" + obj1 + obj2 + obj3 + obj4_str
    xref  = b"xref\n0 5\n0000000000 65535 f \n"
    off   = 9
    for obj in [obj1, obj2, obj3, obj4_str]:
        xref += f"{off:010d} 00000 n \n".encode()
        off  += len(obj)

    trailer = f"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n{off}\n%%EOF".encode()
    return body + xref + trailer


def build_zip(csv_bytes: bytes, pdf_bytes: bytes, year: int) -> bytes:
    """Pack CSV and PDF into a ZIP archive and return as bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"OrcaTax-Report-{year}.csv", csv_bytes)
        z.writestr(f"OrcaTax-Report-{year}.pdf", pdf_bytes)
    return buf.getvalue()


# ════════════════════════════════════════════════════════════════════════
# CORE PIPELINE — used by both /api/pnl and /api/report
# ════════════════════════════════════════════════════════════════════════

def run_pnl_pipeline(wallets: list, chains: list, year: int, limit: int,
                     method: str = "fifo", cex_csv: str = None,
                     cex_type: str = "auto",
                     realize_crypto_trades: bool = True,
                     dust_threshold_usd: float = 0.0,
                     wallet_based: bool = False) -> dict:
    """
    Full P&L pipeline: fetch transactions, look up prices, calculate gains/losses.
    Returns the result dict for /api/pnl or the raw data for /api/report.
    """
    all_transactions = []

    # Fetch on-chain transactions in parallel
    result_map  = {}
    fetch_lock  = threading.Lock()

    def _fetch(wallet, chain):
        try:
            txs = _fetch_wallet_transactions(wallet, chain, year)
            with fetch_lock:
                result_map[(wallet, chain)] = txs
        except Exception as e:
            print(f"  [Pipeline] fetch {chain}/{wallet[:8]}... error: {e}")
            with fetch_lock:
                result_map[(wallet, chain)] = []

    threads = []
    for wallet in wallets:
        for chain in chains:
            t = threading.Thread(target=_fetch, args=(wallet, chain), daemon=True)
            threads.append(t)
            t.start()
    for t in threads:
        t.join(timeout=30)

    for (wallet, chain), txs in result_map.items():
        all_transactions.extend(txs)

    # Parse CEX CSV if provided
    if cex_csv:
        cex_txs = parse_cex_csv(cex_csv, cex_type or "auto", year)
        all_transactions.extend(cex_txs)

    # Sort by timestamp
    all_transactions.sort(key=lambda x: x.get("timestamp", 0))

    # Resolve prices for CEX transactions that came in with price_usd already set
    for tx in all_transactions:
        if tx.get("chain") == "cex" and tx.get("price_usd") and tx.get("price_usd") > 0:
            tx["cg_id"] = "manual"  # marker so we don't re-fetch

    # Calculate P&L
    pnl = calculate_pnl(all_transactions, method,
                        realize_crypto_trades=realize_crypto_trades,
                        dust_threshold_usd=dust_threshold_usd,
                        wallet_based=wallet_based)

    # Build holdings + unrealized gains from remaining lots
    holdings, portfolio_value, unrealized_gain_loss = build_holdings(
        pnl["remaining_lots"], pnl["token_meta"],
        dust_threshold_usd=dust_threshold_usd,
    )

    # Apply limit for P&L display (latest transactions first)
    display_txs = sorted(pnl["transactions"], key=lambda x: x.get("date", ""), reverse=True)
    if limit and limit > 0:
        display_txs = display_txs[:limit]

    chains_scanned = list(set(t.get("chain", "") for t in all_transactions if t.get("chain") != "cex"))
    if cex_csv:
        chains_scanned.append("cex")

    return {
        # Summary stats (Koinly-style)
        "total_in_usd":              pnl["total_in_usd"],
        "total_out_usd":             pnl["total_out_usd"],
        "realized_gain_loss_usd":    pnl["total_gain_loss_usd"],
        "st_gain_loss_usd":          pnl["st_gain_loss_usd"],
        "lt_gain_loss_usd":          pnl["lt_gain_loss_usd"],
        "unrealized_gain_loss_usd":  unrealized_gain_loss,
        "portfolio_value_usd":       portfolio_value,
        # Legacy key (kept for compatibility)
        "total_gain_loss_usd":       pnl["total_gain_loss_usd"],
        # Counts
        "tx_count":                  len(all_transactions),
        "chains_scanned":            chains_scanned,
        # Holdings table
        "holdings":                  holdings,
        # Transaction list (limited for free preview)
        "transactions":              display_txs,
        "_all_transactions":         pnl["transactions"],  # used by report endpoint
    }


# ════════════════════════════════════════════════════════════════════════
# HTTP SERVER
# ════════════════════════════════════════════════════════════════════════

SERVER_START = time.time()

VALID_CHAINS  = set(CHAIN_CONFIG.keys())
VALID_METHODS = {"fifo", "lifo", "hifo"}


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass   # suppress default request logging (privacy)

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type",                  "application/json")
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length",               str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, content_type: str, filename: str = None, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type",                  content_type)
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Content-Length",               str(len(data)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, msg, code=400):
        self._send_json({"error": msg}, code)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")

        if path in ("", "/"):
            self._send_json({"service": "OrcaTax API", "version": "1.0", "ok": True})
            return

        if path == "/api/health":
            uptime = int(time.time() - SERVER_START)
            h, rem = divmod(uptime, 3600)
            m = rem // 60
            self._send_json({
                "ok":           True,
                "uptime":       uptime,
                "uptimeLabel":  f"{h}h {m}m",
                "chains":       list(VALID_CHAINS),
                "etherscan_key": bool(ETHERSCAN_API_KEY),
            })
            return

        if path == "/api/lcai-price":
            try:
                price = get_lcai_price()
                self._send_json({"ok": True, "price_usd": price})
            except Exception as e:
                self._send_json({"ok": False, "price_usd": 0.004, "error": str(e)})
            return

        self._send_error("Not found", 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")

        if path == "/api/pnl":
            self._handle_pnl()
        elif path == "/api/report":
            self._handle_report()
        else:
            self._send_error("Not found", 404)

    # ── P&L Endpoint ─────────────────────────────────────────────────

    def _handle_pnl(self):
        body = self._read_body()

        wallets               = body.get("wallets", [])
        chains                = body.get("chains", [])
        year                  = body.get("year", datetime.utcnow().year)
        limit                 = body.get("limit", 20)
        method                = body.get("method", "fifo").lower()
        realize_crypto_trades = bool(body.get("realize_crypto_trades", True))
        dust_threshold_usd    = float(body.get("dust_threshold_usd", 0.0))
        wallet_based          = bool(body.get("wallet_based", False))

        # Validate
        if not wallets:
            self._send_error("wallets is required")
            return
        if not isinstance(wallets, list):
            self._send_error("wallets must be a list")
            return
        if len(wallets) > 3:
            self._send_error("Free P&L check supports up to 3 wallets")
            return
        for w in wallets:
            if not re.match(r"^0x[0-9a-fA-F]{40}$", w):
                self._send_error(f"Invalid wallet address: {w}")
                return

        if not chains:
            chains = ["eth", "lightchain"]
        chains = [c.lower() for c in chains if c.lower() in VALID_CHAINS]
        if not chains:
            self._send_error("No valid chains specified. Valid: " + ", ".join(VALID_CHAINS))
            return

        try:
            year = int(year)
        except (ValueError, TypeError):
            self._send_error("year must be an integer")
            return
        if not (2015 <= year <= datetime.utcnow().year):
            self._send_error(f"year must be between 2015 and {datetime.utcnow().year}")
            return

        if method not in VALID_METHODS:
            method = "fifo"

        try:
            result = run_pnl_pipeline(
                wallets, chains, year, int(limit),
                method=method,
                realize_crypto_trades=realize_crypto_trades,
                dust_threshold_usd=dust_threshold_usd,
                wallet_based=wallet_based,
            )
            # Strip internal field before sending
            result.pop("_all_transactions", None)
            self._send_json({"ok": True, **result})
        except Exception as e:
            print(f"  [PnL] error: {e}")
            self._send_error("Failed to fetch transaction data: " + str(e), 500)

    # ── Report Endpoint ───────────────────────────────────────────────

    def _handle_report(self):
        body = self._read_body()

        wallets               = body.get("wallets", [])
        chains                = body.get("chains", [])
        year                  = body.get("year", datetime.utcnow().year)
        method                = body.get("method", "fifo").lower()
        tx_hash               = body.get("tx_hash", "").strip()
        payer                 = body.get("payer", "").strip()
        cex_csv               = body.get("cex_csv")
        cex_type              = body.get("cex_type", "auto")
        realize_crypto_trades = bool(body.get("realize_crypto_trades", True))
        dust_threshold_usd    = float(body.get("dust_threshold_usd", 0.0))
        wallet_based          = bool(body.get("wallet_based", False))

        # Validate basic inputs
        if not wallets and not cex_csv:
            self._send_error("wallets or cex_csv is required")
            return
        if wallets:
            for w in wallets:
                if not re.match(r"^0x[0-9a-fA-F]{40}$", w):
                    self._send_error(f"Invalid wallet address: {w}")
                    return
        if not chains:
            chains = list(VALID_CHAINS)
        chains = [c.lower() for c in chains if c.lower() in VALID_CHAINS]
        if method not in VALID_METHODS:
            method = "fifo"

        try:
            year = int(year)
        except (ValueError, TypeError):
            self._send_error("year must be an integer")
            return

        # Verify payment
        ok, err = verify_payment(tx_hash, payer)
        if not ok:
            self._send_error(err, 402)
            return

        # Run full pipeline (no tx limit for report)
        try:
            result = run_pnl_pipeline(
                wallets or [], chains, year, limit=0,
                method=method,
                cex_csv=cex_csv,
                cex_type=cex_type or "auto",
                realize_crypto_trades=realize_crypto_trades,
                dust_threshold_usd=dust_threshold_usd,
                wallet_based=wallet_based,
            )

            all_txs         = result.get("_all_transactions", result["transactions"])
            total_gain_loss = result["realized_gain_loss_usd"]
            st_gain_loss    = result.get("st_gain_loss_usd", 0.0)
            lt_gain_loss    = result.get("lt_gain_loss_usd", 0.0)
            total_in        = result.get("total_in_usd", 0.0)
            total_out       = result.get("total_out_usd", 0.0)
            tx_count        = result.get("tx_count", len(all_txs))

            # Generate files
            csv_bytes = generate_csv(
                year, all_txs, total_gain_loss, wallets, chains, method,
                st_gain_loss=st_gain_loss, lt_gain_loss=lt_gain_loss,
            )
            pdf_bytes = generate_pdf(
                year, all_txs, total_gain_loss, wallets, chains, method,
                total_in=total_in, total_out=total_out, tx_count=tx_count,
                st_gain_loss=st_gain_loss, lt_gain_loss=lt_gain_loss,
            )
            zip_bytes = build_zip(csv_bytes, pdf_bytes, year)

            filename = f"OrcaTax-Report-{year}.zip"
            self._send_bytes(zip_bytes, "application/zip", filename)

        except Exception as e:
            print(f"  [Report] error: {e}")
            self._send_error("Report generation failed: " + str(e), 500)


# ════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import socketserver

    class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True

    print(f"OrcaTax backend starting on port {PORT}...")
    server = ThreadedHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"  Ready: http://0.0.0.0:{PORT}")
    print(f"  Chains: {', '.join(CHAIN_CONFIG.keys())}")
    print(f"  Privacy: all processing stateless — no data logged or stored")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
