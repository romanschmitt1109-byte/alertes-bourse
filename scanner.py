"""
Scanner boursier — détecte, sur TOUT le NASDAQ et le S&P 500, les actions qui
prennent +10% (ou plus) dans la journée, et envoie une alerte Telegram.

Configuration via variables d'environnement :
  TELEGRAM_BOT_TOKEN   -> token de ton bot Telegram
  TELEGRAM_CHAT_ID     -> id du chat où envoyer les alertes
  GAIN_THRESHOLD_PCT   -> optionnel, défaut 10 (en %)

Sources de données (gratuites, sans clé API) :
  - Liste complète du NASDAQ : ftp.nasdaqtrader.com (fichier officiel)
  - Liste du S&P 500 : Wikipedia
  - Cours : Yahoo Finance (yfinance), téléchargés par lots

Note sur les catégories : le classement se fait par bourse de cotation réelle
(ex. "NasdaqGS", "NYSE", "NYSE Arca"...), telle que renvoyée par Yahoo Finance
pour chaque titre — pas par indice (Nasdaq/S&P 500 servent uniquement de
listes de tickers à scanner, pas de catégories d'affichage).
"""

import io
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

# Ordre d'affichage préféré pour les bourses les plus courantes ; toute autre
# bourse rencontrée est affichée ensuite, par ordre alphabétique.
CATEGORY_ORDER = [
    "NasdaqGS", "Nasdaq Global Select",
    "NasdaqGM", "Nasdaq Global Market",
    "NasdaqCM", "Nasdaq Capital Market",
    "NYSE", "NYSE Arca", "NYSE American",
    "Cboe BZX", "Cboe US",
]


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
                    "price": round(last_close, 2),
                    "pct_change": round(pct_change, 1),
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


def send_telegram_message(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant — message non envoyé.")
        print(text)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
    if resp.status_code != 200:
        print(f"Échec envoi Telegram: {resp.text}")


def chunked(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def format_grouped_message(quality_winners, header_lines):
    """Groupe les titres retenus par bourse de cotation, puis par secteur
    d'activité, puis les trie par entreprise (ordre alphabétique du nom,
    ticker en repli si le nom est inconnu)."""
    lines = list(header_lines)

    by_category = {}
    for w in quality_winners:
        by_category.setdefault(w["category"], []).append(w)

    # Catégories connues dans l'ordre défini, puis toute catégorie imprévue en fin
    ordered_categories = [c for c in CATEGORY_ORDER if c in by_category]
    ordered_categories += sorted(c for c in by_category if c not in CATEGORY_ORDER)

    for category in ordered_categories:
        group = by_category[category]
        lines.append(f"\n<b>— {category} ({len(group)}) —</b>")

        by_sector = {}
        for w in group:
            by_sector.setdefault(w["sector"], []).append(w)

        for sector in sorted(by_sector.keys()):
            sector_group = sorted(by_sector[sector], key=lambda w: (w["name"] or w["ticker"]).lower())
            lines.append(f"  <i>{sector} :</i>")
            for w in sector_group:
                display_name = w["name"] or w["ticker"]
                lines.append(f"  • <b>{display_name}</b> ({w['ticker']}) : +{w['pct_change']}% ({w['price']}$)")
                news = w.get("news")
                if news:
                    title = news["title"]
                    if len(title) > 90:
                        title = title[:87] + "..."
                    lines.append(f"    📰 {title}")

    return "\n".join(lines)


def main():
    nasdaq_tickers = get_nasdaq_tickers()
    sp500_tickers = get_sp500_tickers()
    tickers = sorted(set(nasdaq_tickers + sp500_tickers))

    if not tickers:
        print("Aucun ticker récupéré, arrêt.")
        return

    print(f"Scan de {len(tickers)} titres (seuil: +{GAIN_THRESHOLD_PCT}%)...")

    all_winners = []
    batches = list(chunked(tickers, BATCH_SIZE))
    for i, batch in enumerate(batches):
        print(f"  Lot {i + 1}/{len(batches)} ({len(batch)} titres)...")
        all_winners.extend(scan_batch(batch))
        time.sleep(PAUSE_BETWEEN_BATCHES)

    now_str = datetime.now(ZoneInfo("Europe/Paris")).strftime('%d/%m/%Y %H:%M')

    if not all_winners:
        message = f"<b>✅ Scan terminé — {now_str}</b>\n<i>RAS : aucune action n'a pris {GAIN_THRESHOLD_PCT}% ou plus " \
                  f"({len(tickers)} titres scannés).</i>"
        print(message)
        send_telegram_message(message)
        return

    print(f"{len(all_winners)} titre(s) en hausse de {GAIN_THRESHOLD_PCT}%+, application des filtres qualité...")

    quality_winners = []
    for w in all_winners:
        ok, name, exchange, sector = passes_quality_filters(w["ticker"], w["price"])
        if ok:
            w["name"] = name
            w["category"] = exchange or "Bourse inconnue"
            w["sector"] = sector or "Secteur inconnu"
            quality_winners.append(w)

    if not quality_winners:
        message = f"<b>✅ Scan terminé — {now_str}</b>\n<i>{len(all_winners)} titre(s) en hausse mais aucun ne " \
                  f"passe les filtres qualité (prix/capitalisation/volume).</i>"
        print(message)
        send_telegram_message(message)
        return

    # Limite à 20 titres (les plus forts en %) pour laisser de la place aux news,
    # puis on les regroupe par catégorie / entreprise pour l'affichage.
    quality_winners.sort(key=lambda w: w["pct_change"], reverse=True)
    shown = quality_winners[:20]
    for w in shown:
        w["news"] = get_news_snippet(w["ticker"])

    header_lines = [
        f"<b>🚀 Hausses du jour ≥ {GAIN_THRESHOLD_PCT}% — {now_str}</b>",
        f"<i>{len(shown)} titre(s) retenu(s) après filtre qualité "
        f"(sur {len(all_winners)} en hausse, {len(tickers)} scannés)</i>",
    ]
    if len(quality_winners) > 20:
        header_lines.append(f"<i>... et {len(quality_winners) - 20} autre(s) non affiché(s)</i>")

    message = format_grouped_message(shown, header_lines)
    print(message)
    send_telegram_message(message)


if __name__ == "__main__":
    sys.exit(main())
