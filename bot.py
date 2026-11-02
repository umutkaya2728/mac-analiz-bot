import os
import math
import time
import html
import asyncio
import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("macbot")

# ----------------------------------------------------------------------------
# AYARLAR (Render > Environment bölümünden girilir)
# ----------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
API_KEY = os.environ["API_FOOTBALL_KEY"]
PORT = int(os.environ.get("PORT", "10000"))
TZ = ZoneInfo("Europe/Istanbul")

# Takip edilen ligler (API-Football lig ID'leri). İstersen LEAGUES env ile değiştir.
# 203 Süper Lig, 39 Premier League, 140 La Liga, 135 Serie A, 78 Bundesliga,
# 61 Ligue 1, 2 Şampiyonlar Ligi, 3 Avrupa Ligi, 848 Konferans Ligi
DEFAULT_LEAGUES = "203,39,140,135,78,61,2,3,848"
LEAGUES = {int(x) for x in os.environ.get("LEAGUES", DEFAULT_LEAGUES).split(",") if x.strip()}

API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

# ----------------------------------------------------------------------------
# KÜÇÜK ÖNBELLEK (ücretsiz plan günde 100 istek verir, istekleri tasarruflu kullan)
# ----------------------------------------------------------------------------
_cache = {}
_cache_lock = threading.Lock()


def api_get(path, params, ttl=6 * 3600):
    key = (path, tuple(sorted(params.items())))
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    r = requests.get(API_BASE + path, headers=HEADERS, params=params, timeout=25)
    r.raise_for_status()
    data = r.json()
    errors = data.get("errors")
    if errors:
        raise RuntimeError(f"API hatası: {errors}")
    resp = data.get("response", [])
    with _cache_lock:
        _cache[key] = (now, resp)
    return resp


# ----------------------------------------------------------------------------
# ANALİZ MOTORU
# ----------------------------------------------------------------------------
AVG_GOALS = 1.35       # takım başına ortalama gol (genel futbol ortalaması)
HOME_BOOST = 1.08
AWAY_FACTOR = 0.94
DECAY = 0.85           # yakın maçlar daha ağırlıklı
MAX_GOALS = 8


def team_form(team_id, last=10):
    """Takımın son maçlarından ağırlıklı atılan/yenilen gol ve puan formu."""
    fixtures = api_get("/fixtures", {"team": team_id, "last": last})
    fixtures = [f for f in fixtures if f["fixture"]["status"]["short"] in ("FT", "AET", "PEN")]
    fixtures.sort(key=lambda f: f["fixture"]["timestamp"], reverse=True)
    w_sum = gf = ga = pts = 0.0
    wins = draws = losses = 0
    for i, f in enumerate(fixtures):
        w = DECAY ** i
        home = f["teams"]["home"]["id"] == team_id
        g_for = f["goals"]["home"] if home else f["goals"]["away"]
        g_against = f["goals"]["away"] if home else f["goals"]["home"]
        if g_for is None or g_against is None:
            continue
        w_sum += w
        gf += w * g_for
        ga += w * g_against
        if g_for > g_against:
            pts += 3 * w
            wins += 1
        elif g_for == g_against:
            pts += 1 * w
            draws += 1
        else:
            losses += 1
    n = wins + draws + losses
    if w_sum == 0:
        return None
    return {
        "gf": gf / w_sum,
        "ga": ga / w_sum,
        "ppg": pts / w_sum,
        "n": n,
        "w": wins,
        "d": draws,
        "l": losses,
    }


def h2h_info(home_id, away_id):
    fixtures = api_get("/fixtures/headtohead", {"h2h": f"{home_id}-{away_id}", "last": 10})
    fixtures = [f for f in fixtures if f["fixture"]["status"]["short"] in ("FT", "AET", "PEN")]
    hw = aw = dr = 0
    diff_sum = 0.0
    n = 0
    for f in fixtures:
        gh, ga = f["goals"]["home"], f["goals"]["away"]
        if gh is None or ga is None:
            continue
        home_is_ours = f["teams"]["home"]["id"] == home_id
        our, their = (gh, ga) if home_is_ours else (ga, gh)
        diff_sum += our - their
        n += 1
        if our > their:
            hw += 1
        elif our == their:
            dr += 1
        else:
            aw += 1
    return {"n": n, "hw": hw, "dr": dr, "aw": aw, "avg_diff": (diff_sum / n) if n else 0.0}


def injuries_info(fixture_id, home_id, away_id):
    try:
        data = api_get("/injuries", {"fixture": fixture_id}, ttl=3 * 3600)
    except Exception as e:  # noqa: BLE001
        log.warning("Sakatlık verisi alınamadı: %s", e)
        return {"home": [], "away": []}
    out = {"home": [], "away": []}
    seen = set()
    for it in data:
        pid = it["player"].get("id")
        tid = it["team"]["id"]
        if (pid, tid) in seen:
            continue
        seen.add((pid, tid))
        entry = f'{it["player"].get("name", "?")} ({it["player"].get("reason") or it["player"].get("type") or "yok"})'
        if tid == home_id:
            out["home"].append(entry)
        elif tid == away_id:
            out["away"].append(entry)
    return out


def poisson_pmf(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def build_probs(lam_h, lam_a):
    p_home = p_draw = p_away = 0.0
    over25 = btts = 0.0
    scores = []
    for i in range(MAX_GOALS + 1):
        pi = poisson_pmf(i, lam_h)
        for j in range(MAX_GOALS + 1):
            p = pi * poisson_pmf(j, lam_a)
            scores.append(((i, j), p))
            if i > j:
                p_home += p
            elif i == j:
                p_draw += p
            else:
                p_away += p
            if i + j >= 3:
                over25 += p
            if i > 0 and j > 0:
                btts += p
    total = p_home + p_draw + p_away
    p_home, p_draw, p_away = p_home / total, p_draw / total, p_away / total
    scores.sort(key=lambda x: x[1], reverse=True)
    return {
        "1": p_home,
        "X": p_draw,
        "2": p_away,
        "over25": over25 / total,
        "under25": 1 - over25 / total,
        "btts": btts / total,
        "nobtts": 1 - btts / total,
        "top_scores": [(s, p / total) for s, p in scores[:3]],
    }


def analyze_fixture(fix):
    fid = fix["fixture"]["id"]
    home = fix["teams"]["home"]
    away = fix["teams"]["away"]

    hf = team_form(home["id"])
    af = team_form(away["id"])
    if not hf or not af:
        raise RuntimeError("Yeterli geçmiş maç verisi bulunamadı.")
    h2h = h2h_info(home["id"], away["id"])
    inj = injuries_info(fid, home["id"], away["id"])

    # Beklenen goller (Poisson lambda)
    lam_h = AVG_GOALS * (hf["gf"] / AVG_GOALS) * (af["ga"] / AVG_GOALS) * HOME_BOOST
    lam_a = AVG_GOALS * (af["gf"] / AVG_GOALS) * (hf["ga"] / AVG_GOALS) * AWAY_FACTOR

    # H2H düzeltmesi (en fazla %10)
    if h2h["n"] >= 3:
        adj = clamp(h2h["avg_diff"] * 0.05, -0.10, 0.10)
        lam_h *= 1 + adj
        lam_a *= 1 - adj

    # Eksik oyuncu düzeltmesi: eksik başına %3, en fazla %15 hücum kaybı
    pen_h = min(0.15, 0.03 * len(inj["home"]))
    pen_a = min(0.15, 0.03 * len(inj["away"]))
    lam_h *= 1 - pen_h
    lam_a *= 1 - pen_a
    lam_h *= 1 + pen_a * 0.5   # rakibin eksikleri savunmayı zayıflatır
    lam_a *= 1 + pen_h * 0.5

    lam_h = clamp(lam_h, 0.25, 3.5)
    lam_a = clamp(lam_a, 0.25, 3.5)

    probs = build_probs(lam_h, lam_a)

    # Güven skoru: örneklem büyüklüğü + favori farkı
    sample = min(hf["n"], af["n"]) / 10
    top = max(probs["1"], probs["X"], probs["2"])
    conf = clamp(35 + sample * 25 + (top - 0.40) * 100, 20, 85)

    return {
        "home": home["name"],
        "away": away["name"],
        "league": fix["league"]["name"],
        "time": datetime.fromtimestamp(fix["fixture"]["timestamp"], TZ).strftime("%d.%m %H:%M"),
        "hf": hf,
        "af": af,
        "h2h": h2h,
        "inj": inj,
        "lam": (lam_h, lam_a),
        "probs": probs,
        "conf": conf,
    }


def pct(p):
    return f"{p * 100:.1f}%"


def odd(p):
    return f"{1 / p:.2f}" if p > 0.001 else "-"


def esc(s):
    return html.escape(str(s))


def format_report(r):
    p = r["probs"]
    hf, af, h2h, inj = r["hf"], r["af"], r["h2h"], r["inj"]
    lines = [
        f"⚽ <b>{esc(r['home'])} - {esc(r['away'])}</b>",
        f"🏆 {esc(r['league'])} · 🕒 {r['time']}",
        "",
        "📊 <b>Maç Sonucu (olasılık / adil oran)</b>",
        f"MS 1 ({esc(r['home'])}): <b>{pct(p['1'])}</b> · {odd(p['1'])}",
        f"MS X (Beraberlik): <b>{pct(p['X'])}</b> · {odd(p['X'])}",
        f"MS 2 ({esc(r['away'])}): <b>{pct(p['2'])}</b> · {odd(p['2'])}",
        f"1X: {pct(p['1'] + p['X'])} · X2: {pct(p['X'] + p['2'])} · 12: {pct(p['1'] + p['2'])}",
        "",
        "🎯 <b>Gol Piyasaları</b>",
        f"2.5 Üst: <b>{pct(p['over25'])}</b> · {odd(p['over25'])}",
        f"2.5 Alt: <b>{pct(p['under25'])}</b> · {odd(p['under25'])}",
        f"KG Var: <b>{pct(p['btts'])}</b> · {odd(p['btts'])}",
        f"KG Yok: <b>{pct(p['nobtts'])}</b> · {odd(p['nobtts'])}",
        f"Beklenen gol: {r['lam'][0]:.2f} - {r['lam'][1]:.2f}",
        "",
        "🔢 <b>En olası skorlar</b>",
    ]
    for (i, j), sp in p["top_scores"]:
        lines.append(f"{i}-{j}: {pct(sp)}")
    lines += [
        "",
        "📈 <b>Son form (son 10 maç, yakın maç ağırlıklı)</b>",
        f"{esc(r['home'])}: {hf['w']}G {hf['d']}B {hf['l']}M · atılan {hf['gf']:.2f} / yenilen {hf['ga']:.2f}",
        f"{esc(r['away'])}: {af['w']}G {af['d']}B {af['l']}M · atılan {af['gf']:.2f} / yenilen {af['ga']:.2f}",
        "",
        "🤝 <b>Geçmiş karşılaşmalar</b>",
    ]
    if h2h["n"]:
        lines.append(
            f"Son {h2h['n']} maç: {esc(r['home'])} {h2h['hw']} · Beraberlik {h2h['dr']} · {esc(r['away'])} {h2h['aw']}"
        )
    else:
        lines.append("Karşılaşma verisi yok.")
    lines += ["", "🩹 <b>Eksikler / sakatlıklar</b>"]
    for side, name in (("home", r["home"]), ("away", r["away"])):
        lst = inj[side]
        if lst:
            shown = ", ".join(esc(x) for x in lst[:5])
            more = f" (+{len(lst) - 5})" if len(lst) > 5 else ""
            lines.append(f"{esc(name)} [{len(lst)}]: {shown}{more}")
        else:
            lines.append(f"{esc(name)}: bildirilen eksik yok")
    lines += [
        "",
        f"🧠 Model güveni: <b>{r['conf']:.0f}/100</b>",
        "",
        "⚠️ <i>Bu istatistiksel bir tahmindir, kesinlik veya kazanç garantisi değildir. "
        "Bahis risklidir; kaybetmeyi göze alabileceğin miktarla ve 18 yaş üstü, "
        "yasal platformlarda oyna.</i>",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# TELEGRAM KOMUTLARI
# ----------------------------------------------------------------------------
def list_fixtures(day_offset):
    date = (datetime.now(TZ) + timedelta(days=day_offset)).strftime("%Y-%m-%d")
    data = api_get("/fixtures", {"date": date, "timezone": "Europe/Istanbul"}, ttl=2 * 3600)
    data = [f for f in data if f["league"]["id"] in LEAGUES]
    data.sort(key=lambda f: f["fixture"]["timestamp"])
    return date, data


async def send_list(update: Update, day_offset: int):
    msg = await update.message.reply_text("Maçlar getiriliyor...")
    try:
        date, fixtures = await asyncio.to_thread(list_fixtures, day_offset)
    except Exception as e:  # noqa: BLE001
        await msg.edit_text(f"Hata: {e}")
        return
    if not fixtures:
        await msg.edit_text(f"{date} için takip edilen liglerde maç bulunamadı.")
        return
    buttons = []
    for f in fixtures[:40]:
        t = datetime.fromtimestamp(f["fixture"]["timestamp"], TZ).strftime("%H:%M")
        label = f"{t} {f['teams']['home']['name']} - {f['teams']['away']['name']}"
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"a:{f['fixture']['id']}")])
    await msg.edit_text(
        f"📅 {date} · Analiz için bir maç seç:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Merhaba! Maç analiz botuna hoş geldin.\n\n"
        "/bugun - Bugünün maçları\n"
        "/yarin - Yarının maçları\n\n"
        "Bir maça dokun, geçmiş sonuçlar, form, eksikler ve olasılıklarla analiz çıkarayım.\n"
        "Not: Tahminler istatistikseldir, kesin sonuç garantisi vermez."
    )


async def cmd_bugun(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_list(update, 0)


async def cmd_yarin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_list(update, 1)


def run_analysis(fixture_id):
    data = api_get("/fixtures", {"id": fixture_id}, ttl=3600)
    if not data:
        raise RuntimeError("Maç bulunamadı.")
    return format_report(analyze_fixture(data[0]))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not q.data.startswith("a:"):
        return
    fid = int(q.data.split(":")[1])
    await q.message.reply_text("Analiz hazırlanıyor, birkaç saniye sürebilir...")
    try:
        text = await asyncio.to_thread(run_analysis, fid)
    except Exception as e:  # noqa: BLE001
        log.exception("analiz hatası")
        await q.message.reply_text(f"Analiz yapılamadı: {e}")
        return
    await q.message.reply_text(text, parse_mode="HTML")


# ----------------------------------------------------------------------------
# UPTIMEROBOT İÇİN CANLILIK SUNUCUSU
# ----------------------------------------------------------------------------
web = Flask(__name__)


@web.route("/")
def index():
    return "Bot çalışıyor", 200


def run_web():
    web.run(host="0.0.0.0", port=PORT)


def main():
    threading.Thread(target=run_web, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("bugun", cmd_bugun))
    app.add_handler(CommandHandler("yarin", cmd_yarin))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("Bot başlıyor...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
