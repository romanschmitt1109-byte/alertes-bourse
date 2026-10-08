"""
Scanner boursier — détecte, sur TOUT le NASDAQ et le S&P 500, les actions qui
prennent +10% (ou plus) dans la journée, et publie les résultats dans un
fichier JSON (docs/data.json) consommé par un site statique (GitHub Pages).

Configuration via variables d'environnement :
  GAIN_THRESHOLD_PCT   -> optionnel, défaut 10 (en %)

Sources de données (gratuites, sans clé API) :
  - Liste complète du NASDAQ : miroir GitHub des données officielles NASDAQ
  - Liste du S&P 500 : Wikipedia
  - Cours : Yahoo Finance (yfinance), téléchargés par lots

Note sur les catégories : le classement se fait par bourse de cotation réelle
(ex. "NasdaqGS", "NYSE", "NYSE Arca"...), telle que renvoyée par Yahoo Finance
pour chaque titre — pas par indice (Nasdaq/S&P 500 servent uniquement de
listes de tickers à scanner, pas de catégories d'affichage).
"""

import io
import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
import pandas as pd
import yfinance as yf
from deep_translator import GoogleTranslator

GAIN_THRESHOLD_PCT = float(os.environ.get("GAIN_THRESHOLD_PCT", 10))
BATCH_SIZE = 150          # nb de tickers par requête groupée
PAUSE_BETWEEN_BATCHES = 2  # secondes, pour ménager l'API gratuite

# --- Filtres "qualité" — appliqués seulement aux titres qui ont déjà pris 10%+
# But : éliminer le bruit (penny stocks, micro-caps illiquides) pour ne garder
# que des entreprises avec une taille et une liquidité réelles.
PRICE_MIN = float(os.environ.get("PRICE_MIN", 5))              # $ — exclut les penny stocks
MARKET_CAP_MIN = float(os.environ.get("MARKET_CAP_MIN", 300_000_000))  # 300M$ — exclut les micro/nano-caps
AVG_VOLUME_MIN = float(os.environ.get("AVG_VOLUME_MIN", 200_000))      # titres/jour — assure la liquidité

# Nombre maximum de titres détaillés (avec news) publiés sur le site
MAX_SHOWN = 50

# Où écrire le résultat pour le site statique (GitHub Pages sert /docs)
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "docs")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "data.json")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def get_nasdaq_tickers():
    """Liste complète des titres cotés au NASDAQ, via un miroir GitHub mis à
    jour chaque nuit à partir des données officielles NASDAQ (contourne le
    blocage du serveur ftp.nasdaqtrader.com envers les IP des runners GitHub)."""
    urls = [
        "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/nasdaq/nasdaq_tickers.txt",
        "https://raw.githubusercontent.com/deltaray-io/US-Stock-Symbols/main/nasdaq/nasdaq_tickers.txt",
    ]
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            tickers = [t.strip() for t in resp.text.splitlines() if t.strip()]
            if tickers:
                return tickers
        except Exception as e:
            print(f"Source NASDAQ {url} indisponible ({e}), essai suivant...")
    print("Impossible de récupérer la liste NASDAQ complète depuis toutes les sources.")
    return []


def get_sp500_tickers():
    try:
        resp = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers=HEADERS, timeout=20,
        )
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text))
        return tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
    except Exception as e:
        print(f"Impossible de récupérer le S&P 500 ({e}).")
        return []


def scan_batch(tickers):
    """Télécharge les cours d'un lot de tickers et renvoie les hausses >= seuil."""
    winners = []
    try:
        data = yf.download(
            tickers, period="5d", interval="1d",
            group_by="ticker", threads=True, progress=False,
        )
    except Exception as e:
        print(f"Erreur de téléchargement du lot: {e}")
        return winners

    for ticker in tickers:
        try:
            closes = data[ticker]["Close"].dropna() if len(tickers) > 1 else data["Close"].dropna()
            if len(closes) < 2:
                continue
            prev_close, last_close = closes.iloc[-2], closes.iloc[-1]
            pct_change = (last_close - prev_close) / prev_close * 100
            if pct_change >= GAIN_THRESHOLD_PCT:
                winners.append({
                    "ticker": ticker,
                    "price": round(float(last_close), 2),
                    "pct_change": round(float(pct_change), 1),
                })
        except Exception:
            continue
    return winners


def passes_quality_filters(ticker, price):
    """Vérifie prix, capitalisation et volume moyen, et renvoie aussi le nom
    de l'entreprise, sa bourse de cotation réelle, son secteur d'activité,
    sa capitalisation, son volume du jour et son résumé d'activité (en
    anglais, pas encore traduit) — tout ça récupéré au passage, sans requête
    supplémentaire. Appelé seulement sur les titres déjà repérés en hausse de
    10%+, pour limiter le nombre d'appels.
    Renvoie un dict avec les clés : ok, name, exchange, sector, market_cap,
    day_volume, avg_volume, summary_raw."""
    empty = {
        "ok": False, "name": None, "exchange": None, "sector": None,
        "market_cap": None, "day_volume": None, "avg_volume": None, "summary_raw": None,
    }
    if price < PRICE_MIN:
        return empty
    try:
        info = yf.Ticker(ticker).info
        market_cap = info.get("marketCap") or 0
        avg_volume = info.get("averageVolume") or info.get("averageDailyVolume10Day") or 0
        day_volume = info.get("volume") or info.get("regularMarketVolume") or None
        name = info.get("shortName") or info.get("longName")
        exchange = info.get("fullExchangeName") or info.get("exchange") or "Bourse inconnue"
        sector = info.get("sector") or info.get("industry") or "Secteur inconnu"
        summary_raw = info.get("longBusinessSummary")
        result = {
            "ok": False, "name": name, "exchange": exchange, "sector": sector,
            "market_cap": market_cap or None, "day_volume": day_volume,
            "avg_volume": avg_volume or None, "summary_raw": summary_raw,
        }
        if market_cap < MARKET_CAP_MIN:
            return result
        if avg_volume < AVG_VOLUME_MIN:
            return result
        result["ok"] = True
        return result
    except Exception:
        return empty  # par prudence, on écarte si l'info n'est pas récupérable


def translate_summary(summary_raw):
    """Traduit et tronque le résumé d'activité d'une entreprise (en anglais
    sur Yahoo Finance) pour un affichage compact en français."""
    if not summary_raw:
        return None
    text = summary_raw
    if len(text) > 500:
        text = text[:497] + "..."
    try:
        text = GoogleTranslator(source="auto", target="fr").translate(text)
    except Exception:
        pass  # si la traduction échoue, on garde le résumé original en anglais
    if len(text) > 320:
        text = text[:317] + "..."
    return text


def series_to_points(hist):
    """Convertit une série de cours yfinance en liste de points {t, c} —
    horodatage ISO (UTC) et cours de clôture — exploitable côté site pour
    tracer un graphique sur différentes périodes."""
    closes = hist["Close"].dropna()
    if len(closes) < 2:
        return []
    points = []
    for ts, c in closes.items():
        try:
            t_iso = ts.tz_convert("UTC").isoformat()
        except Exception:
            t_iso = ts.isoformat()
        points.append({"t": t_iso, "c": round(float(c), 2)})
    return points


def get_price_charts(ticker):
    """Récupère deux historiques de cours pour un titre :
    - "intraday" : 5 jours, par pas d'1h (court terme : 1 jour / 5 jours)
    - "daily"    : 1 an, par pas d'1 jour (long terme : 1 mois / 6 mois / 1 an)
    Appelé seulement sur les titres affichés sur le site (peu nombreux), donc
    deux requêtes dédiées par titre restent raisonnables."""
    try:
        tk = yf.Ticker(ticker)
        intraday = series_to_points(tk.history(period="5d", interval="60m"))
        daily = series_to_points(tk.history(period="1y", interval="1d"))
        return {"intraday": intraday, "daily": daily}
    except Exception:
        return {"intraday": [], "daily": []}


def compute_risk_badges(w):
    """Calcule des repères factuels (volatilité, taille, liquidité, actu) sur
    un titre — jamais un verdict ou une note globale, juste des constats
    neutres pour aider à juger soi-même le niveau de risque d'un pari
    spéculatif. 'level' : 1 = neutre/info, 2 = à surveiller, 3 = à surveiller
    de près. Triés du plus au moins préoccupant."""
    badges = []

    # Volatilité : écart-type des variations quotidiennes sur les ~30
    # dernières séances (à partir de l'historique déjà récupéré pour le
    # graphique, donc sans requête supplémentaire).
    daily_points = (w.get("chart") or {}).get("daily") or []
    closes = [p["c"] for p in daily_points][-31:]
    if len(closes) >= 10:
        returns = [
            (closes[i] - closes[i - 1]) / closes[i - 1] * 100
            for i in range(1, len(closes)) if closes[i - 1]
        ]
        if returns:
            mean = sum(returns) / len(returns)
            variance = sum((r - mean) ** 2 for r in returns) / len(returns)
            volatility = variance ** 0.5
            if volatility >= 6:
                badges.append({"label": "Forte volatilité", "level": 3})
            elif volatility >= 3:
                badges.append({"label": "Volatilité modérée", "level": 2})
            else:
                badges.append({"label": "Volatilité faible", "level": 1})

    market_cap = w.get("market_cap")
    if market_cap:
        if market_cap < 2_000_000_000:
            badges.append({"label": "Petite capitalisation", "level": 2})
        elif market_cap < 10_000_000_000:
            badges.append({"label": "Capitalisation moyenne", "level": 1})
        else:
            badges.append({"label": "Grande capitalisation", "level": 1})

    day_volume = w.get("day_volume")
    avg_volume = w.get("avg_volume")
    if day_volume and avg_volume:
        ratio = day_volume / avg_volume
        if ratio <= 0.3:
            badges.append({"label": "Faible liquidité du jour", "level": 3})
        elif ratio >= 3:
            badges.append({"label": "Volume très inhabituel", "level": 2})
        else:
            badges.append({"label": "Volume dans la normale", "level": 1})

    if w.get("news") and w["news"].get("title"):
        badges.append({"label": "Actu identifiée", "level": 1})
    else:
        badges.append({"label": "Pas d'actu identifiée", "level": 2})

    badges.sort(key=lambda b: -b["level"])
    return badges


def enrich_ticker(ticker):
    """Construit une fiche complète et à jour pour un ticker donné (prix,
    variation, capitalisation, volume, secteur, graphique, actu, résumé),
    sans exiger qu'il dépasse le seuil de hausse du jour. Utilisé pour
    rafraîchir les derniers signaux connus (prix/graphique à la dernière
    clôture) les jours où aucun nouveau signal n'est détecté."""
    try:
        tk = yf.Ticker(ticker)
        info = tk.info
        daily_hist = tk.history(period="5d", interval="1d")
        closes = daily_hist["Close"].dropna()
        if len(closes) < 1:
            return None
        last_close = float(closes.iloc[-1])
        if len(closes) >= 2:
            prev_close = float(closes.iloc[-2])
            pct_change = round((last_close - prev_close) / prev_close * 100, 1)
        else:
            pct_change = 0.0
        row = {
            "ticker": ticker,
            "price": round(last_close, 2),
            "pct_change": pct_change,
            "name": info.get("shortName") or info.get("longName") or ticker,
            "category": info.get("fullExchangeName") or info.get("exchange") or "Bourse inconnue",
            "sector": info.get("sector") or info.get("industry") or "Secteur inconnu",
            "market_cap": info.get("marketCap") or None,
            "day_volume": info.get("volume") or info.get("regularMarketVolume") or None,
            "avg_volume": info.get("averageVolume") or info.get("averageDailyVolume10Day") or None,
        }
        row["news"] = get_news_snippet(ticker)
        row["chart"] = get_price_charts(ticker)
        row["summary"] = translate_summary(info.get("longBusinessSummary"))
        row["badges"] = compute_risk_badges(row)
        row.pop("avg_volume", None)
        return row
    except Exception:
        return None


def get_news_snippet(ticker):
    """Récupère le titre de l'actualité la plus récente pour un ticker (gratuit,
    via Yahoo Finance), traduit en français. Retourne None si rien n'est trouvé."""
    try:
        news = yf.Ticker(ticker).news
        if not news:
            return None
        item = news[0]
        # yfinance structure les news sous 'content' depuis les versions récentes
        content = item.get("content", item)
        title = content.get("title")
        link = (content.get("canonicalUrl") or {}).get("url") or content.get("link")
        if not title:
            return None
        try:
            title = GoogleTranslator(source="auto", target="fr").translate(title)
        except Exception:
            pass  # si la traduction échoue, on garde le titre original en anglais
        return {"title": title, "link": link}
    except Exception:
        return None


def chunked(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def load_existing():
    """Relit le data.json déjà publié (s'il existe), pour pouvoir reporter les
    résultats de la veille d'un scan à l'autre."""
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def compute_previous_day(old_data, today_str):
    """Détermine ce qu'il faut garder comme "résultats de la veille" :
    - si le dernier data.json publié date d'un autre jour et contenait des
      résultats, on les archive comme "veille" ;
    - sinon (même jour, ou dernier scan vide), on reporte simplement la
      "veille" déjà enregistrée, pour ne jamais la perdre à cause d'un scan
      du jour qui ne trouve rien."""
    if not old_data:
        return None
    old_generated_at = old_data.get("generated_at") or ""
    old_date = old_generated_at.split(" ")[0] if old_generated_at else None
    if old_date and old_date != today_str and old_data.get("results"):
        return {
            "date": old_date,
            "generated_at": old_generated_at,
            "total_quality": old_data.get("total_quality"),
            "results": old_data["results"],
        }
    return old_data.get("previous_day")


def write_results(shown, total_scanned, total_risers, total_quality, now_dt, old_data):
    """Écrit le JSON consommé par le site statique (docs/index.html), en
    conservant les résultats de la veille, et surtout les "derniers signaux
    connus" (last_signal) : le dernier jeu de titres ayant déclenché un
    signal, réaffiché et rafraîchi (prix, graphique) tant qu'aucun nouveau
    signal n'est trouvé — pour que le site ne soit jamais vide."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    today_str = now_dt.strftime("%d/%m/%Y")
    previous_day = compute_previous_day(old_data, today_str)

    if shown:
        last_signal = {
            "date": today_str,
            "generated_at": now_dt.strftime("%d/%m/%Y %H:%M"),
            "results": shown,
        }
    else:
        old_last_signal = (old_data or {}).get("last_signal")
        last_signal = old_last_signal
        if old_last_signal and old_last_signal.get("results"):
            tickers = [w["ticker"] for w in old_last_signal["results"]]
            refreshed = []
            for t in tickers:
                row = enrich_ticker(t)
                if row:
                    refreshed.append(row)
            if refreshed:
                refreshed.sort(key=lambda w: w["pct_change"], reverse=True)
                last_signal = {
                    "date": old_last_signal.get("date"),
                    "generated_at": old_last_signal.get("generated_at"),
                    "results": refreshed,
                }

    payload = {
        "generated_at": now_dt.strftime("%d/%m/%Y %H:%M"),
        "generated_at_iso": now_dt.isoformat(),
        "threshold_pct": GAIN_THRESHOLD_PCT,
        "total_scanned": total_scanned,
        "total_risers": total_risers,
        "total_quality": total_quality,
        "total_shown": len(shown),
        "results": shown,
        "previous_day": previous_day,
        "last_signal": last_signal,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Résultats écrits dans {OUTPUT_FILE} ({len(shown)} titre(s)).")


def main():
    nasdaq_tickers = get_nasdaq_tickers()
    sp500_tickers = get_sp500_tickers()
    tickers = sorted(set(nasdaq_tickers + sp500_tickers))

    now_dt = datetime.now(ZoneInfo("Europe/Paris"))
    old_data = load_existing()

    if not tickers:
        print("Aucun ticker récupéré, arrêt.")
        write_results([], 0, 0, 0, now_dt, old_data)
        return

    print(f"Scan de {len(tickers)} titres (seuil: +{GAIN_THRESHOLD_PCT}%)...")

    all_winners = []
    batches = list(chunked(tickers, BATCH_SIZE))
    for i, batch in enumerate(batches):
        print(f"  Lot {i + 1}/{len(batches)} ({len(batch)} titres)...")
        all_winners.extend(scan_batch(batch))
        time.sleep(PAUSE_BETWEEN_BATCHES)

    if not all_winners:
        print(f"RAS : aucune action n'a pris {GAIN_THRESHOLD_PCT}% ou plus.")
        write_results([], len(tickers), 0, 0, now_dt, old_data)
        return

    print(f"{len(all_winners)} titre(s) en hausse de {GAIN_THRESHOLD_PCT}%+, application des filtres qualité...")

    quality_winners = []
    for w in all_winners:
        q = passes_quality_filters(w["ticker"], w["price"])
        if q["ok"]:
            w["name"] = q["name"] or w["ticker"]
            w["category"] = q["exchange"] or "Bourse inconnue"
            w["sector"] = q["sector"] or "Secteur inconnu"
            w["market_cap"] = q["market_cap"]
            w["day_volume"] = q["day_volume"]
            w["avg_volume"] = q["avg_volume"]
            w["_summary_raw"] = q["summary_raw"]  # traduit plus bas, seulement pour les titres affichés
            quality_winners.append(w)

    if not quality_winners:
        print(f"{len(all_winners)} titre(s) en hausse mais aucun ne passe les filtres qualité.")
        write_results([], len(tickers), len(all_winners), 0, now_dt, old_data)
        return

    # On garde tous les titres qui passent les filtres qualité (triés par
    # % de hausse), mais on ne va chercher une actu que pour les MAX_SHOWN
    # premiers, pour limiter le nombre de requêtes.
    quality_winners.sort(key=lambda w: w["pct_change"], reverse=True)
    shown = quality_winners[:MAX_SHOWN]
    for w in shown:
        w["news"] = get_news_snippet(w["ticker"])
        w["chart"] = get_price_charts(w["ticker"])
        w["summary"] = translate_summary(w.pop("_summary_raw", None))
        w["badges"] = compute_risk_badges(w)
        w.pop("avg_volume", None)

    # Les titres qualité non affichés gardent quand même leur brouillon de
    # résumé en interne ; on le retire pour ne pas le publier non traduit.
    for w in quality_winners:
        w.pop("_summary_raw", None)
        w.pop("avg_volume", None)

    write_results(shown, len(tickers), len(all_winners), len(quality_winners), now_dt, old_data)


if __name__ == "__main__":
    sys.exit(main())
