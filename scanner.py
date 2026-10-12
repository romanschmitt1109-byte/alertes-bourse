"""
Scanner boursier — liste TOUTES les actions cotées aux États-Unis (NASDAQ, NYSE,
NYSE American), sans seuil de hausse ni filtre de prix / capitalisation / volume,
et publie le tout dans un fichier JSON (docs/data.json) consommé par un site
statique (GitHub Pages).

Pour chaque action : dernier cours, variation du jour, volume, mini-historique
des 5 dernières séances, nom, bourse, secteur et capitalisation. Des fiches
détaillées (graphiques 5 jours → 1 an, actualité, consensus d'analystes, repères
de risque) sont ajoutées pour les titres les plus échangés et ceux qui bougent
le plus.

Sources de données (gratuites, sans clé API) :
  - Listes NASDAQ / NYSE / NYSE American : miroir GitHub des données officielles
  - Noms, secteurs, capitalisations : fichiers complets du miroir, Wikipedia
    (S&P 500), puis Yahoo Finance au compte-gouttes (quelques centaines de
    titres par scan, les plus échangés d'abord) — la couverture se complète
    donc scan après scan
  - Cours et volumes : Yahoo Finance (yfinance), téléchargés par lots

Seuls les symboles techniquement inexploitables (actions préférentielles,
warrants et autres symboles contenant ^ ou /) sont écartés : ce ne sont pas des
actions ordinaires et Yahoo Finance ne sait pas les servir.
"""

import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
import pandas as pd
import yfinance as yf
from deep_translator import GoogleTranslator

BATCH_SIZE = 150           # nb de tickers par requête groupée
PAUSE_BETWEEN_BATCHES = 2  # secondes, pour ménager l'API gratuite
MAX_STALE_DAYS = 6         # un titre dont le dernier cours a plus de 6 jours de retard sur le marché est considéré comme radié

# Fiches détaillées (graphiques longs, actualité, analystes...) : réservées à un
# sous-ensemble de titres pour garder un scan rapide. Ce n'est PAS un filtre sur
# la liste : toutes les actions restent affichées sur le site.
DETAIL_TOP_ACTIVE = int(os.environ.get("DETAIL_TOP_ACTIVE", 30))    # les plus échangés (prix x volume)
DETAIL_TOP_GAINERS = int(os.environ.get("DETAIL_TOP_GAINERS", 20))  # plus fortes hausses du jour
DETAIL_TOP_LOSERS = int(os.environ.get("DETAIL_TOP_LOSERS", 10))    # plus fortes baisses du jour
# Pour les palmarès hausses/baisses, on ignore les titres quasi non échangés
# (sinon les premières places seraient prises par des valeurs sans volume).
DETAIL_MIN_DOLLAR_VOLUME = float(os.environ.get("DETAIL_MIN_DOLLAR_VOLUME", 1_000_000))

# Nombre de titres dont on complète le nom / secteur via Yahoo Finance à chaque scan
META_FILL_PER_RUN = int(os.environ.get("META_FILL_PER_RUN", 250))
META_MAX_TRIES = 3

# Où écrire le résultat pour le site statique (GitHub Pages sert /docs)
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "docs")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "data.json")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

RAW_BASES = [
    "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/",
    "https://raw.githubusercontent.com/deltaray-io/US-Stock-Symbols/main/",
]
# Fichiers "complets" du miroir : un objet par action avec symbole, nom, secteur,
# capitalisation... (une seule requête par bourse, tout est déjà dedans)
TICKER_SOURCES = [
    ("NASDAQ", "nasdaq/nasdaq_full_tickers.json", "nasdaq/nasdaq_tickers.txt"),
    ("NYSE", "nyse/nyse_full_tickers.json", "nyse/nyse_tickers.txt"),
    ("NYSE American", "amex/amex_full_tickers.json", "amex/amex_tickers.txt"),
]
SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


# --------------------------------------------------------------------------
# Normalisation des symboles, noms et secteurs
# --------------------------------------------------------------------------

def clean_symbol(raw):
    """Met un symbole au format Yahoo Finance (BRK.B -> BRK-B). Renvoie None
    pour les symboles inexploitables (préférentielles, warrants : ^ ou /)."""
    sym = (raw or "").strip().upper() if isinstance(raw, str) else ""
    if not sym or any(ch in sym for ch in "^/ $"):
        return None
    return sym.replace(".", "-")


SECTOR_FR = {
    "technology": "Technologie", "information technology": "Technologie",
    "healthcare": "Santé", "health care": "Santé",
    "financial services": "Finance", "financial": "Finance",
    "financials": "Finance", "finance": "Finance",
    "consumer cyclical": "Consommation cyclique", "consumer discretionary": "Consommation cyclique",
    "consumer defensive": "Consommation de base", "consumer staples": "Consommation de base",
    "energy": "Énergie",
    "industrials": "Industrie", "industrial": "Industrie",
    "basic materials": "Matériaux", "materials": "Matériaux",
    "real estate": "Immobilier",
    "communication services": "Communication", "telecommunications": "Communication",
    "telecommunication services": "Communication",
    "utilities": "Services publics",
    "miscellaneous": "Divers",
}


def normalize_sector(raw):
    """Unifie les secteurs venant de sources différentes (Wikipedia/GICS,
    NASDAQ, Yahoo Finance) sous un même libellé français. None si inconnu."""
    if not isinstance(raw, str):
        return None
    key = raw.strip().lower()
    if not key or key in ("nan", "none", "n/a", "na"):
        return None
    return SECTOR_FR.get(key, raw.strip())


NAME_SUFFIX_RE = re.compile(
    r"\s+(?:(?:Class|Series)\s+[A-Z0-9]+\s+)?"
    r"(?:Common\s+Stock|Common\s+Shares?|Ordinary\s+Shares?|"
    r"American\s+Depositary\s+(?:Shares|Receipts?)|Depositary\s+Shares?|Capital\s+Stock)\b.*$",
    re.IGNORECASE,
)


def clean_name(name):
    """'Apple Inc. Common Stock' -> 'Apple Inc.' (le NASDAQ ajoute le type de titre)."""
    if not name or not isinstance(name, str):
        return None
    cleaned = NAME_SUFFIX_RE.sub("", name).strip(" -,")
    return cleaned or name.strip() or None


def parse_money(value):
    """'3,401,234,567' / '$12.5' / '' -> float ou None."""
    if value is None:
        return None
    s = str(value).replace("$", "").replace(",", "").strip()
    if not s or s.upper() in ("N/A", "NA", "NAN"):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def round_price(x):
    """4 décimales sous 1$ (les penny stocks), 2 décimales au-dessus."""
    return round(x, 4) if x < 1 else round(x, 2)


# --------------------------------------------------------------------------
# Listes de tickers et métadonnées
# --------------------------------------------------------------------------

def _fetch(path, as_json):
    for base in RAW_BASES:
        try:
            resp = requests.get(base + path, headers=HEADERS, timeout=40)
            resp.raise_for_status()
            return resp.json() if as_json else [t for t in resp.text.splitlines() if t.strip()]
        except Exception as e:
            print(f"Source {base + path} indisponible ({e}), essai suivant...")
    return None


def get_listings():
    """Renvoie ({ticker: bourse}, {ticker: {n, g, m}}) pour toutes les actions
    des listes NASDAQ, NYSE et NYSE American : la bourse, le nom, le secteur et
    la capitalisation viennent directement du fichier source (aucun filtre)."""
    mapping, meta = {}, {}
    for label, json_path, txt_path in TICKER_SOURCES:
        rows = _fetch(json_path, True)
        if isinstance(rows, list) and rows:
            for r in rows:
                sym = clean_symbol(r.get("symbol")) if isinstance(r, dict) else None
                if not sym or sym in mapping:
                    continue
                mapping[sym] = label
                meta[sym] = {
                    "n": clean_name(r.get("name")),
                    "g": normalize_sector(r.get("sector")),
                    "m": parse_money(r.get("marketCap")),
                }
            continue
        tickers = _fetch(txt_path, False) or []
        if not tickers:
            print(f"Liste {label} introuvable.")
        for raw in tickers:
            sym = clean_symbol(raw)
            if sym and sym not in mapping:
                mapping[sym] = label
    return mapping, meta


def get_sp500_meta():
    """Noms et secteurs (GICS) des 500 valeurs du S&P 500, depuis Wikipedia."""
    try:
        resp = requests.get(SP500_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        df = pd.read_html(io.StringIO(resp.text))[0]
        meta = {}
        for _, r in df.iterrows():
            sym = clean_symbol(str(r.get("Symbol", "")))
            if not sym:
                continue
            name = r.get("Security")
            meta[sym] = {
                "n": str(name).strip() if isinstance(name, str) and name.strip() else None,
                "g": normalize_sector(r.get("GICS Sector")),
            }
        return meta
    except Exception as e:
        print(f"Impossible de récupérer le S&P 500 ({e}).")
        return {}


def build_meta(tickers, sp_meta, nq_meta, old_universe):
    """Fusionne les sources de métadonnées. Priorité : Wikipedia (noms et
    secteurs plus propres) > écran NASDAQ > données du scan précédent."""
    old = {u["t"]: u for u in old_universe if isinstance(u, dict) and u.get("t")}
    meta = {}
    for sym in tickers:
        o, s, q = old.get(sym, {}), sp_meta.get(sym, {}), nq_meta.get(sym, {})
        meta[sym] = {
            "n": s.get("n") or q.get("n") or o.get("n"),
            "g": s.get("g") or q.get("g") or o.get("g"),
            "m": q.get("m") or o.get("m"),
            "mt": o.get("mt"),
        }
    return meta


# --------------------------------------------------------------------------
# Cours : téléchargement par lots
# --------------------------------------------------------------------------

def _series(data, ticker, field):
    """Extrait la colonne `field` d'un ticker, que yfinance renvoie ou non un
    index à deux niveaux (cas d'un lot ne contenant qu'un seul ticker)."""
    try:
        col = data[ticker][field]
    except Exception:
        try:
            col = data[field]
        except Exception:
            return None
    if isinstance(col, pd.DataFrame):
        col = col.iloc[:, 0]
    return col


def scan_batch(tickers):
    """Télécharge 5 jours de cours pour un lot de tickers et renvoie, pour
    chacun : dernier cours, variation du jour, volume, 5 dernières clôtures et
    date de la dernière séance (clé interne "_d")."""
    rows = []
    data = None
    for attempt in range(2):
        try:
            data = yf.download(
                tickers, period="5d", interval="1d",
                group_by="ticker", threads=True, progress=False,
            )
            break
        except Exception as e:
            print(f"Erreur de téléchargement du lot (essai {attempt + 1}/2): {e}")
            time.sleep(5)
    if data is None or getattr(data, "empty", True):
        return rows

    for ticker in tickers:
        try:
            closes = _series(data, ticker, "Close")
            if closes is None:
                continue
            closes = closes.dropna()
            if len(closes) < 2:
                continue
            last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
            if last <= 0 or prev <= 0:
                continue
            volume = None
            volumes = _series(data, ticker, "Volume")
            if volumes is not None:
                v = volumes.reindex(closes.index).iloc[-1]
                if pd.notna(v) and v > 0:
                    volume = int(v)
            rows.append({
                "t": ticker,
                "p": round_price(last),
                "c": round((last - prev) / prev * 100, 2),
                "v": volume,
                "s": [round_price(float(x)) for x in closes.iloc[-5:]],
                "_d": closes.index[-1].date(),
            })
        except Exception:
            continue
    return rows


def chunked(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def scan_all(tickers):
    all_rows = []
    batches = list(chunked(tickers, BATCH_SIZE))
    for i, batch in enumerate(batches):
        print(f"  Lot {i + 1}/{len(batches)} ({len(batch)} titres)...")
        all_rows.extend(scan_batch(batch))
        time.sleep(PAUSE_BETWEEN_BATCHES)
    return all_rows


def drop_stale(rows):
    """Écarte les titres dont la dernière séance est très en retard sur le
    reste du marché (titres radiés dont Yahoo garde un vieux cours)."""
    if not rows:
        return rows
    latest = max(r["_d"] for r in rows)
    cutoff = latest - timedelta(days=MAX_STALE_DAYS)
    return [r for r in rows if r["_d"] >= cutoff]


def assemble_universe(rows, exchange_map, meta):
    """Construit la liste compacte publiée sur le site. Clés courtes pour
    garder un fichier léger : t ticker, p prix, c variation %, v volume,
    s 5 dernières clôtures, n nom, x bourse, g secteur, m capitalisation."""
    universe = []
    for r in rows:
        m = meta.get(r["t"], {})
        cap = m.get("m")
        u = {
            "t": r["t"], "p": r["p"], "c": r["c"], "v": r["v"], "s": r["s"],
            "n": m.get("n"), "x": exchange_map.get(r["t"]), "g": m.get("g"),
            "m": int(cap) if cap else None, "mt": m.get("mt"),
        }
        universe.append({k: v for k, v in u.items() if v is not None})
    universe.sort(key=lambda u: u["t"])  # ordre stable = commits plus légers
    return universe


def dollar_volume(u):
    return u["p"] * (u.get("v") or 0)


def fill_missing_meta(universe, budget):
    """Complète au compte-gouttes les noms / secteurs / capitalisations
    manquants via Yahoo Finance, en commençant par les titres les plus
    échangés. Un titre introuvable est retenté au plus META_MAX_TRIES fois."""
    if budget <= 0:
        return 0
    missing = [u for u in universe if not u.get("n") and (u.get("mt") or 0) < META_MAX_TRIES]
    missing.sort(key=dollar_volume, reverse=True)
    filled = 0
    for u in missing[:budget]:
        try:
            info = yf.Ticker(u["t"]).info or {}
        except Exception:
            info = {}
        name = info.get("shortName") or info.get("longName")
        if name:
            u["n"] = name
            sector = normalize_sector(info.get("sector") or info.get("industry"))
            if sector and not u.get("g"):
                u["g"] = sector
            cap = info.get("marketCap")
            if cap and not u.get("m"):
                u["m"] = int(cap)
            filled += 1
        else:
            u["mt"] = (u.get("mt") or 0) + 1
    return filled


# --------------------------------------------------------------------------
# Fiches détaillées
# --------------------------------------------------------------------------

def extract_analyst_target(info):
    """Extrait le consensus des analystes (objectif de cours, nombre
    d'analystes, recommandation) directement depuis les données déjà
    récupérées par Yahoo Finance — aucune requête supplémentaire. Renvoie
    None si Yahoo Finance ne fournit aucun avis d'analyste pour ce titre
    (fréquent pour les petites capitalisations peu suivies). C'est un
    consensus EXTERNE (des analystes de marché), pas une estimation
    calculée par ce site."""
    target_mean = info.get("targetMeanPrice")
    rec_key = info.get("recommendationKey")
    if not target_mean and not rec_key:
        return None
    rec_labels = {
        "strong_buy": "Achat fort",
        "buy": "Achat",
        "hold": "Conserver",
        "sell": "Vente",
        "strong_sell": "Vente forte",
    }
    return {
        "target_mean": round(target_mean, 2) if target_mean else None,
        "target_high": round(info.get("targetHighPrice"), 2) if info.get("targetHighPrice") else None,
        "target_low": round(info.get("targetLowPrice"), 2) if info.get("targetLowPrice") else None,
        "num_analysts": info.get("numberOfAnalystOpinions") or None,
        "recommendation": rec_labels.get(rec_key),
    }


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
        points.append({"t": t_iso, "c": round_price(float(c))})
    return points


def get_price_charts(ticker):
    """Récupère deux historiques de cours pour un titre :
    - "intraday" : 5 jours, par pas d'1h (court terme : 1 jour / 5 jours)
    - "daily"    : 1 an, par pas d'1 jour (long terme : 1 mois / 6 mois / 1 an)
    Appelé seulement sur les titres à fiche détaillée (peu nombreux), donc
    deux requêtes dédiées par titre restent raisonnables."""
    try:
        tk = yf.Ticker(ticker)
        intraday = series_to_points(tk.history(period="5d", interval="60m"))
        daily = series_to_points(tk.history(period="1y", interval="1d"))
        return {"intraday": intraday, "daily": daily}
    except Exception:
        return {"intraday": [], "daily": []}


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


def build_detail(ticker):
    """Construit la fiche détaillée d'un titre : graphiques, actualité, résumé
    d'activité, repères de risque et consensus d'analystes. Renvoie
    (fiche, infos) ou None si rien d'exploitable n'a pu être récupéré."""
    try:
        info = yf.Ticker(ticker).info or {}
        row = {
            "market_cap": info.get("marketCap") or None,
            "day_volume": info.get("volume") or info.get("regularMarketVolume") or None,
            "avg_volume": info.get("averageVolume") or info.get("averageDailyVolume10Day") or None,
        }
        row["news"] = get_news_snippet(ticker)
        row["chart"] = get_price_charts(ticker)
        if not row["chart"]["intraday"] and not row["chart"]["daily"]:
            return None
        row["summary"] = translate_summary(info.get("longBusinessSummary"))
        row["badges"] = compute_risk_badges(row)
        row["analyst"] = extract_analyst_target(info)
        detail = {k: row[k] for k in ("news", "chart", "summary", "badges", "analyst")}
        extra = {
            "name": info.get("shortName") or info.get("longName"),
            "sector": normalize_sector(info.get("sector") or info.get("industry")),
            "market_cap": row["market_cap"],
        }
        return detail, extra
    except Exception as e:
        print(f"Fiche détaillée impossible pour {ticker} ({e}).")
        return None


def pick_detail_tickers(universe):
    """Titres à fiche détaillée : les plus échangés, plus les plus fortes
    hausses et baisses du jour parmi les titres réellement échangés."""
    traded = [u for u in universe if dollar_volume(u) >= DETAIL_MIN_DOLLAR_VOLUME]
    active = sorted(universe, key=dollar_volume, reverse=True)[:DETAIL_TOP_ACTIVE]
    gainers = sorted(traded, key=lambda u: u["c"], reverse=True)[:DETAIL_TOP_GAINERS]
    losers = sorted(traded, key=lambda u: u["c"])[:DETAIL_TOP_LOSERS]
    picks, seen = [], set()
    for u in active + gainers + losers:
        if u["t"] not in seen:
            seen.add(u["t"])
            picks.append(u["t"])
    return picks


def build_details(universe):
    by_ticker = {u["t"]: u for u in universe}
    picks = pick_detail_tickers(universe)
    print(f"Fiches détaillées pour {len(picks)} titre(s)...")
    details = {}
    for t in picks:
        res = build_detail(t)
        if not res:
            continue
        detail, extra = res
        details[t] = detail
        u = by_ticker[t]
        # Les infos de la fiche sont les plus fraîches : on en profite pour
        # compléter / rafraîchir la ligne correspondante.
        if extra.get("name") and not u.get("n"):
            u["n"] = extra["name"]
        if extra.get("sector") and not u.get("g"):
            u["g"] = extra["sector"]
        if extra.get("market_cap"):
            u["m"] = int(extra["market_cap"])
    return details


# --------------------------------------------------------------------------
# Écriture et point d'entrée
# --------------------------------------------------------------------------

def load_existing():
    """Relit le data.json déjà publié (s'il existe) : il sert de cache pour les
    noms / secteurs / capitalisations déjà connus."""
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_results(universe, details, now_dt):
    """Écrit le JSON consommé par le site statique (docs/index.html)."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    payload = {
        "generated_at": now_dt.strftime("%d/%m/%Y %H:%M"),
        "generated_at_iso": now_dt.isoformat(),
        "total_stocks": len(universe),
        "universe": universe,
        "details": details,
    }
    tmp = OUTPUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUTPUT_FILE)  # écriture atomique : jamais de fichier à moitié écrit
    print(f"Résultats écrits dans {OUTPUT_FILE} ({len(universe)} actions, {len(details)} fiches détaillées).")


def main():
    now_dt = datetime.now(ZoneInfo("Europe/Paris"))
    old_data = load_existing()
    old_universe = (old_data or {}).get("universe") or []

    exchange_map, nq_meta = get_listings()
    sp_meta = get_sp500_meta()
    for sym in sp_meta:
        exchange_map.setdefault(sym, None)  # valeur du S&P 500 absente des listes : bourse inconnue

    tickers = sorted(exchange_map)
    if not tickers:
        print("Aucun ticker récupéré, arrêt (les données précédentes sont conservées).")
        return

    print(f"Scan de {len(tickers)} actions...")
    rows = drop_stale(scan_all(tickers))
    print(f"{len(rows)} actions avec un cours exploitable.")

    # Garde-fou : si Yahoo Finance nous a limités en cours de route, on préfère
    # garder les dernières données complètes plutôt que publier une liste tronquée.
    if not rows or (old_universe and len(rows) < 0.5 * len(old_universe)):
        print("Résultat incomplet (téléchargement probablement limité par Yahoo Finance) — "
              "les données précédentes sont conservées.")
        return

    meta = build_meta([r["t"] for r in rows], sp_meta, nq_meta, old_universe)
    universe = assemble_universe(rows, exchange_map, meta)

    filled = fill_missing_meta(universe, META_FILL_PER_RUN)
    print(f"Noms complétés via Yahoo Finance : {filled}. "
          f"Sans nom pour l'instant : {sum(1 for u in universe if not u.get('n'))}.")

    details = build_details(universe)
    write_results(universe, details, now_dt)


if __name__ == "__main__":
    sys.exit(main())
