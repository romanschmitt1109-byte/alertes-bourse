"""
Radar des initiés — achats déclarés par les dirigeants, administrateurs et
actionnaires de plus de 10 % de TOUTES les sociétés cotées aux États-Unis.

Source : le formulaire 4 de la SEC (EDGAR), déposé au plus tard 2 jours ouvrés
après l'opération. On ne garde que les achats sur le marché (code « P ») : ce
sont des décisions d'investissement prises avec l'argent personnel de
l'initié. Les ventes sont écartées (souvent automatiques : impôts, plans de
cession programmés, diversification) ainsi que les attributions gratuites,
exercices d'options, dons, etc.

Fonctionnement incrémental :
  1. le flux « derniers dépôts » de la SEC (quelques jours d'historique) donne
     les formulaires 4 arrivés depuis le passage précédent ;
  2. s'il reste du temps, l'index quotidien de la SEC sert à rattraper les
     jours de la fenêtre qui n'ont pas encore été parcourus (premier lancement,
     interruption du workflow...).

Sorties :
  docs/insiders.json          achats publiés sur le site (fenêtre glissante)
  data/insiders_state.json    mémoire du script (dépôts déjà lus, jours rattrapés)

Option : python insiders.py --budget 20000  (nombre maximum de dépôts lus,
utile pour un premier rattrapage complet depuis un ordinateur).
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "docs")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "insiders.json")
STATE_FILE = os.environ.get("INSIDERS_STATE", os.path.join("data", "insiders_state.json"))
WINDOW_DAYS = int(os.environ.get("INSIDERS_WINDOW_DAYS", 30))   # achats affichés : date de dépôt dans les N derniers jours
BUDGET = int(os.environ.get("INSIDERS_BUDGET", 2500))            # dépôts lus au maximum par passage
SEEN_DAYS = 7                                                     # mémoire des dépôts déjà lus
WORKERS = 4
MAX_RATE = 8.0  # requêtes / seconde (la SEC en tolère 10 au maximum)

HEADERS = {"User-Agent": os.environ.get(
    "SEC_USER_AGENT", "AlertesBourse/1.0 (contact: romanschmitt1109-byte@users.noreply.github.com)"),
    "Accept-Encoding": "gzip, deflate"}

FEED_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&company=&dateb="
            "&owner=only&start={start}&count=100&output=atom")
DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{d}.idx"
TXT_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}.txt"

_rate_lock = threading.Lock()
_next_slot = [0.0]


def _wait_turn():
    with _rate_lock:
        now = time.monotonic()
        slot = max(now, _next_slot[0])
        _next_slot[0] = slot + 1.0 / MAX_RATE
    delay = slot - time.monotonic()
    if delay > 0:
        time.sleep(delay)


def get(url):
    for attempt in range(3):
        _wait_turn()
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(3 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.text
        except Exception as e:
            if attempt == 2:
                print(f"  Échec {url} ({e})")
            time.sleep(1)
    return None


# --------------------------------------------------------------------------
# Lecture d'un formulaire 4
# --------------------------------------------------------------------------

def _text(node, path):
    el = node.find(path)
    return el.text.strip() if el is not None and el.text and el.text.strip() else None


def _num(node, path):
    t = _text(node, path)
    try:
        return float(t) if t is not None else None
    except ValueError:
        return None


def _flag(node, path):
    return (_text(node, path) or "").lower() in ("1", "true")


TITLE_FR = [
    (r"\bco-?ceo\b|co-chief executive", "Co-directeur général"),
    (r"\bceo\b|chief executive", "Directeur général"),
    (r"\bcfo\b|chief financial|principal financial", "Directeur financier"),
    (r"\bcoo\b|chief operating", "Directeur des opérations"),
    (r"\bcto\b|chief technology", "Directeur technique"),
    (r"exec(utive)?\.? chair|chairman|chairwoman|chairperson|\bchair\b", "Président du conseil"),
    (r"\bpresident\b", "Président"),
    (r"general counsel|chief legal", "Directeur juridique"),
    (r"\bevp\b|executive vice|\bsvp\b|senior vice|\bvp\b|vice president", "Vice-président"),
    (r"chief accounting|principal accounting|controller", "Directeur comptable"),
]


def role_label(rel):
    """Fonction de l'initié en français, la plus significative d'abord."""
    if rel is None:
        return None
    labels = []
    if _flag(rel, "isOfficer"):
        title = _text(rel, "officerTitle") or ""
        fr = next((lab for pat, lab in TITLE_FR if re.search(pat, title, re.IGNORECASE)), None)
        labels.append(fr or (title.strip()[:40] if title.strip() else "Dirigeant"))
    if _flag(rel, "isDirector"):
        labels.append("Administrateur")
    if _flag(rel, "isTenPercentOwner"):
        labels.append("Actionnaire > 10 %")
    if not labels and _flag(rel, "isOther"):
        labels.append(_text(rel, "otherText") or "Autre")
    return ", ".join(dict.fromkeys(labels)) or None


def clean_ticker(raw):
    t = (raw or "").strip().upper()
    if not t or t in ("NONE", "N/A", "NA") or not re.fullmatch(r"[A-Z0-9.\-]{1,10}", t):
        return None
    return t.replace(".", "-")


def parse_purchases(xml_text):
    """Achats sur le marché (code P) d'un formulaire 4, regroupés par jour
    (un ordre est souvent exécuté en plusieurs fractions, à des prix différents)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    if (_text(root, "documentType") or "") != "4":
        return []
    ticker = clean_ticker(_text(root, "issuer/issuerTradingSymbol"))
    if not ticker:
        return []
    issuer = _text(root, "issuer/issuerName")
    issuer_cik = (_text(root, "issuer/issuerCik") or "").lstrip("0")
    owners = root.findall("reportingOwner")
    owner = _text(owners[0], "reportingOwnerId/rptOwnerName") if owners else None
    role = role_label(owners[0].find("reportingOwnerRelationship")) if owners else None
    if len(owners) > 1:
        owner = (owner or "") + f" (+{len(owners) - 1})"
    grouped = {}
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        if _text(tx, "transactionCoding/transactionCode") != "P":
            continue
        if (_text(tx, "transactionAmounts/transactionAcquiredDisposedCode/value") or "A") != "A":
            continue
        shares = _num(tx, "transactionAmounts/transactionShares/value")
        price = _num(tx, "transactionAmounts/transactionPricePerShare/value")
        if not shares or not price or shares <= 0 or price <= 0:
            continue
        d = _text(tx, "transactionDate/value")
        d = d[:10] if d else None
        post = _num(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")
        g = grouped.setdefault(d, {"sh": 0.0, "v": 0.0, "post": None})
        g["sh"] += shares
        g["v"] += shares * price
        if post is not None:
            g["post"] = max(post, g["post"] or 0)
    out = []
    for d, g in grouped.items():
        row = {"t": ticker, "n": issuer, "c": issuer_cik, "o": owner, "r": role, "d": d,
               "sh": round(g["sh"]), "p": round(g["v"] / g["sh"], 4 if g["v"] / g["sh"] < 1 else 2),
               "v": round(g["v"])}
        if g["post"]:
            row["post"] = round(g["post"])
        out.append(row)
    return out


def extract_xml(txt):
    m = re.search(r"<XML>\s*(.*?)\s*</XML>", txt or "", re.DOTALL | re.IGNORECASE)
    return m.group(1) if m else None


def fetch_filing(item):
    """item = (accession, cik, date de dépôt) -> liste d'achats."""
    acc, cik, filed = item
    txt = get(TXT_URL.format(cik=int(cik), acc_nodash=acc.replace("-", ""), acc=acc))
    if txt is None:
        return item, None
    xml = extract_xml(txt)
    rows = parse_purchases(xml) if xml else []
    for r in rows:
        r["f"] = filed
        r["a"] = acc
    return item, rows


# --------------------------------------------------------------------------
# Sources : flux « derniers dépôts » et index quotidien
# --------------------------------------------------------------------------

ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.DOTALL)
ACC_RE = re.compile(r"accession-number=(\d{10}-\d{2}-\d{6})")
LINK_RE = re.compile(r"/Archives/edgar/data/(\d+)/")
FILED_RE = re.compile(r"Filed:</b>\s*(\d{4}-\d{2}-\d{2})|Filed:&lt;/b&gt;\s*(\d{4}-\d{2}-\d{2})")
TERM_RE = re.compile(r'label="form type" term="([^"]+)"')


def feed_items(seen, max_pages=40):
    """Formulaires 4 récents pas encore lus, du plus ancien au plus récent."""
    found = {}
    for page in range(max_pages):
        text = get(FEED_URL.format(start=page * 100))
        entries = ENTRY_RE.findall(text or "")
        if not entries:
            break
        new_on_page = 0
        for e in entries:
            term = TERM_RE.search(e)
            acc = ACC_RE.search(e)
            link = LINK_RE.search(e)
            if not (term and acc and link) or term.group(1) != "4":
                continue
            a = acc.group(1)
            if a in seen:
                continue
            new_on_page += 1
            fm = FILED_RE.search(e)
            filed = (fm.group(1) or fm.group(2)) if fm else None
            found.setdefault(a, (a, link.group(1), filed))
        if new_on_page == 0:
            break   # page entièrement déjà lue : on a rattrapé le passage précédent
    items = list(found.values())
    items.sort(key=lambda x: (x[2] or "", x[0]))
    return items


def daily_index_items(day):
    """Formulaires 4 d'un jour donné d'après l'index quotidien de la SEC.
    Renvoie None si l'index n'existe pas (week-end, jour férié, pas encore publié)."""
    q = (day.month - 1) // 3 + 1
    text = get(DAILY_INDEX_URL.format(y=day.year, q=q, d=day.strftime("%Y%m%d")))
    if text is None:
        return None
    items = {}
    for line in text.splitlines():
        if not line.startswith("4 "):
            continue
        m = re.search(r"edgar/data/(\d+)/(\d{10}-\d{2}-\d{6})\.txt", line)
        if m:
            items.setdefault(m.group(2), (m.group(2), m.group(1), day.isoformat()))
    return sorted(items.values())


# --------------------------------------------------------------------------
# État, sortie
# --------------------------------------------------------------------------

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, payload):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def process(items, buys, seen, budget):
    """Lit les dépôts (en parallèle, au rythme autorisé par la SEC) et ajoute
    les achats trouvés. Renvoie le nombre de dépôts lus."""
    items = [i for i in items if i[0] not in seen][:budget]
    if not items:
        return 0
    known = {b["a"] for b in buys}
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for item, rows in pool.map(fetch_filing, items):
            if rows is None:
                continue   # échec réseau : le dépôt sera retenté au prochain passage
            seen[item[0]] = item[2] or date.today().isoformat()
            done += 1
            if rows and item[0] not in known:
                buys.extend(rows)
                known.add(item[0])
    return done


def flag_plans(buys):
    """Marque les achats probablement automatiques : au moins 3 initiés d'une
    même société qui achètent le même jour exactement au même prix (plan
    d'achat d'actions salarié, réinvestissement collectif...). Ce n'est pas
    une décision individuelle : le site les écarte des achats groupés."""
    groups = {}
    for b in buys:
        groups.setdefault((b["t"], b.get("d"), b.get("p")), set()).add(b.get("o"))
    for b in buys:
        if len(groups[(b["t"], b.get("d"), b.get("p"))]) >= 3:
            b["plan"] = 1
        else:
            b.pop("plan", None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget", type=int, default=BUDGET)
    args = parser.parse_args()
    budget = args.budget

    now = datetime.now(ZoneInfo("America/New_York"))
    today = now.date()
    start = today - timedelta(days=WINDOW_DAYS)

    out = load_json(OUTPUT_FILE, {})
    state = load_json(STATE_FILE, {})
    buys = out.get("buys") or []
    seen = state.get("seen") or {}
    days_done = set(state.get("days_done") or [])

    # 1. derniers dépôts
    items = feed_items(seen)
    n = process(items, buys, seen, budget)
    budget -= n
    print(f"Flux récent : {len(items)} nouveau(x) dépôt(s), {n} lu(s).")

    # 2. rattrapage des jours de la fenêtre pas encore parcourus (du plus récent au plus ancien)
    day = today - timedelta(days=1)
    while budget > 0 and day >= start:
        key = day.isoformat()
        if key not in days_done and day.weekday() < 5:
            items = daily_index_items(day)
            if items is None:
                if (today - day).days >= 3:
                    days_done.add(key)   # jour férié : pas d'index
            else:
                n = process(items, buys, seen, budget)
                budget -= n
                remaining = sum(1 for i in items if i[0] not in seen)
                print(f"Rattrapage du {key} : {n} dépôt(s) lu(s), {remaining} restant(s).")
                if remaining == 0:
                    days_done.add(key)
                    # ces dépôts n'ont plus besoin d'être mémorisés un par un
                    if (today - day).days > SEEN_DAYS:
                        for i in items:
                            seen.pop(i[0], None)
        day -= timedelta(days=1)

    # fenêtre glissante
    cutoff = start.isoformat()
    buys = [b for b in buys if (b.get("f") or "") >= cutoff]
    buys.sort(key=lambda b: (b.get("f") or "", b.get("d") or "", b.get("v") or 0), reverse=True)
    flag_plans(buys)
    seen_cut = (today - timedelta(days=SEEN_DAYS)).isoformat()
    seen = {a: f for a, f in seen.items() if (f or "") >= seen_cut}
    days_done = sorted(d for d in days_done if d >= cutoff)

    write_json(OUTPUT_FILE, {
        "generated_at": datetime.now(ZoneInfo("Europe/Paris")).strftime("%d/%m/%Y %H:%M"),
        "window_days": WINDOW_DAYS,
        "days_done": days_done,
        "buys": buys,
    })
    write_json(STATE_FILE, {"seen": seen, "days_done": days_done})
    print(f"Écrit {OUTPUT_FILE} : {len(buys)} achat(s) d'initiés sur {WINDOW_DAYS} jours.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
