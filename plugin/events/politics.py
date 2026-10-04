"""US-Politik: Politiker-Trades (PTR), Ausschuesse, Gesetzgebung.

Quellen und Nutzungsbedingungen / AGB-Lage:
- Tracefour (https://tracefour.com/api-docs): Read-only-JSON, ohne Schluessel 60 Requests/Stunde
  je IP (600 mit kostenlosem Schluessel ``TRACEFOUR_API_KEY``, optional). Kompilation CC BY 4.0,
  Quelle verlinken (meta.attribution); zugrundeliegende Meldungen gemeinfrei (U.S. government works).
  Die API ist ausdruecklich fuer Programme gedacht; robots.txt sperrt nur /api/ und /embed/ fuer
  Crawler, nicht /v1/.
- House Clerk (https://disclosures-clerk.house.gov/FinancialDisclosure): offizieller Jahresindex
  ``<Jahr>FD.zip`` (XML) aller Offenlegungen. Oeffentliche Behoerdendaten, keine robots.txt.
  Dient als Abgleich: welche PTRs (FilingType P) wurden gemeldet, sind sie bei Tracefour schon da?
- Senate eFD (efdsearch.senate.gov) wird NICHT automatisch abgerufen: die Nutzungsbedingungen
  verbieten u. a. kommerzielle Nutzung und verlangen eine Bestaetigung je Sitzung.
- congress-legislators (https://github.com/unitedstates/congress-legislators): Ausschuss-
  Mitgliedschaften, gemeinfrei (CC0), statische JSON-Dateien auf GitHub Pages.
- congress.gov API (https://api.congress.gov/, Schluessel ``CONGRESS_API_KEY`` von api.data.gov):
  Gesetzentwuerfe; api.data.gov-Limit 5.000 Requests/Stunde je Schluessel.
  https://www.loc.gov/legal/  ·  https://api.data.gov/docs/rate-limits/
"""

from __future__ import annotations

import io
import json
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta

from ..adapters.http import ApiError
from .base import EventContext, EventItem, EventSource, FetchResult, day_iso, iso, parse_time

TRACEFOUR = "https://tracefour.com"
HOUSE_ZIP = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_PTR_PDF = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
LEGISLATORS = "https://unitedstates.github.io/congress-legislators/legislators-current.json"
COMMITTEES = "https://unitedstates.github.io/congress-legislators/committees-current.json"
MEMBERSHIP = "https://unitedstates.github.io/congress-legislators/committee-membership-current.json"
CONGRESS_API = "https://api.congress.gov/v3"
_PDF_DOC = re.compile(r"/ptr-pdfs/(\d{4})/(\d+)\.pdf", re.I)


def slugify(name: str) -> str:
    t = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def house_doc_key(doc_id: str) -> str:
    return f"house:ptr:{doc_id}"


def politician_committees(conn, *, bioguide: str | None = None, slug: str | None = None,
                          last_name: str | None = None, state: str | None = None) -> list[str]:
    """Ausschuesse (thomas_id) eines Politikers, gefunden ueber bioguide, Tracefour-Slug oder Nachname+Staat."""
    if not bioguide and slug:
        row = conn.execute("SELECT bioguide FROM politician WHERE tracefour_slug = ?", (slug,)).fetchone()
        bioguide = row[0] if row else None
    if not bioguide and last_name:
        rows = conn.execute("SELECT bioguide FROM politician WHERE lower(last_name) = lower(?)"
                            + (" AND state = ?" if state else ""), (last_name, state) if state else (last_name,)).fetchall()
        bioguide = rows[0][0] if len(rows) == 1 else None
    if not bioguide:
        return []
    return [r[0] for r in conn.execute("SELECT thomas_id FROM politician_committee WHERE bioguide = ? ORDER BY thomas_id",
                                       (bioguide,))]


# ------------------------------------------------------------------ Tracefour

class _TracefourBase(EventSource):
    env_key = None
    max_rps = 1.0
    terms_url = "https://tracefour.com/api-docs"
    limit_note = "ohne Schluessel 60 Requests/Stunde je IP (mit freiem Schluessel 600/h); CC BY 4.0"

    def _headers(self) -> dict:
        key = (os.environ.get("TRACEFOUR_API_KEY") or "").strip()
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _get(self, path: str, params: dict | None = None):
        data = self.http.get_json(TRACEFOUR + path, params, headers=self._headers())
        if not isinstance(data, dict) or "data" not in data:
            raise ApiError(f"Tracefour: unerwartete Antwort fuer {path}", body=str(data)[:200])
        if data.get("error"):
            raise ApiError(f"Tracefour: {data['error']}")
        return data["data"]


class TracefourPtrSource(_TracefourBase):
    """STOCK-Act-Trades (PTR) von Kongressmitgliedern ueber Tracefour."""

    name = "tracefour_ptr"
    kind = "Politiker-Trades (House/Senate-PTR) ueber Tracefour"
    priority = 6

    def fetch(self, ctx: EventContext) -> FetchResult:
        res = FetchResult()
        budget = int(self.options.get("max_requests", 20))
        since = (ctx.today - timedelta(days=int(self.options.get("lookback_days", 120)))).isoformat()
        members = self._get("/v1/congress")
        budget -= 1
        if not isinstance(members, list):
            raise ApiError("Tracefour /v1/congress: Liste erwartet")
        by_slug = {m["slug"]: m for m in members if m.get("slug")}
        res.details["members_total"] = len(by_slug)
        _link_tracefour_slugs(ctx.conn, members)

        # Reihenfolge: (1) Mitglieder mit neuen House-PTRs ohne Tracefour-Eintrag, (2) follow, (3) Rotation.
        wanted = []
        for slug in _slugs_with_open_house_ptrs(ctx, by_slug):
            wanted.append(slug)
        wanted += [s for s in self.options.get("follow") or [] if s in by_slug]
        rot = sorted(by_slug)
        start = int(ctx.cursor_get(self.name, "rotation") or 0) % max(len(rot), 1)
        wanted += rot[start:] + rot[:start]
        seen, order = set(), []
        for s in wanted:
            if s not in seen:
                seen.add(s)
                order.append(s)
        unknown_follow = [s for s in self.options.get("follow") or [] if s not in by_slug]
        if unknown_follow:
            res.details["follow_unknown"] = unknown_follow

        fetched = 0
        for slug in order:
            if budget <= 0:
                break
            budget -= 1
            try:
                data = self._get(f"/v1/congress/{slug}")
            except ApiError as exc:
                res.errors[slug] = str(exc)
                if exc.status == 429:
                    break
                continue
            fetched += 1
            res.ok_parts += 1
            member = data.get("member") or {}
            for t in data.get("trades") or []:
                if (t.get("disclosureDate") or "") < since:
                    continue
                try:
                    res.events.append(ptr_event(ctx.conn, member, t))
                except ValueError as exc:
                    res.errors[f"{slug}:{t.get('filingId')}"] = f"ValueError: {exc}"
            if slug in rot:
                ctx.cursor_set(self.name, "rotation", str((rot.index(slug) + 1) % len(rot)))
        res.details["members_fetched"] = fetched
        res.ok_parts += 1   # Mitgliederliste
        return res


def _link_tracefour_slugs(conn, members: list[dict]) -> None:
    """Tracefour-Slug an Politiker (aus congress-legislators) haengen: Kammer + Name."""
    rows = conn.execute("SELECT bioguide, name, first_name, last_name, chamber FROM politician").fetchall()
    if not rows:
        return
    index = {}
    for r in rows:
        for variant in {slugify(r["name"]), slugify(f"{r['first_name']} {r['last_name']}")}:
            index.setdefault((r["chamber"], variant), r["bioguide"])
    with conn:
        for m in members:
            bg = index.get((m.get("chamber"), m.get("slug"))) or index.get((m.get("chamber"), slugify(m.get("name", ""))))
            if bg:
                conn.execute("UPDATE politician SET tracefour_slug = ? WHERE bioguide = ?", (m["slug"], bg))


def _slugs_with_open_house_ptrs(ctx: EventContext, by_slug: dict) -> list[str]:
    """House-PTRs aus dem Clerk-Index, zu denen es noch keinen Tracefour-Trade gibt -> Mitglied zuerst abfragen."""
    rows = ctx.conn.execute(
        "SELECT e.details_json FROM event e WHERE e.source = 'house_clerk' AND NOT EXISTS ("
        " SELECT 1 FROM event_key k1 JOIN event_key k2 ON k1.key = k2.key AND k2.event_id != k1.event_id"
        " JOIN event o ON o.id = k2.event_id AND o.source = 'tracefour_ptr' WHERE k1.event_id = e.id)"
        " ORDER BY e.event_time DESC LIMIT 50").fetchall()
    house = {s: m for s, m in by_slug.items() if m.get("chamber") == "house"}
    by_last: dict[str, list[str]] = {}
    for s, m in house.items():
        last = slugify((m.get("name") or "").split()[-1]) if m.get("name") else ""
        by_last.setdefault(last, []).append(s)
    out = []
    for (dj,) in rows:
        d = json.loads(dj or "{}")
        cands = by_last.get(slugify(d.get("last") or ""), [])
        if len(cands) == 1:
            out.append(cands[0])
        else:
            dist = (d.get("state_district") or "").replace("-", "")
            out += [s for s in cands if (house[s].get("district") or "").replace("-", "") == dist]
    return out


def ptr_event(conn, member: dict, t: dict) -> EventItem:
    name = t.get("memberName") or member.get("memberName") or t.get("memberSlug")
    ticker = (t.get("ticker") or "").strip().upper() or None
    typ = t.get("type") or "?"
    asset = t.get("assetDescription") or ticker or "?"
    party = t.get("party") or member.get("party")
    title = f"PTR {name} ({party or '?'}-{t.get('district') or member.get('district') or '?'}): {typ} {asset}"
    if ticker:
        title += f" ({ticker})"
    title += f", {t.get('amountLabel') or 'Betrag ?'}"
    summary = (f"Trade {t.get('transactionDate')}, gemeldet {t.get('disclosureDate')}, Eigentuemer {t.get('owner') or '-'},"
               f" Typ {t.get('assetType') or '-'}")
    link = t.get("sourceLink") or ""
    cross = []
    m = _PDF_DOC.search(link)
    if m:
        cross.append(house_doc_key(m.group(2)))
    committees = politician_committees(conn, slug=t.get("memberSlug") or member.get("slug"))
    sub = {"purchase": "purchase", "sale": "sale", "sale (full)": "sale", "sale (partial)": "sale",
           "exchange": "exchange"}.get(typ.lower(), typ.lower())
    return EventItem(
        source_id=t.get("filingId") or f"{t.get('memberSlug')}|{ticker}|{t.get('transactionDate')}|{typ}",
        type="ptr", subtype=sub, event_time=day_iso(str(t.get("disclosureDate") or t.get("transactionDate") or "")),
        title=title, summary=summary, url=link or f"{TRACEFOUR}/congress/{t.get('memberSlug')}", country="US",
        tickers=[ticker] if ticker else [], committees=committees, cross_keys=cross, url_key=False,
        details={"member": name, "member_slug": t.get("memberSlug"), "party": party, "chamber": t.get("chamber"),
                 "transaction_date": t.get("transactionDate"), "disclosure_date": t.get("disclosureDate"),
                 "amount_min": t.get("amountMin"), "amount_max": t.get("amountMax"), "owner": t.get("owner"),
                 "asset_type": t.get("assetType"), "committees": committees,
                 "attribution": "Data via Tracefour (https://tracefour.com), CC BY 4.0"},
        raw=t)


class TracefourForm4Source(_TracefourBase):
    """Form-4-Boersengeschaefte (P/S) der US-Watchlist-Werte ueber Tracefour (ohne Schluessel)."""

    name = "tracefour_form4"
    kind = "Insider-Kaeufe/-Verkaeufe (Form 4, open market) der US-Watchlist-Werte ueber Tracefour"
    priority = 6

    def fetch(self, ctx: EventContext) -> FetchResult:
        res = FetchResult()
        since = (ctx.today - timedelta(days=int(self.options.get("lookback_days", 30)))).isoformat()
        for inst in ctx.watchlist["instruments"]:
            if inst.get("market") != "us_equity":
                continue
            ticker = inst["symbol"].split(".")[0]
            try:
                rows = self._get("/v1/filings", {"ticker": ticker, "direction": "all", "since": since, "limit": 100})
            except ApiError as exc:
                res.errors[ticker] = str(exc)
                if exc.status == 429:
                    break
                continue
            res.ok_parts += 1
            groups: dict[str, list] = {}
            for r in rows or []:
                acc = (r.get("filingId") or "").rsplit("-", 1)[0]
                if acc:
                    groups.setdefault(acc, []).append(r)
            for acc, legs in groups.items():
                try:
                    res.events.append(tracefour_form4_event(acc, legs))
                except ValueError as exc:
                    res.errors[f"{ticker}:{acc}"] = f"ValueError: {exc}"
        return res


def tracefour_form4_event(acc: str, legs: list[dict]) -> EventItem:
    f = legs[0]
    codes = sorted({str(x["transactionCode"]) for x in legs if x.get("transactionCode")})
    value = sum(float(x.get("totalValue") or 0) for x in legs)
    shares = sum(float(x.get("shares") or 0) for x in legs)
    word = {"P": "Kauf (Boerse)", "S": "Verkauf (Boerse)"}
    title = (f"Form 4 {f.get('issuerTicker')}: {f.get('ownerName')} ({f.get('ownerTitle') or 'Insider'}) - "
             f"{', '.join(word.get(c, c) for c in codes)}, {shares:,.0f} Stk., {value:,.0f} USD")
    if f.get("is10b5_1"):
        title += " [10b5-1-Plan]"
    filed = str(f.get("filedAt") or f.get("periodOfReport") or "")
    ticker = str(f.get("issuerTicker") or "")
    cik = str(f.get("cik") or "")
    return EventItem(
        source_id=acc, type="insider", subtype="4:" + "".join(codes), event_time=iso(parse_time(filed)), title=title,
        summary=f"Trade {f.get('transactionDate')}, gemeldet {filed[:10]}, {len(legs)} Teil-Transaktion(en)",
        url=f.get("rawFilingUrl") or f.get("url"), country="US",
        tickers=[ticker] if ticker else [], ciks=[cik] if cik else [],
        dedup_keys=[f"sec:accession:{acc}"],
        details={"owner": f.get("ownerName"), "owner_title": f.get("ownerTitle"), "codes": codes,
                 "open_market_value_usd": round(value, 2), "shares": shares, "is10b5_1": f.get("is10b5_1"),
                 "attribution": "Data via Tracefour (https://tracefour.com/filings), CC BY 4.0"},
        raw=legs[:5])


# ------------------------------------------------------------------ House Clerk

class HouseClerkSource(EventSource):
    """Offizieller House-Index: neue PTR-Meldungen (FilingType P) als Abgleich zu Tracefour."""

    name = "house_clerk"
    kind = "House-Clerk-Index der PTR-Meldungen (Abgleich, offizielle Quelle)"
    env_key = None
    max_rps = 1.0
    terms_url = "https://disclosures-clerk.house.gov/FinancialDisclosure"
    limit_note = "kein veroeffentlichtes Limit; 1 ZIP (~60 KB) je Jahr und Lauf"
    # Niedriger als Tracefour: der Index-Eintrag ist nur ein Hinweis "Meldung eingegangen"; sobald
    # Tracefour die Einzeltrades liefert, zeigt der Eintrag per dup_of auf einen davon.
    priority = 4

    def fetch(self, ctx: EventContext) -> FetchResult:
        res = FetchResult()
        since = ctx.today - timedelta(days=int(self.options.get("lookback_days", 60)))
        years = sorted({since.year, ctx.today.year})
        for year in years:
            try:
                raw = self.http.get_bytes(HOUSE_ZIP.format(year=year), max_bytes=20 * 1024 * 1024)
                members = parse_house_index(raw)
            except (ApiError, ValueError) as exc:
                res.errors[str(year)] = f"{type(exc).__name__}: {exc}"
                continue
            res.ok_parts += 1
            for m in members:
                if m["filing_type"] != "P" or m["filing_date"] < since.isoformat():
                    continue
                res.events.append(house_ptr_event(ctx.conn, m))
        return res


def parse_house_index(raw: bytes) -> list[dict]:
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("House-Index ist kein ZIP") from None
    name = next((n for n in z.namelist() if n.lower().endswith(".xml")), None)
    if not name:
        raise ValueError("House-Index: XML fehlt im ZIP")
    root = ET.fromstring(z.read(name))
    out = []
    for m in root.iter("Member"):
        fd = (m.findtext("FilingDate") or "").strip()
        try:
            filing_date = datetime.strptime(fd, "%m/%d/%Y").date().isoformat()
        except ValueError:
            continue
        out.append({
            "prefix": (m.findtext("Prefix") or "").strip(), "first": (m.findtext("First") or "").strip(),
            "last": (m.findtext("Last") or "").strip(), "suffix": (m.findtext("Suffix") or "").strip(),
            "filing_type": (m.findtext("FilingType") or "").strip(), "state_district": (m.findtext("StateDst") or "").strip(),
            "year": (m.findtext("Year") or "").strip(), "filing_date": filing_date, "doc_id": (m.findtext("DocID") or "").strip(),
        })
    return out


def house_ptr_event(conn, m: dict) -> EventItem:
    name = " ".join(x for x in (m["first"], m["last"], m["suffix"]) if x)
    state = m["state_district"][:2] or None
    committees = politician_committees(conn, last_name=m["last"], state=state)
    year = m["year"] or m["filing_date"][:4]
    return EventItem(
        source_id=m["doc_id"], type="ptr", subtype="house_filing", event_time=day_iso(m["filing_date"]),
        title=f"PTR-Meldung House: {name} ({m['state_district']}), eingereicht {m['filing_date']}",
        summary="Offizieller Eintrag im House-Clerk-Index; Einzeltrades stehen im PDF bzw. bei Tracefour.",
        url=HOUSE_PTR_PDF.format(year=year, doc=m["doc_id"]), country="US", committees=committees,
        cross_keys=[house_doc_key(m["doc_id"])], url_key=False,
        details={"first": m["first"], "last": m["last"], "state_district": m["state_district"],
                 "filing_date": m["filing_date"], "doc_id": m["doc_id"], "committees": committees},
        raw=m)


# ------------------------------------------------------------------ Ausschuesse

class CongressCommitteesSource(EventSource):
    """Stammdaten Politiker -> Ausschuss (congress-legislators, CC0) und Ausschuss -> Branche."""

    name = "congress_committees"
    kind = "Stammdaten Politiker -> Ausschuss -> Branche (congress-legislators, CC0)"
    env_key = None
    max_rps = 1.0
    terms_url = "https://github.com/unitedstates/congress-legislators"
    limit_note = "statische Dateien (GitHub Pages), 3 Abrufe je Lauf, hoechstens einmal pro Tag"

    def fetch(self, ctx: EventContext) -> FetchResult:
        res = FetchResult()
        last = ctx.cursor_get(self.name, "loaded_on")
        if last == ctx.today.isoformat() and not ctx.full:
            res.details["note"] = "heute bereits geladen"
            return res
        legislators = self.http.get_json(LEGISLATORS)
        committees = self.http.get_json(COMMITTEES)
        membership = self.http.get_json(MEMBERSHIP)
        if not (isinstance(legislators, list) and isinstance(committees, list) and isinstance(membership, dict)):
            raise ApiError("congress-legislators: unerwartetes Format")
        from .registry import load_sectors
        from .matcher import Matcher

        sectors = load_sectors()
        matcher = Matcher(ctx.conn, sectors)
        now = iso(ctx.now)
        n = 0
        with ctx.conn:
            for p in legislators:
                term = (p.get("terms") or [{}])[-1]
                nm = p.get("name") or {}
                full = nm.get("official_full") or f"{nm.get('first', '')} {nm.get('last', '')}".strip()
                chamber = {"rep": "house", "sen": "senate"}.get(term.get("type"))
                district = f"{term.get('state')}{int(term['district']):02d}" if term.get("district") is not None else term.get("state")
                ctx.conn.execute(
                    "INSERT INTO politician(bioguide, name, first_name, last_name, chamber, state, district, party, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(bioguide) DO UPDATE SET name=excluded.name,"
                    " first_name=excluded.first_name, last_name=excluded.last_name, chamber=excluded.chamber,"
                    " state=excluded.state, district=excluded.district, party=excluded.party, updated_at=excluded.updated_at",
                    (p["id"]["bioguide"], full, nm.get("first"), nm.get("last"), chamber, term.get("state"), district,
                     term.get("party"), now))
                n += 1
            for c in committees:
                rows = [(c["thomas_id"], c["name"], c.get("type"), None)]
                rows += [(c["thomas_id"] + s["thomas_id"], f"{c['name']} - {s['name']}", c.get("type"), c["thomas_id"])
                         for s in c.get("subcommittees") or []]
                for tid, cname, chamber, parent in rows:
                    ctx.conn.execute(
                        "INSERT INTO committee(thomas_id, name, chamber, parent_id, sectors_json, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(thomas_id) DO UPDATE SET name=excluded.name,"
                        " chamber=excluded.chamber, parent_id=excluded.parent_id, sectors_json=excluded.sectors_json,"
                        " updated_at=excluded.updated_at",
                        (tid, cname, chamber, parent, json.dumps(matcher.committee_sectors(tid)), now))
                    n += 1
            ctx.conn.execute("DELETE FROM politician_committee")
            for tid, mems in membership.items():
                for mm in mems:
                    if mm.get("bioguide"):
                        ctx.conn.execute("INSERT OR REPLACE INTO politician_committee(bioguide, thomas_id, title, rank)"
                                         " VALUES (?, ?, ?, ?)", (mm["bioguide"], tid, mm.get("title"), mm.get("rank")))
                        n += 1
        ctx.cursor_set(self.name, "loaded_on", ctx.today.isoformat())
        res.items_extra = n
        res.ok_parts = 3
        res.details.update({"politicians": len(legislators), "committees": len(committees),
                            "memberships": sum(len(v) for v in membership.values())})
        return res


# ------------------------------------------------------------------ Gesetzentwuerfe

class CongressBillsSource(EventSource):
    """Gesetzentwuerfe mit Ausschuss-Zuordnung (congress.gov API)."""

    name = "congress_bills"
    kind = "US-Gesetzentwuerfe (congress.gov), Ausschuss -> Branche"
    env_key = "CONGRESS_API_KEY"
    max_rps = 1.0
    terms_url = "https://api.congress.gov/"
    limit_note = "5.000 Requests/Stunde je Schluessel (api.data.gov)"
    priority = 8

    def fetch(self, ctx: EventContext) -> FetchResult:
        key = self.require_key()
        res = FetchResult()
        since = ctx.now - timedelta(days=int(self.options.get("lookback_days", 7)))
        params = {"format": "json", "limit": 250, "sort": "updateDate+desc",
                  "fromDateTime": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "toDateTime": ctx.now.strftime("%Y-%m-%dT%H:%M:%SZ")}
        headers = {"X-Api-Key": key}
        max_bills = int(self.options.get("max_bills", 250))
        bills: list = []
        offset = 0
        while len(bills) < max_bills:
            data = self.http.get_json(f"{CONGRESS_API}/bill", {**params, "offset": offset}, headers=headers)
            if not isinstance(data, dict) or "bills" not in data:
                raise ApiError("congress.gov: 'bills' fehlt", body=str(data)[:200])
            page = data["bills"] or []
            bills += page
            res.ok_parts += 1
            if len(page) < 250 or not (data.get("pagination") or {}).get("next"):
                break
            offset += 250
        for b in bills[:max_bills]:
            res.events.append(bill_event(ctx.conn, b))
        res.details["bills"] = len(bills[:max_bills])
        return res


def bill_event(conn, b: dict) -> EventItem:
    num = f"{(b.get('type') or '').upper()} {b.get('number')}"
    action = b.get("latestAction") or {}
    title = f"{num} ({b.get('congress')}. Kongress): {b.get('title')}"
    committees = []
    # Die Listenantwort nennt keine Ausschuesse; der Titel + letzte Aktion laufen ueber die Stichwoerter.
    m = re.search(r"Committee on ([A-Za-z ,'&-]+)", action.get("text") or "")
    if m:
        row = conn.execute("SELECT thomas_id FROM committee WHERE parent_id IS NULL AND name LIKE ?",
                           (f"%{m.group(1).strip()}%",)).fetchone()
        if row:
            committees.append(row[0])
    url = f"https://www.congress.gov/bill/{b.get('congress')}th-congress/{_bill_path(b.get('type'))}/{b.get('number')}"
    when = action.get("actionDate") or (b.get("updateDate") or "")[:10]
    return EventItem(
        source_id=f"{b.get('congress')}-{(b.get('type') or '').lower()}-{b.get('number')}-{when}", type="bill",
        subtype=(b.get("type") or "").lower(), event_time=day_iso(when), title=title,
        summary=f"Letzte Aktion {when}: {action.get('text') or '-'}", url=url, country="US", committees=committees,
        details={"congress": b.get("congress"), "bill_type": b.get("type"), "number": b.get("number"),
                 "origin_chamber": b.get("originChamber"), "latest_action": action, "committees": committees,
                 "update_date": b.get("updateDate")},
        raw=b, url_key=False)


def _bill_path(t: str | None) -> str:
    return {"HR": "house-bill", "S": "senate-bill", "HRES": "house-resolution", "SRES": "senate-resolution",
            "HJRES": "house-joint-resolution", "SJRES": "senate-joint-resolution",
            "HCONRES": "house-concurrent-resolution", "SCONRES": "senate-concurrent-resolution"}.get((t or "").upper(), "bill")
