import os
import asyncio
import json
import time
import io
import requests
import logging
import websockets
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime, timezone
from collections import deque, defaultdict

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SOL_PRIVATE_KEY  = os.environ.get("SOL_PRIVATE_KEY", "")   # base58 Solana private key
PUMPPORTAL_API_KEY = os.environ.get("PUMPPORTAL_API_KEY", "")  # needed for trade data
BSC_PRIVATE_KEY  = os.environ.get("BSC_PRIVATE_KEY", "")   # hex BSC private key
BSC_RPC          = os.environ.get("BSC_RPC", "https://bsc-dataseed.binance.org/")
SOL_RPC          = os.environ.get("SOL_RPC", "https://api.mainnet-beta.solana.com")

# ── Settings ──────────────────────────────────────────────────────────────────
settings = {
    "max_alerts_hr":    13,
    "min_rug_score":    35,        # FIXED: lowered from 45 so more tokens pass
    "min_buys_5min":    5,         # FIXED: lowered from 10
    "min_bonding_pct":  3.0,       # FIXED: lowered from 5.0
    "min_vol_usd":      500,       # FIXED: lowered from 2000
    "buy_ratio_min":    0.55,      # FIXED: lowered from 0.60
    "min_liq":          1000,      # FIXED: lowered from 3000
    "chains":           ["solana", "bsc", "base", "ethereum"],
    "wc_mode":          True,
    "gem_mode":         True,
    "gem_mc_min":       2000,
    "gem_mc_max":       50000,     # FIXED: raised from 30000
    "paused":           False,
    "charts":           True,
    "safe_only":        False,
    "threshold":        20,
    "min_rug_score_wc": 25,        # FIXED: lowered from 30
    "auto_buy":         False,     # auto buy without confirmation
    "max_trade_usd":    5.0,       # max $5 per trade
    "take_profit_pct":  100.0,     # sell half at 2x
    "trailing_stop":    25.0,      # trailing stop loss 25% from peak
    "dead_vol_mins":    30,        # auto sell if no volume for 30 mins
    "min_score_buy":    50,        # min safety score to allow buying
}

ALL_CHAINS = ["solana", "bsc", "base", "ethereum"]

WC_KEYWORDS = set([
    "worldcup","world cup","wc2026","worldcup2026","fifa2026",
    "fifa","fwc","fwc26","fifawc","fifameme","fifacoin",
    "footballcoin","soccercoin","goatcoin","championsleague",
    "goldenboot","hatrick","penalty","freekick","worldgoal",
    "usmnt","uswnt","usasoccer","mexicofifa","canadafc",
    "losangeles","miami","dallas","seattle","houston",
    "philadelphia","atlanta","toronto","vancouver","guadalajara",
    "england","threelions","france","germany","mannschaft",
    "spain","lafuria","portugal","selecao","netherlands",
    "croatia","belgium","switzerland","scotland","norway",
    "sweden","turkey","turkiye","czechia","bosnia","argentina",
    "albiceleste","brazil","canarinho","colombia","uruguay",
    "ecuador","paraguay","morocco","algeria","egypt",
    "ghana","tunisia","japan","samuraiblue","southkorea",
    "australia","iran","jordan","uzbekistan","panama",
    "curacao","haiti","newzealand","capeverde",
    "ronaldo","cr7","messi","mbappe","neymar",
    "haaland","vinicius","bellingham","salah","modric",
    "pedri","yamal","osimhen","lewandowski","kane",
    "saka","rashford","pulisic","ferran","gavi",
    "wc","wcup","goal","striker","keeper",
    "offside","redcard","yellowcard","shootout",
])

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── State ─────────────────────────────────────────────────────────────────────
seen: dict = {}
watchlist: set = set()
rug_blacklist: set = set()          # never buy these again
last_update_id = 0
start_time = time.time()
total_alerts = 0
total_gems = 0
total_scans = 0
ws_events_received = 0
ws_creates_received = 0
ws_trades_received = 0
ws_connected_since = 0
last_event_time = 0
alert_times = deque()
last_tg_send = 0
token_activity: dict = defaultdict(lambda: {
    "buys": 0, "sells": 0, "wallets": set(),
    "volume_sol": 0.0, "first_seen": time.time(),
    "dev_sold": False, "bonding_pct": 0.0,
    "name": "", "symbol": "", "mint": "",
})

# ── Positions & Trade History ─────────────────────────────────────────────────
# { mint: { name, symbol, chain, entry_price, amount_usd, amount_tokens,
#           peak_price, opened_at, half_sold, url } }
positions: dict = {}

# [ { mint, name, symbol, chain, entry, exit, pnl_usd, pnl_pct,
#     held_secs, reason, opened_at, closed_at } ]
trade_history: list = []

# ── Price Alerts ──────────────────────────────────────────────────────────────
price_alerts: dict = {}

# ── Recap Log ─────────────────────────────────────────────────────────────────
recap_log: list = []

# ── Pending buy confirmations ─────────────────────────────────────────────────
# { callback_query_id / mint : { pair, score, verdict, flags, wc, gem, activity } }
pending_buys: dict = {}

# ── Whale wallets to track ────────────────────────────────────────────────────
whale_wallets: set = set()

# ── Rate Limiting ─────────────────────────────────────────────────────────────
def can_alert():
    now = time.time()
    while alert_times and now - alert_times[0] > 3600:
        alert_times.popleft()
    return len(alert_times) < settings["max_alerts_hr"]

def record_alert():
    alert_times.append(time.time())

# ── Helpers ───────────────────────────────────────────────────────────────────
def is_wc_token(name="", symbol=""):
    name = name.lower(); symbol = symbol.lower()
    for kw in WC_KEYWORDS:
        if kw in name or kw in symbol:
            return True
    return False

def is_stable(symbol):
    return symbol.upper() in {"USDT","USDC","BUSD","DAI","WETH","WBNB","WSOL","ETH","BNB","SOL","WBTC"}

def fmt_usd(v):
    if v >= 1_000_000: return f"${v/1_000_000:.2f}M"
    if v >= 1_000:     return f"${v:,.0f}"
    return f"${v:.4f}"

# ── Image Card Generator ──────────────────────────────────────────────────────
def _rounded_rect(draw, xy, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)

def _score_color(score):
    if score >= 60: return "#22c55e"
    if score >= 35: return "#f59e0b"
    return "#ef4444"

def make_alert_card(data: dict) -> bytes:
    W, H   = 620, 400
    BG     = "#0d1117"; CARD = "#161b22"; BORDER = "#30363d"
    WHITE  = "#f0f6fc"; MUTED = "#8b949e"
    GREEN  = "#22c55e"; RED = "#ef4444"
    PURPLE = "#a855f7"; SOCCER = "#3b82f6"
    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    f_huge = ImageFont.load_default(size=26)
    f_big  = ImageFont.load_default(size=20)
    f_med  = ImageFont.load_default(size=15)
    f_tiny = ImageFont.load_default(size=11)
    _rounded_rect(draw, [10,10,W-10,H-10], 12, CARD, BORDER, 1)
    _rounded_rect(draw, [10,10,W-10,58],   12, "#1a2332")
    draw.rectangle([10,46,W-10,58], fill="#1a2332")
    is_wc  = data.get("is_wc", False)
    is_gem = data.get("is_gem", False)
    if is_wc:   hdr_color, hdr_text = SOCCER, "⚽ WC MEMECOIN ALERT"
    elif is_gem: hdr_color, hdr_text = PURPLE, "💎 EARLY GEM ALERT"
    else:        hdr_color, hdr_text = GREEN,  "🚀 TOKEN ALERT"
    draw.text((24, 18), hdr_text, font=f_big, fill=hdr_color)
    chain = data.get("chain","SOL").upper()
    chain_colors = {"SOLANA":"#9945ff","BSC":"#f0b90b","BASE":"#0052ff","ETHEREUM":"#627eea"}
    cbg = chain_colors.get(chain, "#444")
    cw  = draw.textlength(chain, font=f_tiny) + 14
    _rounded_rect(draw, [W-24-cw,20,W-20,40], 6, cbg)
    draw.text((W-20-cw+7, 23), chain, font=f_tiny, fill=WHITE)
    name   = data.get("name","Unknown")[:22]
    symbol = data.get("symbol","?")
    draw.text((24, 68), name, font=f_huge, fill=WHITE)
    sym_x = 26 + draw.textlength(name, font=f_huge) + 8
    draw.text((sym_x, 74), f"${symbol}", font=f_med, fill=MUTED)
    age_txt = f"Age: {data.get('age','?')}"
    _rounded_rect(draw,[24,102,24+draw.textlength(age_txt,font=f_tiny)+12,120],6,"#21262d")
    draw.text((30,104), age_txt, font=f_tiny, fill=MUTED)
    draw.line([(24,128),(W-24,128)], fill=BORDER, width=1)
    draw.text((24,138), "PRICE", font=f_tiny, fill=MUTED)
    draw.text((24,154), f"${data.get('price','?')}", font=f_big, fill=WHITE)
    mc = data.get("mc",0)
    if mc:
        mc_str = f"${mc/1_000_000:.2f}M" if mc >= 1_000_000 else f"${mc:,.0f}"
        draw.text((200,138), "MARKET CAP", font=f_tiny, fill=MUTED)
        draw.text((200,154), mc_str, font=f_big, fill=WHITE)
    x = 24
    for label, val in [("1H",data.get("ch1",0)),("6H",data.get("ch6",0)),("24H",data.get("ch24",0))]:
        col  = GREEN if val >= 0 else RED
        sign = "+" if val >= 0 else ""
        _rounded_rect(draw,[x,192,x+90,228],8,"#21262d")
        draw.text((x+8,196), label,               font=f_tiny, fill=MUTED)
        draw.text((x+8,209), f"{sign}{val:.1f}%", font=f_med,  fill=col)
        x += 100
    draw.text((24,240),  "LIQUIDITY",                    font=f_tiny, fill=MUTED)
    draw.text((24,254),  f"${data.get('liq',0):,.0f}",  font=f_med,  fill=WHITE)
    draw.text((200,240), "VOL 1H",                       font=f_tiny, fill=MUTED)
    draw.text((200,254), f"${data.get('vol1h',0):,.0f}", font=f_med,  fill=WHITE)
    buys  = data.get("buys",0)
    sells = data.get("sells",0)
    total = buys + sells
    draw.text((24,280), f"BUYS {buys}  /  SELLS {sells}", font=f_tiny, fill=MUTED)
    if total > 0:
        bar_w = W - 48; buy_w = int(bar_w * buys / total)
        _rounded_rect(draw,[24,294,24+buy_w,306],4,GREEN)
        if buy_w < bar_w:
            _rounded_rect(draw,[24+buy_w,294,24+bar_w,306],4,RED)
    draw.line([(24,316),(W-24,316)], fill=BORDER, width=1)
    score   = data.get("score",0)
    verdict = data.get("verdict","?")
    scol    = _score_color(score)
    draw.text((24,326), "SAFETY SCORE", font=f_tiny, fill=MUTED)
    draw.text((24,340), verdict,         font=f_med,  fill=scol)
    for i in range(5):
        cx  = W-120+i*22
        col = scol if i < score//20 else "#21262d"
        draw.ellipse([cx,336,cx+14,350], fill=col)
    draw.text((W-30,340), str(score), font=f_med, fill=scol)
    draw.text((24,H-28),    data.get("trigger",""), font=f_tiny, fill=MUTED)
    draw.text((W-160,H-28), data.get("time",""),    font=f_tiny, fill=MUTED)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.read()

def make_pnl_card(trade: dict) -> bytes:
    """Generate a PnL card image when a trade closes."""
    W, H  = 620, 360
    profit = trade["pnl_usd"] >= 0
    BG     = "#0d1117"; CARD = "#161b22"; BORDER = "#30363d"
    WHITE  = "#f0f6fc"; MUTED = "#8b949e"
    GREEN  = "#22c55e"; RED = "#ef4444"
    ACCENT = GREEN if profit else RED
    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    f_huge = ImageFont.load_default(size=28)
    f_big  = ImageFont.load_default(size=20)
    f_med  = ImageFont.load_default(size=15)
    f_tiny = ImageFont.load_default(size=11)
    _rounded_rect(draw, [10,10,W-10,H-10], 12, CARD, BORDER, 1)
    # Header
    _rounded_rect(draw, [10,10,W-10,62], 12, "#1a2332")
    draw.rectangle([10,50,W-10,62], fill="#1a2332")
    hdr = "💰 TRADE CLOSED — PROFIT" if profit else "📉 TRADE CLOSED — LOSS"
    draw.text((24, 22), hdr, font=f_big, fill=ACCENT)
    # Chain badge
    chain = trade.get("chain","SOL").upper()
    chain_colors = {"SOLANA":"#9945ff","BSC":"#f0b90b","BASE":"#0052ff","ETHEREUM":"#627eea"}
    cbg = chain_colors.get(chain, "#444")
    cw  = draw.textlength(chain, font=f_tiny) + 14
    _rounded_rect(draw, [W-24-cw,18,W-20,40], 6, cbg)
    draw.text((W-20-cw+7, 21), chain, font=f_tiny, fill=WHITE)
    # Token name
    name   = trade.get("name","Unknown")[:22]
    symbol = trade.get("symbol","?")
    draw.text((24, 72), name, font=f_huge, fill=WHITE)
    sx = 26 + draw.textlength(name, font=f_huge) + 8
    draw.text((sx, 80), f"${symbol}", font=f_med, fill=MUTED)
    draw.line([(24,112),(W-24,112)], fill=BORDER, width=1)
    # Entry / Exit
    draw.text((24,122),  "ENTRY PRICE", font=f_tiny, fill=MUTED)
    draw.text((24,138),  f"${trade['entry']:.10f}".rstrip('0'), font=f_med, fill=WHITE)
    draw.text((220,122), "EXIT PRICE",  font=f_tiny, fill=MUTED)
    draw.text((220,138), f"${trade['exit']:.10f}".rstrip('0'),  font=f_med, fill=WHITE)
    # PnL big number
    sign   = "+" if profit else ""
    pnl_pct = trade["pnl_pct"]
    pnl_usd = trade["pnl_usd"]
    draw.text((24,172), f"{sign}{pnl_pct:.1f}%", font=f_huge, fill=ACCENT)
    usd_x = 26 + draw.textlength(f"{sign}{pnl_pct:.1f}%", font=f_huge) + 16
    draw.text((usd_x, 182), f"{sign}${abs(pnl_usd):.2f}", font=f_big, fill=ACCENT)
    # Investment → Return
    invested = trade.get("amount_usd", settings["max_trade_usd"])
    returned = invested + pnl_usd
    draw.text((24,218),  "INVESTED",        font=f_tiny, fill=MUTED)
    draw.text((24,234),  f"${invested:.2f}", font=f_med,  fill=WHITE)
    draw.text((160,218), "RETURNED",        font=f_tiny, fill=MUTED)
    draw.text((160,234), f"${returned:.2f}", font=f_med,  fill=ACCENT)
    # Held time
    held = trade.get("held_secs", 0)
    if held < 3600:
        held_str = f"{int(held//60)}m {int(held%60)}s"
    else:
        held_str = f"{int(held//3600)}h {int((held%3600)//60)}m"
    draw.text((320,218), "HELD",     font=f_tiny, fill=MUTED)
    draw.text((320,234), held_str,   font=f_med,  fill=WHITE)
    # Reason
    draw.line([(24,262),(W-24,262)], fill=BORDER, width=1)
    draw.text((24,272), f"Reason: {trade.get('reason','Manual')}", font=f_tiny, fill=MUTED)
    # Total stats
    total_trades = len(trade_history)
    wins  = sum(1 for t in trade_history if t["pnl_usd"] >= 0)
    total_pnl = sum(t["pnl_usd"] for t in trade_history)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    draw.text((24,292),  f"Win rate: {win_rate:.0f}%",         font=f_tiny, fill=MUTED)
    draw.text((180,292), f"Total trades: {total_trades}",       font=f_tiny, fill=MUTED)
    pnl_col = GREEN if total_pnl >= 0 else RED
    draw.text((340,292), f"All-time PnL: {'+' if total_pnl>=0 else ''}${total_pnl:.2f}", font=f_tiny, fill=pnl_col)
    draw.text((24,H-28),    datetime.now(timezone.utc).strftime("%H:%M UTC %d %b"), font=f_tiny, fill=MUTED)
    draw.text((W-120,H-28), "Alpha Bot v7", font=f_tiny, fill=MUTED)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.read()

def build_card_data(pair, trigger, verdict, score, wc, gem):
    base    = pair.get("baseToken") or {}
    created = pair.get("pairCreatedAt") or 0
    age_min = int(((time.time() * 1000) - created) / 60_000) if created else 0
    age_str = f"{age_min}m" if age_min < 120 else f"{age_min//60}h {age_min%60}m"
    return {
        "name":    base.get("name",""),
        "symbol":  base.get("symbol",""),
        "chain":   pair.get("chainId","solana").upper(),
        "price":   pair.get("priceUsd","?"),
        "mc":      pair.get("marketCap") or pair.get("fdv") or 0,
        "ch1":     (pair.get("priceChange") or {}).get("h1",0) or 0,
        "ch6":     (pair.get("priceChange") or {}).get("h6",0) or 0,
        "ch24":    (pair.get("priceChange") or {}).get("h24",0) or 0,
        "liq":     (pair.get("liquidity") or {}).get("usd",0) or 0,
        "vol1h":   (pair.get("volume") or {}).get("h1",0) or 0,
        "buys":    ((pair.get("txns") or {}).get("h1") or {}).get("buys",0),
        "sells":   ((pair.get("txns") or {}).get("h1") or {}).get("sells",0),
        "score":   score,
        "verdict": verdict,
        "age":     age_str,
        "is_wc":   wc,
        "is_gem":  gem,
        "trigger": trigger,
        "time":    datetime.now(timezone.utc).strftime("%H:%M UTC"),
    }

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_tg(chat_id, text, photo_bytes=None, reply_markup=None):
    global last_tg_send
    wait = 3 - (time.time() - last_tg_send)
    if wait > 0: time.sleep(wait)
    try:
        if photo_bytes and settings.get("charts", True):
            buf = io.BytesIO(photo_bytes)
            data = {"chat_id": chat_id, "caption": text[:1024], "parse_mode": "HTML"}
            if reply_markup:
                data["reply_markup"] = json.dumps(reply_markup)
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                data=data,
                files={"photo": ("alert.png", buf, "image/png")},
                timeout=20,
            )
            last_tg_send = time.time()
            if r.ok: return
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload, timeout=10,
        ).raise_for_status()
        last_tg_send = time.time()
    except Exception as e:
        log.error(f"TG error: {e}")

def answer_callback(callback_query_id, text="✅"):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=5,
        )
    except: pass

def broadcast(text, photo_bytes=None, reply_markup=None):
    send_tg(TELEGRAM_CHAT_ID, text, photo_bytes, reply_markup)

# ── DEXScreener ───────────────────────────────────────────────────────────────
def dex_get(mint):
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=10)
        r.raise_for_status()
        pairs = r.json().get("pairs", []) or []
        if pairs:
            return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0))
        return {}
    except Exception:
        return {}

def dex_search(keyword):
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={keyword}", timeout=10)
        r.raise_for_status()
        return r.json().get("pairs", []) or []
    except Exception:
        return []

# ── Trading ───────────────────────────────────────────────────────────────────
def execute_buy_solana(mint: str, usd_amount: float) -> dict:
    """Buy a Solana token via Jupiter API."""
    try:
        from solders.keypair import Keypair  # type: ignore
        from solders.pubkey import Pubkey    # type: ignore
        import base58, struct
        if not SOL_PRIVATE_KEY:
            return {"ok": False, "error": "No SOL private key set"}
        kp        = Keypair.from_base58_string(SOL_PRIVATE_KEY)
        wallet    = str(kp.pubkey())
        sol_price = get_sol_price()
        sol_amt   = usd_amount / sol_price if sol_price else 0
        lamports  = int(sol_amt * 1e9)
        WSOL      = "So11111111111111111111111111111111111111112"
        # Get quote from Jupiter
        q = requests.get(
            "https://quote-api.jup.ag/v6/quote",
            params={"inputMint": WSOL, "outputMint": mint, "amount": lamports, "slippageBps": 1000},
            timeout=10,
        ).json()
        if "error" in q:
            return {"ok": False, "error": q["error"]}
        # Get swap transaction
        swap = requests.post(
            "https://quote-api.jup.ag/v6/swap",
            json={"quoteResponse": q, "userPublicKey": wallet, "wrapAndUnwrapSol": True},
            timeout=10,
        ).json()
        tx_b64 = swap.get("swapTransaction")
        if not tx_b64:
            return {"ok": False, "error": "No swap tx returned"}
        import base64
        from solders.transaction import VersionedTransaction  # type: ignore
        tx_bytes = base64.b64decode(tx_b64)
        tx       = VersionedTransaction.from_bytes(tx_bytes)
        tx.sign([kp])
        signed_b64 = base64.b64encode(bytes(tx)).decode()
        # Send transaction
        resp = requests.post(SOL_RPC, json={
            "jsonrpc":"2.0","id":1,"method":"sendTransaction",
            "params":[signed_b64, {"encoding":"base64","skipPreflight":True}]
        }, timeout=15).json()
        sig = resp.get("result","")
        if not sig:
            return {"ok": False, "error": str(resp.get("error","Unknown"))}
        out_tokens = int(q.get("outAmount", 0))
        return {"ok": True, "sig": sig, "tokens": out_tokens, "sol_spent": sol_amt}
    except ImportError:
        return {"ok": False, "error": "solders not installed — add 'solders' to requirements.txt"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def execute_sell_solana(mint: str, token_amount: int) -> dict:
    """Sell a Solana token via Jupiter API."""
    try:
        from solders.keypair import Keypair  # type: ignore
        import base64
        from solders.transaction import VersionedTransaction  # type: ignore
        if not SOL_PRIVATE_KEY:
            return {"ok": False, "error": "No SOL private key set"}
        kp     = Keypair.from_base58_string(SOL_PRIVATE_KEY)
        wallet = str(kp.pubkey())
        WSOL   = "So11111111111111111111111111111111111111112"
        q = requests.get(
            "https://quote-api.jup.ag/v6/quote",
            params={"inputMint": mint, "outputMint": WSOL, "amount": token_amount, "slippageBps": 1500},
            timeout=10,
        ).json()
        if "error" in q:
            return {"ok": False, "error": q["error"]}
        swap = requests.post(
            "https://quote-api.jup.ag/v6/swap",
            json={"quoteResponse": q, "userPublicKey": wallet, "wrapAndUnwrapSol": True},
            timeout=10,
        ).json()
        tx_b64 = swap.get("swapTransaction")
        if not tx_b64:
            return {"ok": False, "error": "No swap tx"}
        tx_bytes = base64.b64decode(tx_b64)
        tx       = VersionedTransaction.from_bytes(tx_bytes)
        tx.sign([kp])
        signed_b64 = base64.b64encode(bytes(tx)).decode()
        resp = requests.post(SOL_RPC, json={
            "jsonrpc":"2.0","id":1,"method":"sendTransaction",
            "params":[signed_b64, {"encoding":"base64","skipPreflight":True}]
        }, timeout=15).json()
        sig = resp.get("result","")
        sol_out = int(q.get("outAmount", 0)) / 1e9
        return {"ok": True, "sig": sig, "sol_received": sol_out}
    except ImportError:
        return {"ok": False, "error": "solders not installed"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def execute_buy_bsc(token_address: str, usd_amount: float) -> dict:
    """Buy a BSC token via PancakeSwap."""
    try:
        from web3 import Web3  # type: ignore
        if not BSC_PRIVATE_KEY:
            return {"ok": False, "error": "No BSC private key set"}
        w3      = Web3(Web3.HTTPProvider(BSC_RPC))
        account = w3.eth.account.from_key(BSC_PRIVATE_KEY)
        bnb_price = get_bnb_price()
        bnb_amt   = usd_amount / bnb_price if bnb_price else 0
        bnb_wei   = w3.to_wei(bnb_amt, "ether")
        ROUTER    = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
        WBNB      = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
        router_abi = [{"name":"swapExactETHForTokensSupportingFeeOnTransferTokens",
                        "type":"function","stateMutability":"payable",
                        "inputs":[{"name":"amountOutMin","type":"uint256"},
                                   {"name":"path","type":"address[]"},
                                   {"name":"to","type":"address"},
                                   {"name":"deadline","type":"uint256"}],
                        "outputs":[{"name":"amounts","type":"uint256[]"}]}]
        router   = w3.eth.contract(address=Web3.to_checksum_address(ROUTER), abi=router_abi)
        deadline = int(time.time()) + 300
        tx = router.functions.swapExactETHForTokensSupportingFeeOnTransferTokens(
            0, [Web3.to_checksum_address(WBNB), Web3.to_checksum_address(token_address)],
            account.address, deadline,
        ).build_transaction({
            "from": account.address, "value": bnb_wei,
            "gas": 300000, "gasPrice": w3.to_wei("5","gwei"),
            "nonce": w3.eth.get_transaction_count(account.address),
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        return {"ok": True, "sig": tx_hash.hex(), "bnb_spent": bnb_amt}
    except ImportError:
        return {"ok": False, "error": "web3 not installed — add 'web3' to requirements.txt"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def get_sol_price() -> float:
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", timeout=5)
        return r.json()["solana"]["usd"]
    except: return 150.0

def get_bnb_price() -> float:
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=binancecoin&vs_currencies=usd", timeout=5)
        return r.json()["binancecoin"]["usd"]
    except: return 600.0

def open_position(pair: dict, amount_usd: float, tokens_received: int = 0):
    base    = pair.get("baseToken") or {}
    mint    = base.get("address","")
    price   = float(pair.get("priceUsd") or 0)
    chain   = pair.get("chainId","solana")
    positions[mint] = {
        "name":          base.get("name","?"),
        "symbol":        base.get("symbol","?"),
        "chain":         chain,
        "entry_price":   price,
        "amount_usd":    amount_usd,
        "amount_tokens": tokens_received,
        "peak_price":    price,
        "opened_at":     time.time(),
        "half_sold":     False,
        "url":           pair.get("url",""),
        "last_vol_time": time.time(),
    }
    log.info(f"Position opened: {base.get('name')} @ ${price}")

def close_position(mint: str, exit_price: float, reason: str) -> dict:
    pos = positions.pop(mint, None)
    if not pos: return {}
    entry    = pos["entry_price"]
    pnl_pct  = ((exit_price - entry) / entry * 100) if entry > 0 else 0
    pnl_usd  = pos["amount_usd"] * (pnl_pct / 100)
    held     = time.time() - pos["opened_at"]
    trade = {
        "mint":       mint,
        "name":       pos["name"],
        "symbol":     pos["symbol"],
        "chain":      pos["chain"],
        "entry":      entry,
        "exit":       exit_price,
        "pnl_usd":    pnl_usd,
        "pnl_pct":    pnl_pct,
        "amount_usd": pos["amount_usd"],
        "held_secs":  held,
        "reason":     reason,
        "opened_at":  pos["opened_at"],
        "closed_at":  time.time(),
        "url":        pos.get("url",""),
    }
    trade_history.append(trade)
    sign = "+" if pnl_usd >= 0 else ""
    emoji = "🟢" if pnl_usd >= 0 else "🔴"
    # Send PnL card
    pnl_bytes = make_pnl_card(trade)
    msg = f"""{emoji} <b>TRADE CLOSED</b>
━━━━━━━━━━━━━━━━━━━━
🪙 <b>{pos['name']} (${pos['symbol']})</b>
📢 Reason: {reason}
💰 Entry: ${entry:.10f}
💰 Exit:  ${exit_price:.10f}
📊 PnL: {sign}{pnl_pct:.1f}% ({sign}${pnl_usd:.2f})
⏱ Held: {int(held//60)}m {int(held%60)}s
🔍 <a href="{pos.get('url','')}">DEXScreener</a>
━━━━━━━━━━━━━━━━━━━━"""
    broadcast(msg, photo_bytes=pnl_bytes)
    log.info(f"Position closed: {pos['name']} PnL={sign}{pnl_pct:.1f}%")
    return trade

# ── Rug Score ─────────────────────────────────────────────────────────────────
def rug_score(pair, gem_mode=False, activity=None):
    flags=[]; greens=[]; danger=0; safety=0
    liq      = (pair.get("liquidity") or {}).get("usd", 0) or 0
    fdv      = pair.get("fdv") or 0
    mc       = pair.get("marketCap") or fdv or 0
    vol_h1   = (pair.get("volume") or {}).get("h1", 0) or 0
    ch_h1    = (pair.get("priceChange") or {}).get("h1", 0) or 0
    ch_h6    = (pair.get("priceChange") or {}).get("h6", 0) or 0
    ch_h24   = (pair.get("priceChange") or {}).get("h24", 0) or 0
    created  = pair.get("pairCreatedAt") or 0
    info     = pair.get("info") or {}
    socials  = info.get("socials") or []
    websites = info.get("websites") or []
    boosts   = pair.get("boosts") or {}
    dex_paid = boosts.get("active", 0) or 0
    txns_h1  = ((pair.get("txns") or {}).get("h1") or {})
    buys_h1  = txns_h1.get("buys", 0) or 0
    sells_h1 = txns_h1.get("sells", 0) or 0
    age_min  = ((time.time() * 1000) - created) / 60_000 if created else 9999
    if activity:
        unique_wallets = len(activity.get("wallets", set()))
        if unique_wallets >= 20:  greens.append(f"✅ {unique_wallets} unique buyers"); safety += 15
        elif unique_wallets >= 10: greens.append(f"✅ {unique_wallets} unique buyers"); safety += 8
        if activity.get("dev_sold"): flags.append("🚨 Dev already sold"); danger += 30
        else:                        greens.append("✅ Dev hasn't sold"); safety += 10
        bc = activity.get("bonding_pct", 0)
        if bc >= 50:   greens.append(f"✅ Bonding {bc:.0f}%"); safety += 15
        elif bc >= 20: greens.append(f"✅ Bonding {bc:.0f}%"); safety += 8
        elif bc >= 5:  flags.append(f"⚠️ Bonding only {bc:.0f}%"); danger += 5
    if dex_paid > 0: greens.append(f"✅ DEX Paid ({dex_paid} boosts)"); safety += 20
    else:             flags.append("❌ No DEX Paid"); danger += 5
    if liq >= 50_000:   greens.append(f"✅ Strong liq (${liq:,.0f})"); safety += 15
    elif liq >= 10_000: greens.append(f"✅ Decent liq (${liq:,.0f})"); safety += 10
    elif liq >= 3_000:  flags.append(f"⚠️ Low liq (${liq:,.0f})"); danger += 8
    else:                flags.append(f"🚨 Very low liq (${liq:,.0f})"); danger += 20
    if mc > 0 and liq > 0:
        mc_liq = mc / liq
        if mc_liq < 5 and gem_mode: greens.append(f"💎 Ultra low MC/Liq ({mc_liq:.1f}x)"); safety += 15
        elif mc_liq < 10:            greens.append(f"✅ Healthy MC/Liq ({mc_liq:.1f}x)"); safety += 10
        elif mc_liq > 500:           flags.append(f"🚨 MC/Liq {mc_liq:.0f}x"); danger += 20
    if fdv > 0 and liq > 0:
        ratio = fdv / liq
        if ratio > 1000:   flags.append(f"🚨 FDV/Liq {ratio:.0f}x — honeypot"); danger += 20
        elif ratio > 200:  flags.append(f"⚠️ FDV/Liq {ratio:.0f}x"); danger += 10
        elif ratio < 20:   greens.append(f"✅ Healthy FDV/Liq ({ratio:.0f}x)"); safety += 8
    if age_min < 10:      flags.append(f"🚨 Only {age_min:.0f} min old"); danger += 8
    elif age_min < 60:    flags.append(f"⚠️ {age_min:.0f} min old"); danger += 4
    elif age_min > 1440:  greens.append(f"✅ Survived 24h+"); safety += 10
    total_txns = buys_h1 + sells_h1
    if total_txns > 0:
        buy_ratio = buys_h1 / total_txns
        if buy_ratio > 0.7 and buys_h1 > 20:   greens.append(f"✅ Strong buys ({buys_h1} vs {sells_h1})"); safety += 15
        elif buy_ratio > 0.6 and buys_h1 > 10:  greens.append(f"✅ More buys ({buys_h1} vs {sells_h1})"); safety += 8
        elif buy_ratio < 0.3 and total_txns > 10: flags.append(f"🚨 Mostly sells ({sells_h1} vs {buys_h1})"); danger += 15
    if vol_h1 > 50_000:    greens.append(f"✅ Hot vol ${vol_h1:,.0f}/1h"); safety += 15
    elif vol_h1 > 10_000:  greens.append(f"✅ Growing vol ${vol_h1:,.0f}/1h"); safety += 8
    elif vol_h1 > 2_000:   greens.append(f"⚠️ Early vol ${vol_h1:,.0f}/1h"); safety += 4
    elif vol_h1 < 500 and age_min > 30: flags.append("❌ Almost no volume"); danger += 15
    social_types = [s.get("type","").lower() for s in socials]
    has_tw = "twitter" in social_types; has_tg = "telegram" in social_types; has_web = len(websites) > 0
    if has_tw and has_tg and has_web:   greens.append("✅ Full socials"); safety += 15
    elif has_tw and has_tg:              greens.append("✅ Twitter + TG"); safety += 10
    elif has_tw or has_tg:               flags.append("⚠️ One social"); danger += 5
    else:                                 flags.append("🚨 No socials — anon dev"); danger += 20
    if ch_h1 > 300 and liq < 30_000:    flags.append("🚨 300%+ pump + low liq"); danger += 25
    elif ch_h1 > 100 and liq < 10_000:  flags.append("⚠️ Big pump + low liq"); danger += 15
    if ch_h1 > 5 and ch_h6 > 10 and ch_h24 > 20 and liq > 5_000:
        greens.append("✅ Consistent growth 1h/6h/24h"); safety += 10
    holders = (pair.get("info") or {}).get("holders") or []
    if holders:
        top10_pct = sum(float(h.get("percentage",0)) for h in holders[:10])
        if top10_pct > 80:   flags.append(f"🚨 Top 10 own {top10_pct:.0f}%"); danger += 20
        elif top10_pct > 60: flags.append(f"⚠️ Top 10 own {top10_pct:.0f}%"); danger += 10
        else:                  greens.append(f"✅ Healthy distribution"); safety += 10
    score = max(0, min(100, safety - danger + 30))
    if danger >= 45 or (danger >= 25 and safety < 15): verdict = "🚨 LIKELY RUG"
    elif danger >= 20 or safety < 20:                   verdict = "⚠️ RISKY"
    elif score >= 60:                                    verdict = "💎 POTENTIAL GEM" if gem_mode else "✅ LOOKS GOOD"
    else:                                                verdict = "✅ LOOKS GOOD"
    return verdict, greens + flags, score

# ── Format Alert ──────────────────────────────────────────────────────────────
def format_alert(pair, trigger, verdict, flags, score, is_wc=False, is_gem=False, activity=None):
    base    = pair.get("baseToken") or {}
    name    = base.get("name","Unknown"); symbol = base.get("symbol","?")
    address = base.get("address","");    chain  = pair.get("chainId","solana").upper()
    price   = pair.get("priceUsd") or "?"
    mc      = pair.get("marketCap") or pair.get("fdv") or 0
    liq     = (pair.get("liquidity") or {}).get("usd",0) or 0
    vol_h1  = (pair.get("volume") or {}).get("h1",0) or 0
    vol_h24 = (pair.get("volume") or {}).get("h24",0) or 0
    ch_h1   = (pair.get("priceChange") or {}).get("h1",0) or 0
    ch_h6   = (pair.get("priceChange") or {}).get("h6",0) or 0
    ch_h24  = (pair.get("priceChange") or {}).get("h24",0) or 0
    txns    = ((pair.get("txns") or {}).get("h1") or {})
    buys    = txns.get("buys",0); sells = txns.get("sells",0)
    url     = pair.get("url",f"https://dexscreener.com/solana/{address}")
    created = pair.get("pairCreatedAt") or 0
    age_min = int(((time.time()*1000)-created)/60_000) if created else 0
    age_str = f"{age_min}m" if age_min < 120 else f"{age_min//60}h {age_min%60}m"
    bar     = "🟢"*(score//20)+"⚪"*(5-score//20)
    flags_text = "\n".join(flags) if flags else "None"
    mc_line = f"📊 MC: ${mc:,.0f}\n" if mc > 0 else ""
    alerts_left = settings["max_alerts_hr"] - len(alert_times)
    if is_wc:   header = "⚽ <b>WC MEMECOIN ALERT</b> ⚽"
    elif is_gem: header = "💎 <b>EARLY GEM ALERT</b> 💎"
    else:        header = "🚀 <b>TOKEN ALERT</b> 🚀"
    activity_line = ""
    if activity:
        wallets = len(activity.get("wallets",set()))
        bc      = activity.get("bonding_pct",0)
        vol_sol = activity.get("volume_sol",0)
        activity_line = f"🔥 {wallets} wallets | {bc:.0f}% bonding | {vol_sol:.1f} SOL\n"
    in_position = "📌 <b>IN POSITION</b>\n" if address in positions else ""
    return f"""{header}
━━━━━━━━━━━━━━━━━━━━
{in_position}🪙 <b>{name} (${symbol})</b>
🔗 Chain: {chain} | ⏱ Age: {age_str}
📢 <b>{trigger}</b>
💰 Price: ${price}
{mc_line}{activity_line}📊 1h: {ch_h1:+.1f}% | 6h: {ch_h6:+.1f}% | 24h: {ch_h24:+.1f}%
💧 Liquidity: ${liq:,.0f}
📦 Vol 1h: ${vol_h1:,.0f} | 24h: ${vol_h24:,.0f}
🔄 Buys/Sells (1h): {buys} / {sells}
🛡 Rug Score: {verdict}
{bar} {score}/100
{flags_text}
🔍 <a href="{url}">DEXScreener</a>
📋 CA: <code>{address}</code>
━━━━━━━━━━━━━━━━━━━━
⏰ {datetime.now(timezone.utc).strftime("%H:%M:%S UTC")} | {alerts_left} alerts left/hr"""

# ── Buy Confirmation Markup ───────────────────────────────────────────────────
def buy_markup(mint: str) -> dict:
    return {"inline_keyboard": [[
        {"text": f"✅ BUY ${settings['max_trade_usd']}", "callback_data": f"buy:{mint}"},
        {"text": "❌ SKIP",                               "callback_data": f"skip:{mint}"},
        {"text": "👀 WATCH",                              "callback_data": f"watch:{mint}"},
    ]]}

# ── Help Text ─────────────────────────────────────────────────────────────────
HELP_TEXT = """🤖 <b>Alpha Bot v7 Commands</b>
━━━━━━━━━━━━━━━━━━━━
<b>⚙️ Controls</b>
/pause — pause alerts
/resume — resume alerts
/status — current settings
/uptime — runtime stats

<b>🔗 Chain</b>
/chain solana|bsc|base|eth|all

<b>⚽ WC Scanner</b>
/wc on|off
/trending — hottest WC pumps
/top — top 5 WC tokens

<b>💎 Gem Hunter</b>
/gem on|off
/mcap [min] [max]
/minscore [0-100]

<b>📊 Filters</b>
/maxalerts [n]
/minbuys [n]
/minbonding [%]
/minliq [amount]
/threshold [%]
/safeonly on|off
/charts on|off

<b>💰 Trading</b>
/buy [CA] — manual buy
/sell [CA] — manual sell
/positions — open trades + PnL
/history — closed trades
/autobuy on|off — auto buy on alerts
/tradesize [$] — set trade size
/tp [%] — set take profit
/sl [%] — set trailing stop loss
/blacklist [CA] — never buy again
/blacklisted — view blacklist

<b>🔍 Lookup</b>
/check [CA]
/top
/trending
/findbetter

<b>🎯 Price Alerts</b>
/alert [CA] [%]
/alerts
/cancelalert [CA]

<b>🐋 Whale Tracker</b>
/addwhale [wallet]
/removewhale [wallet]
/whales — view tracked wallets

<b>📋 Recap</b>
/recap — last 24h summary
/pnl — all-time PnL summary
/debug — raw WebSocket diagnostics

<b>📌 Watchlist</b>
/watch [CA]
/unwatch [CA]
/watchlist

<b>🔄 Reset</b>
/reset — reset all settings
━━━━━━━━━━━━━━━━━━━━"""

# ── Commands ──────────────────────────────────────────────────────────────────
def handle_command(chat_id, text):
    text=text.strip(); parts=text.split(); cmd=parts[0].lower().split("@")[0]

    # ── v6 commands (all kept) ────────────────────────────────────────────────
    if cmd in ["/start","/help"]:
        send_tg(chat_id, HELP_TEXT)
    elif cmd in ["/uptime","/runtime"]:
        u=int(time.time()-start_time); d=u//86400; h=(u%86400)//3600; m=(u%3600)//60; s=u%60
        up=f"{d}d {h}h {m}m {s}s" if d>0 else f"{h}h {m}m {s}s"
        started=datetime.fromtimestamp(start_time,tz=timezone.utc).strftime("%b %d at %H:%M UTC")
        now=time.time()
        while alert_times and now-alert_times[0]>3600: alert_times.popleft()
        send_tg(chat_id,f"""⏱ <b>Bot Runtime</b>
━━━━━━━━━━━━━━━━━━━━
🟢 Uptime: {up}
📅 Started: {started}
📢 Total alerts: {total_alerts}
💎 Gems found: {total_gems}
📊 Alerts this hour: {len(alert_times)}/{settings['max_alerts_hr']}
💰 Open positions: {len(positions)}
📈 Closed trades: {len(trade_history)}
━━━━━━━━━━━━━━━━━━━━""")
    elif cmd=="/status":
        send_tg(chat_id,f"""⚙️ <b>Bot Status</b>
━━━━━━━━━━━━━━━━━━━━
{'⏸ PAUSED' if settings['paused'] else '▶️ RUNNING'}
🔗 Chains: {', '.join(settings['chains']).upper()}
⚽ WC Mode: {'On' if settings['wc_mode'] else 'Off'}
💎 Gem Mode: {'On' if settings['gem_mode'] else 'Off'}
💰 Trade size: ${settings['max_trade_usd']}
🎯 Take profit: {settings['take_profit_pct']}%
🛑 Trailing SL: {settings['trailing_stop']}%
🤖 Auto-buy: {'On' if settings['auto_buy'] else 'Off'}
🛡 Min score to buy: {settings['min_score_buy']}/100
📊 Alert threshold: {settings['threshold']}%
━━━━━━━━━━━━━━━━━━━━""")
    elif cmd=="/pause":  settings["paused"]=True;  send_tg(chat_id,"⏸ Paused.")
    elif cmd=="/resume": settings["paused"]=False; send_tg(chat_id,"▶️ Resumed!")
    elif cmd=="/maxalerts":
        if len(parts)<2 or not parts[1].isdigit(): send_tg(chat_id,"Usage: /maxalerts [n]"); return
        settings["max_alerts_hr"]=int(parts[1]); send_tg(chat_id,f"✅ Max alerts/hr: {settings['max_alerts_hr']}")
    elif cmd=="/minbuys":
        if len(parts)<2 or not parts[1].isdigit(): send_tg(chat_id,"Usage: /minbuys [n]"); return
        settings["min_buys_5min"]=int(parts[1]); send_tg(chat_id,f"✅ Min buys: {settings['min_buys_5min']}")
    elif cmd=="/minbonding":
        if len(parts)<2: send_tg(chat_id,"Usage: /minbonding [%]"); return
        settings["min_bonding_pct"]=float(parts[1]); send_tg(chat_id,f"✅ Min bonding: {settings['min_bonding_pct']}%")
    elif cmd=="/minliq":
        if len(parts)<2: send_tg(chat_id,"Usage: /minliq [amount]"); return
        settings["min_liq"]=int(parts[1]); send_tg(chat_id,f"✅ Min liq: ${settings['min_liq']:,}")
    elif cmd=="/threshold":
        if len(parts)<2: send_tg(chat_id,"Usage: /threshold [%]"); return
        settings["threshold"]=float(parts[1]); send_tg(chat_id,f"✅ Threshold: {settings['threshold']}%")
    elif cmd=="/minscore":
        if len(parts)<2: send_tg(chat_id,"Usage: /minscore [0-100]"); return
        settings["min_rug_score"]=int(parts[1]); send_tg(chat_id,f"✅ Min score: {settings['min_rug_score']}/100")
    elif cmd=="/safeonly":
        if len(parts)<2 or parts[1].lower() not in ["on","off"]: send_tg(chat_id,"Usage: /safeonly on|off"); return
        settings["safe_only"]=parts[1].lower()=="on"; send_tg(chat_id,f"✅ Safe only: {'On' if settings['safe_only'] else 'Off'}")
    elif cmd=="/charts":
        if len(parts)<2 or parts[1].lower() not in ["on","off"]: send_tg(chat_id,"Usage: /charts on|off"); return
        settings["charts"]=parts[1].lower()=="on"; send_tg(chat_id,f"✅ Charts: {'On' if settings['charts'] else 'Off'}")
    elif cmd=="/chain":
        if len(parts)<2: send_tg(chat_id,"Usage: /chain [solana|bsc|base|eth|all]"); return
        val=parts[1].lower()
        chain_map={"solana":["solana"],"bsc":["bsc"],"base":["base"],"eth":["ethereum"],"all":ALL_CHAINS[:]}
        if val not in chain_map: send_tg(chat_id,"Options: solana, bsc, base, eth, all"); return
        settings["chains"]=chain_map[val]; send_tg(chat_id,f"✅ Scanning: {', '.join(settings['chains']).upper()}")
    elif cmd=="/wc":
        if len(parts)<2 or parts[1].lower() not in ["on","off"]: send_tg(chat_id,"Usage: /wc on|off"); return
        settings["wc_mode"]=parts[1].lower()=="on"; send_tg(chat_id,f"✅ WC Scanner: {'On' if settings['wc_mode'] else 'Off'}")
    elif cmd=="/gem":
        if len(parts)<2 or parts[1].lower() not in ["on","off"]: send_tg(chat_id,"Usage: /gem on|off"); return
        settings["gem_mode"]=parts[1].lower()=="on"; send_tg(chat_id,f"✅ Gem Mode: {'On' if settings['gem_mode'] else 'Off'}")
    elif cmd=="/mcap":
        if len(parts)<3: send_tg(chat_id,"Usage: /mcap [min] [max]"); return
        settings["gem_mc_min"]=int(parts[1]); settings["gem_mc_max"]=int(parts[2])
        send_tg(chat_id,f"✅ MC range: ${settings['gem_mc_min']:,} - ${settings['gem_mc_max']:,}")
    elif cmd=="/reset":
        settings.update({"max_alerts_hr":13,"min_rug_score":35,"min_buys_5min":5,
            "min_bonding_pct":3.0,"min_vol_usd":500,"buy_ratio_min":0.55,
            "min_liq":1000,"chains":ALL_CHAINS[:],"wc_mode":True,"gem_mode":True,
            "gem_mc_min":2000,"gem_mc_max":50000,"paused":False,"charts":True,
            "safe_only":False,"threshold":20,"min_rug_score_wc":25,
            "auto_buy":False,"max_trade_usd":5.0,"take_profit_pct":100.0,
            "trailing_stop":25.0,"dead_vol_mins":30,"min_score_buy":50})
        send_tg(chat_id,"✅ All settings reset!")
    elif cmd=="/check":
        if len(parts)<2: send_tg(chat_id,"Usage: /check [CA]"); return
        send_tg(chat_id,"🔍 Checking token...")
        pair=dex_get(parts[1])
        if not pair: send_tg(chat_id,"❌ Token not found."); return
        base=pair.get("baseToken") or {}
        wc=is_wc_token(base.get("name",""),base.get("symbol",""))
        mc=pair.get("marketCap") or pair.get("fdv") or 0
        gem=settings["gem_mc_min"]<=mc<=settings["gem_mc_max"]
        act=token_activity.get(parts[1])
        verdict,flags,score=rug_score(pair,gem_mode=gem,activity=act)
        card_d=build_card_data(pair,"📋 Manual Check",verdict,score,wc,gem)
        card_b=make_alert_card(card_d)
        markup=buy_markup(parts[1]) if score>=settings["min_score_buy"] and parts[1] not in positions else None
        send_tg(chat_id,format_alert(pair,"📋 Manual Check",verdict,flags,score,is_wc=wc,is_gem=gem,activity=act),
                photo_bytes=card_b,reply_markup=markup)
        pending_buys[parts[1]]={"pair":pair,"score":score,"verdict":verdict,"flags":flags,"wc":wc,"gem":gem}
    elif cmd=="/watch":
        if len(parts)<2: send_tg(chat_id,"Usage: /watch [CA]"); return
        watchlist.add(parts[1].lower()); send_tg(chat_id,f"📌 Added! Watchlist: {len(watchlist)} tokens.")
    elif cmd=="/unwatch":
        if len(parts)<2: send_tg(chat_id,"Usage: /unwatch [CA]"); return
        watchlist.discard(parts[1].lower()); send_tg(chat_id,"✅ Removed.")
    elif cmd=="/watchlist":
        if not watchlist: send_tg(chat_id,"📌 Empty."); return
        send_tg(chat_id,"📌 <b>Watchlist</b>\n"+"".join([f"• <code>{ca}</code>\n" for ca in watchlist]))
    elif cmd=="/top":
        send_tg(chat_id,"🔍 Fetching top WC tokens...")
        results=[]
        for kw in ["worldcup","wc2026","fifa","mbappe","messi"]:
            for pair in dex_search(kw):
                if pair.get("chainId") in settings["chains"]:
                    vol=(pair.get("volume") or {}).get("h24",0) or 0
                    liq=(pair.get("liquidity") or {}).get("usd",0) or 0
                    if liq>1000: results.append((vol,pair))
        results.sort(key=lambda x:x[0],reverse=True)
        if not results: send_tg(chat_id,"No WC tokens found."); return
        msg="🏆 <b>Top 5 WC Tokens</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        for i,(vol,pair) in enumerate(results[:5],1):
            base=pair.get("baseToken") or {}
            ch24=(pair.get("priceChange") or {}).get("h24",0) or 0
            liq=(pair.get("liquidity") or {}).get("usd",0) or 0
            msg+=f"{i}. <b>{base.get('name','?')} (${base.get('symbol','?')})</b>\n   Vol: ${vol:,.0f} | Liq: ${liq:,.0f} | 24h: {ch24:+.1f}%\n   <a href=\"{pair.get('url','')}\">Chart</a>\n\n"
        send_tg(chat_id,msg)
    elif cmd=="/trending":
        send_tg(chat_id,"🔍 Finding hottest pumps...")
        results=[]
        for kw in list(WC_KEYWORDS)[:15]:
            for pair in dex_search(kw):
                if pair.get("chainId") in settings["chains"]:
                    ch1=(pair.get("priceChange") or {}).get("h1",0) or 0
                    liq=(pair.get("liquidity") or {}).get("usd",0) or 0
                    if liq>1000 and ch1>0: results.append((ch1,pair))
        results.sort(key=lambda x:x[0],reverse=True)
        if not results: send_tg(chat_id,"Nothing pumping."); return
        msg="🚀 <b>Trending (1h)</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        for i,(ch1,pair) in enumerate(results[:5],1):
            base=pair.get("baseToken") or {}
            liq=(pair.get("liquidity") or {}).get("usd",0) or 0
            wc="⚽" if is_wc_token(base.get("name",""),base.get("symbol","")) else ""
            msg+=f"{i}. {wc}<b>{base.get('name','?')} (${base.get('symbol','?')})</b> +{ch1:.1f}%\n   Liq: ${liq:,.0f} | <a href=\"{pair.get('url','')}\">Chart</a>\n\n"
        send_tg(chat_id,msg)
    elif cmd=="/findbetter":
        send_tg(chat_id,"💎 Hunting gems...")
        found=0; now=time.time()
        for mint,act in list(token_activity.items()):
            if found>=5: break
            if now-act.get("first_seen",now)>1800: continue
            if act.get("buys",0)<5 or act.get("bonding_pct",0)<3: continue
            pair=dex_get(mint)
            if not pair: continue
            liq=(pair.get("liquidity") or {}).get("usd",0) or 0
            if liq<500: continue
            wc=is_wc_token(act.get("name",""),act.get("symbol",""))
            mc=pair.get("marketCap") or pair.get("fdv") or 0
            gem=settings["gem_mc_min"]<=mc<=settings["gem_mc_max"]
            verdict,flags,score=rug_score(pair,gem_mode=gem,activity=act)
            if score>=35 and "RUG" not in verdict:
                card_d=build_card_data(pair,"💎 Manual Hunt",verdict,score,wc,gem)
                card_b=make_alert_card(card_d)
                markup=buy_markup(mint) if score>=settings["min_score_buy"] else None
                send_tg(chat_id,format_alert(pair,"💎 Manual Hunt",verdict,flags,score,is_wc=wc,is_gem=gem,activity=act),
                        photo_bytes=card_b,reply_markup=markup)
                pending_buys[mint]={"pair":pair,"score":score,"wc":wc,"gem":gem}
                found+=1; time.sleep(3)
        if found==0: send_tg(chat_id,"No fresh gems. Try again soon!")
    elif cmd=="/alert":
        if len(parts)<3: send_tg(chat_id,"Usage: /alert [CA] [%]"); return
        mint=parts[1]
        try: target=float(parts[2])
        except: send_tg(chat_id,"Invalid % — example: /alert ABC123 50"); return
        send_tg(chat_id,"🔍 Fetching...")
        pair=dex_get(mint)
        if not pair: send_tg(chat_id,"❌ Not found."); return
        entry=float(pair.get("priceUsd") or 0)
        if entry<=0: send_tg(chat_id,"❌ No price."); return
        base=pair.get("baseToken") or {}
        name=base.get("name",mint[:8]); sym=base.get("symbol","?")
        price_alerts[mint]={"target_pct":target,"entry_price":entry,"name":name,"symbol":sym,
                             "chat_id":chat_id,"url":pair.get("url",""),"set_at":time.time()}
        send_tg(chat_id,f"🎯 Alert set!\n<b>{name} (${sym})</b>\nEntry: ${entry}\nTarget: +{target}% 🔔")
    elif cmd=="/alerts":
        if not price_alerts: send_tg(chat_id,"No active alerts. Use /alert [CA] [%]"); return
        msg="🎯 <b>Active Price Alerts</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        for mint,a in price_alerts.items():
            pair=dex_get(mint); cur=float((pair or {}).get("priceUsd") or 0)
            pct=((cur-a["entry_price"])/a["entry_price"]*100) if a["entry_price"]>0 else 0
            msg+=f"• <b>{a['name']}</b> — Target: +{a['target_pct']}% | Now: {pct:+.1f}%\n"
        send_tg(chat_id,msg)
    elif cmd=="/cancelalert":
        if len(parts)<2: send_tg(chat_id,"Usage: /cancelalert [CA]"); return
        if parts[1] in price_alerts:
            name=price_alerts[parts[1]]["name"]; del price_alerts[parts[1]]
            send_tg(chat_id,f"✅ Alert removed for {name}.")
        else: send_tg(chat_id,"❌ Not found.")
    elif cmd=="/recap":
        now=time.time(); recent=[r for r in recap_log if now-r["time"]<=86400]
        if not recent: send_tg(chat_id,"📋 No alerts in last 24h."); return
        wc_c=sum(1 for r in recent if r.get("is_wc")); gem_c=sum(1 for r in recent if r.get("is_gem"))
        avg=sum(r["score"] for r in recent)/len(recent)
        msg=f"📋 <b>Last 24h Recap</b>\n━━━━━━━━━━━━━━━━━━━━\n📢 Alerts: {len(recent)} | ⚽ WC: {wc_c} | 💎 Gems: {gem_c}\n🛡 Avg score: {avg:.0f}/100\n━━━━━━━━━━━━━━━━━━━━\n"
        for r in recent[-10:]:
            t=datetime.fromtimestamp(r["time"],tz=timezone.utc).strftime("%H:%M")
            msg+=f"[{t}] <b>{r['name']} (${r['symbol']})</b> — {r['verdict']} {r['score']}/100\n"
        send_tg(chat_id,msg)

    # ── v7 NEW commands ───────────────────────────────────────────────────────
    elif cmd=="/buy":
        if len(parts)<2: send_tg(chat_id,"Usage: /buy [CA]"); return
        mint=parts[1]
        if mint in positions: send_tg(chat_id,"⚠️ Already in position for this token."); return
        if mint in rug_blacklist: send_tg(chat_id,"🚫 This token is blacklisted."); return
        send_tg(chat_id,"🔍 Fetching token..."); pair=dex_get(mint)
        if not pair: send_tg(chat_id,"❌ Token not found."); return
        base=pair.get("baseToken") or {}; chain=pair.get("chainId","solana")
        price=float(pair.get("priceUsd") or 0)
        send_tg(chat_id,f"🔄 Buying ${settings['max_trade_usd']} of {base.get('name','?')}...")
        if chain=="solana":
            result=execute_buy_solana(mint,settings["max_trade_usd"])
        elif chain=="bsc":
            result=execute_buy_bsc(mint,settings["max_trade_usd"])
        else:
            send_tg(chat_id,f"❌ Trading not supported on {chain} yet."); return
        if result["ok"]:
            open_position(pair,settings["max_trade_usd"],result.get("tokens",0))
            send_tg(chat_id,f"✅ <b>Bought!</b>\n🪙 {base.get('name','?')} (${base.get('symbol','?')})\n💰 ${settings['max_trade_usd']} at ${price}\n🔗 Tx: <code>{result.get('sig','')[:24]}...</code>")
        else:
            send_tg(chat_id,f"❌ Buy failed: {result.get('error','Unknown error')}")

    elif cmd=="/sell":
        if len(parts)<2: send_tg(chat_id,"Usage: /sell [CA]"); return
        mint=parts[1]
        if mint not in positions: send_tg(chat_id,"❌ No open position for this token."); return
        pos=positions[mint]; chain=pos["chain"]
        send_tg(chat_id,f"🔄 Selling {pos['name']}...")
        pair=dex_get(mint); exit_price=float((pair or {}).get("priceUsd") or 0)
        if chain=="solana":
            result=execute_sell_solana(mint,pos.get("amount_tokens",0))
        else:
            send_tg(chat_id,f"❌ Sell not supported on {chain} yet."); return
        if result["ok"]:
            close_position(mint,exit_price,"Manual sell")
        else:
            send_tg(chat_id,f"❌ Sell failed: {result.get('error','Unknown')}")

    elif cmd=="/positions":
        if not positions: send_tg(chat_id,"📊 No open positions."); return
        msg="📊 <b>Open Positions</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        total_pnl=0.0
        for mint,pos in positions.items():
            pair=dex_get(mint); cur=float((pair or {}).get("priceUsd") or 0)
            entry=pos["entry_price"]
            pnl_pct=((cur-entry)/entry*100) if entry>0 else 0
            pnl_usd=pos["amount_usd"]*(pnl_pct/100)
            total_pnl+=pnl_usd
            held=int((time.time()-pos["opened_at"])//60)
            emoji="🟢" if pnl_usd>=0 else "🔴"
            sign="+" if pnl_usd>=0 else ""
            peak_pct=((pos["peak_price"]-entry)/entry*100) if entry>0 else 0
            msg+=f"{emoji} <b>{pos['name']} (${pos['symbol']})</b>\n"
            msg+=f"   Entry: ${entry:.8f} | Now: ${cur:.8f}\n"
            msg+=f"   PnL: {sign}{pnl_pct:.1f}% ({sign}${pnl_usd:.2f})\n"
            msg+=f"   Peak: +{peak_pct:.1f}% | Held: {held}m\n"
            msg+=f"   <code>{mint[:20]}...</code>\n\n"
        sign="+" if total_pnl>=0 else ""
        msg+=f"━━━━━━━━━━━━━━━━━━━━\n💼 Total unrealised: {sign}${total_pnl:.2f}"
        send_tg(chat_id,msg)

    elif cmd=="/history":
        if not trade_history: send_tg(chat_id,"📈 No closed trades yet."); return
        wins=sum(1 for t in trade_history if t["pnl_usd"]>=0)
        total_pnl=sum(t["pnl_usd"] for t in trade_history)
        win_rate=(wins/len(trade_history)*100) if trade_history else 0
        sign="+" if total_pnl>=0 else ""
        msg=f"📈 <b>Trade History</b>\n━━━━━━━━━━━━━━━━━━━━\n✅ Wins: {wins} | ❌ Losses: {len(trade_history)-wins}\n🎯 Win rate: {win_rate:.0f}%\n💰 Total PnL: {sign}${total_pnl:.2f}\n━━━━━━━━━━━━━━━━━━━━\n"
        for t in trade_history[-10:]:
            emoji="🟢" if t["pnl_usd"]>=0 else "🔴"
            s="+" if t["pnl_usd"]>=0 else ""
            dt=datetime.fromtimestamp(t["closed_at"],tz=timezone.utc).strftime("%m/%d %H:%M")
            msg+=f"{emoji} [{dt}] <b>{t['name']}</b> {s}{t['pnl_pct']:.0f}% ({s}${t['pnl_usd']:.2f})\n"
        send_tg(chat_id,msg)

    elif cmd=="/pnl":
        if not trade_history: send_tg(chat_id,"No closed trades yet."); return
        wins=sum(1 for t in trade_history if t["pnl_usd"]>=0)
        losses=len(trade_history)-wins
        total_pnl=sum(t["pnl_usd"] for t in trade_history)
        best=max(trade_history,key=lambda t:t["pnl_pct"])
        worst=min(trade_history,key=lambda t:t["pnl_pct"])
        avg_hold=sum(t["held_secs"] for t in trade_history)/len(trade_history)
        sign="+" if total_pnl>=0 else ""
        send_tg(chat_id,f"""💰 <b>All-Time PnL Summary</b>
━━━━━━━━━━━━━━━━━━━━
📊 Total trades: {len(trade_history)}
✅ Wins: {wins} | ❌ Losses: {losses}
🎯 Win rate: {wins/len(trade_history)*100:.0f}%
💵 Total PnL: {sign}${total_pnl:.2f}
📈 Best trade: +{best['pnl_pct']:.0f}% ({best['name']})
📉 Worst trade: {worst['pnl_pct']:.0f}% ({worst['name']})
⏱ Avg hold time: {int(avg_hold//60)}m
━━━━━━━━━━━━━━━━━━━━""")

    elif cmd=="/autobuy":
        if len(parts)<2 or parts[1].lower() not in ["on","off"]: send_tg(chat_id,"Usage: /autobuy on|off"); return
        settings["auto_buy"]=parts[1].lower()=="on"
        send_tg(chat_id,f"🤖 Auto-buy: {'ON ⚠️ Bot will buy automatically!' if settings['auto_buy'] else 'OFF'}")

    elif cmd=="/tradesize":
        if len(parts)<2: send_tg(chat_id,"Usage: /tradesize [$amount]"); return
        try: settings["max_trade_usd"]=float(parts[1]); send_tg(chat_id,f"✅ Trade size: ${settings['max_trade_usd']}")
        except: send_tg(chat_id,"Invalid amount")

    elif cmd=="/tp":
        if len(parts)<2: send_tg(chat_id,"Usage: /tp [%]"); return
        try: settings["take_profit_pct"]=float(parts[1]); send_tg(chat_id,f"✅ Take profit: +{settings['take_profit_pct']}%")
        except: send_tg(chat_id,"Invalid %")

    elif cmd=="/sl":
        if len(parts)<2: send_tg(chat_id,"Usage: /sl [%]"); return
        try: settings["trailing_stop"]=float(parts[1]); send_tg(chat_id,f"✅ Trailing stop: -{settings['trailing_stop']}%")
        except: send_tg(chat_id,"Invalid %")

    elif cmd=="/blacklist":
        if len(parts)<2: send_tg(chat_id,"Usage: /blacklist [CA]"); return
        rug_blacklist.add(parts[1])
        if parts[1] in positions: close_position(parts[1],0,"Blacklisted")
        send_tg(chat_id,f"🚫 Blacklisted! Total: {len(rug_blacklist)}")

    elif cmd=="/blacklisted":
        if not rug_blacklist: send_tg(chat_id,"🚫 Blacklist is empty."); return
        send_tg(chat_id,"🚫 <b>Blacklist</b>\n"+"".join([f"• <code>{ca}</code>\n" for ca in rug_blacklist]))

    elif cmd=="/addwhale":
        if len(parts)<2: send_tg(chat_id,"Usage: /addwhale [wallet]"); return
        whale_wallets.add(parts[1]); send_tg(chat_id,f"🐋 Whale added! Tracking {len(whale_wallets)} wallets.")

    elif cmd=="/removewhale":
        if len(parts)<2: send_tg(chat_id,"Usage: /removewhale [wallet]"); return
        whale_wallets.discard(parts[1]); send_tg(chat_id,"✅ Removed.")

    elif cmd=="/whales":
        if not whale_wallets: send_tg(chat_id,"🐋 No whale wallets tracked. Use /addwhale [wallet]"); return
        send_tg(chat_id,"🐋 <b>Tracked Whales</b>\n"+"".join([f"• <code>{w}</code>\n" for w in whale_wallets]))

    elif cmd=="/debug":
        now=time.time()
        connected_secs=int(now-ws_connected_since) if ws_connected_since else 0
        since_last=int(now-last_event_time) if last_event_time else -1
        active_tokens=len(token_activity)
        tokens_with_buys=sum(1 for a in token_activity.values() if a.get("buys",0)>0)
        send_tg(chat_id,f"""🔧 <b>Debug Diagnostics</b>
━━━━━━━━━━━━━━━━━━━━
🔑 API key: {'✅ Loaded' if PUMPPORTAL_API_KEY else '❌ Missing'}
🔌 WS connected for: {connected_secs}s
📨 Total events: {ws_events_received}
🆕 'create' events: {ws_creates_received}
💱 'buy'/'sell' events: {ws_trades_received}
⏱ Last event: {since_last}s ago
📦 Tokens tracked: {active_tokens}
📈 Tokens w/ buys: {tokens_with_buys}
🔍 Last scan: {total_scans}
━━━━━━━━━━━━━━━━━━━━
{'⚠️ No events in 60s+ — WS may be silently stalled' if since_last>60 else '✅ Receiving data normally'}""")

    else:
        send_tg(chat_id,"❓ Unknown command. Send /help")

# ── Callback Query Handler (inline buttons) ───────────────────────────────────
def handle_callback(callback_query):
    cq_id   = callback_query["id"]
    data    = callback_query.get("data","")
    chat_id = str((callback_query.get("message") or {}).get("chat",{}).get("id",""))
    if not data or not chat_id: return
    action, mint = data.split(":",1) if ":" in data else (data,"")
    if action=="buy":
        answer_callback(cq_id,"🔄 Buying...")
        if mint in positions:
            send_tg(chat_id,"⚠️ Already in position."); return
        if mint in rug_blacklist:
            send_tg(chat_id,"🚫 Blacklisted."); return
        pb=pending_buys.get(mint,{})
        pair=pb.get("pair") or dex_get(mint)
        if not pair: send_tg(chat_id,"❌ Token data expired."); return
        base=pair.get("baseToken") or {}; chain=pair.get("chainId","solana")
        send_tg(chat_id,f"🔄 Buying ${settings['max_trade_usd']} of {base.get('name','?')}...")
        if chain=="solana":   result=execute_buy_solana(mint,settings["max_trade_usd"])
        elif chain=="bsc":    result=execute_buy_bsc(mint,settings["max_trade_usd"])
        else:                 send_tg(chat_id,f"❌ Trading not supported on {chain}."); return
        if result["ok"]:
            open_position(pair,settings["max_trade_usd"],result.get("tokens",0))
            send_tg(chat_id,f"✅ <b>Bought!</b> {base.get('name','?')} @ ${pair.get('priceUsd','?')}\n🔗 Tx: <code>{result.get('sig','')[:24]}...</code>")
        else:
            send_tg(chat_id,f"❌ Buy failed: {result.get('error','')}")
    elif action=="skip":
        answer_callback(cq_id,"❌ Skipped")
    elif action=="watch":
        watchlist.add(mint.lower())
        answer_callback(cq_id,"👀 Added to watchlist!")
        send_tg(chat_id,f"📌 Added to watchlist! ({len(watchlist)} total)")

# ── Poll Telegram ─────────────────────────────────────────────────────────────
def poll_commands():
    global last_update_id
    if not TELEGRAM_TOKEN: return
    try:
        r=requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset":last_update_id+1,"timeout":5},timeout=10,
        )
        for update in r.json().get("result",[]):
            last_update_id=update["update_id"]
            # Handle inline button presses
            if "callback_query" in update:
                handle_callback(update["callback_query"]); continue
            msg=update.get("message") or update.get("channel_post") or {}
            text=msg.get("text",""); chat_id=(msg.get("chat") or {}).get("id")
            if text.startswith("/") and chat_id:
                handle_command(str(chat_id),text)
    except Exception as e:
        log.error(f"Poll error: {e}")

# ── Position Monitor (trailing stop + take profit) ────────────────────────────
async def position_monitor():
    while True:
        await asyncio.sleep(30)
        if not positions: continue
        for mint in list(positions.keys()):
            try:
                pos  = positions.get(mint)
                if not pos: continue
                pair = dex_get(mint)
                if not pair: continue
                cur  = float(pair.get("priceUsd") or 0)
                if cur<=0: continue
                entry= pos["entry_price"]
                # Update peak price
                if cur > pos["peak_price"]:
                    positions[mint]["peak_price"]=cur
                peak = positions[mint]["peak_price"]
                pnl_pct = ((cur-entry)/entry*100) if entry>0 else 0
                drop_from_peak = ((peak-cur)/peak*100) if peak>0 else 0
                vol_h1=(pair.get("volume") or {}).get("h1",0) or 0
                if vol_h1>0:
                    positions[mint]["last_vol_time"]=time.time()
                # Take profit: sell HALF at 2x, let rest ride
                if pnl_pct>=settings["take_profit_pct"] and not pos["half_sold"]:
                    positions[mint]["half_sold"]=True
                    positions[mint]["amount_usd"]/=2
                    chain=pos["chain"]
                    half_tokens=pos.get("amount_tokens",0)//2
                    if chain=="solana" and half_tokens>0:
                        result=execute_sell_solana(mint,half_tokens)
                        if result["ok"]:
                            positions[mint]["amount_tokens"]-=half_tokens
                            pnl_usd=pos["amount_usd"]*(pnl_pct/100)
                            send_tg(TELEGRAM_CHAT_ID,f"🎯 <b>HALF SOLD at {settings['take_profit_pct']}% target!</b>\n🪙 {pos['name']}\n💰 +${pnl_usd:.2f}\n🔁 Other half still riding...")
                # Trailing stop loss
                elif drop_from_peak>=settings["trailing_stop"] and pnl_pct<settings["take_profit_pct"]:
                    chain=pos["chain"]
                    if chain=="solana":
                        result=execute_sell_solana(mint,pos.get("amount_tokens",0))
                        if result["ok"]:
                            close_position(mint,cur,f"Trailing SL (-{drop_from_peak:.0f}% from peak)")
                        else:
                            close_position(mint,cur,f"Trailing SL (sell failed: {result.get('error','')})")
                    else:
                        close_position(mint,cur,f"Trailing SL (-{drop_from_peak:.0f}% from peak)")
                # Dead volume auto-sell
                elif (time.time()-pos.get("last_vol_time",pos["opened_at"]))/60>settings["dead_vol_mins"]:
                    close_position(mint,cur,f"Dead — no volume for {settings['dead_vol_mins']}min")
            except Exception as e:
                log.error(f"Position monitor error [{mint[:8]}]: {e}")

# ── Price Alert Checker ───────────────────────────────────────────────────────
async def price_alert_loop():
    while True:
        await asyncio.sleep(60)
        if not price_alerts: continue
        to_remove=[]
        for mint,a in list(price_alerts.items()):
            try:
                pair=dex_get(mint)
                if not pair: continue
                cur=float(pair.get("priceUsd") or 0)
                if cur<=0: continue
                pct=(cur-a["entry_price"])/a["entry_price"]*100
                if pct>=a["target_pct"]:
                    send_tg(a["chat_id"],f"""🎯 <b>PRICE ALERT HIT!</b>
🪙 <b>{a['name']} (${a['symbol']})</b>
📈 Target: +{a['target_pct']}% ✅ Actual: +{pct:.1f}%
💰 Price now: ${cur}
🔍 <a href="{a['url']}">DEXScreener</a>""")
                    to_remove.append(mint)
            except Exception as e:
                log.error(f"Price alert error: {e}")
        for mint in to_remove: price_alerts.pop(mint,None)

# ── DEXScreener Fallback Scanner (runs if WebSocket dies) ────────────────────
async def dexscreener_fallback():
    """Scan DEXScreener every 30s as a fallback when WebSocket is down."""
    log.info("DEXScreener fallback scanner started")
    while True:
        await asyncio.sleep(30)
        if settings["paused"]: continue
        try:
            checked = set()
            keywords = list(WC_KEYWORDS)[:20] if settings["wc_mode"] else ["solana","bsc","meme","pepe","doge"]
            for kw in keywords:
                pairs = dex_search(kw)
                for pair in pairs:
                    chain = pair.get("chainId","")
                    if chain not in settings["chains"]: continue
                    base  = pair.get("baseToken") or {}
                    mint  = base.get("address","")
                    if not mint or mint in checked or mint in seen or mint in rug_blacklist: continue
                    checked.add(mint)
                    sym = base.get("symbol","").upper()
                    if is_stable(sym): continue
                    liq    = (pair.get("liquidity") or {}).get("usd",0) or 0
                    vol_h1 = (pair.get("volume") or {}).get("h1",0) or 0
                    ch_h1  = (pair.get("priceChange") or {}).get("h1",0) or 0
                    created= pair.get("pairCreatedAt") or 0
                    age_min= ((time.time()*1000)-created)/60_000 if created else 9999
                    if liq < settings["min_liq"]: continue
                    # Only alert on new tokens (under 30 min) or big moves
                    if age_min > 30 and abs(ch_h1) < settings["threshold"]: continue
                    if not can_alert(): break
                    fake_activity = {
                        "buys":int(vol_h1/50) if vol_h1 else 5,
                        "sells":2,"wallets":set(),"volume_sol":vol_h1/150,
                        "first_seen":time.time()-(age_min*60),
                        "dev_sold":False,"bonding_pct":10.0,
                        "name":base.get("name",""),"symbol":sym,"mint":mint,
                    }
                    loop = asyncio.get_event_loop()
                    loop.run_in_executor(None, process_token, mint, fake_activity)
                    await asyncio.sleep(0.5)
        except Exception as e:
            log.error(f"DEXScreener fallback error: {e}")

# ── Pump.fun WebSocket ────────────────────────────────────────────────────────
WS_URL = f"wss://pumpportal.fun/api/data?api-key={PUMPPORTAL_API_KEY}" if PUMPPORTAL_API_KEY else "wss://pumpportal.fun/api/data"

async def pumpfun_ws():
    global ws_events_received, ws_creates_received, ws_trades_received, ws_connected_since, last_event_time
    log.info("Connecting to Pump.fun WebSocket...")
    retry_count = 0
    subscribed_mints = []  # FIX: accumulate all mints, don't replace on each new token
    while True:
        log.info(f"Trying WebSocket: {WS_URL} (attempt {retry_count+1})")
        try:
            async with websockets.connect(WS_URL,ping_interval=20,ping_timeout=10) as ws:
                log.info(f"Connected to {WS_URL}!")
                ws_connected_since = time.time()
                if retry_count > 0:
                    broadcast(f"🔌 WebSocket reconnected after {retry_count} retries")
                retry_count = 0
                subscribed_mints = []  # reset on reconnect, will re-subscribe as creates come in
                await ws.send(json.dumps({"method":"subscribeNewToken"}))
                async for raw in ws:
                    try:
                        ws_events_received += 1
                        last_event_time = time.time()
                        event=json.loads(raw)
                        tx_type=event.get("txType",""); mint=event.get("mint","")
                        if not mint: continue
                        if tx_type=="create":
                            ws_creates_received += 1
                            name=event.get("name",""); symbol=event.get("symbol","")
                            if is_stable(symbol): continue
                            token_activity[mint]["name"]=name
                            token_activity[mint]["symbol"]=symbol
                            token_activity[mint]["mint"]=mint
                            token_activity[mint]["first_seen"]=time.time()
                            token_activity[mint]["bonding_pct"]=float(event.get("vSolInBondingCurve",0) or 0)/85*100
                            # FIX: accumulate mints, send full list so old subscriptions aren't dropped
                            subscribed_mints.append(mint)
                            if len(subscribed_mints) > 200:  # cap to avoid unbounded growth
                                subscribed_mints = subscribed_mints[-200:]
                            await ws.send(json.dumps({"method":"subscribeTokenTrade","keys":subscribed_mints}))
                        elif tx_type in ["buy","sell"]:
                            ws_trades_received += 1
                            act=token_activity[mint]
                            trader=event.get("traderPublicKey","")
                            creator=event.get("creatorPublicKey","")
                            sol_amt=float(event.get("solAmount",0) or 0)/1e9
                            if tx_type=="buy":
                                act["buys"]+=1; act["volume_sol"]+=sol_amt
                                if trader: act["wallets"].add(trader)
                                # Whale detection
                                if trader in whale_wallets:
                                    pair=dex_get(mint)
                                    name=act.get("name",mint[:8])
                                    broadcast(f"🐋 <b>WHALE ALERT!</b>\nTracked wallet bought <b>{name}</b>\n💰 {sol_amt:.2f} SOL\n📋 <code>{mint}</code>")
                            else:
                                act["sells"]+=1
                                if trader==creator: act["dev_sold"]=True
                            bc_sol=float(event.get("vSolInBondingCurve",0) or 0)
                            if bc_sol>0: act["bonding_pct"]=bc_sol/85*100
                            age_min=(time.time()-act["first_seen"])/60
                            buys=act["buys"]; unique_w=len(act["wallets"])
                            vol_sol=act["volume_sol"]; bc_pct=act["bonding_pct"]
                            total_txns=buys+act["sells"]
                            buy_ratio=buys/total_txns if total_txns>0 else 0
                            vol_usd_est=vol_sol*150
                            wc_token=is_wc_token(act["name"],act["symbol"])
                            # ── FIXED FILTERS: WC tokens easier, non-WC also realistic
                            if wc_token and settings["wc_mode"]:
                                passes=(buys>=5 and not act["dev_sold"] and buy_ratio>=0.5 and mint not in seen and mint not in rug_blacklist)
                            else:
                                # FIXED: much more lenient so non-WC tokens show up
                                passes=(
                                    unique_w>=settings["min_buys_5min"] and      # 5 (was 10)
                                    bc_pct>=settings["min_bonding_pct"] and       # 3% (was 5%)
                                    not act["dev_sold"] and
                                    buy_ratio>=settings["buy_ratio_min"] and      # 0.55 (was 0.60)
                                    vol_usd_est>=settings["min_vol_usd"] and      # $500 (was $2000)
                                    age_min<=15 and                                # 15min (was 10)
                                    mint not in seen and
                                    mint not in rug_blacklist
                                )
                            if passes and can_alert() and not settings["paused"]:
                                loop=asyncio.get_event_loop()
                                loop.run_in_executor(None,process_token,mint,dict(act))
                    except json.JSONDecodeError: continue
                    except Exception as e: log.error(f"Event error: {e}")
        except Exception as e:
            retry_count += 1
            wait = min(5 * retry_count, 30)  # backoff up to 30s max
            log.error(f"WS error: {e} — retry #{retry_count} in {wait}s...")
            if retry_count in (3, 10, 20):  # alert you if it's struggling
                broadcast(f"⚠️ WebSocket reconnecting (attempt {retry_count})... DEXScreener fallback is still scanning.")
            await asyncio.sleep(wait)

# ── Process Token ─────────────────────────────────────────────────────────────
def process_token(mint,activity):
    global total_alerts,total_gems
    if not can_alert() or settings["paused"]: return
    if mint in rug_blacklist: return
    pair=dex_get(mint)
    if not pair: return
    liq=(pair.get("liquidity") or {}).get("usd",0) or 0
    if liq<settings["min_liq"]: return
    base=pair.get("baseToken") or {}
    name=base.get("name",activity.get("name",""))
    symbol=base.get("symbol",activity.get("symbol",""))
    wc=is_wc_token(name,symbol)
    mc=pair.get("marketCap") or pair.get("fdv") or 0
    gem=settings["gem_mc_min"]<=mc<=settings["gem_mc_max"]
    verdict,flags,score=rug_score(pair,gem_mode=gem,activity=activity)
    min_score=settings["min_rug_score_wc"] if wc else settings["min_rug_score"]
    if score<min_score: return
    if settings["safe_only"] and "RUG" in verdict: return
    if wc:   trigger="⚽ NEW WC TOKEN — Pump.fun Launch"
    elif gem: trigger=f"💎 EARLY GEM — MC ${mc:,.0f}"
    else:     trigger="🆕 NEW TOKEN — Passed filters"
    card_d=build_card_data(pair,trigger,verdict,score,wc,gem)
    card_b=make_alert_card(card_d)
    # Show BUY button if score is high enough
    markup=None
    if score>=settings["min_score_buy"] and mint not in positions:
        markup=buy_markup(mint)
        pending_buys[mint]={"pair":pair,"score":score,"wc":wc,"gem":gem}
    # Auto-buy if enabled
    if settings["auto_buy"] and score>=settings["min_score_buy"] and mint not in positions:
        chain=pair.get("chainId","solana")
        if chain=="solana": result=execute_buy_solana(mint,settings["max_trade_usd"])
        elif chain=="bsc":  result=execute_buy_bsc(mint,settings["max_trade_usd"])
        else:               result={"ok":False,"error":"unsupported chain"}
        if result["ok"]:
            open_position(pair,settings["max_trade_usd"],result.get("tokens",0))
            markup=None  # no need for button if already bought
    broadcast(
        format_alert(pair,trigger,verdict,flags,score,is_wc=wc,is_gem=gem,activity=activity),
        photo_bytes=card_b, reply_markup=markup,
    )
    record_alert(); total_alerts+=1
    if gem: total_gems+=1
    seen[mint]={"alerted_at":time.time()}
    recap_log.append({"time":time.time(),"name":name,"symbol":symbol,"trigger":trigger,
                       "score":score,"verdict":verdict,"is_wc":wc,"is_gem":gem,"url":pair.get("url","")})
    cutoff=time.time()-86400
    while recap_log and recap_log[0]["time"]<cutoff: recap_log.pop(0)
    log.info(f"Alerted: {name} ({symbol}) score={score} wc={wc} gem={gem}")

# ── Loops ─────────────────────────────────────────────────────────────────────
async def command_loop():
    while True:
        try: await asyncio.get_event_loop().run_in_executor(None,poll_commands)
        except Exception as e: log.error(f"Command loop error: {e}")
        await asyncio.sleep(3)

async def cleanup_loop():
    while True:
        await asyncio.sleep(300)
        now=time.time()
        to_del=[m for m,a in token_activity.items() if now-a.get("first_seen",now)>3600]
        seen_del=[m for m,v in seen.items() if now-v.get("alerted_at",now)>86400]
        for m in to_del: del token_activity[m]
        for m in seen_del: del seen[m]

async def daily_pnl_summary():
    """Send daily PnL summary at midnight UTC."""
    while True:
        now=datetime.now(timezone.utc)
        secs_to_midnight=(24-now.hour)*3600-now.minute*60-now.second
        await asyncio.sleep(secs_to_midnight)
        if not trade_history: continue
        today=[t for t in trade_history if time.time()-t["closed_at"]<=86400]
        if not today: continue
        wins=sum(1 for t in today if t["pnl_usd"]>=0)
        total_pnl=sum(t["pnl_usd"] for t in today)
        sign="+" if total_pnl>=0 else ""
        emoji="🟢" if total_pnl>=0 else "🔴"
        broadcast(f"""{emoji} <b>Daily PnL Summary</b>
━━━━━━━━━━━━━━━━━━━━
📊 Trades today: {len(today)}
✅ Wins: {wins} | ❌ Losses: {len(today)-wins}
💰 Today's PnL: {sign}${total_pnl:.2f}
━━━━━━━━━━━━━━━━━━━━
All-time PnL: {'+' if sum(t['pnl_usd'] for t in trade_history)>=0 else ''}${sum(t['pnl_usd'] for t in trade_history):.2f}""")

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    log.info("Alpha Bot v7 starting...")
    key_status = "✅ PumpPortal API key loaded — trade data enabled" if PUMPPORTAL_API_KEY else "⚠️ No PumpPortal API key — trade events may not work!"
    log.info(key_status)
    broadcast(
        "🤖 <b>Alpha Bot v7 is LIVE!</b>\n"
        "🔌 Pump.fun WebSocket connected\n"
        f"{key_status}\n"
        "⚡ Real-time token detection\n"
        "⚽ WC tokens + 💎 Gems + 🛡 Rug Score\n"
        "🖼 Image cards ON\n"
        "💰 Trading: SOL (Jupiter) + BSC (PancakeSwap)\n"
        "📊 PnL cards on every trade close\n"
        "🐋 Whale wallet tracker\n"
        "🔧 FIXED: All token types now showing\n\n"
        "/help to see all commands 👇"
    )
    await asyncio.gather(
        pumpfun_ws(),
        dexscreener_fallback(),
        command_loop(),
        cleanup_loop(),
        price_alert_loop(),
        position_monitor(),
        daily_pnl_summary(),
    )

if __name__=="__main__":
    asyncio.run(main())
