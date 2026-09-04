#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scraper 22bet LiveFeed — liste -> détails par match -> JSON exploitable.
+ Modes serveur pour Render avec routes ajoutées :
    /list            : liste légère (Get1x2_VZip)
    /detail?id=N     : détail d'un match (GetGameZip)
    /list-full?..    : liste + tous les détails en un seul appel (bloquant)
    /logo?name=..    : URL de logo résolue
    /proxy/logo?...  : télécharge le logo et renvoie les bytes (anti-hotlink)
    /health          : informations pour debug (+ asset_host)

Usage CLI :
    python scraper_22bet.py --sport 85                  # JSON stdout
    python scraper_22bet.py --sport 85 --out matchs.json
    python scraper_22bet.py --serve                     # serveur HTTP Render
"""

import argparse, gzip, io, json, os, re, sys, time, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from typing import List, Dict, Optional

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE      = os.getenv("22BET_BASE", "https://22bet.com")
DELAY     = float(os.getenv("22BET_DELAY", "0.35"))   # secondes entre détail
SESSION   = requests.Session()
SESSION.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate",
    "X-Requested-With": "XMLHttpRequest",
})
_asset_host = None
PROXY_TIMEOUT = float(os.getenv("22BET_PROXY_TMO", "10"))

# ---------------------------------------------------------------------------
# Dictionnaires marchés (à ajuster / compléter par sport)
# ---------------------------------------------------------------------------
MARKET_NAME = {
    1:"Winner (1X2)",2:"Winner Home",3:"Winner Away",4:"1X(Home/Draw)",
    5:"1 (Home + Draw (1X))",6:"2 (Away + Draw (12))",
    7:"Handicap Home",8:"Handicap Away",9:"Total Over",10:"Total Under",
    11:"Home Total Over",12:"Home Total Under",13:"Away Total Over",
    14:"Away Total Under",
    731:"Exact Score",5298:"Alternative Scores",
    11273:"1st Half DH",11274:"2nd Half DH",
}
def market_label(t): return MARKET_NAME.get(t, f"Market_{t}")

GROUP_NAME = {
    1:"Result",2:"Handicap",8:"Main / Double",15:"Team Totals",
    17:"Total Goals",19:"1st Period",62:"Over/Under combos",
    136:"Exact Score",9939:"Alternative Score",
}

# ---------------------------------------------------------------------------
# Requête robuste (retry, gzip, gestion 406/429)
# ---------------------------------------------------------------------------
def http_get(url: str, timeout: int = 25, retries: int = 4, binary: bool = False):
    """Retourne dict JSON, OU bytes si binary=True (images)."""
    last = None
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=timeout)
            code = r.status_code
            if code in (406, 425) and attempt < retries - 1:   # anti-bot
                time.sleep(DELAY * (2 ** attempt) + 0.5); continue
            if code == 429:
                time.sleep(1.5 * (2 ** attempt)); continue
            if code == 404 and not binary:
                return {"_404": True, "Success": False}
            r.raise_for_status()
            if binary:
                return r.content
            try:
                return r.json()
            except Exception:
                return json.loads(gunzip(r.content))
        except (requests.RequestException, ValueError) as e:
            last = e; time.sleep(0.7 * (attempt + 1))
    return {"_error": f"{last}", "Success": False}

def gunzip(raw: bytes) -> bytes:
    if raw[:2] == b"\x1f\x8b":
        return gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    return raw

# ---------------------------------------------------------------------------
# Hôte des images + résolution de logo
# ---------------------------------------------------------------------------
def asset_host() -> str:
    global _asset_host
    if _asset_host: return _asset_host
    for page in (f"{BASE}/line/", f"{BASE}/"):
        try:
            html = SESSION.get(page, timeout=15).text
            m = re.search(r'https?://([a-z0-9.\-]+)/[^"\']*genfiles/cms/', html)
            if m: _asset_host = f"https://{m.group(1)}"; return _asset_host
        except Exception: pass
    for prefix in (f"{BASE}/image", BASE):
        try:
            r = SESSION.get(f"{prefix}/genfiles/cms/sample.png", timeout=6)
            if r.status_code in (404, 200):
                _asset_host = prefix; return _asset_host
        except Exception: pass
    _asset_host = BASE; return _asset_host

def resolve_logo(raw) -> str:
    if isinstance(raw, list): raw = raw[0] if raw else ""
    if not raw: return ""
    raw = str(raw)
    if raw.startswith("//"):  return "https:" + raw
    if raw.startswith("http"): return raw
    host = asset_host().rstrip("/")
    raw  = raw.lstrip("/")
    return f"{host}/{raw}"

# ---------------------------------------------------------------------------
# Métadonnées d'un match (depuis un objet liste OU détail)
# ---------------------------------------------------------------------------
def meta_from(m: Dict) -> Dict:
    ts = m.get("S"); dt = None
    if ts: dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()
    def team(s):
        i = m.get(f"O{s}I"); arr = m.get(f"O{s}IMG", []) or []
        fn = arr[0] if isinstance(arr, list) and arr else (f"{i}.png" if i else "")
        return {"name": m.get(f"O{s}"), "name_ru": m.get(f"O{s}R"),
                "name_en": m.get(f"O{s}E"), "team_id": i,
                "country_id": m.get(f"O{s}C"), "city": m.get(f"O{s}CT"),
                "logo_url": resolve_logo(fn)}
    sc = m.get("SC") or {}
    return {
        "game_id": m.get("N"), "feed_id": m.get("I"),
        "sport_id": m.get("SI"), "sport": m.get("SN"),
        "league": m.get("L"), "league_id": m.get("LI"),
        "country": m.get("CN"), "start_utc": dt, "start_ts": ts,
        "status_text": sc.get("I"), "home": team(1), "away": team(2),
        "league_logo_url": resolve_logo(m.get("CHIMG")),
        "status_score": sc.get("FS") or {},
    }

def group_markets(events: List[Dict]) -> List[Dict]:
    groups: Dict[int, list] = {}
    for e in events or []:
        g = e.get("G") or 0
        groups.setdefault(g, []).append({
            "market": market_label(e.get("T")), "type_id": e.get("T"),
            "line": e.get("P"), "odds": e.get("C"), "odds_cv": e.get("CV"),
            "central": bool(e.get("CE")), "blocked": bool(e.get("B")),
        })
    return [{"group_id": g, "group_name": GROUP_NAME.get(g, f"Group_{g}"),
             "n": len(groups[g]), "markets": groups[g]}
            for g in sorted(groups)]

# ---------------------------------------------------------------------------
# Récupération : liste + détail
# ---------------------------------------------------------------------------
def fetch_list(sport: int, count: int, mode: int, lng: str) -> List[Dict]:
    url = (f"{BASE}/LiveFeed/Get1x2_VZip?sports={sport}&count={count}"
           f"&lng={lng}&mode={mode}&cyberFlag=1")
    data = http_get(url)
    if not data.get("Success"): raise RuntimeError(data.get("Error") or "liste KO")
    return data.get("Value") or []

def fetch_detail(game_id: int, lng: str) -> Dict:
    return http_get(f"{BASE}/LiveFeed/GetGameZip?id={game_id}&lng={lng}")

def build_match(row, detail, degraded: bool) -> Dict:
    meta = meta_from(row)
    meta["degraded"] = degraded
    if detail and detail.get("Value"):
        v = detail["Value"]
        meta["markets_groups"] = group_markets(v.get("E") or [])
        meta["n_markets_total"] = v.get("EC")
        meta["market_categories"] = v.get("MEC")
        meta["game_extra"] = {kv["Key"]: kv["Value"]
                              for kv in (v.get("SC") or {}).get("S", [])}
        # enrichissement si le détail porte des infos absentes de la liste
        for k, src in (("league","L"),("country","CN")):
            if v.get(src): meta[k] = v.get(src)
        meta["home"].update(name=meta["home"].get("name") or v.get("O1"))
        meta["away"].update(name=meta["away"].get("name") or v.get("O2"))
    else:
        ev = list(row.get("E") or [])
        for ae in row.get("AE") or []:
            ev += [dict(x, G=ae.get("G")) for x in ae.get("ME", [])]
        meta["markets_groups"] = group_markets(ev)
        meta["n_markets_total"] = row.get("EC")
    return meta

# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run(sport: int, count: int, mode: int, lng: str,
        with_details: bool, progress: Optional[dict] = None) -> Dict:
    rows = fetch_list(sport, count, mode, lng)
    total = len(rows)
    out = {"fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
           "sport": sport, "count": count, "listed": total,
           "asset_host": asset_host(), "matches": []}
    if progress: progress["total"] = total; progress["done"] = 0
    for idx, row in enumerate(rows):
        if with_details and idx:
            time.sleep(DELAY)               # rate-limit explicite
        detail = None; degraded = False
        if with_details:
            try:
                d = fetch_detail(row.get("N"), lng)
                if d.get("_404") or not d.get("Value"):
                    degraded = True
                else:
                    detail = d
            except Exception:
                degraded = True
        match = build_match(row, detail, degraded)
        # si détail absent mais qu'il reste un petit score/statut, on garde une valeureuse ligne
        out["matches"].append(match)
        if progress:
            progress["done"] += 1
    return out

# ---------------------------------------------------------------------------
# Serveur HTTP
# ---------------------------------------------------------------------------
def _send_json(h, obj, status=200):
    b = json.dumps(obj, ensure_ascii=False).encode()
    h.send_response(status)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Cache-Control", "no-store")
    h.send_header("Content-Length", str(len(b)))
    h.end_headers()
    h.wfile.write(b)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    # ------------------ CORS pour appel depuis navigateur/site ------------------
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query); p = u.path.lstrip("/")
        def gi(k, d): 
            try: return int(q.get(k, [d])[0])
            except Exception: return d
        try:
            if p == "favicon.ico":
                return self.send_error(404)
            if p == "health":
                d = {"ok": True, "asset_host": asset_host(), "base": BASE,
                     "delay": DELAY, "time_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
                if self.headers.get("Origin"):  self._cors()
                return _send_json(self, d)
            if p == "list":
                mode  = gi("mode", 1); count = min(gi("count", 100), 500)
                res   = run(gi("sport", 85), count, mode, "en", False)
                if self.headers.get("Origin"): self._cors()
                return _send_json(self, res)
            if p == "detail":
                res = fetch_detail(gi("id", 0), "en")
                v = res.get("Value")
                body = {"meta": meta_from(v) if v else {},
                        "markets": group_markets(v.get("E") or []) if v else [],
                        "market_categories": (v or {}).get("MEC"),
                        "n_total": (v or {}).get("EC")}
                if self.headers.get("Origin"): self._cors()
                return _send_json(self, body)
            if p == "list-full":
                mode  = gi("mode", 1); count = min(gi("count", 50), 500)
                res   = run(gi("sport", 85), count, mode, "en", True)
                if self.headers.get("Origin"): self._cors()
                return _send_json(self, res)
            if p == "logo":
                name = (q.get("name") or [""])[0]
                if self.headers.get("Origin"): self._cors()
                return _send_json(self, {"logo_url": resolve_logo(name)})
            if p == "proxy/logo":
                return self._proxy_logo(q)
            self.send_error(404)
        except Exception as ex:
            if self.headers.get("Origin"): self._cors()
            return _send_json(self, {"error": str(ex)}, 500)

    # ------------------ Proxy images (contourne hotlink CDN) ------------------
    def _proxy_logo(self, q):
        name = (q.get("name") or [""])[0]
        if not name:
            return _send_json(self, {"error": "name requis"}, 400)
        url = resolve_logo(name)
        # cache-négation : on garde l'image en mémoire le temps du hit seulement
        data = http_get(url, timeout=PROXY_TIMEOUT, binary=True)
        if isinstance(data, dict):           # erreur
            return _send_json(self, {"error": str(data)}, 502)
        ctype = "image/png"
        low = name.lower()
        if low.endswith(".jpg") or low.endswith(".jpeg"): ctype = "image/jpeg"
        elif low.endswith(".gif"): ctype = "image/gif"
        elif low.endswith(".svg"): ctype = "image/svg+xml"
        elif low.endswith(".webp"): ctype = "image/webp"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

# ---------------------------------------------------------------------------
def serve():
    port = int(os.getenv("PORT", "8000"))
    print(f"22bet-explorer démarré sur :{port} (asset_host={asset_host()})")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()

# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", type=int, default=85)
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--mode", type=int, default=1)
    ap.add_argument("--lng", default="en")
    ap.add_argument("--no-details", action="store_true")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    if a.serve:
        serve(); sys.exit(0)
    res = run(a.sport, a.count, a.mode, a.lng, not a.no_details, {})
    txt = json.dumps(res, ensure_ascii=False, indent=2)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(txt)
        print(f"{len(res['matches'])} matchs -> {a.out}")
    else:
        print(txt)