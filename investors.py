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
TOP_HOLDINGS = int(os.environ.get("TOP_HOLDINGS", 25))
DATA_FILE = os.path.join(OUTPUT_DIR, "data.json")
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
    ("1656456", "Appaloosa", "David Tepper"),
    ("1040273", "Third Point", "Dan Loeb"),
    ("921669", "Icahn", "Carl Icahn"),
    ("1536411", "Duquesne Family Office", "Stanley Druckenmiller"),
    ("1549575", "Dalal Street", "Mohnish Pabrai"),
    ("1167483", "Tiger Global", "Chase Coleman"),
    ("1709323", "Himalaya Capital", "Li Lu"),
    ("1345471", "Trian Fund", "Nelson Peltz"),
    ("1791786", "Elliott Management", "Paul Singer"),
    ("1029160", "Soros Fund Management", "George Soros"),
    ("1112520", "Akre Capital", "Chuck Akre"),
    ("1061165", "Lone Pine Capital", "Stephen Mandel"),
]
QUARTERS_13F = 3   # derniers 13F lus : positions actuelles + achats des 2 derniers trimestres
CONVERGENCE_MIN = 2

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


NAME_DROP = {"INC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED", "PLC", "LLC", "LP", "NV", "SA", "AG",
             "HOLDINGS", "HLDGS", "HOLDING", "GROUP", "THE", "DEL", "NEW", "COM", "ADR", "ADS", "SPONSORED", "CL", "CLASS"}


def norm_name(name):
    name = re.sub(r"\(.*?\)", " ", name or "")   # « Alphabet Inc. (Class A) » -> « Alphabet Inc. »
    words = re.sub(r"[^A-Z0-9 ]", " ", name.upper().replace("&", " AND ")).split()
    words = [w for w in words if w not in NAME_DROP]
    return " ".join(words)


def load_name_map():
    """Associe le nom normalisé d'une société à son ticker, d'après la liste du
    scan (docs/data.json). En cas de doublon (ex. Alphabet A et C), on garde le
    titre le plus échangé."""
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            universe = json.load(f).get("universe") or []
    except Exception:
        return {}
    best = {}
    for u in universe:
        key = norm_name(u.get("n"))
        if not key:
            continue
        score = (u.get("v") or 0) * (u.get("p") or 0)
        if key not in best or score > best[key][0]:
            best[key] = (score, u["t"])
    return {k: v[1] for k, v in best.items()}


def parse_infotable(xml_text):
    """Lit un tableau 13F : {cusip: {name, value, shares}} pour les actions
    (les options d'achat / de vente et les obligations sont ignorées). Depuis
    2023 les valeurs sont en dollars."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    out = {}
    for el in root.iter():
        if el.tag.split("}")[-1] != "infoTable":
            continue
        d = {}
        for child in el.iter():
            d[child.tag.split("}")[-1]] = (child.text or "").strip()
        if d.get("putCall") or d.get("sshPrnamtType", "SH") != "SH":
            continue
        try:
            value = float(d.get("value", "0"))
            shares = float(d.get("sshPrnamt", "0"))
        except ValueError:
            continue
        key = d.get("cusip") or d.get("nameOfIssuer")
        g = out.setdefault(key, {"name": d.get("nameOfIssuer"), "value": 0.0, "shares": 0.0})
        g["value"] += value
        g["shares"] += shares
    # Certains déclarants (ex. Baupost, Duquesne) indiquent encore les montants en
    # milliers de dollars : le prix implicite par action est alors absurde (< 1 $).
    units = sorted(g["value"] / g["shares"] for g in out.values() if g["shares"] > 0 and g["value"] > 0)
    if units and units[len(units) // 2] < 1:
        for g in out.values():
            g["value"] *= 1000
    return out


def build_holdings(table, name_map):
    total = sum(h["value"] for h in table.values())
    if total <= 0:
        return None
    rows = sorted(table.values(), key=lambda h: h["value"], reverse=True)
    top = []
    for h in rows[:TOP_HOLDINGS]:
        top.append({
            "name": (h["name"] or "").title(),
            "ticker": name_map.get(norm_name(h["name"])),
            "value": round(h["value"]), "shares": round(h["shares"]),
            "pct": round(h["value"] / total * 100, 2),
        })
    return {"total": round(total), "count": len(rows), "top": top,
            "others_pct": round(max(0.0, 100 - sum(t["pct"] for t in top)), 2)}


def fetch_table(inv, acc):
    """Tableau d'un 13F : on repère le fichier « information table » dans le
    dossier du dépôt (son nom varie selon le déclarant)."""
    base = f"https://www.sec.gov/Archives/edgar/data/{int(inv['cik'])}/{acc.replace('-', '')}/"
    index = get(base + "index.json", as_json=True)
    names = [i.get("name", "") for i in ((index or {}).get("directory") or {}).get("item", [])]
    xmls = [n for n in names if n.lower().endswith(".xml") and "primary_doc" not in n.lower()]
    xmls.sort(key=lambda n: ("infotable" not in n.lower(), n))
    for n in xmls[:2]:
        text = get(base + n)
        table = parse_infotable(text) if text else {}
        if table:
            return table, base
    return None, base


def quarter_label(iso):
    """'2026-06-30' -> 'T2 2026'."""
    try:
        y, m = int(iso[:4]), int(iso[5:7])
    except (TypeError, ValueError):
        return None
    return f"T{(m - 1) // 3 + 1} {y}"


MIN_WEIGHT = 0.1      # % du portefeuille : en dessous, un achat est jugé anecdotique
MIN_ADD_PCT = 10      # % : un renforcement plus petit n'est pas compté comme un achat


def quarter_changes(cur, prev, name_map):
    """Compare deux 13F consécutifs (positions en nombre d'actions, par CUSIP) :
    nouvelles positions et renforcements (achats), allègements et sorties (ventes).
    Seuls les achats significatifs sont retenus : au moins MIN_WEIGHT % du
    portefeuille, et pour un renforcement au moins +MIN_ADD_PCT % d'actions
    (sinon les gros fonds à mille lignes noieraient le radar)."""
    total = sum(h["value"] for h in cur.values()) or 1
    buys, sells = [], []
    for key in set(cur) | set(prev):
        c, p = cur.get(key), prev.get(key)
        cs, ps = (c or {}).get("shares", 0), (p or {}).get("shares", 0)
        ref = c or p
        unit = (c["value"] / cs) if c and cs else ((p["value"] / ps) if p and ps else 0)
        row = {"name": (ref["name"] or "").title(), "ticker": name_map.get(norm_name(ref["name"]))}
        if ps <= 0 and cs > 0:
            row.update(kind="new", value=round(c["value"]), shares=round(cs))
            buys.append(row)
        elif cs > ps * 1.01:
            row.update(kind="add", value=round((cs - ps) * unit), shares=round(cs - ps), pct=round((cs / ps - 1) * 100, 1))
            buys.append(row)
        elif cs <= 0 < ps:
            row.update(kind="exit", value=round(p["value"]), shares=round(ps))
            sells.append(row)
        elif cs < ps * 0.99:
            row.update(kind="cut", value=round((ps - cs) * unit), shares=round(ps - cs), pct=round((cs / ps - 1) * 100, 1))
            sells.append(row)
    for b in buys:
        b["w"] = round(b["value"] / total * 100, 2)
    buys = [b for b in buys if b["w"] >= MIN_WEIGHT and (b["kind"] == "new" or b["pct"] >= MIN_ADD_PCT)]
    buys.sort(key=lambda r: r["value"], reverse=True)
    sells.sort(key=lambda r: r["value"], reverse=True)
    # tous les achats identifiés (pour le radar de convergence), les plus gros des autres
    buys = [b for b in buys if b["ticker"]] + [b for b in buys if not b["ticker"]][:5]
    return buys, sells[:15]


def fetch_13f(inv, rows13f, name_map, old):
    """Positions du dernier 13F et achats / ventes des trimestres précédents.
    Si les dépôts n'ont pas changé depuis le passage précédent, on réutilise
    le résultat déjà publié (aucune requête)."""
    accs = [r[2] for r in rows13f]
    if old and old.get("accs") == accs and old.get("holdings"):
        return old["holdings"], old.get("quarters") or [], accs
    tables = []
    holdings = None
    for i, (form, fdate, acc, rdate) in enumerate(rows13f):
        table, base = fetch_table(inv, acc)
        if not table:
            break
        tables.append((table, rdate))
        if i == 0:
            holdings = build_holdings(table, name_map)
            if holdings:
                holdings.update(period=rdate, filed=fdate, link=base)
    quarters = []
    for (cur, rdate), (prev, prev_date) in zip(tables, tables[1:]):
        buys, sells = quarter_changes(cur, prev, name_map)
        quarters.append({"period": rdate, "label": quarter_label(rdate), "prev": prev_date, "buys": buys, "sells": sells})
    return holdings, quarters, accs if tables else []


def filing_url(cik, accession, doc):
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{doc}"


def raw_xml_doc(primary):
    """Le document principal d'un formulaire 4 est donné sous sa version mise en
    page (xslF345X05/nom.xml) ; la version brute est le même nom sans le dossier."""
    return primary.split("/")[-1]


def fetch_investor(inv, since, name_map=None, old=None):
    name_map = name_map or {}
    old = old or {}
    data = get(f"https://data.sec.gov/submissions/CIK{int(inv['cik']):010d}.json", as_json=True)
    if not data:
        return None
    recent = (data.get("filings") or {}).get("recent") or {}
    rows = list(zip(recent.get("form", []), recent.get("filingDate", []),
                    recent.get("accessionNumber", []), recent.get("primaryDocument", [])))
    reports = recent.get("reportDate", [])
    rows13f, periods = [], set()
    for i, r in enumerate(rows):   # du plus récent au plus ancien ; un seul 13F par trimestre
        rdate = reports[i] if i < len(reports) else None
        if r[0] == "13F-HR" and rdate and rdate not in periods:
            periods.add(rdate)
            rows13f.append((r[0], r[1], r[2], rdate))
            if len(rows13f) >= QUARTERS_13F:
                break
    holdings, quarters, accs = fetch_13f(inv, rows13f, name_map, old) if rows13f else (None, [], [])

    # formulaires 4 : seuls les dépôts pas encore lus sont téléchargés
    f4_seen = {a: d for a, d in (old.get("f4_seen") or []) if d >= since}
    moves, filings, last_13f = [], [], None
    form4_count = 0
    for form, fdate, acc, doc in rows:   # du plus récent au plus ancien
        if form.startswith("13F") and not last_13f:
            last_13f = {"date": fdate, "form": form, "link": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={int(inv['cik'])}&type=13F"}
        if fdate < since:
            continue
        if form in FORM4_FORMS and acc not in f4_seen and form4_count < MAX_FORM4_PER_INVESTOR:
            form4_count += 1
            xml = get(filing_url(inv["cik"], acc, raw_xml_doc(doc))) if doc else None
            if xml is None:
                continue
            f4_seen[acc] = fdate
            for m in parse_form4(xml):
                m["filed"] = fdate
                m["link"] = filing_url(inv["cik"], acc, doc)
                moves.append(m)
        elif form.upper() in OWNERSHIP_FORMS:
            filings.append({"date": fdate, "form": form, "link": filing_url(inv["cik"], acc, doc)})
    # fusion des fractions d'ordre, en gardant la date de dépôt la plus récente
    if "f4_seen" in old:   # (un ancien fichier sans cette clé : tout est relu, pour ne rien compter deux fois)
        moves += [m for m in (old.get("moves") or []) if (m.get("filed") or "") >= since]
    meta = {}
    for m in moves:
        k = (m["date"], m["ticker"], m["action"])
        if k not in meta or (m.get("filed") or "") > (meta[k].get("filed") or ""):
            meta[k] = m
    merged = merge_moves(moves)
    for m in merged:
        src = meta.get((m["date"], m["ticker"], m["action"]), {})
        m["filed"] = src.get("filed")
        m["link"] = src.get("link")
    merged = [m for m in merged if m.get("ticker")]
    merged.sort(key=lambda m: (m.get("date") or "", m.get("filed") or ""), reverse=True)
    return {
        "cik": inv["cik"], "name": inv["name"], "person": inv.get("person"),
        "moves": merged, "filings": filings[:5], "last_13f": last_13f, "holdings": holdings,
        "quarters": quarters, "accs": accs, "f4_seen": sorted(f4_seen.items()),
    }


def build_convergence(investors):
    """Radar de convergence : actions achetées par au moins CONVERGENCE_MIN
    investisseurs suivis au cours d'un même trimestre — d'après les 13F
    (nouvelle position ou renforcement) et les formulaires 4 (achats déclarés)."""
    groups = {}
    for inv in investors:
        who = inv.get("person") or inv["name"]
        for q in inv.get("quarters") or []:
            for b in q["buys"]:
                if not b.get("ticker"):
                    continue
                g = groups.setdefault((b["ticker"], q["label"]), {"t": b["ticker"], "name": b["name"], "q": q["label"],
                                                                  "end": q["period"], "who": {}})
                g["who"][who] = {"who": who, "fund": inv["name"], "kind": b["kind"], "value": b["value"],
                                 "pct": b.get("pct"), "w": b.get("w"), "src": "13F"}
        for m in inv.get("moves") or []:
            if m["action"] != "Achat" or not m.get("ticker"):
                continue
            label = quarter_label(m["date"])
            g = groups.setdefault((m["ticker"], label), {"t": m["ticker"], "name": (m.get("issuer") or "").title(),
                                                         "q": label, "end": m["date"], "who": {}})
            e = g["who"].get(who)
            if e and e["src"] == "13F":
                e["f4"] = (e.get("f4") or 0) + (m.get("value") or 0)
                e["date"] = max(e.get("date") or "", m["date"])
            elif e:
                e["value"] += m.get("value") or 0
                e["date"] = max(e["date"], m["date"])
            else:
                g["who"][who] = {"who": who, "fund": inv["name"], "kind": "f4", "value": m.get("value") or 0,
                                 "date": m["date"], "src": "F4"}
    out = []
    for g in groups.values():
        if len(g["who"]) < CONVERGENCE_MIN:
            continue
        who = sorted(g["who"].values(), key=lambda w: w["value"] or 0, reverse=True)
        out.append({"t": g["t"], "name": g["name"], "q": g["q"], "count": len(who),
                    "value": sum(w["value"] or 0 for w in who), "who": who})
    def qkey(label):
        q, y = label.split()
        return int(y) * 10 + int(q[1:])
    out.sort(key=lambda c: (qkey(c["q"]), c["count"], c["value"]), reverse=True)
    # les 3 trimestres les plus récents, 30 titres au plus par trimestre
    labels = sorted({c["q"] for c in out}, key=qkey, reverse=True)[:3]
    return [c for q in labels for c in [c for c in out if c["q"] == q][:30]]


def main():
    now = datetime.now(ZoneInfo("Europe/Paris"))
    since = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    result = []
    name_map = load_name_map()
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            previous = {i["cik"]: i for i in json.load(f).get("investors", [])}
    except Exception:
        previous = {}
    for inv in load_investors():
        print(f"{inv['name']}...")
        res = fetch_investor(inv, since, name_map, previous.get(inv["cik"]))
        if res:
            result.append(res)
        elif inv["cik"] in previous:
            result.append(previous[inv["cik"]])   # SEC injoignable pour cet investisseur : on garde l'ancien résultat
    if not result:
        print("ERREUR : aucune donnée récupérée auprès de la SEC (voir les messages « Échec » ci-dessus).")
        return 1
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    payload = {"generated_at": now.strftime("%d/%m/%Y %H:%M"), "lookback_days": LOOKBACK_DAYS,
               "convergence": build_convergence(result), "investors": result}
    tmp = OUTPUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUTPUT_FILE)
    print(f"Écrit {OUTPUT_FILE} : {sum(len(i['moves']) for i in result)} mouvement(s), "
          f"{len(payload['convergence'])} convergence(s).")


if __name__ == "__main__":
    sys.exit(main())
