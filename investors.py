"""
Suivi des grands investisseurs — mouvements déclarés à la SEC (EDGAR).

Source la plus rapide qui soit publique et gratuite : le formulaire 4, qui doit
être déposé dans les 2 jours ouvrés suivant une opération d'achat ou de vente
d'un dirigeant ou d'un actionnaire de plus de 10 % (ex. Berkshire Hathaway sur
une société dont elle détient plus de 10 %). On relève aussi les déclarations
de participation importante (SCHEDULE 13D / 13G) et la date du dernier
formulaire 13F trimestriel.

Limites : un investisseur qui détient moins de 10 % d'une société et qui n'y est
pas dirigeant ne dépose pas de formulaire 4 — ses positions n'apparaissent alors
que dans son 13F trimestriel (publié jusqu'à 45 jours après la fin du trimestre).

Fichier facultatif investors.txt (racine du dépôt) pour ajouter des investisseurs :
une ligne par investisseur, "CIK Nom" (le CIK se trouve sur sec.gov/edgar/search).

Sortie : docs/investors.json
"""

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "docs")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "investors.json")
INVESTORS_FILE = os.environ.get("INVESTORS_FILE", "investors.txt")
LOOKBACK_DAYS = int(os.environ.get("INVESTORS_LOOKBACK_DAYS", 120))
MAX_FORM4_PER_INVESTOR = int(os.environ.get("MAX_FORM4_PER_INVESTOR", 25))
PAUSE = 0.2  # l'EDGAR tolère 10 requêtes/seconde au maximum

# La SEC demande de s'identifier dans le User-Agent.
HEADERS = {"User-Agent": os.environ.get(
    "SEC_USER_AGENT", "AlertesBourse/1.0 (contact: romanschmitt1109-byte@users.noreply.github.com)"),
    "Accept-Encoding": "gzip, deflate"}

INVESTORS = [
    ("1067983", "Berkshire Hathaway", "Warren Buffett"),
    ("1336528", "Pershing Square", "Bill Ackman"),
    ("1649339", "Scion Asset Management", "Michael Burry"),
    ("1350694", "Bridgewater Associates", "Ray Dalio"),
    ("1061768", "Baupost Group", "Seth Klarman"),
]

FORM4_FORMS = {"4", "4/A"}
OWNERSHIP_FORMS = {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A",
                   "SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A"}


def get(url, as_json=False):
    time.sleep(PAUSE)
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json() if as_json else r.text
        except Exception as e:
            if attempt == 2:
                print(f"  Échec {url} ({e})")
    return None


def load_investors():
    investors = [{"cik": c, "name": n, "person": p} for c, n, p in INVESTORS]
    try:
        with open(INVESTORS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                m = re.match(r"^(\d{4,10})\s+(.+)$", line)
                if m and not any(i["cik"] == m.group(1).lstrip("0") for i in investors):
                    investors.append({"cik": m.group(1).lstrip("0"), "name": m.group(2).strip(), "person": None})
    except Exception:
        pass
    return investors


def _text(node, path):
    el = node.find(path)
    return el.text.strip() if el is not None and el.text else None


def _num(node, path):
    t = _text(node, path)
    try:
        return float(t) if t is not None else None
    except ValueError:
        return None


def parse_form4(xml_text):
    """Extrait d'un formulaire 4 : émetteur, ticker, déclarant et opérations
    sur actions (achat P / vente S ; les autres codes — attribution, exercice,
    don... — sont ignorés car ce ne sont pas des décisions d'investissement)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    issuer = _text(root, "issuer/issuerName")
    ticker = _text(root, "issuer/issuerTradingSymbol")
    owner = _text(root, "reportingOwner/reportingOwnerId/rptOwnerName")
    moves = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _text(tx, "transactionCoding/transactionCode")
        if code not in ("P", "S"):
            continue
        shares = _num(tx, "transactionAmounts/transactionShares/value")
        price = _num(tx, "transactionAmounts/transactionPricePerShare/value")
        moves.append({
            "date": _text(tx, "transactionDate/value"),
            "action": "Achat" if code == "P" else "Vente",
            "ticker": ticker, "issuer": issuer, "owner": owner,
            "shares": shares, "price": price,
            "value": round(shares * price) if shares and price else None,
        })
    return moves


def merge_moves(moves):
    """Regroupe les lignes d'un même jour / titre / sens (un ordre est souvent
    exécuté en plusieurs fractions, à des prix différents)."""
    grouped = {}
    for m in moves:
        key = (m["date"], m["ticker"], m["action"])
        g = grouped.setdefault(key, dict(m, shares=0.0, value=0.0, _p=False))
        g["shares"] += m["shares"] or 0
        if m["value"]:
            g["value"] += m["value"]
            g["_p"] = True
    out = []
    for g in grouped.values():
        g["price"] = round(g["value"] / g["shares"], 2) if g["_p"] and g["shares"] else None
        g["value"] = round(g["value"]) if g["_p"] else None
        g["shares"] = round(g["shares"])
        g.pop("_p", None)
        out.append(g)
    return out


def filing_url(cik, accession, doc):
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{doc}"


def raw_xml_doc(primary):
    """Le document principal d'un formulaire 4 est donné sous sa version mise en
    page (xslF345X05/nom.xml) ; la version brute est le même nom sans le dossier."""
    return primary.split("/")[-1]


def fetch_investor(inv, since):
    data = get(f"https://data.sec.gov/submissions/CIK{int(inv['cik']):010d}.json", as_json=True)
    if not data:
        return None
    recent = (data.get("filings") or {}).get("recent") or {}
    rows = list(zip(recent.get("form", []), recent.get("filingDate", []),
                    recent.get("accessionNumber", []), recent.get("primaryDocument", [])))
    moves, filings, last_13f = [], [], None
    form4_count = 0
    for form, fdate, acc, doc in rows:   # du plus récent au plus ancien
        if form.startswith("13F") and not last_13f:
            last_13f = {"date": fdate, "form": form, "link": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={int(inv['cik'])}&type=13F"}
        if fdate < since:
            continue
        if form in FORM4_FORMS and form4_count < MAX_FORM4_PER_INVESTOR:
            form4_count += 1
            xml = get(filing_url(inv["cik"], acc, raw_xml_doc(doc))) if doc else None
            for m in parse_form4(xml) if xml else []:
                m["filed"] = fdate
                m["link"] = filing_url(inv["cik"], acc, doc)
                moves.append(m)
        elif form.upper() in OWNERSHIP_FORMS:
            filings.append({"date": fdate, "form": form, "link": filing_url(inv["cik"], acc, doc)})
    # fusion des fractions d'ordre, en gardant la date de dépôt la plus récente
    merged = merge_moves(moves)
    meta = {(m["date"], m["ticker"], m["action"]): m for m in moves}
    for m in merged:
        src = meta.get((m["date"], m["ticker"], m["action"]), {})
        m["filed"] = src.get("filed")
        m["link"] = src.get("link")
    merged = [m for m in merged if m.get("ticker")]
    merged.sort(key=lambda m: (m.get("date") or "", m.get("filed") or ""), reverse=True)
    return {
        "cik": inv["cik"], "name": inv["name"], "person": inv.get("person"),
        "moves": merged, "filings": filings[:5], "last_13f": last_13f,
    }


def main():
    now = datetime.now(ZoneInfo("Europe/Paris"))
    since = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    result = []
    for inv in load_investors():
        print(f"{inv['name']}...")
        res = fetch_investor(inv, since)
        if res:
            result.append(res)
    if not result:
        print("ERREUR : aucune donnée récupérée auprès de la SEC (voir les messages « Échec » ci-dessus).")
        return 1
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    payload = {"generated_at": now.strftime("%d/%m/%Y %H:%M"), "lookback_days": LOOKBACK_DAYS, "investors": result}
    tmp = OUTPUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUTPUT_FILE)
    print(f"Écrit {OUTPUT_FILE} : {sum(len(i['moves']) for i in result)} mouvement(s).")


if __name__ == "__main__":
    sys.exit(main())
