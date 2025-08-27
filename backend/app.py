# app.py
from flask import Flask, render_template, request, redirect, url_for
import requests
import sqlite3
from datetime import datetime
import logging
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

DB_NAME = "portfolio.db"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# --- Logging & resilient HTTP session ---
logging.basicConfig(level=logging.INFO)
session = requests.Session()
retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retries)
session.mount("http://", adapter)
session.mount("https://", adapter)
DEFAULT_HEADERS = {"User-Agent": "CryptoTracker/1.0 (+http://localhost)"}


# ------------- DB SETUP -------------
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin_id TEXT,
            name TEXT,
            symbol TEXT,
            quantity REAL,
            current_price REAL,
            last_updated TEXT
        )
        """
    )
    conn.commit()
    conn.close()


init_db()


# ------------- COINGECKO HELPERS -------------
def http_get(url, params=None):
    """GET with retries, timeout, and UA header."""
    try:
        r = session.get(url, params=params or {}, headers=DEFAULT_HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        logging.warning(f"HTTP error for {url}: {e}")
        return None


def get_top_coins():
    """
    Returns top 20 coins by market cap with current prices.
    Keys include: id, name, symbol, current_price, market_cap, price_change_percentage_24h
    """
    url = f"{COINGECKO_BASE}/coins/markets"
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": 20,
        "page": 1,
        "sparkline": "false",
        "price_change_percentage": "24h",
    }
    data = http_get(url, params)
    return data or []


def get_simple_prices(ids):
    """Fetch simple prices for multiple ids (list or comma string)."""
    if isinstance(ids, list):
        ids = ",".join(ids)
    url = f"{COINGECKO_BASE}/simple/price"
    params = {"ids": ids, "vs_currencies": "usd"}
    data = http_get(url, params)
    return data or {}


def get_btc_chart_7d():
    """Return (labels, values) for BTC last 7 days."""
    url = f"{COINGECKO_BASE}/coins/bitcoin/market_chart"
    params = {"vs_currency": "usd", "days": 7}
    data = http_get(url, params) or {}
    prices = data.get("prices", [])
    labels = [datetime.utcfromtimestamp(p[0] / 1000).strftime("%b %d") for p in prices]
    values = [round(p[1], 2) for p in prices]
    return labels, values


# ------------- PORTFOLIO HELPERS -------------
def refresh_portfolio_prices():
    """Refresh current_price for all coins in the portfolio using a single bulk request."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT DISTINCT coin_id FROM portfolio")
    ids = [row[0] for row in c.fetchall()]
    if not ids:
        conn.close()
        return

    prices = get_simple_prices(ids)
    now_iso = datetime.utcnow().isoformat()

    for coin_id in ids:
        usd_price = prices.get(coin_id, {}).get("usd")
        if usd_price is None:
            continue
        c.execute(
            "UPDATE portfolio SET current_price=?, last_updated=? WHERE coin_id=?",
            (float(usd_price), now_iso, coin_id),
        )

    conn.commit()
    conn.close()


def get_portfolio_rows():
    """Return all rows from portfolio table."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT * FROM portfolio")
    rows = c.fetchall()
    conn.close()
    return rows


# ------------- ROUTES -------------
@app.route("/")
def index():
    # keep local totals fresh
    refresh_portfolio_prices()

    coins = get_top_coins()
    labels, values = get_btc_chart_7d()

    # Portfolio stats
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT SUM(quantity * current_price), COUNT(*) FROM portfolio")
    total_value, coins_tracked = c.fetchone()
    conn.close()

    total_value = total_value or 0.0
    coins_tracked = coins_tracked or 0

    # Use BTC's 24h change if available, else 0
    change_24h = 0.0
    if coins:
        btc = next((x for x in coins if x.get("id") == "bitcoin"), coins[0])
        change_24h = btc.get("price_change_percentage_24h") or 0.0

    return render_template(
        "index.html",
        coins=coins,
        total_value=total_value,
        coins_tracked=coins_tracked,
        change_24h=change_24h,
        labels=labels,
        values=values,
    )


@app.route("/prices")
def prices():
    coins = get_top_coins()
    q = (request.args.get("search") or "").lower().strip()
    if q:
        coins = [
            c
            for c in coins
            if q in (c.get("name", "").lower())
               or q in (c.get("symbol", "").lower())
               or q == c.get("id", "").lower()
        ]
    return render_template("prices.html", coins=coins)


@app.route("/portfolio")
def portfolio():
    refresh_portfolio_prices()
    rows = get_portfolio_rows()

    # NEW: fetch logos for distinct coin_ids in the portfolio
    coin_ids = sorted({r[1] for r in rows})  # r[1] == coin_id
    logo_urls = get_coin_logos_by_ids(coin_ids)

    return render_template("portfolio.html", portfolio=rows, logo_urls=logo_urls)




@app.route("/portfolio/add", methods=["GET", "POST"])
def add_coin():
    if request.method == "POST":
        coin_id = request.form.get("coin_id", "").strip().lower()
        name = request.form.get("name", "").strip()
        symbol = request.form.get("symbol", "").strip().lower()
        quantity_raw = request.form.get("quantity", "0").strip()

        try:
            quantity = float(quantity_raw)
        except ValueError:
            quantity = 0.0

        prices = get_simple_prices([coin_id])
        usd_price = prices.get(coin_id, {}).get("usd", 0.0)

        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO portfolio (coin_id, name, symbol, quantity, current_price, last_updated)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                coin_id,
                name,
                symbol,
                quantity,
                float(usd_price),
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("portfolio"))

    return render_template("add_coin.html")


@app.route("/portfolio/delete/<int:item_id>")
def delete_coin(item_id: int):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM portfolio WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("portfolio"))


@app.route("/contact")
def contact():
    return render_template("contact.html")

def get_coin_logos_by_ids(ids):
    """Return {coin_id: image_url} for given CoinGecko ids using /coins/markets."""
    if not ids:
        return {}
    url = f"{COINGECKO_BASE}/coins/markets"
    params = {
        "vs_currency": "usd",
        "ids": ",".join(ids),
        "per_page": len(ids) or 1,
        "page": 1,
        "sparkline": "false",
    }
    data = http_get(url, params) or []
    return {item.get("id"): item.get("image") for item in data if item.get("id")}


# ------------- ENTRYPOINT -------------
if __name__ == "__main__":
    # For local dev; change port if needed, e.g., app.run(port=5001)
    app.run(debug=True)
