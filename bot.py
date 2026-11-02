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
AF_KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()      # API-Football (Süper Lig, sakatlık)
FD_TOKEN = os.environ.get("FOOTBALL_DATA_TOKEN", "").strip()  # football-data.org (büyük ligler)
PORT = int(os.environ.get("PORT", "10000"))
TZ = ZoneInfo("Europe/Istanbul")

# API-Football'dan çekilecek ligler (football-data.org'un ücretsiz planında olmayanlar).
# 203 Süper Lig, 3 Avrupa Ligi, 848 Konferans Ligi
AF_LEAGUES = {int(x) for x in os.environ.get("AF_LEAGUES", "203,3,848").split(",") if x.strip()}

AF_BASE = "https://v3.football.api-sports.io"
FD_BASE = "https://api.football-data.org/v4"

# ----------------------------------------------------------------------------
# ÖNBELLEK + İSTEK SINIRI
# ----------------------------------------------------------------------------
_cache = {}
_lock = threading.Lock()
MATCHES = {}      # listelenen maçlar: (kaynak, id) -> maç bilgisi
_fd_calls = []    # football-data.org: dakikada 10 istek sınırı


def cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (now, val)
    return val


def af_get(path, params, ttl=6 * 3600):
    if not AF_KEY:
        raise RuntimeError("API_FOOTBALL_KEY tanımlı değil")

    def call():
        r = requests.get(AF_BASE + path, headers={"x-apisports-key": AF_KEY}, params=params, timeout=25)
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            raise RuntimeError(f"API-Football: {data['errors']}")
        return data.get("response", [])

    return cached(("af", path, tuple(sorted(params.items()))), ttl, call)


def fd_throttle():
    while True:
        with _lock:
            now = time.time()
            while _fd_calls and now - _fd_calls[0] > 60:
                _fd_calls.pop(0)
            if len(_fd_calls) < 9:
                _fd_calls.append(now)
                return
            wait = 61 - (now - _fd_calls[0])
        time.sleep(max(wait, 1))


def fd_get(path, params=None, ttl=6 * 3600):
    if not FD_TOKEN:
        raise RuntimeError("FOOTBALL_DATA_TOKEN tanımlı değil")
    params = params or {}

    def call():
        fd_throttle()
        r = requests.get(FD_BASE + path, headers={"X-Auth-Token": FD_TOKEN}, params=params, timeout=25)
        if r.status_code != 200:
            try:
                msg = r.json().get("message", r.text[:150])
            except Exception:  # noqa: BLE001
                msg = r.text[:150]
            raise RuntimeError(f"football-data.org {r.status_code}: {msg}")
        return r.json()

    return cached(("fd", path, tuple(sorted(params.items()))), ttl, call)


# ----------------------------------------------------------------------------
# VERİ NORMALLEŞTİRME (iki kaynağı aynı biçime getirir)
# sonuç: {"ts", "home_id", "away_id", "hg", "ag"}
# ----------------------------------------------------------------------------
def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def af_results(fixtures):
    out = []
    for f in fixtures:
        if f["fixture"]["status"]["short"] not in ("FT", "AET", "PEN"):
            continue
        hg, ag = f["goals"]["home"], f["goals"]["away"]
        if hg is None or ag is None:
            continue
        out.append({
            "ts": f["fixture"]["timestamp"],
            "home_id": f["teams"]["home"]["id"],
            "away_id": f["teams"]["away"]["id"],
            "hg": hg,
            "ag": ag,
        })
    return out


def fd_results(matches):
    out = []
    for m in matches:
        if m.get("status") != "FINISHED":
            continue
        ft = (m.get("score") or {}).get("fullTime") or {}
        hg, ag = ft.get("home"), ft.get("away")
        if hg is None or ag is None:
            continue
        out.append({
            "ts": parse_ts(m["utcDate"]),
            "home_id": m["homeTeam"]["id"],
            "away_id": m["awayTeam"]["id"],
            "hg": hg,
            "ag": ag,
        })
    return out


def current_season():
    now = datetime.now(TZ)
    return now.year if now.month >= 7 else now.year - 1


def recent_results(src, team_id, last=10):
    """Takımın son bitmiş maçları (sezon bazlı çekilir, son N tanesi alınır)."""
    results, errors = [], []
    season = current_season()
    for s in (season, season - 1):
        try:
            if src == "af":
                data = af_get("/fixtures", {"team": team_id, "season": s}, ttl=12 * 3600)
                results += af_results(data)
            else:
                data = fd_get(f"/teams/{team_id}/matches", {"season": s, "status": "FINISHED"}, ttl=12 * 3600)
                results += fd_results(data.get("matches", []))
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
            continue
        if len(results) >= last:
            break
    if not results and errors:
        raise RuntimeError(errors[0])
    results.sort(key=lambda r: r["ts"], reverse=True)
    return results[:last]


def get_h2h(src, m):
    try:
        if src == "af":
            data = af_get("/fixtures/headtohead", {"h2h": f"{m['home_id']}-{m['away_id']}"}, ttl=24 * 3600)
            res = af_results(data)
        else:
            data = fd_get(f"/matches/{m['id']}/head2head", {"limit": 10}, ttl=24 * 3600)
            res = fd_results(data.get("matches", []))
    except Exception as e:  # noqa: BLE001
        log.warning("H2H alınamadı: %s", e)
        return []
    res.sort(key=lambda r: r["ts"], reverse=True)
    return res[:10]


def injuries_info(fixture_id, home_id, away_id):
    """Sadece API-Football'da var. Veri yoksa None döner."""
    try:
        data = af_get("/injuries", {"fixture": fixture_id}, ttl=3 * 3600)
    except Exception as e:  # noqa: BLE001
        log.warning("Sakatlık verisi alınamadı: %s", e)
        return None
    out = {"home": [], "away": []}
    seen = set()
    for it in data:
        pid = it["player"].get("id")
        tid = it["team"]["id"]
        if (pid, tid) in seen:
            continue
        seen.add((pid, tid))
        reason = it["player"].get("reason") or it["player"].get("type") or "yok"
        entry = f'{it["player"].get("name", "?")} ({reason})'
        if tid == home_id:
            out["home"].append(entry)
        elif tid == away_id:
            out["away"].append(entry)
    return out


# ----------------------------------------------------------------------------
# ANALİZ MOTORU
# ----------------------------------------------------------------------------
AVG_GOALS = 1.35
HOME_BOOST = 1.08
AWAY_FACTOR = 0.94
DECAY = 0.85
MAX_GOALS = 8


def team_form(results, team_id):
    w_sum = gf = ga = pts = 0.0
    wins = draws = losses = 0
    for i, r in enumerate(results):
        w = DECAY ** i
        home = r["home_id"] == team_id
        g_for = r["hg"] if home else r["ag"]
        g_against = r["ag"] if home else r["hg"]
        w_sum += w
        gf += w * g_for
        ga += w * g_against
        if g_for > g_against:
            pts += 3 * w
            wins += 1
        elif g_for == g_against:
            pts += w
            draws += 1
        else:
            losses += 1
    if w_sum == 0:
        return None
    return {"gf": gf / w_sum, "ga": ga / w_sum, "ppg": pts / w_sum,
            "n": wins + draws + losses, "w": wins, "d": draws, "l": losses}


def h2h_info(results, home_id):
    hw = aw = dr = 0
    diff_sum = 0.0
    for r in results:
        our, their = (r["hg"], r["ag"]) if r["home_id"] == home_id else (r["ag"], r["hg"])
        diff_sum += our - their
        if our > their:
            hw += 1
        elif our == their:
            dr += 1
        else:
            aw += 1
    n = len(results)
    return {"n": n, "hw": hw, "dr": dr, "aw": aw, "avg_diff": (diff_sum / n) if n else 0.0}


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
        "1": p_home, "X": p_draw, "2": p_away,
        "over25": over25 / total, "under25": 1 - over25 / total,
        "btts": btts / total, "nobtts": 1 - btts / total,
        "top_scores": [(s, p / total) for s, p in scores[:3]],
    }


def fd_scorers(code):
    try:
        data = fd_get(f"/competitions/{code}/scorers", {"limit": 100}, ttl=12 * 3600)
    except Exception:  # noqa: BLE001
        try:
            data = fd_get(f"/competitions/{code}/scorers", {}, ttl=12 * 3600)
        except Exception as e:  # noqa: BLE001
            log.warning("Gol krallığı alınamadı: %s", e)
            return []
    return data.get("scorers", [])


def team_scorers(code, home_id, away_id, lam_h, lam_a):
    """Ligdeki gol listesinden iki takımın en golcü oyuncularını ve tahmini gol ihtimalini bulur."""
    out = {"home": [], "away": []}
    if not code:
        return out
    for s in fd_scorers(code):
        tid = (s.get("team") or {}).get("id")
        side = "home" if tid == home_id else ("away" if tid == away_id else None)
        if not side or len(out[side]) >= 2:
            continue
        played = s.get("playedMatches") or 0
        goals = s.get("goals") or 0
        if played <= 0 or goals <= 0:
            continue
        lam = lam_h if side == "home" else lam_a
        rate = goals / played * clamp(lam / AVG_GOALS, 0.6, 1.6)
        out[side].append({
            "name": (s.get("player") or {}).get("name", "?"),
            "goals": goals, "played": played, "p": 1 - math.exp(-rate),
        })
    return out


def analyze(m):
    src = m["src"]
    hres = recent_results(src, m["home_id"])
    ares = recent_results(src, m["away_id"])
    hf = team_form(hres, m["home_id"])
    awf = team_form(ares, m["away_id"])
    if not hf or not awf:
        raise RuntimeError("Yeterli geçmiş maç verisi bulunamadı.")
    h2h = h2h_info(get_h2h(src, m), m["home_id"])
    inj = injuries_info(m["id"], m["home_id"], m["away_id"]) if src == "af" else None

    lam_h = AVG_GOALS * (hf["gf"] / AVG_GOALS) * (awf["ga"] / AVG_GOALS) * HOME_BOOST
    lam_a = AVG_GOALS * (awf["gf"] / AVG_GOALS) * (hf["ga"] / AVG_GOALS) * AWAY_FACTOR

    if h2h["n"] >= 3:
        adj = clamp(h2h["avg_diff"] * 0.05, -0.10, 0.10)
        lam_h *= 1 + adj
        lam_a *= 1 - adj

    if inj:
        pen_h = min(0.15, 0.03 * len(inj["home"]))
        pen_a = min(0.15, 0.03 * len(inj["away"]))
        lam_h *= (1 - pen_h) * (1 + pen_a * 0.5)
        lam_a *= (1 - pen_a) * (1 + pen_h * 0.5)

    lam_h = clamp(lam_h, 0.25, 3.5)
    lam_a = clamp(lam_a, 0.25, 3.5)
    probs = build_probs(lam_h, lam_a)
    scorers = team_scorers(m.get("comp"), m["home_id"], m["away_id"], lam_h, lam_a) if src == "fd" else None

    sample = min(hf["n"], awf["n"]) / 10
    top = max(probs["1"], probs["X"], probs["2"])
    conf = clamp(35 + sample * 25 + (top - 0.40) * 100, 20, 85)

    return {
        "src": src, "home": m["home"], "away": m["away"], "league": m["league"],
        "time": datetime.fromtimestamp(m["ts"], TZ).strftime("%d.%m %H:%M"),
        "hf": hf, "af": awf, "h2h": h2h, "inj": inj,
        "lam": (lam_h, lam_a), "probs": probs, "conf": conf,
        "scorers": scorers,
        "p_score": (1 - math.exp(-lam_h), 1 - math.exp(-lam_a)),
    }


def pct(p):
    return f"{p * 100:.1f}%"


def odd(p):
    return f"{1 / p:.2f}" if p > 0.001 else "-"


def min_odd(p):
    """%5 güvenlik payıyla: bahisçi oranı bunun üstündeyse model lehine."""
    return f"{1.05 / p:.2f}" if p > 0.001 else "-"


def summary_lines(r):
    p = r["probs"]
    opts = [(f"{r['home']} kazanır", p["1"]), ("Beraberlik", p["X"]), (f"{r['away']} kazanır", p["2"])]
    best = max(opts, key=lambda x: x[1])
    (i, j), sp = p["top_scores"][0]
    ph, pa = r["p_score"]
    if best[1] < 0.42:
        head = f"Net favori yok. Hafif öndeki: <b>{esc(best[0])}</b> ({pct(best[1])})"
    else:
        head = f"Sonuç tahmini: <b>{esc(best[0])}</b> ({pct(best[1])})"
    return [
        "🔮 <b>ÖZET</b>",
        head,
        f"Kazanma: {esc(r['home'])} {pct(p['1'])} · Beraberlik {pct(p['X'])} · {esc(r['away'])} {pct(p['2'])}",
        f"Gol atma ihtimali: {esc(r['home'])} {pct(ph)} · {esc(r['away'])} {pct(pa)}",
        f"En olası skor: {i}-{j} ({pct(sp)})",
        "",
    ]


def scorer_lines(r):
    sc = r.get("scorers")
    out = ["🥅 <b>Gol atması muhtemel oyuncular</b> (sezon istatistiğine göre, kadro bilinmiyor)"]
    if sc is None:
        return out + ["Bu veri kaynağında oyuncu gol verisi yok."]
    found = False
    for side, name in (("home", r["home"]), ("away", r["away"])):
        for pl in sc[side]:
            found = True
            out.append(
                f"{esc(name)}: {esc(pl['name'])} · {pct(pl['p'])} · sezon {pl['goals']} gol / {pl['played']} maç"
            )
    if not found:
        out.append("Oyuncu verisi alınamadı (takımın golcüleri listenin ilk sıralarında değil).")
    return out


def top_picks(p):
    """Her piyasadan en olası seçeneği alır, olasılığa göre ilk 3'ü döndürür."""
    cands = []
    ms = max((("MS 1", p["1"]), ("MS X", p["X"]), ("MS 2", p["2"])), key=lambda x: x[1])
    cands.append(ms)
    if ms[1] < 0.50:  # net favori yoksa çifte şans da değerlendir
        dc = max(
            (("1X (çifte şans)", p["1"] + p["X"]), ("X2 (çifte şans)", p["X"] + p["2"]),
             ("12 (çifte şans)", p["1"] + p["2"])),
            key=lambda x: x[1],
        )
        cands.append(dc)
    cands.append(max((("2.5 Üst", p["over25"]), ("2.5 Alt", p["under25"])), key=lambda x: x[1]))
    cands.append(max((("KG Var", p["btts"]), ("KG Yok", p["nobtts"])), key=lambda x: x[1]))
    cands.sort(key=lambda x: x[1], reverse=True)
    return cands[:3]


def pick_lines(r):
    p, conf = r["probs"], r["conf"]
    picks = top_picks(p)
    out = ["🏁 <b>Model seçimleri (en olasıdan başlayarak)</b>"]
    for name, pr in picks:
        strong, mid = (0.80, 0.70) if "çifte şans" in name else (0.65, 0.55)
        tier = "güçlü" if pr >= strong else ("orta" if pr >= mid else "zayıf")
        out.append(f"{name}: <b>{pct(pr)}</b> · {tier} · adil oran {odd(pr)} · oynamak için ≥ {min_odd(pr)}")
    if all("zayıf" in l for l in out[1:]) or conf < 45:
        out.append("➡️ Net bir seçim yok, bu maçı pas geçmek mantıklı olabilir.")
    out.append("<i>En olası seçenek, her zaman iyi bahis demek değildir: oran düşükse kazandırmaz.</i>")
    return out


def esc(s):
    return html.escape(str(s))


def format_report(r):
    p = r["probs"]
    hf, af, h2h, inj = r["hf"], r["af"], r["h2h"], r["inj"]
    source = "API-Football" if r["src"] == "af" else "football-data.org"
    lines = [
        f"⚽ <b>{esc(r['home'])} - {esc(r['away'])}</b>",
        f"🏆 {esc(r['league'])} · 🕒 {r['time']}",
        "",
    ] + summary_lines(r) + [
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
    lines += [""] + scorer_lines(r)
    lines += [
        "",
        f"📈 <b>Son form (son {max(hf['n'], af['n'])} maça kadar, yakın maç ağırlıklı)</b>",
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
        lines.append("Karşılaşma verisi alınamadı.")
    lines += ["", "🩹 <b>Eksikler / sakatlıklar</b>"]
    if inj is None:
        lines.append("Bu maç için eksik oyuncu verisi yok (analize dahil edilmedi).")
    else:
        for side, name in (("home", r["home"]), ("away", r["away"])):
            lst = inj[side]
            if lst:
                shown = ", ".join(esc(x) for x in lst[:5])
                more = f" (+{len(lst) - 5})" if len(lst) > 5 else ""
                lines.append(f"{esc(name)} [{len(lst)}]: {shown}{more}")
            else:
                lines.append(f"{esc(name)}: bildirilen eksik yok")
    lines += [""] + pick_lines(r)
    lines += [
        "",
        "💡 <b>Değer eşiği</b> (bahisçi oranı bunun üstündeyse model lehine, %5 payla)",
        f"MS 1 ≥ {min_odd(p['1'])} · MS X ≥ {min_odd(p['X'])} · MS 2 ≥ {min_odd(p['2'])}",
        f"2.5 Üst ≥ {min_odd(p['over25'])} · 2.5 Alt ≥ {min_odd(p['under25'])}",
        f"KG Var ≥ {min_odd(p['btts'])} · KG Yok ≥ {min_odd(p['nobtts'])}",
        "Bahisçinin gerçek oranlarını girmek için: /oran 2.10 3.40 3.20 (1 X 2)",
        "",
        f"🧠 Model güveni: <b>{r['conf']:.0f}/100</b> · 📡 Veri: {source}",
        "",
        "⚠️ <i>Bu istatistiksel bir tahmindir, kesinlik veya kazanç garantisi değildir. "
        "Bahis risklidir; kaybetmeyi göze alabileceğin miktarla ve 18 yaş üstü, "
        "yasal platformlarda oyna.</i>",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# MAÇ LİSTESİ
# ----------------------------------------------------------------------------
def af_list(date):
    data = af_get("/fixtures", {"date": date, "timezone": "Europe/Istanbul"}, ttl=2 * 3600)
    now = time.time()
    out = []
    for f in data:
        if f["league"]["id"] not in AF_LEAGUES:
            continue
        if f["fixture"]["status"]["short"] not in ("NS", "TBD"):
            continue
        ts = f["fixture"]["timestamp"]
        if ts <= now:
            continue
        out.append({
            "src": "af", "id": f["fixture"]["id"], "ts": ts, "league": f["league"]["name"],
            "home_id": f["teams"]["home"]["id"], "home": f["teams"]["home"]["name"],
            "away_id": f["teams"]["away"]["id"], "away": f["teams"]["away"]["name"],
        })
    return out


def fd_list(date):
    d = datetime.strptime(date, "%Y-%m-%d").date()
    data = fd_get(
        "/matches",
        {"dateFrom": (d - timedelta(days=1)).isoformat(), "dateTo": (d + timedelta(days=1)).isoformat()},
        ttl=2 * 3600,
    )
    now = time.time()
    out = []
    for m in data.get("matches", []):
        if m.get("status") not in ("SCHEDULED", "TIMED"):
            continue
        h, a = m.get("homeTeam") or {}, m.get("awayTeam") or {}
        if not h.get("id") or not a.get("id"):
            continue
        ts = parse_ts(m["utcDate"])
        if ts <= now or datetime.fromtimestamp(ts, TZ).date() != d:
            continue
        out.append({
            "src": "fd", "id": m["id"], "ts": ts, "league": m["competition"]["name"],
            "comp": m["competition"].get("code"),
            "home_id": h["id"], "home": h.get("shortName") or h["name"],
            "away_id": a["id"], "away": a.get("shortName") or a["name"],
        })
    return out


def list_matches(day_offset):
    date = (datetime.now(TZ) + timedelta(days=day_offset)).strftime("%Y-%m-%d")
    matches, notes = [], []
    if AF_KEY:
        try:
            matches += af_list(date)
        except Exception as e:  # noqa: BLE001
            log.warning("API-Football liste hatası: %s", e)
            notes.append(f"⚠️ Süper Lig / Avrupa kupaları şu an alınamadı (API-Football): {e}")
    if FD_TOKEN:
        try:
            matches += fd_list(date)
        except Exception as e:  # noqa: BLE001
            log.warning("football-data liste hatası: %s", e)
            notes.append(f"⚠️ Büyük ligler şu an alınamadı (football-data.org): {e}")
    if not AF_KEY and not FD_TOKEN:
        notes.append("Hiçbir veri kaynağı anahtarı tanımlı değil.")
    matches.sort(key=lambda x: x["ts"])
    with _lock:
        for m in matches:
            MATCHES[(m["src"], m["id"])] = m
    return date, matches, notes


# ----------------------------------------------------------------------------
# TELEGRAM KOMUTLARI
# ----------------------------------------------------------------------------
async def send_list(update: Update, day_offset: int):
    msg = await update.message.reply_text("Maçlar getiriliyor...")
    try:
        date, matches, notes = await asyncio.to_thread(list_matches, day_offset)
    except Exception as e:  # noqa: BLE001
        await msg.edit_text(f"Hata: {e}")
        return
    note_text = ("\n\n" + "\n".join(notes)) if notes else ""
    if not matches:
        await msg.edit_text(f"{date} için başlamamış maç bulunamadı.{note_text}"[:4000])
        return
    buttons = []
    for m in matches[:40]:
        t = datetime.fromtimestamp(m["ts"], TZ).strftime("%H:%M")
        label = f"{t} {m['home']} - {m['away']}"
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"a:{m['src']}:{m['id']}")])
    await msg.edit_text(
        f"📅 {date} · Analiz için bir maç seç:{note_text}"[:4000],
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Merhaba! Maç analiz botuna hoş geldin.\n\n"
        "/bugun - Bugünün maçları\n"
        "/yarin - Yarının maçları\n"
        "/oran - Son analiz ettiğin maç için bahisçi oranlarını karşılaştır\n\n"
        "Bir maça dokun, geçmiş sonuçlar, form, eksikler ve olasılıklarla analiz çıkarayım.\n"
        "Not: Tahminler istatistikseldir, kesin sonuç garantisi vermez."
    )


async def cmd_bugun(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_list(update, 0)


async def cmd_yarin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_list(update, 1)


def split_message(text, limit=3800):
    """Telegram 4096 karakter sınırı için mesajı boş satırlardan böler."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for block in text.split("\n\n"):
        if cur and len(cur) + len(block) + 2 > limit:
            parts.append(cur)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    if cur:
        parts.append(cur)
    return parts


def run_analysis(m):
    r = analyze(m)
    return format_report(r), r


def value_lines(title, items):
    """items: [(ad, model_olasılığı, bahisçi_oranı)]"""
    inv = sum(1 / o for _, _, o in items)
    out = [f"<b>{title}</b>"]
    for name, p, o in items:
        implied = (1 / o) / inv          # bahisçi marjından arındırılmış olasılık
        ev = p * o - 1                   # 1 birim bahis başına beklenen getiri
        if ev >= 0.05:
            verdict = "✅ değer var"
        elif ev >= 0:
            verdict = "➖ sınırda"
        else:
            verdict = "❌ değer yok"
        out.append(
            f"{name}: oran {o:.2f} · model {pct(p)} · bahisçi {pct(implied)} · "
            f"beklenen getiri {ev * 100:+.1f}% · {verdict}"
        )
    out.append(f"Bahisçi marjı: %{(inv - 1) * 100:.1f}")
    return out


OLD_USAGE = (
    "Kullanım (önce bir maçı analiz et):\n"
    "/oran 2.10 3.40 3.20  → MS 1 / X / 2 oranları\n"
    "/oran gol 1.85 1.95  → 2.5 Üst / Alt\n"
    "/oran kg 1.70 2.05  → KG Var / Yok"
)


async def cmd_oran(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = context.user_data.get("last")
    if not r:
        await update.message.reply_text("Önce /bugun veya /yarin ile bir maçı analiz et.\n\n" + OLD_USAGE)
        return
    args = context.args or []
    kind = "ms"
    if args and args[0].lower() in ("gol", "kg"):
        kind = args[0].lower()
        args = args[1:]
    try:
        nums = [float(x.replace(",", ".")) for x in args]
    except ValueError:
        await update.message.reply_text(OLD_USAGE)
        return
    if not nums or any(n <= 1.0 for n in nums):
        await update.message.reply_text(OLD_USAGE)
        return
    p = r["probs"]
    if kind == "ms" and len(nums) == 3:
        title = "Maç Sonucu"
        items = [("MS 1", p["1"], nums[0]), ("MS X", p["X"], nums[1]), ("MS 2", p["2"], nums[2])]
    elif kind == "gol" and len(nums) == 2:
        title = "2.5 Alt/Üst"
        items = [("2.5 Üst", p["over25"], nums[0]), ("2.5 Alt", p["under25"], nums[1])]
    elif kind == "kg" and len(nums) == 2:
        title = "Karşılıklı Gol"
        items = [("KG Var", p["btts"], nums[0]), ("KG Yok", p["nobtts"], nums[1])]
    else:
        await update.message.reply_text(OLD_USAGE)
        return
    text = "\n".join(
        [f"⚽ <b>{esc(r['home'])} - {esc(r['away'])}</b>", ""]
        + value_lines(title, items)
        + [
            "",
            "ℹ️ <i>Beklenen getirinin pozitif çıkması kazanç garantisi değildir. Model basit bir "
            "istatistiktir; küçük farklar çoğu zaman gürültüdür. Kaybetmeyi göze alabileceğin "
            "miktarla oyna.</i>",
        ]
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = (q.data or "").split(":")
    if len(parts) != 3 or parts[0] != "a":
        return
    with _lock:
        m = MATCHES.get((parts[1], int(parts[2])))
    if not m:
        await q.message.reply_text("Bu liste eskimiş. Lütfen /bugun veya /yarin yazıp listeyi yenile.")
        return
    await q.message.reply_text("Analiz hazırlanıyor, birkaç saniye sürebilir...")
    try:
        text, result = await asyncio.to_thread(run_analysis, m)
    except Exception as e:  # noqa: BLE001
        log.exception("analiz hatası")
        await q.message.reply_text(f"Analiz yapılamadı: {e}")
        return
    context.user_data["last"] = result
    for part in split_message(text):
        await q.message.reply_text(part, parse_mode="HTML")


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
    app.add_handler(CommandHandler("oran", cmd_oran))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("Bot başlıyor...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
