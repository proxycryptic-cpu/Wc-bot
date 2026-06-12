import os
import time
import requests
import logging
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Default Settings ──────────────────────────────────────────────────────────
settings = {
    "interval":      30,
    "threshold":     20,
    "min_liq":       500,
    "max_age":       None,
    "chains":        ["solana", "bsc", "base", "ethereum"],
    "safe_only":     False,
    "new_only":      False,
    "paused":        False,
    "charts":        True,
    "gem_mode":      True,
    "gem_mc_min":    2000,
    "gem_mc_max":    30000,
    "wc_mode":       True,
    "min_rug_score": 30,
}

ALL_CHAINS = ["solana", "bsc", "base", "ethereum"]

# ── World Cup Keywords ────────────────────────────────────────────────────────
WC_KEYWORDS = [
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
]

WC_SET = set(WC_KEYWORDS)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

seen: dict = {}
watchlist: set = set()
last_update_id = 0
start_time = time.time()
total_scans = 0
total_alerts = 0
total_gems = 0


# ── Helpers ───────────────────────────────────────────────────────────────────
def is_wc_token(pair):
    base   = pair.get("baseToken") or {}
    name   = (base.get("name") or "").lower()
    symbol = (base.get("symbol") or "").lower()
    for kw in WC_SET:
        if kw in name or kw in symbol:
            return True
    return False


# ── Telegram ──────────────────────────────────────────────────────────────────
def send(chat_id, text, photo_url=None):
    if not TELEGRAM_TOKEN:
        print(text)
        return
    try:
        if photo_url and settings["charts"]:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                json={
                    "chat_id": chat_id,
                    "photo": photo_url,
                    "caption": text[:1024],
                    "parse_mode": "HTML",
                },
                timeout=15,
            )
            if r.ok:
                return
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
        log.error(f"Telegram error: {e}")


def broadcast(text, photo_url=None):
    send(TELEGRAM_CHAT_ID, text, photo_url)


# ── DEXScreener ───────────────────────────────────────────────────────────────
def dex_new_pairs(chain):
    urls = [
        f"https://api.dexscreener.com/token-profiles/latest/v1?chain={chain}",
        f"https://api.dexscreener.com/latest/dex/search?q=new&chain={chain}",
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=10)
            if r.ok:
                data = r.json()
                pairs = data if isinstance(data, list) else data.get("pairs", [])
                if pairs:
                    return pairs or []
        except Exception:
            continue
    return []


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
    addr  = (pair.get("baseToken") or {}).get("address", "")
    chain = pair.get("chainId", "")
    if addr and chain:
        return f"https://dexscreener.com/{chain}/{addr}/chart.png"
    return None


# ── Rug Score ─────────────────────────────────────────────────────────────────
def rug_score(pair, gem_mode=False):
    flags   = []
    greens  = []
    danger  = 0
    safety  = 0

    liq      = (pair.get("liquidity") or {}).get("usd", 0) or 0
    fdv      = pair.get("fdv") or 0
    mc       = pair.get("marketCap") or fdv or 0
    vol_h1   = (pair.get("volume") or {}).get("h1", 0) or 0
    vol_h24  = (pair.get("volume") or {}).get("h24", 0) or 0
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

    age_min = ((time.time() * 1000) - created) / 60_000 if created else 9999

    # 1. DEX Paid
    if dex_paid > 0:
        greens.append(f"✅ DEX Paid ({dex_paid} boosts)")
        safety += 20
    else:
        flags.append("❌ No DEX Paid")
        danger += 5

    # 2. Liquidity
    if liq >= 50_000:
        greens.append(f"✅ Strong liquidity (${liq:,.0f})")
        safety += 15
    elif liq >= 10_000:
        greens.append(f"✅ Decent liquidity (${liq:,.0f})")
        safety += 10
    elif liq >= 3_000:
        flags.append(f"⚠️ Low liquidity (${liq:,.0f})")
        danger += 8
    elif liq >= 500:
        flags.append(f"⚠️ Very low liquidity (${liq:,.0f})")
        danger += 15
    else:
        flags.append(f"🚨 Micro liquidity (${liq:,.0f}) — easy rug")
        danger += 25

    # 3. MC/Liq ratio
    if mc > 0 and liq > 0:
        mc_liq = mc / liq
        if mc_liq < 5 and gem_mode:
            greens.append(f"💎 Ultra low MC/Liq ({mc_liq:.1f}x) — early gem")
            safety += 15
        elif mc_liq < 10:
            greens.append(f"✅ Healthy MC/Liq ({mc_liq:.1f}x)")
            safety += 10
        elif mc_liq > 500:
            flags.append(f"🚨 MC/Liq {mc_liq:.0f}x — extremely overvalued")
            danger += 20

    # 4. FDV/Liq honeypot
    if fdv > 0 and liq > 0:
        ratio = fdv / liq
        if ratio > 1000:
            flags.append(f"🚨 FDV/Liq {ratio:.0f}x — honeypot risk")
            danger += 20
        elif ratio > 200:
            flags.append(f"⚠️ FDV/Liq {ratio:.0f}x — high risk")
            danger += 10
        elif ratio < 20:
            greens.append(f"✅ Healthy FDV/Liq ({ratio:.0f}x)")
            safety += 8

    # 5. Token age
    if age_min < 5:
        flags.append(f"🚨 {age_min:.1f} min old — brand new, extreme risk")
        danger += 10
    elif age_min < 30:
        flags.append(f"⚠️ {age_min:.0f} min old — very new")
        danger += 6
    elif age_min < 120:
        flags.append(f"⚠️ {age_min:.0f} min old — new token")
        danger += 3
    elif age_min > 1440:
        greens.append(f"✅ Survived 24h+ ({age_min/60:.0f}h old)")
        safety += 10

    # 6. Buy pressure
    total_txns = buys_h1 + sells_h1
    if total_txns > 0:
        buy_ratio = buys_h1 / total_txns
        if buy_ratio > 0.7 and buys_h1 > 20:
            greens.append(f"✅ Strong buys ({buys_h1} buys vs {sells_h1} sells)")
            safety += 15
        elif buy_ratio > 0.6 and buys_h1 > 10:
            greens.append(f"✅ More buys than sells ({buys_h1} vs {sells_h1})")
            safety += 8
        elif buy_ratio < 0.3 and total_txns > 10:
            flags.append(f"🚨 Mostly sells ({sells_h1} sells vs {buys_h1} buys)")
            danger += 15

    # 7. Volume
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

    # 8. Socials
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
        flags.append("⚠️ Only one social found")
        danger += 5
    else:
        flags.append("🚨 No socials — anon dev")
        danger += 20

    # 9. Rug pump pattern
    if ch_h1 > 300 and liq < 30_000:
        flags.append("🚨 300%+ pump + low liq — classic rug setup")
        danger += 25
    elif ch_h1 > 100 and liq < 10_000:
        flags.append("⚠️ Big pump + very low liq")
        danger += 15

    # 10. Consistent growth
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
def format_alert(pair, trigger, verdict, flags, score, is_wc=False, is_gem=False):
    base    = pair.get("baseToken") or {}
    name    = base.get("name", "Unknown")
    symbol  = base.get("symbol", "?")
    address = base.get("address", "")
    chain   = pair.get("chainId", "").upper()
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
    url     = pair.get("url", "")
    created = pair.get("pairCreatedAt") or 0
    age_min = int(((time.time() * 1000) - created) / 60_000) if created else 0
    age_str = f"{age_min}m" if age_min < 120 else f"{age_min//60}h {age_min%60}m"
    bar     = "🟢" * (score // 20) + "⚪" * (5 - score // 20)
    flags_text = "\n".join(flags) if flags else "None"

    if is_wc:
        header = "⚽ <b>WC MEMECOIN ALERT</b> ⚽"
    elif is_gem:
        header = "💎 <b>ULTRA EARLY GEM ALERT</b> 💎"
    else:
        header = "🚀 <b>NEW TOKEN ALERT</b> 🚀"

    mc_line = f"📊 MC: ${mc:,.0f}\n" if mc > 0 else ""

    return f"""{header}
━━━━━━━━━━━━━━━━━━━━
🪙 <b>{name} (${symbol})</b>
🔗 Chain: {chain} | ⏱ Age: {age_str}
📢 <b>{trigger}</b>

💰 Price: ${price}
{mc_line}📊 1h: {ch_h1:+.1f}% | 6h: {ch_h6:+.1f}% | 24h: {ch_h24:+.1f}%
💧 Liquidity: ${liq:,.0f}
📦 Vol 1h: ${vol_h1:,.0f} | 24h: ${vol_h24:,.0f}
🔄 Buys/Sells (1h): {buys} / {sells}

🛡 Rug Score: {verdict}
{bar} {score}/100
{flags_text}

🔍 <a href="{url}">DEXScreener</a>
📋 CA: <code>{address}</code>
━━━━━━━━━━━━━━━━━━━━
⏰ {datetime.now(timezone.utc).strftime("%H:%M:%S UTC")}"""


# ── Help Text ─────────────────────────────────────────────────────────────────
HELP_TEXT = """🤖 <b>Alpha Bot v5 Commands</b>
━━━━━━━━━━━━━━━━━━━━

<b>⚙️ Controls</b>
/pause — pause all scanning
/resume — resume scanning
/interval [secs] — scan speed (min 10s)
/status — current settings
/uptime — bot runtime stats
/runtime — same as /uptime

<b>🔗 Chain</b>
/chain solana|bsc|base|eth|all

<b>⚽ WC Scanner</b>
/wc on|off — toggle WC scanning
/threshold [%] — alert on X% move (default 20)
/trending — hottest WC pumps now
/top — top 5 WC tokens by volume

<b>💎 Gem Hunter</b>
/gem on|off — toggle gem hunting
/mcap [min] [max] — MC range (e.g. /mcap 2000 30000)
/minscore [0-100] — min rug score to alert (default 30)
/findbetter — scan for gems right now

<b>📊 Filters</b>
/minliq [amount] — min liquidity
/maxage [mins]|off — max token age
/safeonly on|off — safe tokens only
/newonly on|off — new launches only
/charts on|off — chart images

<b>🔍 Lookup</b>
/check [CA] — check any token

<b>📌 Watchlist</b>
/watch [CA] — add to watchlist
/unwatch [CA] — remove
/watchlist — show all

<b>🔄 Reset</b>
/reset — reset all to default
━━━━━━━━━━━━━━━━━━━━"""


# ── Commands ──────────────────────────────────────────────────────────────────
def handle_command(chat_id, text):
    text  = text.strip()
    parts = text.split()
    cmd   = parts[0].lower().split("@")[0]

    if cmd in ["/start", "/help"]:
        send(chat_id, HELP_TEXT)

    elif cmd in ["/uptime", "/runtime"]:
        uptime_secs = int(time.time() - start_time)
        days  = uptime_secs // 86400
        hours = (uptime_secs % 86400) // 3600
        mins  = (uptime_secs % 3600) // 60
        secs  = uptime_secs % 60
        uptime_str = f"{days}d {hours}h {mins}m {secs}s" if days > 0 else f"{hours}h {mins}m {secs}s"
        started = datetime.fromtimestamp(start_time, tz=timezone.utc).strftime("%b %d at %H:%M UTC")
        send(chat_id, f"""⏱ <b>Bot Runtime</b>
━━━━━━━━━━━━━━━━━━━━
🟢 Uptime: {uptime_str}
📅 Started: {started}
🔄 Total scans: {total_scans}
📢 Total alerts: {total_alerts}
💎 Gems found: {total_gems}
━━━━━━━━━━━━━━━━━━━━""")

    elif cmd == "/status":
        chains_str = ", ".join(settings["chains"]).upper()
        send(chat_id, f"""⚙️ <b>Bot Status</b>
━━━━━━━━━━━━━━━━━━━━
{'⏸ PAUSED' if settings['paused'] else '▶️ RUNNING'}
⏱ Interval: {settings['interval']}s
🔗 Chains: {chains_str}
⚽ WC Mode: {'On' if settings['wc_mode'] else 'Off'}
💎 Gem Mode: {'On' if settings['gem_mode'] else 'Off'}
💎 MC Range: ${settings['gem_mc_min']:,} - ${settings['gem_mc_max']:,}
🛡 Min Rug Score: {settings['min_rug_score']}/100
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
        send(chat_id, "⏸ Paused. Send /resume to restart.")

    elif cmd == "/resume":
        settings["paused"] = False
        send(chat_id, f"▶️ Resumed! Scanning every {settings['interval']}s.")

    elif cmd == "/interval":
        if len(parts) < 2 or not parts[1].isdigit():
            send(chat_id, "Usage: /interval [seconds]")
            return
        val = int(parts[1])
        if val < 10:
            send(chat_id, "⚠️ Minimum 10 seconds.")
            return
        settings["interval"] = val
        send(chat_id, f"✅ Interval: {val}s.")

    elif cmd == "/threshold":
        if len(parts) < 2:
            send(chat_id, "Usage: /threshold [%]")
            return
        settings["threshold"] = float(parts[1])
        send(chat_id, f"✅ Threshold: {settings['threshold']}%.")

    elif cmd == "/minliq":
        if len(parts) < 2 or not parts[1].isdigit():
            send(chat_id, "Usage: /minliq [amount]")
            return
        settings["min_liq"] = int(parts[1])
        send(chat_id, f"✅ Min liq: ${settings['min_liq']:,}.")

    elif cmd == "/minscore":
        if len(parts) < 2 or not parts[1].isdigit():
            send(chat_id, "Usage: /minscore [0-100]")
            return
        settings["min_rug_score"] = int(parts[1])
        send(chat_id, f"✅ Min rug score: {settings['min_rug_score']}/100.")

    elif cmd == "/maxage":
        if len(parts) < 2:
            send(chat_id, "Usage: /maxage [mins] or /maxage off")
            return
        if parts[1].lower() == "off":
            settings["max_age"] = None
            send(chat_id, "✅ Max age filter removed.")
        elif parts[1].isdigit():
            settings["max_age"] = int(parts[1])
            send(chat_id, f"✅ Max age: {settings['max_age']} mins.")

    elif cmd == "/safeonly":
        if len(parts) < 2 or parts[1].lower() not in ["on", "off"]:
            send(chat_id, "Usage: /safeonly on|off")
            return
        settings["safe_only"] = parts[1].lower() == "on"
        send(chat_id, f"✅ Safe only: {'On' if settings['safe_only'] else 'Off'}.")

    elif cmd == "/newonly":
        if len(parts) < 2 or parts[1].lower() not in ["on", "off"]:
            send(chat_id, "Usage: /newonly on|off")
            return
        settings["new_only"] = parts[1].lower() == "on"
        send(chat_id, f"✅ New only: {'On' if settings['new_only'] else 'Off'}.")

    elif cmd == "/charts":
        if len(parts) < 2 or parts[1].lower() not in ["on", "off"]:
            send(chat_id, "Usage: /charts on|off")
            return
        settings["charts"] = parts[1].lower() == "on"
        send(chat_id, f"✅ Charts: {'On' if settings['charts'] else 'Off'}.")

    elif cmd == "/chain":
        if len(parts) < 2:
            send(chat_id, "Usage: /chain [solana|bsc|base|eth|all]")
            return
        val = parts[1].lower()
        chain_map = {
            "solana": ["solana"], "bsc": ["bsc"],
            "base": ["base"], "eth": ["ethereum"], "all": ALL_CHAINS[:]
        }
        if val not in chain_map:
            send(chat_id, "Options: solana, bsc, base, eth, all")
            return
        settings["chains"] = chain_map[val]
        send(chat_id, f"✅ Scanning: {', '.join(settings['chains']).upper()}")

    elif cmd == "/wc":
        if len(parts) < 2 or parts[1].lower() not in ["on", "off"]:
            send(chat_id, "Usage: /wc on|off")
            return
        settings["wc_mode"] = parts[1].lower() == "on"
        send(chat_id, f"✅ WC Scanner: {'On' if settings['wc_mode'] else 'Off'}.")

    elif cmd == "/gem":
        if len(parts) < 2 or parts[1].lower() not in ["on", "off"]:
            send(chat_id, "Usage: /gem on|off")
            return
        settings["gem_mode"] = parts[1].lower() == "on"
        send(chat_id, f"✅ Gem Hunter: {'On' if settings['gem_mode'] else 'Off'}.")

    elif cmd == "/mcap":
        if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
            send(chat_id, "Usage: /mcap [min] [max]")
            return
        settings["gem_mc_min"] = int(parts[1])
        settings["gem_mc_max"] = int(parts[2])
        send(chat_id, f"✅ MC range: ${settings['gem_mc_min']:,} - ${settings['gem_mc_max']:,}.")

    elif cmd == "/reset":
        settings.update({
            "interval": 30, "threshold": 20, "min_liq": 500,
            "max_age": None, "chains": ALL_CHAINS[:],
            "safe_only": False, "new_only": False,
            "paused": False, "charts": True,
            "gem_mode": True, "gem_mc_min": 2000, "gem_mc_max": 30000,
            "wc_mode": True, "min_rug_score": 30,
        })
        send(chat_id, "✅ All settings reset to default!")

    elif cmd == "/check":
        if len(parts) < 2:
            send(chat_id, "Usage: /check [contract address]")
            return
        send(chat_id, "🔍 Checking token...")
        pair = dex_by_address(parts[1])
        if not pair:
            send(chat_id, "❌ Token not found on DEXScreener.")
            return
        wc  = is_wc_token(pair)
        mc  = pair.get("marketCap") or pair.get("fdv") or 0
        gem = settings["gem_mc_min"] <= mc <= settings["gem_mc_max"]
        verdict, flags, score = rug_score(pair, gem_mode=gem)
        msg = format_alert(pair, "📋 Manual Check", verdict, flags, score, is_wc=wc, is_gem=gem)
        send(chat_id, msg, chart_url(pair))

    elif cmd == "/watch":
        if len(parts) < 2:
            send(chat_id, "Usage: /watch [CA]")
            return
        watchlist.add(parts[1].lower())
        send(chat_id, f"📌 Added! Watchlist: {len(watchlist)} tokens.")

    elif cmd == "/unwatch":
        if len(parts) < 2:
            send(chat_id, "Usage: /unwatch [CA]")
            return
        watchlist.discard(parts[1].lower())
        send(chat_id, "✅ Removed.")

    elif cmd == "/watchlist":
        if not watchlist:
            send(chat_id, "📌 Empty. Use /watch [CA] to add tokens.")
            return
        items = "\n".join([f"• <code>{ca}</code>" for ca in watchlist])
        send(chat_id, f"📌 <b>Watchlist ({len(watchlist)})</b>\n{items}")

    elif cmd == "/top":
        send(chat_id, "🔍 Fetching top WC tokens...")
        results = []
        for kw in ["worldcup", "wc2026", "fifa", "mbappe", "messi"]:
            for pair in dex_search(kw):
                if pair.get("chainId") in settings["chains"]:
                    vol = (pair.get("volume") or {}).get("h24", 0) or 0
                    liq = (pair.get("liquidity") or {}).get("usd", 0) or 0
                    if liq > 500:
                        results.append((vol, pair))
        results.sort(key=lambda x: x[0], reverse=True)
        if not results:
            send(chat_id, "No WC tokens found right now.")
            return
        msg = "🏆 <b>Top 5 WC Tokens (24h Volume)</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        for i, (vol, pair) in enumerate(results[:5], 1):
            base = pair.get("baseToken") or {}
            name = base.get("name", "?")
            sym  = base.get("symbol", "?")
            ch24 = (pair.get("priceChange") or {}).get("h24", 0) or 0
            liq  = (pair.get("liquidity") or {}).get("usd", 0) or 0
            url  = pair.get("url", "")
            msg += f"{i}. <b>{name} (${sym})</b>\n"
            msg += f"   Vol: ${vol:,.0f} | Liq: ${liq:,.0f} | 24h: {ch24:+.1f}%\n"
            msg += f"   <a href=\"{url}\">Chart</a>\n\n"
        send(chat_id, msg)

    elif cmd == "/trending":
        send(chat_id, "🔍 Finding hottest pumps...")
        results = []
        for kw in WC_KEYWORDS[:20]:
            for pair in dex_search(kw):
                if pair.get("chainId") in settings["chains"]:
                    ch1 = (pair.get("priceChange") or {}).get("h1", 0) or 0
                    liq = (pair.get("liquidity") or {}).get("usd", 0) or 0
                    if liq > 500 and ch1 > 0:
                        results.append((ch1, pair))
        results.sort(key=lambda x: x[0], reverse=True)
        if not results:
            send(chat_id, "Nothing pumping right now.")
            return
        msg = "🚀 <b>Trending WC Tokens (1h)</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        for i, (ch1, pair) in enumerate(results[:5], 1):
            base = pair.get("baseToken") or {}
            name = base.get("name", "?")
            sym  = base.get("symbol", "?")
            liq  = (pair.get("liquidity") or {}).get("usd", 0) or 0
            url  = pair.get("url", "")
            msg += f"{i}. <b>{name} (${sym})</b> +{ch1:.1f}%\n"
            msg += f"   Liq: ${liq:,.0f} | <a href=\"{url}\">Chart</a>\n\n"
        send(chat_id, msg)

    elif cmd == "/findbetter":
        send(chat_id, "💎 Hunting for ultra early gems right now...")
        found = 0
        for chain in settings["chains"]:
            pairs = dex_new_pairs(chain)
            for pair in pairs:
                mc  = pair.get("marketCap") or pair.get("fdv") or 0
                liq = (pair.get("liquidity") or {}).get("usd", 0) or 0
                if settings["gem_mc_min"] <= mc <= settings["gem_mc_max"] and liq >= settings["min_liq"]:
                    verdict, flags, score = rug_score(pair, gem_mode=True)
                    if "RUG" not in verdict and score >= settings["min_rug_score"]:
                        wc = is_wc_token(pair)
                        send(chat_id, format_alert(pair, "💎 Manual Gem Scan", verdict, flags, score, is_wc=wc, is_gem=True), chart_url(pair))
                        found += 1
                        time.sleep(0.5)
                        if found >= 5:
                            break
            if found >= 5:
                break
        if found == 0:
            send(chat_id, "No gems found right now. Try again in a few minutes!")


# ── Poll Commands ─────────────────────────────────────────────────────────────
def poll_commands():
    global last_update_id
    if not TELEGRAM_TOKEN:
        return
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
            text    = msg.get("text", "")
            chat_id = (msg.get("chat") or {}).get("id")
            if text.startswith("/") and chat_id:
                handle_command(str(chat_id), text)
    except Exception as e:
        log.error(f"Poll error: {e}")


# ── Main Scan ─────────────────────────────────────────────────────────────────
def scan():
    global total_scans, total_alerts, total_gems
    if settings["paused"]:
        return

    checked = set()
    alerted = 0

    # Watchlist first
    for ca in list(watchlist):
        pair = dex_by_address(ca)
        if not pair:
            continue
        address = (pair.get("baseToken") or {}).get("address", "")
        if not address:
            continue
        checked.add(address)
        ch_h1 = (pair.get("priceChange") or {}).get("h1", 0) or 0
        if abs(ch_h1) >= settings["threshold"]:
            wc = is_wc_token(pair)
            mc = pair.get("marketCap") or pair.get("fdv") or 0
            gem = settings["gem_mc_min"] <= mc <= settings["gem_mc_max"]
            verdict, flags, score = rug_score(pair, gem_mode=gem)
            direction = "🚀 PUMPING" if ch_h1 > 0 else "💀 DUMPING"
            trigger = f"📌 WATCHLIST — {direction} {ch_h1:+.1f}% in 1h"
            broadcast(format_alert(pair, trigger, verdict, flags, score, is_wc=wc, is_gem=gem), chart_url(pair))
            alerted += 1

    # Scan all new pairs across chains
    for chain in settings["chains"]:
        pairs = dex_new_pairs(chain)
        for pair in pairs:
            address = (pair.get("baseToken") or {}).get("address", "")
            if not address or address in checked:
                continue
            checked.add(address)

            sym = (pair.get("baseToken") or {}).get("symbol", "").upper()
            if sym in {"USDT","USDC","BUSD","DAI","WETH","WBNB","WSOL","ETH","BNB","SOL"}:
                continue

            liq = (pair.get("liquidity") or {}).get("usd", 0) or 0
            if liq < settings["min_liq"]:
                continue

            mc  = pair.get("marketCap") or pair.get("fdv") or 0
            wc  = is_wc_token(pair)
            gem = settings["gem_mc_min"] <= mc <= settings["gem_mc_max"]

            # Skip if both modes off for this token
            if not wc and not settings["gem_mode"]:
                continue
            if wc and not settings["wc_mode"]:
                continue

            created_ms = pair.get("pairCreatedAt") or 0
            age_min    = ((time.time() * 1000) - created_ms) / 60_000 if created_ms else 9999
            if settings["max_age"] and age_min > settings["max_age"]:
                continue

            ch_h1 = (pair.get("priceChange") or {}).get("h1", 0) or 0
            price = float(pair.get("priceUsd") or 0)
            prev  = seen.get(address, {})
            now   = time.time()
            last_alert = prev.get("last_alert", 0)
            cooldown   = 1800

            trigger = None
            if address not in seen:
                if wc:
                    trigger = "🆕 NEW WC TOKEN DETECTED"
                elif gem:
                    trigger = f"💎 NEW GEM — MC ${mc:,.0f}"
                else:
                    trigger = "🆕 NEW TOKEN DETECTED"
            elif abs(ch_h1) >= settings["threshold"] and (now - last_alert) > cooldown:
                direction = "🚀 PUMPING" if ch_h1 > 0 else "💀 DUMPING"
                trigger   = f"{direction} {ch_h1:+.1f}% in 1h"

            if not trigger:
                seen[address] = {**prev, "last_price": price}
                continue

            if settings["new_only"] and "NEW" not in trigger and "GEM" not in trigger:
                seen[address] = {**prev, "last_price": price}
                continue

            verdict, flags, score = rug_score(pair, gem_mode=gem)

            if score < settings["min_rug_score"]:
                seen[address] = {**prev, "last_price": price, "last_alert": now}
                continue

            if settings["safe_only"] and "RUG" in verdict:
                seen[address] = {**prev, "last_price": price, "last_alert": now}
                continue

            broadcast(format_alert(pair, trigger, verdict, flags, score, is_wc=wc, is_gem=gem), chart_url(pair))
            seen[address] = {"first_seen": prev.get("first_seen", now), "last_alert": now, "last_price": price}
            alerted += 1
            if gem and "NEW" in trigger:
                total_gems += 1
            time.sleep(0.3)

    # Also scan WC keywords to catch tokens missed by new pairs
    if settings["wc_mode"]:
        for kw in WC_KEYWORDS:
            for pair in dex_search(kw):
                chain = pair.get("chainId", "")
                if chain not in settings["chains"]:
                    continue
                address = (pair.get("baseToken") or {}).get("address", "")
                if not address or address in checked:
                    continue
                checked.add(address)
                sym = (pair.get("baseToken") or {}).get("symbol", "").upper()
                if sym in {"USDT","USDC","BUSD","DAI","WETH","WBNB","WSOL","ETH","BNB","SOL"}:
                    continue
                liq = (pair.get("liquidity") or {}).get("usd", 0) or 0
                if liq < settings["min_liq"]:
                    continue
                ch_h1 = (pair.get("priceChange") or {}).get("h1", 0) or 0
                price = float(pair.get("priceUsd") or 0)
                prev  = seen.get(address, {})
                now   = time.time()
                last_alert = prev.get("last_alert", 0)
                trigger = None
                if address not in seen:
                    trigger = "🆕 NEW WC TOKEN DETECTED"
                elif abs(ch_h1) >= settings["threshold"] and (now - last_alert) > 1800:
                    direction = "🚀 PUMPING" if ch_h1 > 0 else "💀 DUMPING"
                    trigger   = f"{direction} {ch_h1:+.1f}% in 1h"
                if not trigger:
                    seen[address] = {**prev, "last_price": price}
                    continue
                mc  = pair.get("marketCap") or pair.get("fdv") or 0
                gem = settings["gem_mc_min"] <= mc <= settings["gem_mc_max"]
                verdict, flags, score = rug_score(pair, gem_mode=gem)
                if score < settings["min_rug_score"] and "NEW" not in trigger:
                    seen[address] = {**prev, "last_price": price, "last_alert": now}
                    continue
                broadcast(format_alert(pair, trigger, verdict, flags, score, is_wc=True, is_gem=gem), chart_url(pair))
                seen[address] = {"first_seen": prev.get("first_seen", now), "last_alert": now, "last_price": price}
                alerted += 1
                time.sleep(0.3)

    total_alerts += alerted
    total_scans  += 1
    log.info(f"Scan done — {len(checked)} checked, {alerted} alerts sent")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("Alpha Bot v5 starting...")
    broadcast(
        "🤖 <b>Alpha Bot v5 is LIVE!</b>\n"
        "⚽ WC Scanner + 💎 Gem Hunter + 🛡 Rug Score\n"
        "Scanning ALL new token creations from launch\n"
        "Solana, BSC, Base and ETH\n\n"
        "Send /help to see all commands 👇"
    )
    while True:
        try:
            poll_commands()
            scan()
        except Exception as e:
            log.error(f"Main loop error: {e}")
        time.sleep(settings["interval"])


if __name__ == "__main__":
    main()
