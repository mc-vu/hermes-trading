"""SEC EDGAR: 8-K und Form 4 (Insider) fuer die Watchlist-Werte, 13F-Quartalsdatensatz.

Nutzungsbedingungen / AGB-Lage:
- Oeffentliche, kostenlose Daten; automatisierter Abruf ist erlaubt, solange die
  "Fair Access"-Regeln eingehalten werden: hoechstens 10 Requests/Sekunde und ein
  User-Agent mit Kontaktadresse. Wir bleiben bei <= 5 Requests/Sekunde.
  https://www.sec.gov/os/accessing-edgar-data
  https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
- Ohne ``SEC_CONTACT_EMAIL`` (in ``~/.hermes/.env``) sind beide Adapter deaktiviert
  (Status ``not_configured``, kein Request). Die Adresse geht nur im User-Agent an die SEC.
- 13F-Datensaetze: https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets
  (quartalsweise ZIP mit TSV-Tabellen; Inhalt "as filed", VALUE seit 2023 in US-Dollar).
"""

from __future__ import annotations

import csv
import io
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta

from ..adapters.http import ApiError, NotConfigured
from .base import EventContext, EventItem, EventSource, FetchResult, day_iso, iso

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{doc}"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}-index.htm"
F13_URL = "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/{label}_form13f.zip"
TERMS = "https://www.sec.gov/os/accessing-edgar-data"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

ITEMS_8K = {
    "1.01": "Wesentlicher Vertrag", "1.02": "Vertragsende", "1.03": "Insolvenz", "1.05": "Cybersicherheitsvorfall",
    "2.01": "Kauf/Verkauf von Vermoegenswerten", "2.02": "Geschaeftszahlen", "2.03": "Neue Finanzverbindlichkeit",
    "2.04": "Ausloeser fuer Verbindlichkeit", "2.05": "Restrukturierung", "2.06": "Wertminderung",
    "3.01": "Boersennotierung/Delisting", "3.02": "Unregistrierte Aktienausgabe", "3.03": "Aenderung Aktionaersrechte",
    "4.01": "Wechsel des Abschlusspruefers", "4.02": "Fruehere Abschluesse nicht mehr verlaesslich",
    "5.01": "Kontrollwechsel", "5.02": "Wechsel in Vorstand/Aufsichtsrat", "5.03": "Satzungsaenderung",
    "5.07": "Hauptversammlung/Abstimmung", "7.01": "Regulation FD (Mitteilung)", "8.01": "Sonstige Ereignisse",
    "9.01": "Anlagen/Exhibits",
}
F4_CODES = {"P": "Kauf (Boerse)", "S": "Verkauf (Boerse)", "A": "Zuteilung", "M": "Optionsausuebung",
            "F": "Steuereinbehalt", "G": "Schenkung", "C": "Umwandlung", "D": "Rueckgabe an Emittent",
            "X": "Optionsausuebung", "J": "Sonstige"}


def sec_user_agent() -> str:
    email = (os.environ.get("SEC_CONTACT_EMAIL") or "").strip()
    if not email:
        raise NotConfigured("sec", "SEC_CONTACT_EMAIL")
    if not _EMAIL.match(email):
        raise ValueError("SEC_CONTACT_EMAIL ist keine gueltige E-Mail-Adresse")
    return f"hermes-trading/0.1 {email}"


class _SecBase(EventSource):
    env_key = "SEC_CONTACT_EMAIL"
    max_rps = 5.0
    terms_url = TERMS
    limit_note = "max. 10 Requests/s (Fair Access), im Code <= 5/s; User-Agent mit Kontaktadresse Pflicht"
    priority = 10

    def configured(self) -> bool:
        if self._api_key is not None:
            return bool(self._api_key)
        return bool((os.environ.get("SEC_CONTACT_EMAIL") or "").strip())

    def _headers(self) -> dict:
        if self._api_key:
            if not _EMAIL.match(self._api_key):
                raise ValueError("SEC-Kontaktadresse ist keine gueltige E-Mail-Adresse")
            return {"User-Agent": f"hermes-trading/0.1 {self._api_key}"}
        return {"User-Agent": sec_user_agent()}


def _issuers(watchlist: dict) -> list[dict]:
    return [i for i in watchlist["instruments"] if i.get("cik")]


def accession_key(acc: str) -> str:
    return "sec:accession:" + acc.strip()


class SecEdgarSource(_SecBase):
    """8-K und Form 4 aus ``data.sec.gov/submissions`` je Watchlist-Emittent."""

    name = "sec_edgar"
    kind = "Pflichtmeldungen 8-K und Insider (Form 4) der US-Watchlist-Werte"

    def fetch(self, ctx: EventContext) -> FetchResult:
        headers = self._headers()
        forms = set(self.options.get("forms") or ["8-K", "4"])
        since = (ctx.today - timedelta(days=int(self.options.get("lookback_days", 30)))).isoformat()
        max_details = int(self.options.get("max_form4_details", 25))
        res = FetchResult()
        details_used = 0
        for inst in _issuers(ctx.watchlist):
            cik = int(inst["cik"])
            try:
                data = self.http.get_json(SUBMISSIONS_URL.format(cik=cik), headers=headers)
                recent = (data.get("filings") or {}).get("recent") or {}
                rows = _columns_to_rows(recent)
            except (ApiError, ValueError, AttributeError) as exc:
                res.errors[inst["symbol"]] = f"{type(exc).__name__}: {exc}"
                continue
            res.ok_parts += 1
            for r in rows:
                form = r.get("form")
                if form not in forms or (r.get("filingDate") or "") < since:
                    continue
                acc = r.get("accessionNumber") or ""
                if not acc:
                    continue
                if form == "4":
                    parsed = None
                    if details_used < max_details and not (ctx.event_exists(self.name, acc) and not ctx.full):
                        details_used += 1
                        try:
                            xml = self.http.get_text(_form4_xml_url(cik, acc, r.get("primaryDocument") or ""),
                                                     headers=headers, accept="application/xml, text/xml")
                            parsed = parse_form4(xml)
                        except (ApiError, ValueError, ET.ParseError) as exc:
                            res.errors[f"{inst['symbol']}:{acc}"] = f"{type(exc).__name__}: {exc}"
                    elif ctx.event_exists(self.name, acc):
                        continue   # schon gespeichert, Details nicht erneut holen
                    res.events.append(form4_event(inst, cik, r, parsed))
                else:
                    res.events.append(form8k_event(inst, cik, r))
        return res


def _columns_to_rows(cols: dict) -> list[dict]:
    keys = list(cols)
    if not keys:
        return []
    n = len(cols[keys[0]])
    return [{k: (cols[k][i] if i < len(cols[k]) else None) for k in keys} for i in range(n)]


def _form4_xml_url(cik: int, acc: str, primary: str) -> str:
    # primaryDocument zeigt auf die XSL-Darstellung ("xslF345X05/datei.xml"); das Roh-XML liegt ohne Praefix.
    doc = primary.split("/")[-1] if primary else ""
    if not doc.endswith(".xml"):
        raise ValueError(f"Form 4 ohne XML-Dokument: {primary!r}")
    return ARCHIVE_URL.format(cik=cik, acc_nodash=acc.replace("-", ""), doc=doc)


def _accepted_time(r: dict) -> str:
    acc = r.get("acceptanceDateTime")
    if acc:
        try:
            return iso(datetime.fromisoformat(acc.replace("Z", "+00:00")))
        except ValueError:
            pass
    return day_iso(r["filingDate"])


def form8k_event(inst: dict, cik: int, r: dict) -> EventItem:
    acc = r["accessionNumber"]
    items = [x.strip() for x in (r.get("items") or "").split(",") if x.strip()]
    labels = [f"{x} {ITEMS_8K.get(x, '')}".strip() for x in items if x != "9.01"]
    ticker = inst["symbol"].split(".")[0]
    title = f"8-K {inst['name']} ({ticker}): " + ("; ".join(labels) if labels else (r.get("primaryDocDescription") or "Meldung"))
    url = INDEX_URL.format(cik=cik, acc_nodash=acc.replace("-", ""), acc=acc)
    return EventItem(
        source_id=acc, type="filing", subtype=r.get("form") or "8-K", event_time=_accepted_time(r), title=title,
        summary=f"Eingereicht {r.get('filingDate')}, Stichtag {r.get('reportDate') or '-'}; Items: {', '.join(items) or '-'}",
        url=url, country="US", tickers=[ticker], ciks=[str(cik)], dedup_keys=[accession_key(acc)],
        details={"items": items, "filing_date": r.get("filingDate"), "report_date": r.get("reportDate"),
                 "primary_document": r.get("primaryDocument")},
        raw={k: r.get(k) for k in ("accessionNumber", "form", "filingDate", "reportDate", "acceptanceDateTime",
                                   "items", "primaryDocument", "primaryDocDescription")})


def _txt(node, path: str) -> str | None:
    el = node.find(path)
    if el is None:
        return None
    v = el.find("value")
    text = (v.text if v is not None else el.text) or ""
    return text.strip() or None


def _num(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def parse_form4(xml: str) -> dict:
    root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    if root.tag != "ownershipDocument":
        raise ValueError(f"kein Form-4-Dokument (Wurzel {root.tag!r})")
    owner = root.find("reportingOwner")
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    roles = []
    if rel is not None:
        for tag, label in (("isDirector", "Director"), ("isOfficer", "Officer"), ("isTenPercentOwner", "10%-Eigner"),
                           ("isOther", "Sonstige")):
            if (rel.findtext(tag) or "").strip() in ("1", "true"):
                roles.append(label)
    tx = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        tx.append({
            "date": _txt(t, "transactionDate"),
            "code": (t.findtext("transactionCoding/transactionCode") or "").strip() or None,
            "shares": _num(_txt(t, "transactionAmounts/transactionShares")),
            "price": _num(_txt(t, "transactionAmounts/transactionPricePerShare")),
            "acq_disp": _txt(t, "transactionAmounts/transactionAcquiredDisposedCode"),
            "owned_after": _num(_txt(t, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
        })
    return {
        "issuer_ticker": (root.findtext("issuer/issuerTradingSymbol") or "").strip() or None,
        "owner": (owner.findtext("reportingOwnerId/rptOwnerName") if owner is not None else None),
        "officer_title": (rel.findtext("officerTitle") if rel is not None else None),
        "roles": roles,
        "aff10b5one": (root.findtext("aff10b5One") or "").strip() in ("1", "true"),
        "transactions": tx,
    }


def form4_event(inst: dict, cik: int, r: dict, parsed: dict | None) -> EventItem:
    acc = r["accessionNumber"]
    ticker = inst["symbol"].split(".")[0]
    url = INDEX_URL.format(cik=cik, acc_nodash=acc.replace("-", ""), acc=acc)
    details: dict = {"filing_date": r.get("filingDate"), "parsed": parsed is not None}
    if parsed:
        details.update(parsed)
        codes = sorted({str(t["code"]) for t in parsed["transactions"] if t.get("code")})
        value = sum((t["shares"] or 0) * (t["price"] or 0) for t in parsed["transactions"] if t.get("code") in ("P", "S"))
        shares = sum(t["shares"] or 0 for t in parsed["transactions"] if t.get("code") in ("P", "S"))
        who = parsed.get("owner") or "?"
        role = parsed.get("officer_title") or ", ".join(parsed.get("roles") or []) or "Insider"
        what = ", ".join(F4_CODES.get(c, c) for c in codes) or "ohne Boersengeschaeft"
        title = f"Form 4 {ticker}: {who} ({role}) - {what}"
        if value:
            title += f", {shares:,.0f} Stk., {value:,.0f} USD"
        subtype = "4:" + "".join(codes) if codes else "4"
        details["open_market_value_usd"] = round(value, 2)
    else:
        title = f"Form 4 {ticker}: Insider-Meldung (Details nicht geladen)"
        subtype = "4"
    return EventItem(
        source_id=acc, type="insider", subtype=subtype, event_time=_accepted_time(r), title=title,
        summary=f"Eingereicht {r.get('filingDate')}", url=url, country="US", tickers=[ticker], ciks=[str(cik)],
        dedup_keys=[accession_key(acc)], details=details,
        raw={k: r.get(k) for k in ("accessionNumber", "form", "filingDate", "acceptanceDateTime", "primaryDocument")})


# ------------------------------------------------------------------ 13F

def f13_labels(today: date, count: int = 4) -> list[str]:
    """Moegliche Datensatz-Namen, neueste zuerst. Seit 2024 dreimonatig, veroeffentlicht nach Ende
    Februar, Mai, August, November (z. B. ``01mar2026-31may2026``)."""
    import calendar

    out = []
    y, m = today.year, today.month
    # letztes abgeschlossenes Ende-Monat aus {2, 5, 8, 11}
    while m not in (2, 5, 8, 11):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    for _ in range(count):
        end_y, end_m = y, m
        start_m, start_y = end_m - 2, end_y
        if start_m <= 0:
            start_m += 12
            start_y -= 1
        last = calendar.monthrange(end_y, end_m)[1]
        mon = lambda k: calendar.month_abbr[k].lower()  # noqa: E731
        out.append(f"01{mon(start_m)}{start_y}-{last:02d}{mon(end_m)}{end_y}")
        m -= 3
        if m <= 0:
            m += 12
            y -= 1
    return out


class Sec13fSource(_SecBase):
    """13F-Bestaende ausgewaehlter Manager aus dem Quartalsdatensatz der SEC."""

    name = "sec_13f"
    kind = "13F-Bestaende konfigurierter Fondsmanager (SEC-Quartalsdatensatz)"
    limit_note = "Quartals-ZIP ca. 80-100 MB, nur bei neuem Datensatz; Fair Access <= 5/s"

    def fetch(self, ctx: EventContext) -> FetchResult:
        headers = self._headers()
        managers = {str(int(m["cik"])): m.get("name") for m in self.options.get("managers") or []}
        res = FetchResult()
        if not managers:
            res.details["note"] = "keine Manager konfiguriert"
            return res
        max_bytes = int(float(self.options.get("max_zip_mb", 200)) * 1024 * 1024)
        done = ctx.cursor_get(self.name, "dataset")
        for label in f13_labels(ctx.today):
            if label == done and not ctx.full:
                res.details["dataset"] = label
                res.details["note"] = "Datensatz bereits verarbeitet"
                return res
            try:
                raw = self.http.get_bytes(F13_URL.format(label=label), headers=headers, max_bytes=max_bytes)
            except ApiError as exc:
                if exc.status == 404:
                    res.details.setdefault("not_found", []).append(label)
                    continue
                raise
            events = parse_13f_zip(raw, managers, label, int(self.options.get("top_positions", 15)))
            res.events.extend(events)
            res.ok_parts += 1
            res.details["dataset"] = label
            res.details["filings"] = len(events)
            missing = sorted(set(managers) - {e.details["manager_cik"] for e in events})
            if missing:
                res.details["managers_without_filing"] = missing
            ctx.cursor_set(self.name, "dataset", label)
            return res
        raise ApiError(f"kein 13F-Datensatz gefunden (versucht: {', '.join(res.details.get('not_found', []))})")


def _tsv(z: zipfile.ZipFile, name: str):
    member = next((n for n in z.namelist() if n.split("/")[-1].upper() == name), None)
    if member is None:
        raise ValueError(f"13F-ZIP: {name} fehlt")
    with z.open(member) as fh:
        yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8", errors="replace"), delimiter="\t")


def parse_13f_zip(raw: bytes, managers: dict, label: str, top_n: int = 15) -> list[EventItem]:
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("13F-Datensatz ist kein ZIP") from None
    subs = {}
    for r in _tsv(z, "SUBMISSION.TSV"):
        cik = str(int(r.get("CIK") or 0))
        if cik in managers and (r.get("SUBMISSIONTYPE") or "").startswith("13F-HR"):
            subs[r["ACCESSION_NUMBER"]] = r
    if not subs:
        return []
    cover = {r["ACCESSION_NUMBER"]: r for r in _tsv(z, "COVERPAGE.TSV") if r.get("ACCESSION_NUMBER") in subs}
    holdings: dict[str, list] = {a: [] for a in subs}
    for r in _tsv(z, "INFOTABLE.TSV"):
        acc = r.get("ACCESSION_NUMBER")
        if acc in holdings:
            holdings[acc].append(r)
    events = []
    for acc, s in subs.items():
        cik = str(int(s["CIK"]))
        rows = holdings[acc]
        by_cusip: dict[str, dict] = {}
        for h in rows:
            if (h.get("PUTCALL") or "").strip():
                continue          # Optionen nicht als Bestand werten
            cusip = (h.get("CUSIP") or "").upper()
            agg = by_cusip.setdefault(cusip, {"issuer": h.get("NAMEOFISSUER"), "cusip": cusip, "value_usd": 0.0,
                                              "shares": 0.0, "class": h.get("TITLEOFCLASS")})
            agg["value_usd"] += float(h.get("VALUE") or 0)
            if (h.get("SSHPRNAMTTYPE") or "SH") == "SH":
                agg["shares"] += float(h.get("SSHPRNAMT") or 0)
        total = sum(p["value_usd"] for p in by_cusip.values())
        top = sorted(by_cusip.values(), key=lambda p: -p["value_usd"])[:top_n]
        for p in top:
            p["weight_pct"] = round(100 * p["value_usd"] / total, 2) if total else None
        period = s.get("PERIODOFREPORT") or ""
        filed = s.get("FILING_DATE") or ""
        name = (cover.get(acc) or {}).get("FILINGMANAGER_NAME") or managers.get(cik) or cik
        title = (f"13F {managers.get(cik) or name}: {len(by_cusip)} Positionen, {total / 1e9:,.1f} Mrd USD"
                 f" (Stichtag {_sec_date(period)})")
        summary = "Groesste Positionen: " + "; ".join(
            f"{p['issuer']} {p['weight_pct']} %" for p in top[:5]) if top else "keine Positionen"
        events.append(EventItem(
            source_id=acc, type="fund_holding", subtype=s.get("SUBMISSIONTYPE") or "13F-HR",
            event_time=day_iso(_sec_date(filed)), title=title, summary=summary,
            url=INDEX_URL.format(cik=cik, acc_nodash=acc.replace("-", ""), acc=acc), country="US",
            cusips=list(by_cusip), dedup_keys=[accession_key(acc)],
            details={"manager_cik": cik, "manager": name, "period": _sec_date(period), "dataset": label,
                     "positions": len(by_cusip), "total_value_usd": round(total, 2), "top": top},
            raw={"submission": dict(s), "cover": {k: v for k, v in (cover.get(acc) or {}).items()
                                                  if k in ("FILINGMANAGER_NAME", "REPORTCALENDARORQUARTER", "ISAMENDMENT")}}))
    return events


def _sec_date(value: str) -> str:
    """'31-MAR-2026' oder '2026-03-31' -> '2026-03-31'."""
    v = (value or "").strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unbekanntes SEC-Datum {value!r}")


__all__ = ["SecEdgarSource", "Sec13fSource", "parse_form4", "parse_13f_zip", "f13_labels"]
