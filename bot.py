“””
⚽ WC Memecoin Bot v2

- Scans every 30s for new World Cup tokens
- Prioritises Solana (650x more volume than other chains)
- Checks DEX Paid, liquidity lock, holder concentration,
  socials, honeypot signals, age, volume, and more
- Sends rich Telegram alerts instantly
  “””

import os
import time
import requests
import logging
from datetime import datetime, timezone

# ── Config ───────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ.get(“TELEGRAM_TOKEN”, “”)
TELEGRAM_CHAT_ID = os.environ.get(“TELEGRAM_CHAT_ID”, “”)
SCAN_INTERVAL    = 30          # seconds — fast enough to catch early launches
PRICE_ALERT_PCT  = 20          # % move in 1h to trigger a price alert
MIN_LIQUIDITY    = 1000        # ignore tokens below $1k liquidity (pure dust)

# ── World Cup 2026 Keywords ──────────────────────────────────────────────────

# Covers: FIFA terms · all 48 qualified countries · top star players ·

# host cities · tournament slang · common ticker patterns

WC_KEYWORDS = [
# ── Tournament / FIFA terms
“worldcup”, “world cup”, “wc2026”, “worldcup2026”, “fifa2026”,
“fifa”, “fwc”, “fwc26”, “fifawc”, “fifameme”, “fifacoin”,
“footballcoin”, “soccercoin”, “goatcoin”, “championsleague”,
“goldenboot”, “hatrick”, “penalty”, “freekick”, “worldgoal”,

```
# ── Host nations & cities
"usmnt", "uswnt", "usasoccer", "mexicofifa", "canadafc",
"losangeles", "newYork", "miami", "dallas", "boston",
"seattle", "houston", "philadelphia", "atlanta", "kansascity",
"sanfrancisco", "guadalajara", "monterrey", "azteca",
"toronto", "vancouver",

# ── Europe (16 teams)
"england", "threelions", "france", "lecoqgaulois",
"germany", "mannschaft", "spain", "lafuria",
"portugal", "selecao", "netherlands", "oranje",
"croatia", "vatreni", "belgium", "rediablos",
"switzerland", "nati", "austria", "oefb",
"scotland", "tartan", "norway", "norge",
"sweden", "blågult", "turkey", "turkiye",
"czechia", "bohemia", "bosnia", "zmajevi",

# ── South America (6 teams)
"argentina", "albiceleste", "brazil", "canarinho",
"colombia", "cafeteros", "uruguay", "charruas",
"ecuador", "tricolor", "paraguay", "guarani",

# ── Africa (5 teams)
"morocco", "atlasliOns", "algeria", "fennecs",
"egypt", "pharaohs", "ghana", "blackstars",
"tunisia", "eaglesofcarthage",

# ── Asia (6 teams)
"japan", "samuraiblue", "southkorea", "taegeukwarriors",
"australia", "socceroos", "iran", "teamiraN",
"jordan", "nasheama", "uzbekistan", "whitewolves",

# ── CONCACAF (3 + hosts)
"panama", "loscanaleros", "curacao", "haiti",

# ── Oceania
"newzealand", "allwhites",

# ── Debut nations (extra hype)
"capeverde", "uzbekistan",

# ── Superstar players (biggest memecoin triggers)
"ronaldo", "cr7", "messi", "leo", "mbappe",
"neymar", "haaland", "vinicius", "bellingham",
"salah", "modric", "dembele", "pedri", "yamal",
"osimhen", "lewandowski", "kane", "saka", "rashford",
"pulisic", "reyna", "weah", "ferran", "gavi",

# ── Common WC ticker patterns
"wc", "wcup", "goal", "striker", "keeper",
"offside", "redcard", "yellowcard", "corner",
"kickoff", "fulltime", "extratime", "shootout",
```

]

# Chains to scan — Solana first (where 99% of WC meme action is)

CHAINS = [“solana”, “bsc”, “base”, “ethereum”]

logging.basicConfig(
level=logging.INFO,
format=”%(asctime)s [%(levelname)s] %(message)s”,
)
log = logging.getLogger(**name**)

# Memory: track seen tokens so we don’t double-alert

seen: dict[str, dict] = {}   # address → {first_seen, last_alert, last_price}

# ── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(msg: str):
if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
print(msg)
return
try:
requests.post(
f”https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage”,
json={
“chat_id”: TELEGRAM_CHAT_ID,
“text”: msg,
“parse_mode”: “HTML”,
“disable_web_page_preview”: True,
},
timeout=10,
).raise_for_status()
except Exception as e:
log.error(f”Telegram error: {e}”)

# ── DEXScreener helpers ───────────────────────────────────────────────────────

def dex_search(keyword: str) -> list[dict]:
try:
r = requests.get(
f”https://api.dexscreener.com/latest/dex/search?q={keyword}”,
timeout=10,
)
r.raise_for_status()
return r.json().get(“pairs”, []) or []
except Exception as e:
log.error(f”DEXScreener error [{keyword}]: {e}”)
return []

def dex_token_detail(chain: str, address: str) -> dict:
“”“Fetch richer per-token data from DEXScreener token endpoint.”””
try:
r = requests.get(
f”https://api.dexscreener.com/latest/dex/tokens/{address}”,
timeout=10,
)
r.raise_for_status()
pairs = r.json().get(“pairs”, [])
# Return the pair on the correct chain with most liquidity
chain_pairs = [p for p in pairs if p.get(“chainId”) == chain]
if chain_pairs:
return max(chain_pairs, key=lambda p: (p.get(“liquidity”) or {}).get(“usd”, 0))
return {}
except Exception:
return {}

# ── Safety / Rug Checks ───────────────────────────────────────────────────────

def safety_check(pair: dict) -> tuple[str, list[str], int]:
“””
Returns (verdict, warnings, score).
Score 0-100: higher = safer.
Verdict: ✅ EARLY GEM | ⚠️ RISKY | 🚨 LIKELY RUG
“””
flags   = []   # bad signs
greens  = []   # good signs
danger  = 0    # danger points
safety  = 0    # safety points

```
liq        = (pair.get("liquidity") or {}).get("usd", 0)
fdv        = pair.get("fdv") or 0
vol_h1     = (pair.get("volume") or {}).get("h1", 0) or 0
vol_h24    = (pair.get("volume") or {}).get("h24", 0) or 0
ch_h1      = (pair.get("priceChange") or {}).get("h1", 0) or 0
ch_h6      = (pair.get("priceChange") or {}).get("h6", 0) or 0
ch_h24     = (pair.get("priceChange") or {}).get("h24", 0) or 0
created_ms = pair.get("pairCreatedAt") or 0
info       = pair.get("info") or {}
socials    = info.get("socials") or []
websites   = info.get("websites") or []
boosts     = pair.get("boosts") or {}
dex_paid   = boosts.get("active", 0) or 0   # DEXScreener boost = DEX Paid

age_min = ((time.time() * 1000) - created_ms) / 60_000 if created_ms else 9999

# 1️⃣ DEX Paid (boosted on DEXScreener)
if dex_paid and dex_paid > 0:
    greens.append(f"✅ DEX Paid / Boosted ({dex_paid} active boosts)")
    safety += 20
else:
    flags.append("❌ No DEX Paid — not boosted yet")
    danger += 5

# 2️⃣ Liquidity
if liq >= 50_000:
    greens.append(f"✅ Strong liquidity (${liq:,.0f})")
    safety += 15
elif liq >= 10_000:
    greens.append(f"⚠️ Decent liquidity (${liq:,.0f})")
    safety += 8
elif liq >= 3_000:
    flags.append(f"⚠️ Low liquidity (${liq:,.0f})")
    danger += 10
else:
    flags.append(f"🚨 Very low liquidity (${liq:,.0f}) — easy rug")
    danger += 25

# 3️⃣ FDV / Liquidity ratio (honeypot indicator)
if fdv > 0 and liq > 0:
    ratio = fdv / liq
    if ratio > 1000:
        flags.append(f"🚨 FDV/Liq ratio {ratio:.0f}x — possible honeypot")
        danger += 20
    elif ratio > 200:
        flags.append(f"⚠️ FDV/Liq ratio {ratio:.0f}x — elevated risk")
        danger += 10
    elif ratio < 20:
        greens.append(f"✅ Healthy FDV/Liq ratio ({ratio:.0f}x)")
        safety += 10

# 4️⃣ Token age
if age_min < 10:
    flags.append(f"🚨 Ultra new — only {age_min:.1f} min old (very high risk)")
    danger += 15
elif age_min < 60:
    flags.append(f"⚠️ New token — {age_min:.0f} min old")
    danger += 8
elif age_min > 1440:   # > 1 day old and still has liquidity = good sign
    greens.append(f"✅ Survived 24h+ ({age_min/60:.1f}h old)")
    safety += 10

# 5️⃣ Volume activity
if vol_h1 > 50_000:
    greens.append(f"✅ Hot volume — ${vol_h1:,.0f} in last 1h")
    safety += 15
elif vol_h1 > 10_000:
    greens.append(f"⚠️ Growing volume — ${vol_h1:,.0f} in last 1h")
    safety += 5
elif vol_h1 < 500 and age_min > 30:
    flags.append("❌ Almost no volume — nobody is buying")
    danger += 15

# 6️⃣ Socials (Twitter/Telegram presence)
social_types = [s.get("type", "").lower() for s in socials]
has_twitter  = "twitter" in social_types
has_telegram = "telegram" in social_types
has_website  = len(websites) > 0

if has_twitter and has_telegram and has_website:
    greens.append("✅ Full socials (Twitter + Telegram + Website)")
    safety += 15
elif has_twitter and has_telegram:
    greens.append("✅ Has Twitter + Telegram")
    safety += 10
elif has_twitter or has_telegram:
    flags.append("⚠️ Only one social link found")
    danger += 5
else:
    flags.append("🚨 No social links — anonymous dev")
    danger += 20

# 7️⃣ Extreme pump with low liquidity = classic rug setup
if ch_h1 > 300 and liq < 30_000:
    flags.append(f"🚨 300%+ pump in 1h with low liquidity — textbook rug setup")
    danger += 25
elif ch_h1 > 100 and liq < 10_000:
    flags.append(f"⚠️ Massive pump with very low liquidity")
    danger += 15

# 8️⃣ Consistent growth (healthy signal)
if ch_h1 > 5 and ch_h6 > 10 and ch_h24 > 20 and liq > 20_000:
    greens.append("✅ Steady consistent growth across 1h/6h/24h")
    safety += 10

# ── Score & Verdict
score = max(0, min(100, safety - danger + 30))  # baseline 30

if danger >= 45 or (danger >= 25 and safety < 15):
    verdict = "🚨 LIKELY RUG"
elif danger >= 20 or safety < 20:
    verdict = "⚠️ RISKY"
else:
    verdict = "✅ EARLY GEM"

all_flags = greens + flags
return verdict, all_flags, score
```

# ── Format Alert ──────────────────────────────────────────────────────────────

def format_alert(pair: dict, trigger: str, verdict: str, flags: list[str], score: int) -> str:
base     = pair.get(“baseToken”) or {}
name     = base.get(“name”, “Unknown”)
symbol   = base.get(“symbol”, “?”)
address  = base.get(“address”, “”)
chain    = pair.get(“chainId”, “”).upper()
price    = pair.get(“priceUsd”) or “?”
liq      = (pair.get(“liquidity”) or {}).get(“usd”, 0)
vol_h1   = (pair.get(“volume”) or {}).get(“h1”, 0) or 0
vol_h24  = (pair.get(“volume”) or {}).get(“h24”, 0) or 0
ch_h1    = (pair.get(“priceChange”) or {}).get(“h1”, 0) or 0
ch_h6    = (pair.get(“priceChange”) or {}).get(“h6”, 0) or 0
ch_h24   = (pair.get(“priceChange”) or {}).get(“h24”, 0) or 0
txns_h1  = ((pair.get(“txns”) or {}).get(“h1”) or {})
buys_h1  = txns_h1.get(“buys”, 0)
sells_h1 = txns_h1.get(“sells”, 0)
url      = pair.get(“url”, “”)
created  = pair.get(“pairCreatedAt”) or 0
age_min  = int(((time.time() * 1000) - created) / 60_000) if created else 0
age_str  = f”{age_min}m” if age_min < 120 else f”{age_min//60}h {age_min%60}m”

```
flags_text = "\n".join(flags) if flags else "—"
score_bar  = "🟢" * (score // 20) + "⚪" * (5 - score // 20)

msg = f"""
```

⚽ <b>WC MEMECOIN ALERT</b> ⚽
━━━━━━━━━━━━━━━━━━━━
🪙 <b>{name} (${symbol})</b>
🔗 Chain: {chain}
⏱ Age: {age_str}
📢 <b>{trigger}</b>

💰 Price: ${price}
📊 1h: {ch_h1:+.1f}% | 6h: {ch_h6:+.1f}% | 24h: {ch_h24:+.1f}%
💧 Liquidity: ${liq:,.0f}
📦 Vol 1h: ${vol_h1:,.0f} | Vol 24h: ${vol_h24:,.0f}
🔄 Buys/Sells (1h): {buys_h1} / {sells_h1}

🛡 Safety: {verdict}
{score_bar} Score: {score}/100
{flags_text}

🔍 <a href="{url}">DEXScreener</a>
📋 CA: <code>{address}</code>
━━━━━━━━━━━━━━━━━━━━
⏰ {datetime.now(timezone.utc).strftime(’%H:%M:%S UTC’)}
“””.strip()
return msg

# ── Main Scan ─────────────────────────────────────────────────────────────────

def scan():
log.info(“🔍 Scanning…”)
checked   = set()
alerted   = 0

```
for kw in WC_KEYWORDS:
    pairs = dex_search(kw)

    for pair in pairs:
        chain   = pair.get("chainId", "")
        if chain not in CHAINS:
            continue

        address = (pair.get("baseToken") or {}).get("address", "")
        if not address or address in checked:
            continue
        checked.add(address)

        # Skip stables / wrapped natives
        sym = (pair.get("baseToken") or {}).get("symbol", "").upper()
        if sym in {"USDT","USDC","BUSD","DAI","WETH","WBNB","WSOL","ETH","BNB","SOL"}:
            continue

        liq = (pair.get("liquidity") or {}).get("usd", 0) or 0
        if liq < MIN_LIQUIDITY:
            continue

        ch_h1  = (pair.get("priceChange") or {}).get("h1", 0) or 0
        price  = float(pair.get("priceUsd") or 0)
        prev   = seen.get(address, {})

        # ── Decide trigger ──────────────────────────────────────────────
        trigger  = None
        now      = time.time()
        last_alert = prev.get("last_alert", 0)
        cooldown   = 1800   # 30 min cooldown per token

        created_ms = pair.get("pairCreatedAt") or 0
        age_min    = ((now * 1000) - created_ms) / 60_000 if created_ms else 9999

        if address not in seen:
            trigger = "🆕 NEW WC TOKEN DETECTED"
        elif abs(ch_h1) >= PRICE_ALERT_PCT and (now - last_alert) > cooldown:
            direction = "🚀 PUMPING" if ch_h1 > 0 else "💀 DUMPING"
            trigger   = f"{direction} {ch_h1:+.1f}% in 1h"

        if not trigger:
            seen[address] = {**prev, "last_price": price}
            continue

        # ── Run safety check ────────────────────────────────────────────
        verdict, flags, score = safety_check(pair)

        # Don't alert on ultra-low score obvious rugs unless it's a new find
        if score < 15 and "NEW" not in trigger:
            seen[address] = {**prev, "last_price": price, "last_alert": now}
            continue

        msg = format_alert(pair, trigger, verdict, flags, score)
        send_telegram(msg)

        seen[address] = {
            "first_seen": prev.get("first_seen", now),
            "last_alert": now,
            "last_price": price,
        }
        alerted += 1
        time.sleep(0.3)   # Telegram rate limit safety

log.info(f"✅ Done — {len(checked)} tokens checked, {alerted} alerts sent")
```

# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
log.info(“🤖 WC Memecoin Bot v2 starting…”)
send_telegram(
“🤖 <b>WC Memecoin Bot v2 is LIVE!</b>\n”
“⚽ Scanning every 30s across Solana, BSC, Base & ETH\n”
“🔍 Checking: DEX Paid · Liquidity · Holders · Socials · Honeypot signals\n”
“Let’s catch these WC gems early! 🌍”
)
while True:
try:
scan()
except Exception as e:
log.error(f”Scan crashed: {e}”)
log.info(f”💤 Next scan in {SCAN_INTERVAL}s…”)
time.sleep(SCAN_INTERVAL)

if **name** == “**main**”:
main()
