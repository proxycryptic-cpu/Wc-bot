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

# ── Settings ──────────────────────────────────────────────────────────────────
settings = {
    "max_alerts_hr":    13,
    "min_rug_score":    45,
    "min_buys_5min":    10,
    "min_bonding_pct":  5.0,
    "min_vol_usd":      2000,
    "buy_ratio_min":    0.60,
    "min_liq":          3000,
    "chains":           ["solana", "bsc", "base", "ethereum"],
    "wc_mode":          True,
    "gem_mode":         True,
    "gem_mc_min":       2000,
    "gem_mc_max":       30000,
    "paused":           False,
    "charts":           True,
    "safe_only":        False,
    "threshold":        20,
    "min_rug_score_wc": 30,
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
last_update_id = 0
start_time = time.time()
total_alerts = 0
total_gems = 0
total_scans = 0
alert_times = deque()
last_tg_send = 0

token_activity: dict = defaultdict(lambda: {
    "buys": 0, "sells": 0, "wallets": set(),
    "volume_sol": 0.0, "first_seen": time.time(),
    "dev_sold": False, "bonding_pct": 0.0,
    "name": "", "symbol": "", "mint": "",
})

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
    name   = name.lower()
    symbol = symbol.lower()
    for kw in WC_KEYWORDS:
        if kw in name or kw in symbol:
            return True
    return False

def is_stable(symbol):
    return symbol.upper() in {"USDT","USDC","BUSD","DAI","WETH","WBNB","WSOL","ETH","BNB","SOL","WBTC"}

# ── Image Card Generator ──────────────────────────────────────────────────────


def _rounded_rect(draw, xy, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)

def _score_color(score):
    if score >= 60: return "#22c55e"
    if score >= 35: return "#f59e0b"
    return "#ef4444"

def make_alert_card(data: dict) -> bytes:
    W, H   = 620, 400
    BG     = "#0d1117"
    CARD   = "#161b22"
    BORDER = "#30363d"
    WHITE  = "#f0f6fc"
    MUTED  = "#8b949e"
    GREEN  = "#22c55e"
    RED    = "#ef4444"
    PURPLE = "#a855f7"
    SOCCER = "#3b82f6"

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
    if is_wc:
        hdr_color, hdr_text = SOCCER, "WC MEMECOIN ALERT"
    elif is_gem:
        hdr_color, hdr_text = PURPLE, "EARLY GEM ALERT"
    else:
        hdr_color, hdr_text = GREEN, "TOKEN ALERT"
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
        bar_w = W - 48
        buy_w = int(bar_w * buys / total)
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
def send_tg(chat_id, text, photo_url=None, card_data=None):
    global last_tg_send
    wait = 3 - (time.time() - last_tg_send)
    if wait > 0:
        time.sleep(wait)
    try:
        # Try sending generated image card first
        if card_data and settings.get("charts", True):
            try:
                img_bytes = make_alert_card(card_data)
                buf = io.BytesIO(img_bytes)
                r = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                    data={"chat_id": chat_id, "caption": text[:1024], "parse_mode": "HTML"},
                    files={"photo": ("alert.png", buf, "image/png")},
                    timeout=20,
                )
                last_tg_send = time.time()
                if r.ok:
                    return
            except Exception as img_err:
                log.error(f"Card error: {img_err}")

        # Fallback to photo_url
        if photo_url and settings.get("charts", True):
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                json={"chat_id": chat_id, "photo": photo_url,
                      "caption": text[:1024], "parse_mode": "HTML"},
                timeout=15,
            )
            last_tg_send = time.time()
            if r.ok:
                return

        # Fallback to text only
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        ).raise_for_status()
        last_tg_send = time.time()
    except Exception as e:
        log.error(f"TG error: {e}")

def broadcast(text, photo_url=None, card_data=None):
    send_tg(TELEGRAM_CHAT_ID, text, photo_url, card_data)

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

def chart_url(mint, chain="solana"):
    return f"https://dexscreener.com/{chain}/{mint}/chart.png"

# ── Rug Score ─────────────────────────────────────────────────────────────────
def rug_score(pair, gem_mode=False, activity=None):
    flags  = []
    greens = []
    danger = 0
    safety = 0

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
        if unique_wallets >= 20:
            greens.append(f"✅ {unique_wallets} unique buyers at launch")
            safety += 15
        elif unique_wallets >= 10:
            greens.append(f"✅ {unique_wallets} unique buyers at launch")
            safety += 8
        if activity.get("dev_sold"):
            flags.append("🚨 Dev already sold — dumped on buyers")
            danger += 30
        else:
            greens.append("✅ Dev hasn't sold yet")
            safety += 10
        bc = activity.get("bonding_pct", 0)
        if bc >= 50:
            greens.append(f"✅ Bonding curve {bc:.0f}% — strong momentum")
            safety += 15
        elif bc >= 20:
            greens.append(f"✅ Bonding curve {bc:.0f}%")
            safety += 8
        elif bc >= 5:
            flags.append(f"⚠️ Bonding curve only {bc:.0f}%")
            danger += 5

    if dex_paid > 0:
        greens.append(f"✅ DEX Paid ({dex_paid} boosts)")
        safety += 20
    else:
        flags.append("❌ No DEX Paid")
        danger += 5

    if liq >= 50_000:
        greens.append(f"✅ Strong liquidity (${liq:,.0f})")
        safety += 15
    elif liq >= 10_000:
        greens.append(f"✅ Decent liquidity (${liq:,.0f})")
        safety += 10
    elif liq >= 3_000:
        flags.append(f"⚠️ Low liquidity (${liq:,.0f})")
        danger += 8
    else:
        flags.append(f"🚨 Very low liquidity (${liq:,.0f})")
        danger += 20

    if mc > 0 and liq > 0:
        mc_liq = mc / liq
        if mc_liq < 5 and gem_mode:
            greens.append(f"💎 Ultra low MC/Liq ({mc_liq:.1f}x)")
            safety += 15
        elif mc_liq < 10:
            greens.append(f"✅ Healthy MC/Liq ({mc_liq:.1f}x)")
            safety += 10
        elif mc_liq > 500:
            flags.append(f"🚨 MC/Liq {mc_liq:.0f}x — overvalued")
            danger += 20

    if fdv > 0 and liq > 0:
        ratio = fdv / liq
        if ratio > 1000:
            flags.append(f"🚨 FDV/Liq {ratio:.0f}x — honeypot")
            danger += 20
        elif ratio > 200:
            flags.append(f"⚠️ FDV/Liq {ratio:.0f}x — risky")
            danger += 10
        elif ratio < 20:
            greens.append(f"✅ Healthy FDV/Liq ({ratio:.0f}x)")
            safety += 8

    if age_min < 10:
        flags.append(f"🚨 Only {age_min:.0f} min old")
        danger += 8
    elif age_min < 60:
        flags.append(f"⚠️ {age_min:.0f} min old — very new")
        danger += 4
    elif age_min > 1440:
        greens.append(f"✅ Survived 24h+ ({age_min/60:.0f}h old)")
        safety += 10

    total_txns = buys_h1 + sells_h1
    if total_txns > 0:
        buy_ratio = buys_h1 / total_txns
        if buy_ratio > 0.7 and buys_h1 > 20:
            greens.append(f"✅ Strong buys ({buys_h1} vs {sells_h1} sells)")
            safety += 15
        elif buy_ratio > 0.6 and buys_h1 > 10:
            greens.append(f"✅ More buys ({buys_h1} vs {sells_h1})")
            safety += 8
        elif buy_ratio < 0.3 and total_txns > 10:
            flags.append(f"🚨 Mostly sells ({sells_h1} vs {buys_h1} buys)")
            danger += 15

    if vol_h1 > 50_000:
        greens.append(f"✅ Hot volume ${vol_h1:,.0f}/1h")
        safety += 15
    elif vol_h1 > 10_000:
        greens.append(f"✅ Growing volume ${vol_h1:,.0f}/1h")
        safety += 8
    elif vol_h1 > 2_000:
        greens.append(f"⚠️ Early volume ${vol_h1:,.0f}/1h")
        safety += 4
    elif vol_h1 < 500 and age_min > 30:
        flags.append("❌ Almost no volume")
        danger += 15

    social_types = [s.get("type", "").lower() for s in socials]
    has_tw  = "twitter" in social_types
    has_tg  = "telegram" in social_types
    has_web = len(websites) > 0
    if has_tw and has_tg and has_web:
        greens.append("✅ Full socials (Twitter + TG + Website)")
        safety += 15
    elif has_tw and has_tg:
        greens.append("✅ Twitter + Telegram")
        safety += 10
    elif has_tw or has_tg:
        flags.append("⚠️ One social found")
        danger += 5
    else:
        flags.append("🚨 No socials — anon dev")
        danger += 20

    if ch_h1 > 300 and liq < 30_000:
        flags.append("🚨 300%+ pump + low liq — rug setup")
        danger += 25
    elif ch_h1 > 100 and liq < 10_000:
        flags.append("⚠️ Big pump + very low liq")
        danger += 15

    if ch_h1 > 5 and ch_h6 > 10 and ch_h24 > 20 and liq > 5_000:
        greens.append("✅ Consistent growth 1h/6h/24h")
        safety += 10

    score = max(0, min(100, safety - danger + 30))

    if danger >= 45 or (danger >= 25 and safety < 15):
        verdict = "🚨 LIKELY RUG"
    elif danger >= 20 or safety < 20:
        verdict = "⚠️ RISKY"
    elif score >= 60:
        verdict = "💎 POTENTIAL GEM" if gem_mode else "✅ LOOKS GOOD"
    else:
        verdict = "✅ LOOKS GOOD"

    return verdict, greens + flags, score

# ── Format Alert ──────────────────────────────────────────────────────────────
def format_alert(pair, trigger, verdict, flags, score, is_wc=False, is_gem=False, activity=None):
    base    = pair.get("baseToken") or {}
    name    = base.get("name", "Unknown")
    symbol  = base.get("symbol", "?")
    address = base.get("address", "")
    chain   = pair.get("chainId", "solana").upper()
    price   = pair.get("priceUsd") or "?"
    mc      = pair.get("marketCap") or pair.get("fdv") or 0
    liq     = (pair.get("liquidity") or {}).get("usd", 0) or 0
    vol_h1  = (pair.get("volume") or {}).get("h1", 0) or 0
    vol_h24 = (pair.get("volume") or {}).get("h24", 0) or 0
    ch_h1   = (pair.get("priceChange") or {}).get("h1", 0) or 0
    ch_h6   = (pair.get("priceChange") or {}).get("h6", 0) or 0
    ch_h24  = (pair.get("priceChange") or {}).get("h24", 0) or 0
    txns    = ((pair.get("txns") or {}).get("h1") or {})
    buys    = txns.get("buys", 0)
    sells   = txns.get("sells", 0)
    url     = pair.get("url", f"https://dexscreener.com/solana/{address}")
    created = pair.get("pairCreatedAt") or 0
    age_min = int(((time.time() * 1000) - created) / 60_000) if created else 0
    age_str = f"{age_min}m" if age_min < 120 else f"{age_min//60}h {age_min%60}m"
    bar     = "🟢" * (score // 20) + "⚪" * (5 - score // 20)
    flags_text = "\n".join(flags) if flags else "None"
    mc_line = f"📊 MC: ${mc:,.0f}\n" if mc > 0 else ""
    alerts_left = settings["max_alerts_hr"] - len(alert_times)

    if is_wc:
        header = "⚽ <b>WC MEMECOIN ALERT</b> ⚽"
    elif is_gem:
        header = "💎 <b>EARLY GEM ALERT</b> 💎"
    else:
        header = "🚀 <b>TOKEN ALERT</b> 🚀"

    activity_line = ""
    if activity:
        wallets = len(activity.get("wallets", set()))
        bc      = activity.get("bonding_pct", 0)
        vol_sol = activity.get("volume_sol", 0)
        activity_line = f"🔥 Early: {wallets} wallets | {bc:.0f}% bonding | {vol_sol:.1f} SOL vol\n"

    return f"""{header}
━━━━━━━━━━━━━━━━━━━━
🪙 <b>{name} (${symbol})</b>
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
⏰ {datetime.now(timezone.utc).strftime("%H:%M:%S UTC")} | {alerts_left} alerts left this hr"""

# ── Help Text ─────────────────────────────────────────────────────────────────
HELP_TEXT = """🤖 <b>Alpha Bot Commands</b>
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
/mcap [min] [max] — MC range
/minscore [0-100] — min rug score

<b>📊 Filters</b>
/maxalerts [n] — max alerts per hour
/minbuys [n] — min buys in first 5 mins
/minbonding [%] — min bonding curve %
/minliq [amount] — min liquidity
/threshold [%] — pump/dump alert %
/safeonly on|off
/charts on|off

<b>🔍 Lookup</b>
/check [CA] — full token check
/top — top WC tokens
/trending — hottest pumps
/findbetter — find gems now

<b>📌 Watchlist</b>
/watch [CA]
/unwatch [CA]
/watchlist

<b>🔄 Reset</b>
/reset — reset all settings
━━━━━━━━━━━━━━━━━━━━"""

# ── Commands ──────────────────────────────────────────────────────────────────
def handle_command(chat_id, text):
    text  = text.strip()
    parts = text.split()
    cmd   = parts[0].lower().split("@")[0]

    if cmd in ["/start", "/help"]:
        send_tg(chat_id, HELP_TEXT)

    elif cmd in ["/uptime", "/runtime"]:
        uptime_secs = int(time.time() - start_time)
        days  = uptime_secs // 86400
        hours = (uptime_secs % 86400) // 3600
        mins  = (uptime_secs % 3600) // 60
        secs  = uptime_secs % 60
        uptime_str = f"{days}d {hours}h {mins}m {secs}s" if days > 0 else f"{hours}h {mins}m {secs}s"
        started = datetime.fromtimestamp(start_time, tz=timezone.utc).strftime("%b %d at %H:%M UTC")
        now = time.time()
        while alert_times and now - alert_times[0] > 3600:
            alert_times.popleft()
        send_tg(chat_id, f"""⏱ <b>Bot Runtime</b>
━━━━━━━━━━━━━━━━━━━━
🟢 Uptime: {uptime_str}
📅 Started: {started}
📢 Total alerts: {total_alerts}
💎 Gems found: {total_gems}
📊 Alerts this hour: {len(alert_times)}/{settings['max_alerts_hr']}
🔌 Watching: {len(token_activity)} tokens
━━━━━━━━━━━━━━━━━━━━""")

    elif cmd == "/status":
        chains_str = ", ".join(settings["chains"]).upper()
        send_tg(chat_id, f"""⚙️ <b>Bot Status</b>
━━━━━━━━━━━━━━━━━━━━
{'⏸ PAUSED' if settings['paused'] else '▶️ RUNNING'}
🔗 Chains: {chains_str}
⚽ WC Mode: {'On' if settings['wc_mode'] else 'Off'}
💎 Gem Mode: {'On' if settings['gem_mode'] else 'Off'}
💎 MC Range: ${settings['gem_mc_min']:,} - ${settings['gem_mc_max']:,}
🛡 Min Rug Score: {settings['min_rug_score']}/100
📊 Alert threshold: {settings['threshold']}%
📢 Max alerts/hr: {settings['max_alerts_hr']}
💧 Min liquidity: ${settings['min_liq']:,}
🔥 Min buys (5min): {settings['min_buys_5min']}
📈 Min bonding: {settings['min_bonding_pct']}%
💰 Min volume: ${settings['min_vol_usd']:,}
🛡 Safe only: {'On' if settings['safe_only'] else 'Off'}
📸 Charts: {'On' if settings['charts'] else 'Off'}
📌 Watchlist: {len(watchlist)} tokens
━━━━━━━━━━━━━━━━━━━━""")

    elif cmd == "/pause":
        settings["paused"] = True
        send_tg(chat_id, "⏸ Paused.")

    elif cmd == "/resume":
        settings["paused"] = False
        send_tg(chat_id, "▶️ Resumed!")

    elif cmd == "/maxalerts":
        if len(parts) < 2 or not parts[1].isdigit():
            send_tg(chat_id, "Usage: /maxalerts [number]"); return
        settings["max_alerts_hr"] = int(parts[1])
        send_tg(chat_id, f"✅ Max alerts/hr: {settings['max_alerts_hr']}.")

    elif cmd == "/minbuys":
        if len(parts) < 2 or not parts[1].isdigit():
            send_tg(chat_id, "Usage: /minbuys [number]"); return
        settings["min_buys_5min"] = int(parts[1])
        send_tg(chat_id, f"✅ Min buys (5min): {settings['min_buys_5min']}.")

    elif cmd == "/minbonding":
        if len(parts) < 2:
            send_tg(chat_id, "Usage: /minbonding [%]"); return
        settings["min_bonding_pct"] = float(parts[1])
        send_tg(chat_id, f"✅ Min bonding: {settings['min_bonding_pct']}%.")

    elif cmd == "/minliq":
        if len(parts) < 2 or not parts[1].isdigit():
            send_tg(chat_id, "Usage: /minliq [amount]"); return
        settings["min_liq"] = int(parts[1])
        send_tg(chat_id, f"✅ Min liq: ${settings['min_liq']:,}.")

    elif cmd == "/threshold":
        if len(parts) < 2:
            send_tg(chat_id, "Usage: /threshold [%]"); return
        settings["threshold"] = float(parts[1])
        send_tg(chat_id, f"✅ Threshold: {settings['threshold']}%.")

    elif cmd == "/minscore":
        if len(parts) < 2 or not parts[1].isdigit():
            send_tg(chat_id, "Usage: /minscore [0-100]"); return
        settings["min_rug_score"] = int(parts[1])
        send_tg(chat_id, f"✅ Min rug score: {settings['min_rug_score']}/100.")

    elif cmd == "/safeonly":
        if len(parts) < 2 or parts[1].lower() not in ["on","off"]:
            send_tg(chat_id, "Usage: /safeonly on|off"); return
        settings["safe_only"] = parts[1].lower() == "on"
        send_tg(chat_id, f"✅ Safe only: {'On' if settings['safe_only'] else 'Off'}.")

    elif cmd == "/charts":
        if len(parts) < 2 or parts[1].lower() not in ["on","off"]:
            send_tg(chat_id, "Usage: /charts on|off"); return
        settings["charts"] = parts[1].lower() == "on"
        send_tg(chat_id, f"✅ Charts: {'On' if settings['charts'] else 'Off'}.")

    elif cmd == "/chain":
        if len(parts) < 2:
            send_tg(chat_id, "Usage: /chain [solana|bsc|base|eth|all]"); return
        val = parts[1].lower()
        chain_map = {"solana":["solana"],"bsc":["bsc"],"base":["base"],"eth":["ethereum"],"all":ALL_CHAINS[:]}
        if val not in chain_map:
            send_tg(chat_id, "Options: solana, bsc, base, eth, all"); return
        settings["chains"] = chain_map[val]
        send_tg(chat_id, f"✅ Scanning: {', '.join(settings['chains']).upper()}")

    elif cmd == "/wc":
        if len(parts) < 2 or parts[1].lower() not in ["on","off"]:
            send_tg(chat_id, "Usage: /wc on|off"); return
        settings["wc_mode"] = parts[1].lower() == "on"
        send_tg(chat_id, f"✅ WC Scanner: {'On' if settings['wc_mode'] else 'Off'}.")

    elif cmd == "/gem":
        if len(parts) < 2 or parts[1].lower() not in ["on","off"]:
            send_tg(chat_id, "Usage: /gem on|off"); return
        settings["gem_mode"] = parts[1].lower() == "on"
        send_tg(chat_id, f"✅ Gem Hunter: {'On' if settings['gem_mode'] else 'Off'}.")

    elif cmd == "/mcap":
        if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
            send_tg(chat_id, "Usage: /mcap [min] [max]"); return
        settings["gem_mc_min"] = int(parts[1])
        settings["gem_mc_max"] = int(parts[2])
        send_tg(chat_id, f"✅ MC range: ${settings['gem_mc_min']:,} - ${settings['gem_mc_max']:,}.")

    elif cmd == "/reset":
        settings.update({
            "max_alerts_hr":13,"min_rug_score":45,"min_buys_5min":10,
            "min_bonding_pct":5.0,"min_vol_usd":2000,"buy_ratio_min":0.60,
            "min_liq":3000,"chains":ALL_CHAINS[:],"wc_mode":True,"gem_mode":True,
            "gem_mc_min":2000,"gem_mc_max":30000,"paused":False,"charts":True,
            "safe_only":False,"threshold":20,"min_rug_score_wc":30,
        })
        send_tg(chat_id, "✅ All settings reset to default!")

    elif cmd == "/check":
        if len(parts) < 2:
            send_tg(chat_id, "Usage: /check [CA]"); return
        send_tg(chat_id, "🔍 Checking token...")
        pair = dex_get(parts[1])
        if not pair:
            send_tg(chat_id, "❌ Token not found."); return
        base = pair.get("baseToken") or {}
        wc   = is_wc_token(base.get("name",""), base.get("symbol",""))
        mc   = pair.get("marketCap") or pair.get("fdv") or 0
        gem  = settings["gem_mc_min"] <= mc <= settings["gem_mc_max"]
        act  = token_activity.get(parts[1])
        verdict, flags, score = rug_score(pair, gem_mode=gem, activity=act)
        card = build_card_data(pair, "📋 Manual Check", verdict, score, wc, gem)
        send_tg(chat_id, format_alert(pair, "📋 Manual Check", verdict, flags, score, is_wc=wc, is_gem=gem, activity=act), card_data=card)

    elif cmd == "/watch":
        if len(parts) < 2:
            send_tg(chat_id, "Usage: /watch [CA]"); return
        watchlist.add(parts[1].lower())
        send_tg(chat_id, f"📌 Added! Watchlist: {len(watchlist)} tokens.")

    elif cmd == "/unwatch":
        if len(parts) < 2:
            send_tg(chat_id, "Usage: /unwatch [CA]"); return
        watchlist.discard(parts[1].lower())
        send_tg(chat_id, "✅ Removed.")

    elif cmd == "/watchlist":
        if not watchlist:
            send_tg(chat_id, "📌 Empty. Use /watch [CA]."); return
        items = "\n".join([f"• <code>{ca}</code>" for ca in watchlist])
        send_tg(chat_id, f"📌 <b>Watchlist ({len(watchlist)})</b>\n{items}")

    elif cmd == "/top":
        send_tg(chat_id, "🔍 Fetching top WC tokens...")
        results = []
        for kw in ["worldcup","wc2026","fifa","mbappe","messi"]:
            for pair in dex_search(kw):
                if pair.get("chainId") in settings["chains"]:
                    vol = (pair.get("volume") or {}).get("h24",0) or 0
                    liq = (pair.get("liquidity") or {}).get("usd",0) or 0
                    if liq > 1000:
                        results.append((vol, pair))
        results.sort(key=lambda x: x[0], reverse=True)
        if not results:
            send_tg(chat_id, "No WC tokens found."); return
        msg = "🏆 <b>Top 5 WC Tokens (24h Vol)</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        for i, (vol, pair) in enumerate(results[:5], 1):
            base = pair.get("baseToken") or {}
            name = base.get("name","?")
            sym  = base.get("symbol","?")
            ch24 = (pair.get("priceChange") or {}).get("h24",0) or 0
            liq  = (pair.get("liquidity") or {}).get("usd",0) or 0
            url  = pair.get("url","")
            msg += f"{i}. <b>{name} (${sym})</b>\n   Vol: ${vol:,.0f} | Liq: ${liq:,.0f} | 24h: {ch24:+.1f}%\n   <a href=\"{url}\">Chart</a>\n\n"
        send_tg(chat_id, msg)

    elif cmd == "/trending":
        send_tg(chat_id, "🔍 Finding hottest pumps...")
        results = []
        for kw in list(WC_KEYWORDS)[:15]:
            for pair in dex_search(kw):
                if pair.get("chainId") in settings["chains"]:
                    ch1 = (pair.get("priceChange") or {}).get("h1",0) or 0
                    liq = (pair.get("liquidity") or {}).get("usd",0) or 0
                    if liq > 1000 and ch1 > 0:
                        results.append((ch1, pair))
        results.sort(key=lambda x: x[0], reverse=True)
        if not results:
            send_tg(chat_id, "Nothing pumping right now."); return
        msg = "🚀 <b>Trending (1h)</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        for i, (ch1, pair) in enumerate(results[:5], 1):
            base = pair.get("baseToken") or {}
            name = base.get("name","?")
            sym  = base.get("symbol","?")
            liq  = (pair.get("liquidity") or {}).get("usd",0) or 0
            url  = pair.get("url","")
            wc   = "⚽" if is_wc_token(name, sym) else ""
            msg += f"{i}. {wc}<b>{name} (${sym})</b> +{ch1:.1f}%\n   Liq: ${liq:,.0f} | <a href=\"{url}\">Chart</a>\n\n"
        send_tg(chat_id, msg)

    elif cmd == "/findbetter":
        send_tg(chat_id, "💎 Hunting gems from Pump.fun activity...")
        found = 0
        now   = time.time()
        for mint, act in list(token_activity.items()):
            if found >= 5: break
            age = now - act.get("first_seen", now)
            if age > 1800: continue
            buys = act.get("buys",0)
            bc   = act.get("bonding_pct",0)
            if buys < 5 or bc < 3: continue
            pair = dex_get(mint)
            if not pair: continue
            mc  = pair.get("marketCap") or pair.get("fdv") or 0
            liq = (pair.get("liquidity") or {}).get("usd",0) or 0
            if liq < 500: continue
            wc      = is_wc_token(act.get("name",""), act.get("symbol",""))
            gem     = settings["gem_mc_min"] <= mc <= settings["gem_mc_max"]
            verdict, flags, score = rug_score(pair, gem_mode=gem, activity=act)
            if score >= 35 and "RUG" not in verdict:
                card = build_card_data(pair, "💎 Manual Hunt", verdict, score, wc, gem)
                send_tg(chat_id, format_alert(pair, "💎 Manual Hunt", verdict, flags, score, is_wc=wc, is_gem=gem, activity=act), card_data=card)
                found += 1
                time.sleep(3)
        if found == 0:
            send_tg(chat_id, "No fresh gems found. Try again in a few minutes!")

# ── Poll Telegram Commands ────────────────────────────────────────────────────
def poll_commands():
    global last_update_id
    if not TELEGRAM_TOKEN: return
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 5},
            timeout=10,
        )
        updates = r.json().get("result", [])
        for update in updates:
            last_update_id = update["update_id"]
            msg     = update.get("message") or update.get("channel_post") or {}
            text    = msg.get("text","")
            chat_id = (msg.get("chat") or {}).get("id")
            if text.startswith("/") and chat_id:
                handle_command(str(chat_id), text)
    except Exception as e:
        log.error(f"Poll error: {e}")

# ── Process Token ─────────────────────────────────────────────────────────────
def process_token(mint, activity):
    global total_alerts, total_gems
    if not can_alert() or settings["paused"]: return

    pair = dex_get(mint)
    if not pair: return

    liq = (pair.get("liquidity") or {}).get("usd",0) or 0
    if liq < settings["min_liq"]: return

    base    = pair.get("baseToken") or {}
    name    = base.get("name", activity.get("name",""))
    symbol  = base.get("symbol", activity.get("symbol",""))
    wc      = is_wc_token(name, symbol)
    mc      = pair.get("marketCap") or pair.get("fdv") or 0
    gem     = settings["gem_mc_min"] <= mc <= settings["gem_mc_max"]

    verdict, flags, score = rug_score(pair, gem_mode=gem, activity=activity)

    min_score = settings["min_rug_score_wc"] if wc else settings["min_rug_score"]
    if score < min_score: return
    if settings["safe_only"] and "RUG" in verdict: return

    if wc:
        trigger = "⚽ NEW WC TOKEN — Pump.fun Launch"
    elif gem:
        trigger = f"💎 EARLY GEM — MC ${mc:,.0f}"
    else:
        trigger = "🆕 NEW TOKEN — Passed all filters"

    card = build_card_data(pair, trigger, verdict, score, wc, gem)
    broadcast(
        format_alert(pair, trigger, verdict, flags, score, is_wc=wc, is_gem=gem, activity=activity),
        card_data=card,
    )
    record_alert()
    total_alerts += 1
    if gem: total_gems += 1

    seen[mint] = {"alerted_at": time.time()}
    log.info(f"Alerted: {name} ({symbol}) score={score} wc={wc} gem={gem}")

# ── Pump.fun WebSocket ────────────────────────────────────────────────────────
async def pumpfun_ws():
    uri = "wss://pumpdev.io/ws"
    log.info("Connecting to Pump.fun WebSocket…")

    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                log.info("Connected to Pump.fun WebSocket!")
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                log.info("Subscribed to new token launches")

                async for raw in ws:
                    try:
                        event   = json.loads(raw)
                        tx_type = event.get("txType","")
                        mint    = event.get("mint","")
                        if not mint: continue

                        if tx_type == "create":
                            name   = event.get("name","")
                            symbol = event.get("symbol","")
                            if is_stable(symbol): continue
                            token_activity[mint]["name"]        = name
                            token_activity[mint]["symbol"]      = symbol
                            token_activity[mint]["mint"]        = mint
                            token_activity[mint]["first_seen"]  = time.time()
                            token_activity[mint]["bonding_pct"] = float(event.get("vSolInBondingCurve",0) or 0) / 85 * 100
                            await ws.send(json.dumps({"method":"subscribeTokenTrade","keys":[mint]}))
                            log.info(f"New token: {name} ({symbol}) — {mint[:8]}...")

                        elif tx_type in ["buy","sell"]:
                            act     = token_activity[mint]
                            trader  = event.get("traderPublicKey","")
                            creator = event.get("creatorPublicKey","")
                            sol_amt = float(event.get("solAmount",0) or 0) / 1e9

                            if tx_type == "buy":
                                act["buys"]       += 1
                                act["volume_sol"] += sol_amt
                                if trader: act["wallets"].add(trader)
                            else:
                                act["sells"] += 1
                                if trader == creator: act["dev_sold"] = True

                            bc_sol = float(event.get("vSolInBondingCurve",0) or 0)
                            if bc_sol > 0: act["bonding_pct"] = bc_sol / 85 * 100

                            age_min     = (time.time() - act["first_seen"]) / 60
                            buys        = act["buys"]
                            unique_w    = len(act["wallets"])
                            vol_sol     = act["volume_sol"]
                            bc_pct      = act["bonding_pct"]
                            total_txns  = buys + act["sells"]
                            buy_ratio   = buys / total_txns if total_txns > 0 else 0
                            vol_usd_est = vol_sol * 150
                            wc_token    = is_wc_token(act["name"], act["symbol"])

                            if wc_token and settings["wc_mode"]:
                                passes = (buys >= 5 and not act["dev_sold"] and buy_ratio >= 0.5 and mint not in seen)
                            else:
                                passes = (
                                    unique_w    >= settings["min_buys_5min"] and
                                    bc_pct      >= settings["min_bonding_pct"] and
                                    not act["dev_sold"] and
                                    buy_ratio   >= settings["buy_ratio_min"] and
                                    vol_usd_est >= settings["min_vol_usd"] and
                                    age_min     <= 10 and
                                    mint not in seen
                                )

                            if passes and can_alert() and not settings["paused"]:
                                loop = asyncio.get_event_loop()
                                loop.run_in_executor(None, process_token, mint, dict(act))

                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        log.error(f"Event error: {e}")

        except Exception as e:
            log.error(f"WebSocket error: {e} — reconnecting in 5s...")
            await asyncio.sleep(5)

# ── Command Loop ──────────────────────────────────────────────────────────────
async def command_loop():
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, poll_commands)
        except Exception as e:
            log.error(f"Command loop error: {e}")
        await asyncio.sleep(3)

# ── Cleanup Loop ──────────────────────────────────────────────────────────────
async def cleanup_loop():
    while True:
        await asyncio.sleep(300)
        now      = time.time()
        to_del   = [m for m, a in token_activity.items() if now - a.get("first_seen",now) > 3600]
        seen_del = [m for m, v in seen.items() if now - v.get("alerted_at",now) > 86400]
        for m in to_del:   del token_activity[m]
        for m in seen_del: del seen[m]
        if to_del or seen_del:
            log.info(f"Cleaned {len(to_del)} tokens, {len(seen_del)} seen entries")

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    log.info("Alpha Bot v6 + Cards starting…")
    broadcast(
        "🤖 <b>Alpha Bot v6 is LIVE!</b>\n"
        "🔌 Connected to Pump.fun WebSocket\n"
        "⚡ Real-time token detection from creation\n"
        "⚽ WC tokens + 💎 Gems + 🛡 Rug Score\n"
        "🖼 Image cards enabled!\n"
        "Max 13 quality alerts per hour\n\n"
        "Send /help to see all commands 👇"
    )
    await asyncio.gather(
        pumpfun_ws(),
        command_loop(),
        cleanup_loop(),
    )

if __name__ == "__main__":
    asyncio.run(main())
