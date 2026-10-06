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
    de l'entreprise, sa bourse de cotation réelle et son secteur d'activité
    (récupérés au passage, sans requête supplémentaire). Appelé seulement sur
    les titres déjà repérés en hausse de 10%+, pour limiter le nombre d'appels.
    Renvoie (passe: bool, nom_entreprise: str | None, bourse: str | None,
    secteur: str | None)."""
    if price < PRICE_MIN:
        return False, None, None, None
    try:
        info = yf.Ticker(ticker).info
        market_cap = info.get("marketCap") or 0
        avg_volume = info.get("averageVolume") or info.get("averageDailyVolume10Day") or 0
        name = info.get("shortName") or info.get("longName")
        exchange = info.get("fullExchangeName") or info.get("exchange") or "Bourse inconnue"
        sector = info.get("sector") or info.get("industry") or "Secteur inconnu"
        if market_cap < MARKET_CAP_MIN:
            return False, name, exchange, sector
        if avg_volume < AVG_VOLUME_MIN:
            return False, name, exchange, sector
        return True, name, exchange, sector
    except Exception:
        return False, None, None, None  # par prudence, on écarte si l'info n'est pas récupérable


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


def write_results(shown, total_scanned, total_risers, total_quality, now_dt):
    """Écrit le JSON consommé par le site statique (docs/index.html)."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    payload = {
        "generated_at": now_dt.strftime("%d/%m/%Y %H:%M"),
        "generated_at_iso": now_dt.isoformat(),
        "threshold_pct": GAIN_THRESHOLD_PCT,
        "total_scanned": total_scanned,
        "total_risers": total_risers,
        "total_quality": total_quality,
        "total_shown": len(shown),
        "results": shown,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Résultats écrits dans {OUTPUT_FILE} ({len(shown)} titre(s)).")


def main():
    nasdaq_tickers = get_nasdaq_tickers()
    sp500_tickers = get_sp500_tickers()
    tickers = sorted(set(nasdaq_tickers + sp500_tickers))

    now_dt = datetime.now(ZoneInfo("Europe/Paris"))

    if not tickers:
        print("Aucun ticker récupéré, arrêt.")
        write_results([], 0, 0, 0, now_dt)
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
        write_results([], len(tickers), 0, 0, now_dt)
        return

    print(f"{len(all_winners)} titre(s) en hausse de {GAIN_THRESHOLD_PCT}%+, application des filtres qualité...")

    quality_winners = []
    for w in all_winners:
        ok, name, exchange, sector = passes_quality_filters(w["ticker"], w["price"])
        if ok:
            w["name"] = name or w["ticker"]
            w["category"] = exchange or "Bourse inconnue"
            w["sector"] = sector or "Secteur inconnu"
            quality_winners.append(w)

    if not quality_winners:
        print(f"{len(all_winners)} titre(s) en hausse mais aucun ne passe les filtres qualité.")
        write_results([], len(tickers), len(all_winners), 0, now_dt)
        return

    # On garde tous les titres qui passent les filtres qualité (triés par
    # % de hausse), mais on ne va chercher une actu que pour les MAX_SHOWN
    # premiers, pour limiter le nombre de requêtes.
    quality_winners.sort(key=lambda w: w["pct_change"], reverse=True)
    shown = quality_winners[:MAX_SHOWN]
    for w in shown:
        w["news"] = get_news_snippet(w["ticker"])

    write_results(shown, len(tickers), len(all_winners), len(quality_winners), now_dt)


if __name__ == "__main__":
    sys.exit(main())

          
