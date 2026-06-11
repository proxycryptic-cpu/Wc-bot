import os
import time
import requests
import logging
import threading
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Default Settings (changed at runtime via commands) ────────────────────────
settings = {
    "interval":    30,      # scan every N seconds
    "threshold":   20,      # % move to trigger price alert
    "min_liq":     1000,    # minimum liquidity in USD
    "max_age":     None,    # max token age in minutes (None = no limit)
    "chains":      ["solana", "bsc", "base", "ethereum"],
    "safe_only":   False,   # only show EARLY GEM rated tokens
    "new_only":    False,   # only show brand new token discoveries
    "paused":      False,   # pause scanning
    "charts":      True,    # include chart image in alerts
}

# ── World Cup 2026 Keywords ───────────────────────────────────────────────────
WC_KEYWORDS = [
    "worldcup", "world cup", "wc2026", "worldcup2026", "fifa2026",
    "fifa", "fwc", "fwc26", "fifawc", "fifameme", "fifacoin",
    "footballcoin", "soccercoin", "goatcoin", "championsleague",
    "goldenboot", "hatrick", "penalty", "freekick", "worldgoal",
    "usmnt", "uswnt", "usasoccer", "mexicofifa", "canadafc",
    "losangeles", "newYork", "miami", "dallas", "boston",
    "seattle", "houston", "philadelphia", "atlanta", "kansascity",
    "sanfrancisco", "guadalajara", "monterrey", "azteca",
    "toronto", "vancouver",
    "england", "threelions", "france", "lecoqgaulois",
    "germany", "mannschaft", "spain", "lafuria",
    "portugal", "selecao", "netherlands", "oranje",
    "croatia", "vatreni", "belgium", "rediablos",
    "switzerland", "nati", "austria", "oefb",
    "scotland", "tartan", "norway", "norge",
    "sweden", "turkey", "turkiye", "czechia", "bosnia",
    "argentina", "albiceleste", "brazil", "canarinho",
    "colombia", "cafeteros", "uruguay", "charruas",
    "ecuador", "tricolor", "paraguay", "guarani",
    "morocco", "algeria", "fennecs", "egypt", "pharaohs",
    "ghana", "blackstars", "tunisia",
    "japan", "samuraiblue", "southkorea", "australia", "socceroos",
    "iran", "jordan", "uzbekistan",
    "panama", "curacao", "haiti", "newzealand", "capeverde",
    "ronaldo", "cr7", "messi", "leo", "mbappe",
    "neymar", "haaland", "vinicius", "bellingham",
    "salah", "modric", "dembele", "pedri", "yamal",
    "osimhen", "lewandowski", "kane", "saka", "rashford",
    "pulisic", "reyna", "weah", "ferran", "gavi",
    "wc", "wcup", "goal", "striker", "keeper",
    "offside", "redcard", "yellowcard", "corner",
    "kickoff", "fulltime", "extratime", "shootout",
]

ALL_CHAINS = ["solana", "bsc", "base", "ethereum"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

seen: dict = {}
watchlist: set = set()
last_update_id = 0


# ── Telegram ──────────────────────────────────────────────────────────────────
def send(chat_id, text, photo_url=None):
    if not TELEGRAM_TOKEN:
        print(text)
        return
    try:
        if photo_url and settings["charts"]:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                json={
                    "chat_id": chat_id,
                    "photo": photo_url,
                    "caption": text,
                    "parse_mode": "HTML",
                },
                timeout=15,
            )
        else:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            ).raise_for_status()
    except Exception as e:
        log.error(f"Telegram send error: {e}")


def broadcast(text, photo_url=None):
    send(TELEGRAM_CHAT_ID, text, photo_url)


# ── DEXScreener ───────────────────────────────────────────────────────────────
def dex_search(keyword):
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/search?q={keyword}",
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("pairs", []) or []
    except Exception as e:
        log.error(f"DEX search error [{keyword}]: {e}")
        return []


def dex_by_address(address):
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}",
            timeout=10,
        )
        r.raise_for_status()
        pairs = r.json().get("pairs", []) or []
        if pairs:
            return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0))
        return {}
    except Exception:
        return {}


def chart_url(pair):
    token_addr = (pair.get("baseToken") or {}).get("address", "")
    chain = pair.get("chainId", "")
    if token_addr and chain:
        return f"https://dexscreener.com/{chain}/{token_addr}/chart.png"
    return None


# ── Safety Check ──────────────────────────────────────────────────────────────
def safety_check(pair):
    flags = []
    greens = []
    danger = 0
    safety = 0

    liq      = (pair.get("liquidity") or {}).get("usd", 0)
    fdv      = pair.get("fdv") or 0
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

    age_min = ((time.time() * 1000) - created) / 60_000 if created else 9999

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
        greens.append(f"⚠️ Decent liquidity (${liq:,.0f})")
        safety += 8
    elif liq >= 3_000:
        flags.append(f"⚠️ Low liquidity (${liq:,.0f})")
        danger += 10
    else:
        flags.append(f"🚨 Very low liquidity (${liq:,.0f})")
        danger += 25

    if fdv > 0 and liq > 0:
        ratio = fdv / liq
        if ratio > 1000:
            flags.append(f"🚨 FDV/Liq {ratio:.0f}x — honeypot risk")
            danger += 20
        elif ratio > 200:
            flags.append(f"⚠️ FDV/Liq {ratio:.0f}x — elevated risk")
            danger += 10
        elif ratio < 20:
            greens.append(f"✅ Healthy FDV/Liq ({ratio:.0f}x)")
            safety += 10

    if age_min < 10:
        flags.append(f"🚨 Only {age_min:.1f} min old — ultra new")
        danger += 15
    elif age_min < 60:
        flags.append(f"⚠️ {age_min:.0f} min old — new token")
        danger += 8
    elif age_min > 1440:
        greens.append(f"✅ Survived 24h+ ({age_min/60:.1f}h old)")
        safety += 10

    if vol_h1 > 50_000:
        greens.append(f"✅ Hot volume ${vol_h1:,.0f}/1h")
        safety += 15
    elif vol_h1 > 10_000:
        greens.append(f"⚠️ Growing volume ${vol_h1:,.0f}/1h")
        safety += 5
    elif vol_h1 < 500 and age_min > 30:
        flags.append("❌ Almost no volume")
        danger += 15

    social_types = [s.get("type", "").lower() for s in socials]
    has_tw = "twitter" in social_types
    has_tg = "telegram" in social_types
    has_web = len(websites) > 0

    if has_tw and has_tg and has_web:
        greens.append("✅ Full socials (Twitter + TG + Website)")
        safety += 15
    elif has_tw and has_tg:
        greens.append("✅ Twitter + Telegram")
        safety += 10
    elif has_tw or has_tg:
        flags.append("⚠️ Only one social found")
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

    if ch_h1 > 5 and ch_h6 > 10 and ch_h24 > 20 and liq > 20_000:
        greens.append("✅ Consistent growth 1h/6h/24h")
        safety += 10

    score = max(0, min(100, safety - danger + 30))

    if danger >= 45 or (danger >= 25 and safety < 15):
        verdict = "🚨 LIKELY RUG"
    elif danger >= 20 or safety < 20:
        verdict = "⚠️ RISKY"
    else:
        verdict = "✅ EARLY GEM"

    return verdict, greens + flags, score


# ── Format Alert ──────────────────────────────────────────────────────────────
def format_alert(pair, trigger, verdict, flags, score):
    base     = pair.get("baseToken") or {}
    name     = base.get("name", "Unknown")
    symbol   = base.get("symbol", "?")
    address  = base.get("address", "")
    chain    = pair.get("chainId", "").upper()
    price    = pair.get("priceUsd") or "?"
    liq      = (pair.get("liquidity") or {}).get("usd", 0)
    vol_h1   = (pair.get("volume") or {}).get("h1", 0) or 0
    vol_h24  = (pair.get("volume") or {}).get("h24", 0) or 0
    ch_h1    = (pair.get("priceChange") or {}).get("h1", 0) or 0
    ch_h6    = (pair.get("priceChange") or {}).get("h6", 0) or 0
    ch_h24   = (pair.get("priceChange") or {}).get("h24", 0) or 0
    txns     = ((pair.get("txns") or {}).get("h1") or {})
    buys     = txns.get("buys", 0)
    sells    = txns.get("sells", 0)
    url      = pair.get("url", "")
    created  = pair.get("pairCreatedAt") or 0
    age_min  = int(((time.time() * 1000) - created) / 60_000) if created else 0
    age_str  = f"{age_min}m" if age_min < 120 else f"{age_min//60}h {age_min%60}m"
    bar      = "🟢" * (score // 20) + "⚪" * (5 - score // 20)
    flags_text = "\n".join(flags) if flags else "None"

    return f"""⚽ <b>WC MEMECOIN ALERT</b> ⚽
━━━━━━━━━━━━━━━━━━━━
🪙 <b>{name} (${symbol})</b>
🔗 Chain: {chain} | ⏱ Age: {age_str}
📢 <b>{trigger}</b>

💰 Price: ${price}
📊 1h: {ch_h1:+.1f}% | 6h: {ch_h6:+.1f}% | 24h: {ch_h24:+.1f}%
💧 Liquidity: ${liq:,.0f}
📦 Vol 1h: ${vol_h1:,.0f} | 24h: ${vol_h24:,.0f}
🔄 Buys/Sells (1h): {buys} / {sells}

🛡 Safety: {verdict}
{bar} Score: {score}/100
{flags_text}

🔍 <a href="{url}">DEXScreener</a>
📋 CA: <code>{address}</code>
━━━━━━━━━━━━━━━━━━━━
⏰ {datetime.now(timezone.utc).strftime("%H:%M:%S UTC")}"""


# ── Commands ──────────────────────────────────────────────────────────────────
HELP_TEXT = """⚽ <b>WC Memecoin Bot v3 Commands</b> ⚽
━━━━━━━━━━━━━━━━━━━━

<b>⚙️ Scan Controls</b>
/interval [secs] — set scan speed (e.g. /interval 30)
/pause — pause scanning
/resume — resume scanning
/status — show current bot settings

<b>🔗 Chain Filter</b>
/chain solana — scan only Solana
/chain bsc — scan only BSC
/chain base — scan only Base
/chain eth — scan only Ethereum
/chain all — scan all chains

<b>📊 Alert Filters</b>
/threshold [%] — set pump/dump alert % (e.g. /threshold 20)
/minliq [amount] — min liquidity in USD (e.g. /minliq 5000)
/maxage [mins] — only show tokens under X mins old (e.g. /maxage 60)
/maxage off — remove age filter
/safeonly on — only show EARLY GEM tokens
/safeonly off — show all tokens
/newonly on — only alert on brand new launches
/newonly off — show pumps/dumps too
/charts on — include chart image in alerts
/charts off — text only alerts

<b>🔍 Token Lookup</b>
/check [CA] — check any token by contract address
/top — top 5 WC tokens by volume right now
/trending — what is pumping hardest this hour

<b>📌 Watchlist</b>
/watch [CA] — add token to watchlist
/unwatch [CA] — remove from watchlist
/watchlist — show your watchlist

<b>🔄 Reset</b>
/reset — reset ALL settings to default

━━━━━━━━━━━━━━━━━━━━
Bot scans every {interval}s on: {chains}"""


def handle_command(chat_id, text):
    text = text.strip()
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]

    if cmd == "/start" or cmd == "/help":
        chains_str = ", ".join(settings["chains"]).upper()
        reply = HELP_TEXT.format(
            interval=settings["interval"],
            chains=chains_str,
        )
        send(chat_id, reply)

    elif cmd == "/status":
        chains_str = ", ".join(settings["chains"]).upper()
        send(chat_id, f"""⚙️ <b>Bot Status</b>
━━━━━━━━━━━━━━━━━━━━
{'⏸ PAUSED' if settings['paused'] else '▶️ RUNNING'}
⏱ Scan interval: {settings['interval']}s
🔗 Chains: {chains_str}
📊 Alert threshold: {settings['threshold']}%
💧 Min liquidity: ${settings['min_liq']:,}
⏳ Max age: {str(settings['max_age']) + ' min' if settings['max_age'] else 'Off'}
🛡 Safe only: {'On' if settings['safe_only'] else 'Off'}
🆕 New only: {'On' if settings['new_only'] else 'Off'}
📸 Charts: {'On' if settings['charts'] else 'Off'}
📌 Watchlist: {len(watchlist)} tokens
━━━━━━━━━━━━━━━━━━━━""")

    elif cmd == "/pause":
        settings["paused"] = True
        send(chat_id, "⏸ Bot paused. Send /resume to restart scanning.")

    elif cmd == "/resume":
        settings["paused"] = False
        send(chat_id, "▶️ Bot resumed! Scanning every " + str(settings["interval"]) + "s.")

    elif cmd == "/interval":
        if len(parts) < 2 or not parts[1].isdigit():
            send(chat_id, "Usage: /interval [seconds] — e.g. /interval 60")
            return
        val = int(parts[1])
        if val < 10:
            send(chat_id, "⚠️ Minimum interval is 10 seconds.")
            return
        settings["interval"] = val
        send(chat_id, f"✅ Scan interval set to {val} seconds.")

    elif cmd == "/threshold":
        if len(parts) < 2 or not parts[1].replace(".", "").isdigit():
            send(chat_id, "Usage: /threshold [%] — e.g. /threshold 20")
            return
        settings["threshold"] = float(parts[1])
        send(chat_id, f"✅ Alert threshold set to {settings['threshold']}%.")

    elif cmd == "/minliq":
        if len(parts) < 2 or not parts[1].isdigit():
            send(chat_id, "Usage: /minliq [amount] — e.g. /minliq 5000")
            return
        settings["min_liq"] = int(parts[1])
        send(chat_id, f"✅ Minimum liquidity set to ${settings['min_liq']:,}.")

    elif cmd == "/maxage":
        if len(parts) < 2:
            send(chat_id, "Usage: /maxage [mins] or /maxage off")
            return
        if parts[1].lower() == "off":
            settings["max_age"] = None
            send(chat_id, "✅ Max age filter removed.")
        elif parts[1].isdigit():
            settings["max_age"] = int(parts[1])
            send(chat_id, f"✅ Only showing tokens under {settings['max_age']} minutes old.")
        else:
            send(chat_id, "Usage: /maxage [mins] or /maxage off")

    elif cmd == "/safeonly":
        if len(parts) < 2 or parts[1].lower() not in ["on", "off"]:
            send(chat_id, "Usage: /safeonly on or /safeonly off")
            return
        settings["safe_only"] = parts[1].lower() == "on"
        send(chat_id, f"✅ Safe only: {'On — only showing EARLY GEM tokens.' if settings['safe_only'] else 'Off — showing all tokens.'}")

    elif cmd == "/newonly":
        if len(parts) < 2 or parts[1].lower() not in ["on", "off"]:
            send(chat_id, "Usage: /newonly on or /newonly off")
            return
        settings["new_only"] = parts[1].lower() == "on"
        send(chat_id, f"✅ New only: {'On — only alerting on new launches.' if settings['new_only'] else 'Off — alerting on pumps/dumps too.'}")

    elif cmd == "/charts":
        if len(parts) < 2 or parts[1].lower() not in ["on", "off"]:
            send(chat_id, "Usage: /charts on or /charts off")
            return
        settings["charts"] = parts[1].lower() == "on"
        send(chat_id, f"✅ Charts: {'On' if settings['charts'] else 'Off'}.")

    elif cmd == "/chain":
        if len(parts) < 2:
            send(chat_id, "Usage: /chain [solana|bsc|base|eth|all]")
            return
        val = parts[1].lower()
        chain_map = {"solana": ["solana"], "bsc": ["bsc"], "base": ["base"], "eth": ["ethereum"], "all": ALL_CHAINS}
        if val not in chain_map:
            send(chat_id, "Options: solana, bsc, base, eth, all")
            return
        settings["chains"] = chain_map[val]
        send(chat_id, f"✅ Now scanning: {', '.join(settings['chains']).upper()}")

    elif cmd == "/reset":
        settings.update({
            "interval": 30, "threshold": 20, "min_liq": 1000,
            "max_age": None, "chains": ALL_CHAINS[:],
            "safe_only": False, "new_only": False,
            "paused": False, "charts": True,
        })
        send(chat_id, "✅ All settings reset to default!")

    elif cmd == "/check":
        if len(parts) < 2:
            send(chat_id, "Usage: /check [contract address]")
            return
        address = parts[1]
        send(chat_id, "🔍 Checking token...")
        pair = dex_by_address(address)
        if not pair:
            send(chat_id, "❌ Token not found on DEXScreener.")
            return
        verdict, flags, score = safety_check(pair)
        msg = format_alert(pair, "📋 Manual Check", verdict, flags, score)
        photo = chart_url(pair)
        send(chat_id, msg, photo)

    elif cmd == "/watch":
        if len(parts) < 2:
            send(chat_id, "Usage: /watch [contract address]")
            return
        watchlist.add(parts[1].lower())
        send(chat_id, f"📌 Added to watchlist! You now have {len(watchlist)} tokens watched.")

    elif cmd == "/unwatch":
        if len(parts) < 2:
            send(chat_id, "Usage: /unwatch [contract address]")
            return
        watchlist.discard(parts[1].lower())
        send(chat_id, f"✅ Removed from watchlist.")

    elif cmd == "/watchlist":
        if not watchlist:
            send(chat_id, "📌 Your watchlist is empty. Use /watch [CA] to add tokens.")
            return
        items = "\n".join([f"• <code>{ca}</code>" for ca in watchlist])
        send(chat_id, f"📌 <b>Watchlist ({len(watchlist)} tokens)</b>\n{items}")

    elif cmd == "/top":
        send(chat_id, "🔍 Fetching top WC tokens by volume...")
        results = []
        for kw in ["worldcup", "wc2026", "fifa", "mbappe", "messi"]:
            for pair in dex_search(kw):
                if pair.get("chainId") in settings["chains"]:
                    vol = (pair.get("volume") or {}).get("h24", 0) or 0
                    liq = (pair.get("liquidity") or {}).get("usd", 0) or 0
                    if liq > 1000:
                        results.append((vol, pair))
        results.sort(key=lambda x: x[0], reverse=True)
        if not results:
            send(chat_id, "No WC tokens found right now.")
            return
        msg = "🏆 <b>Top 5 WC Tokens by Volume (24h)</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        for i, (vol, pair) in enumerate(results[:5], 1):
            base = pair.get("baseToken") or {}
            name = base.get("name", "?")
            sym = base.get("symbol", "?")
            ch24 = (pair.get("priceChange") or {}).get("h24", 0) or 0
            liq = (pair.get("liquidity") or {}).get("usd", 0) or 0
            url = pair.get("url", "")
            msg += f"{i}. <b>{name} (${sym})</b>\n"
            msg += f"   Vol: ${vol:,.0f} | Liq: ${liq:,.0f} | 24h: {ch24:+.1f}%\n"
            msg += f"   <a href=\"{url}\">Chart</a>\n\n"
        send(chat_id, msg)

    elif cmd == "/trending":
        send(chat_id, "🔍 Finding what is pumping hardest this hour...")
        results = []
        for kw in WC_KEYWORDS[:20]:
            for pair in dex_search(kw):
                if pair.get("chainId") in settings["chains"]:
                    ch1 = (pair.get("priceChange") or {}).get("h1", 0) or 0
                    liq = (pair.get("liquidity")
