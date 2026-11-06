"""Importers for LCS-Training's existing spreadsheets.

* ``import_price_worksheet`` reads a Quote Pricing Worksheet (Price Quote Sheet +
  Zone Rates tabs) into the price list for a school year.
* ``import_client_spreadsheet`` reads the "Client Spreadsheet - Contracts and
  Services" workbook (all tracking tabs) into clients, contacts, leads,
  contracts, onboarding, deliveries, coaches and billing schedules.

Both return an ImportReport the app shows on screen, listing every name match it
was not sure about so nothing is merged silently.
"""
from __future__ import annotations

import datetime as dt
import difflib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import openpyxl
from sqlalchemy import select

from . import logic
from .models import (PARTY_DISTRICT, PARTY_SELPA, BillingInstallment, Coach, CoachMonthCapacity, ConsultPlan,
                     Contact, ContactTag, ContactTagItem, ContractPaperwork, ContractService, LcsDistrict,
                     LcsSelpa, Lead, OnboardingTask, PriceItem, ServiceDeliveryLog, ServiceType,
                     TrainingContract, ZoneRate, school_year_bounds, school_year_for)
from .seed import PAPERWORK_STEPS


@dataclass
class ImportReport:
    title: str
    counts: dict = field(default_factory=lambda: defaultdict(int))
    matched: list = field(default_factory=list)      # (name in sheet, tab, matched to, rule)
    created: list = field(default_factory=list)      # (client, first seen in tab, other tabs)
    warnings: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def as_dict(self):
        return {"title": self.title, "counts": dict(self.counts), "matched": self.matched,
                "created": self.created, "warnings": self.warnings, "notes": self.notes}


# ---------------------------------------------------------------------------
# Cell helpers
# ---------------------------------------------------------------------------
DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})")
DONE_WORDS = {"y", "yes", "x", "done", "complete", "completed", "sent", "received", "already have", "✓"}
NA_WORDS = {"n/a", "na", "n.a.", "not applicable"}
MONTHS = {dt.date(2000, i, 1).strftime("%B").lower(): i for i in range(1, 13)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})


def s(v) -> str:
    return "" if v is None else str(v).strip()


def to_date(v, school_year: Optional[str] = None) -> Optional[dt.date]:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    t = s(v)
    if not t:
        return None
    m = DATE_RE.search(t)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + 2000 if y < 100 else y
        try:
            return dt.date(y, mo, d)
        except ValueError:
            return None
    if school_year and t.lower().split()[0] in MONTHS:
        mo = MONTHS[t.lower().split()[0]]
        start, end = school_year_bounds(school_year)
        return dt.date(start.year if mo >= 7 else end.year, mo, 1)
    return None


def to_step(v) -> tuple[str, Optional[dt.date], Optional[str]]:
    """Spreadsheet cell -> (status, date, note). Free text is kept as a note, never lost."""
    if v is None or s(v) == "":
        return "Not Started", None, None
    if isinstance(v, (dt.datetime, dt.date)):
        return "Done", to_date(v), None
    if isinstance(v, (int, float)):
        return "Done", None, f"{v:g}"
    t = s(v)
    low = t.lower().rstrip(".")
    if low in DONE_WORDS:
        return "Done", None, (t if low == "already have" else None)
    if low in NA_WORDS:
        return "N/A", None, None
    d = to_date(t)
    if d:
        return "Done", d, (t if len(t) > 10 else None)
    return "In Progress", None, t


def to_float(v) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    m = re.match(r"\s*\$?\s*([\d,]+(?:\.\d+)?)", s(v))
    return float(m.group(1).replace(",", "")) if m else None


def fmt_cell(v) -> Optional[str]:
    if v is None or s(v) == "":
        return None
    if isinstance(v, (dt.datetime, dt.date)):
        return to_date(v).strftime("%m/%d/%Y")
    return s(v)


def parse_duration(t: str) -> Optional[float]:
    """'1-3:30' -> 2.5, '9-11' -> 2, '12-4' -> 4."""
    m = re.match(r"\s*(\d{1,2})(?::(\d{2}))?\s*(?:am|pm)?\s*[-–]\s*(\d{1,2})(?::(\d{2}))?", t or "", re.I)
    if not m:
        return None
    a = int(m.group(1)) + int(m.group(2) or 0) / 60
    b = int(m.group(3)) + int(m.group(4) or 0) / 60
    if b <= a:
        b += 12
    d = b - a
    return round(d, 2) if 0 < d <= 10 else None


# ---------------------------------------------------------------------------
# Client name matching
# ---------------------------------------------------------------------------
PHRASES = [
    (r"unified school district", "usd"), (r"union high school district", "uhsd"),
    (r"union high school dist\b", "uhsd"), (r"high school district", "hsd"), (r"high school dist\b", "hsd"),
    (r"elementary school district", "esd"), (r"school district", "sd"), (r"county office of education", "coe"),
    (r"county office of ed\b", "coe"), (r"office of education", "oe"), (r"county superintendent of schools", "coe"),
    (r"\bco\.? office of ed(ucation)?\b", "coe"), (r"\bunified\b", "usd"), (r"\bsch\.? dist\b", "sd"),
]
GENERIC = {"usd", "sd", "uhsd", "hsd", "esd", "coe", "oe", "selpa", "district", "dist", "county", "of", "the",
           "school", "schools", "elementary", "elem", "charter", "invoice", "and", "unified", "union", "high",
           "new", "inactive", "corrected", "only", "time", "one", "2nd"}


def _clean(name: str, drop_parens: bool) -> str:
    t = name.lower().replace("&", " and ").replace("–", "-")
    if drop_parens:
        t = re.sub(r"\([^)]*\)", " ", t)
    else:
        t = t.replace("(", " ").replace(")", " ")
    for pat, rep in PHRASES:
        t = re.sub(pat, rep, t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t.replace("-", " "))
    toks = []
    for tok in t.split():
        for suf in ("usd", "coe", "uhsd", "sd"):  # e.g. "ABCUSD" -> abc + usd
            if tok.endswith(suf) and len(tok) > len(suf) + 1 and tok not in GENERIC:
                toks += [tok[: -len(suf)], suf]
                break
        else:
            toks.append(tok)
    return " ".join(toks)


def name_kind(name: str) -> Optional[str]:
    toks = set(_clean(name, False).split())
    kinds = set()
    if "selpa" in toks:
        kinds.add("selpa")
    if "coe" in toks or "oe" in toks:
        kinds.add("coe")
    if toks & {"usd", "sd", "uhsd", "hsd", "esd", "district", "dist"}:
        kinds.add("district")
    return kinds.pop() if len(kinds) == 1 else None  # a "X USD/SELPA" name could be either


def initials(name: str) -> str:
    return "".join(w[0] for w in re.findall(r"[a-z0-9]+", name.lower().replace("-", " "))
                   if w not in ("of", "the", "and"))


def core(name: str, drop_parens: bool = False) -> list[str]:
    return [t for t in _clean(name, drop_parens).split() if t not in GENERIC]


def match_rule(a: str, b: str) -> Optional[str]:
    """How (if at all) two client names refer to the same organisation."""
    ka, kb = name_kind(a), name_kind(b)
    if ka and kb and ka != kb:
        return None
    if _clean(a, False) == _clean(b, False):
        return "exact"
    for x, y in ((a, b), (b, a)):  # acronyms such as "XUHSD" for "X Union High School District"
        raw = re.sub(r"[^a-z0-9]", "", x.lower())
        if len(raw) >= 3 and " " not in x.strip() and raw == initials(re.sub(r"\([^)]*\)", " ", y)):
            return "abbreviation"
    for dp in (False, True):
        ca, cb = core(a, dp), core(b, dp)
        if not ca or not cb:
            continue
        if ca == cb:
            return "same name, different wording"
        short, long_ = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
        if len(short) == 1 and len(long_) >= 2 and len(short[0]) >= 2 and short[0] == "".join(w[0] for w in long_):
            return "abbreviation"
        if len(short) == len(long_) and all(l.startswith(x) for x, l in zip(short, long_)):
            return "shortened words"
        if long_[: len(short)] == short and (len(short) >= 2 or len(short[0]) >= 6):
            return "starts with name"
        if difflib.SequenceMatcher(None, " ".join(ca), " ".join(cb)).ratio() >= 0.88:
            return "spelling close"
    return None


class ClientResolver:
    def __init__(self, db, report: ImportReport):
        self.db, self.report = db, report
        self.canon: list[dict] = []  # {"name", "aliases":set, "tabs":set, "obj"}
        for p in logic.all_parties(db):
            aliases = {a.strip() for a in (p.aliases or "").splitlines() if a.strip()}
            self.canon.append({"name": p.name, "aliases": aliases | {p.name}, "tabs": set(), "obj": p})
        self._cache: dict[str, Optional[dict]] = {}

    def resolve(self, raw, tab: str, create: bool = True) -> Optional[dict]:
        name = re.sub(r"\s+", " ", s(raw))
        if not name or name.lower() in ("totals", "total", "client", "clients"):
            return None
        if name in self._cache:
            c = self._cache[name]
            if c:
                c["tabs"].add(tab)
            return c
        hits = []
        for c in self.canon:
            rules = [match_rule(name, a) for a in c["aliases"]]
            rules = [r for r in rules if r]
            if rules:
                hits.append((c, "exact" if "exact" in rules else rules[0]))
        exact = [h for h in hits if h[1] == "exact"]
        if exact:
            hits = exact[:1]
        if len(hits) == 1:
            c, rule = hits[0]
            if rule != "exact" and name not in c["aliases"]:
                self.report.matched.append((name, tab, c["name"], rule))
            c["aliases"].add(name)
            c["tabs"].add(tab)
            self._cache[name] = c
            return c
        if len(hits) > 1:
            self.report.warnings.append(
                f"'{name}' ({tab}) could match several clients: {', '.join(h[0]['name'] for h in hits)}. "
                f"Created it separately; merge it on the client's page if needed.")
        if not create:
            self._cache[name] = None
            return None
        c = {"name": name, "aliases": {name}, "tabs": {tab}, "obj": None, "first_tab": tab}
        self.canon.append(c)
        self._cache[name] = c
        return c

    def materialize(self) -> None:
        for c in self.canon:
            if c["obj"] is None:
                kind = name_kind(c["name"])
                if kind == "selpa":
                    obj = LcsSelpa(selpa_name=self._unique(c["name"], LcsSelpa, "selpa_name"))
                else:
                    ck = "County Office of Education" if kind == "coe" else (
                        "Charter" if "charter" in c["name"].lower() else "District")
                    obj = LcsDistrict(district_name=self._unique(c["name"], LcsDistrict, "district_name"),
                                      client_kind=ck)
                self.db.add(obj)
                c["obj"] = obj
                self.report.created.append((c["name"], c.get("first_tab", ""),
                                            ", ".join(sorted(c["tabs"] - {c.get("first_tab")}))))
                self.report.counts["Clients created"] += 1
            obj = c["obj"]
            obj.aliases = "\n".join(sorted(a for a in c["aliases"] if a != obj.name)) or None
        self.db.flush()

    def _unique(self, name, model, col):
        n, i = name, 2
        while self.db.scalar(select(model).where(getattr(model, col) == n)):
            n, i = f"{name} ({i})", i + 1
        return n


# ---------------------------------------------------------------------------
# Price worksheet
# ---------------------------------------------------------------------------
SERVICE_BY_NAME = [  # (substring of item name, service type)
    ("lcs data meeting", "LCS Data Meeting"), ("email q&a - promotional", None), ("email q&a", "Email Q&A"),
    ("audit consult", "Audit Consult"), ("consult services", "Consults"), ("written audit report", "Written Audit Report"),
    ("data submissions", "Data Submissions"), ("fall 1", "Fall 1 Audit"), ("eoy", "EOY Audit"),
    ("seis data audit", "SEIS Data Audit"), ("calpads data audit", "CALPADS Data Audit"),
    ("submission audit", "Submission Audit"), ("compliance a", "Compliance Audit"),
    ("compliance  promotional", "Compliance Audit"), ("compliance promotional", "Compliance Audit"),
    ("new data technician", "NDT Program"), ("trainer of trainer", "TOT Meeting"),
    ("trainings - customized", "Customized Trainings"), ("fixed trainings", "Fixed Trainings"),
    ("mini training", "Mini Trainings"), ("monthly learning", "Monthly Learning Program"),
]
EMAIL_QA_HOURS = {"10-12": 10, "7-9": 7.5, "4-6": 5, "1-3": 2.5}
TIER_HOURS = {  # Specialist Capacity Tracker, hours per tier
    "Data Submissions": (12, 20, 35, 48), "Fall 1 Audit": (8, 10, 12, 18), "EOY Audit": (10, 12, 14, 20),
    "SEIS Data Audit": (10, 12, 14, 18), "CALPADS Data Audit": (12, 14, 16, 22),
}
REF_RE = re.compile(r"([A-Z]{1,2})(\d+)")


def _slug(t: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "-", t.upper()).strip("-")[:60]


def import_price_worksheet(db, path, school_year: str, replace: bool = True) -> ImportReport:
    rep = ImportReport(f"Price list {school_year}")
    wbf = openpyxl.load_workbook(path, data_only=False)
    wbv = openpyxl.load_workbook(path, data_only=True)
    if "Price Quote Sheet" not in wbf.sheetnames:
        raise ValueError("This workbook has no 'Price Quote Sheet' tab.")
    wf, wv = wbf["Price Quote Sheet"], wbv["Price Quote Sheet"]
    st = {x.service_name: x for x in db.scalars(select(ServiceType))}
    if replace:
        for p in db.scalars(select(PriceItem).where(PriceItem.school_year == school_year)):
            db.delete(p)
        for z in db.scalars(select(ZoneRate).where(ZoneRate.school_year == school_year)):
            db.delete(z)
        db.flush()

    # Category of each row from the 'Total for Invoice Category' column (O).
    row_cat: dict[int, str] = {}
    for r in range(1, wf.max_row + 1):
        f = s(wf.cell(r, 15).value)
        if f.startswith("="):
            label = s(wf.cell(r - 1, 15).value) or "Other"
            cat = {"consultation": "Consultation", "training": "Training", "travel": "Travel",
                   "zone": "Zone"}.get(label.lower(), "Other")
            m = re.search(r"SUM\(N(\d+):N(\d+)\)", f)
            rows = range(int(m.group(1)), int(m.group(2)) + 1) if m else [int(x) for x in re.findall(r"N(\d+)", f)]
            for rr in rows:
                row_cat[rr] = cat

    # Rows referenced by a percent-of-group discount
    group_of: dict[int, str] = {}
    bulk = None
    for r in range(1, wf.max_row + 1):
        for c in range(1, 15):
            v = s(wf.cell(r, c).value)
            if v.lower() == "discount rate":
                rate = to_float(wv.cell(r, c + 2).value)
                txt = " ".join(s(wf.cell(r, cc).value) for cc in range(1, 6))
                mm = re.search(r"(\d+)\s+or more", txt)
                if rate:
                    bulk = (float(mm.group(1)) if mm else 4.0, rate)

    order = 0
    for r in range(3, wf.max_row + 1):
        name = s(wf.cell(r, 1).value)
        nf = s(wf.cell(r, 14).value)
        if not nf.startswith("="):
            continue
        variant = s(wf.cell(r, 4).value)
        rate = to_float(wv.cell(r, 11).value)
        rate_f = s(wf.cell(r, 11).value)
        labels = [s(wf.cell(r, c).value) for c in range(3, 9) if isinstance(wf.cell(r, c).value, str)
                  and s(wf.cell(r, c).value) not in ("@",)]
        p = PriceItem(school_year=school_year, item_name=name, needs_review=True, sort_order=order,
                      income_category=row_cat.get(r, "Other"), section=row_cat.get(r, "Other"),
                      notes=f"Imported from worksheet row {r}")
        order += 1
        if "VLOOKUP" in rate_f.upper() or "zone" in name.lower() or (not name and "VLOOKUP" in rate_f.upper()):
            p.method = "zone_additional" if ("A24" in rate_f or "2nd" in " ".join(labels).lower()) else "zone"
            p.item_name = "Zone Charge" if p.method == "zone" else "Zone Charge - Additional Person"
            p.qty_label = "In-person trips"
            p.income_category = p.section = "Zone"
            p.rate = 0
        elif re.search(r"\(C\d+\*D\d+\)", nf):
            p.method = "tiered"
            p.rate_small, p.rate_medium, p.rate_large, p.rate_xl = (to_float(wv.cell(r, c).value) or 0
                                                                    for c in (4, 7, 10, 13))
            variant = ""
        elif nf.startswith("=-(") and "N" in nf.split("*")[0]:
            p.method = "percent_of_group"
            p.rate = rate or 0
            rows = [int(x) for x in re.findall(r"N(\d+)", nf.split("*")[0])]
            key = f"group-r{rows[0]}" if rows else "group"
            p.group_key = key
            for rr in rows:
                group_of[rr] = key
            p.description = f"{p.rate:.0%} off the lines it groups"
        elif nf.startswith("=-("):
            p.method, p.rate = "discount", rate or 0
        else:
            refs = {c for c, _ in REF_RE.findall(nf)}
            if len(refs) >= 3:  # e.g. =G29*I29*K29 or =E54*I54*K54
                p.method, p.rate = "two_factor", rate or 0
                p.qty_label = labels[-1] if labels else "Quantity"
                p.factor_label = labels[0] if len(labels) > 1 else "Second factor"
            else:
                p.method, p.rate = "unit", rate or 0
        if not p.item_name:
            p.item_name = f"Row {r}"
        if variant and p.method != "tiered":
            p.variant = variant
        extra = [x for x in labels if x not in (variant, p.qty_label, p.factor_label)]
        if extra and not p.description:
            p.description = "; ".join(extra)
        low = p.item_name.lower()
        for key, svc in SERVICE_BY_NAME:
            if key in low:
                if svc and svc in st and p.method not in ("discount", "percent_of_group"):
                    p.service_type = st[svc]
                break
        if p.service_type and p.service_type.service_name == "Email Q&A":
            for k, h in EMAIL_QA_HOURS.items():
                if k in (p.variant or ""):
                    p.hours_per_unit = h
            if "ndt wraparound" in (p.variant or "").lower():
                p.hours_per_unit = 8
        if p.service_type and p.service_type.service_name in TIER_HOURS and p.method == "tiered":
            p.hours_small, p.hours_medium, p.hours_large, p.hours_xl = TIER_HOURS[p.service_type.service_name]
        if p.service_type and p.service_type.service_name == "NDT Program" and bulk:
            p.bulk_min_qty, p.bulk_discount_pct = bulk
            p.qty_label = "Participants"
        if "admin fee" in (p.variant or "").lower() or "admin fee" in low:
            p.income_category = p.section = "Other"
        p.item_code = _slug(f"{p.item_name} {p.variant or ''}")
        db.add(p)
        rep.counts["Price lines"] += 1
        if p.item_name.lower().startswith("additional, unlisted") and not p.rate:
            p.active = False
    db.flush()
    for p in db.scalars(select(PriceItem).where(PriceItem.school_year == school_year)):
        r = int(re.search(r"row (\d+)", p.notes or "row 0").group(1))
        if r in group_of and p.method in ("unit", "tiered"):
            p.group_key = group_of[r]
    # Codes must be unique within a year
    seen = defaultdict(int)
    for p in db.scalars(select(PriceItem).where(PriceItem.school_year == school_year).order_by(PriceItem.sort_order)):
        seen[p.item_code] += 1
        if seen[p.item_code] > 1:
            p.item_code = f"{p.item_code}-{seen[p.item_code]}"

    if "Zone Rates" in wbv.sheetnames:
        zv = wbv["Zone Rates"]
        main, extra = {}, {}
        section = "main"
        for r in range(1, zv.max_row + 1):
            a = zv.cell(r, 1).value
            if isinstance(a, str) and "additional" in a.lower():
                section = "extra"
                continue
            if isinstance(a, (int, float)):
                b = to_float(zv.cell(r, 2).value)
                if section == "main":
                    main[float(a)] = (b, int(to_float(zv.cell(r, 3).value) or 0))
                else:
                    extra[float(a)] = b
        for miles, (charge, zone_no) in sorted(main.items()):
            db.add(ZoneRate(school_year=school_year, miles_from=miles, zone_no=zone_no, zone_charge=charge or 0,
                            additional_person_charge=extra.get(miles, (charge or 0) / 2)))
            rep.counts["Zone bands"] += 1
    rep.notes.append("Every imported price is marked 'needs review' until you confirm it for this school year.")
    uncategorized = [p.item_name + (f" - {p.variant}" if p.variant else "") for p in
                     db.scalars(select(PriceItem).where(PriceItem.school_year == school_year,
                                                        PriceItem.income_category == "Other"))]
    if uncategorized:
        rep.notes.append("Not in any of the worksheet's four invoice categories, so filed under 'Other' "
                         "(the worksheet's Total Quote included them but the category totals did not): "
                         + ", ".join(uncategorized))
    db.commit()
    return rep


# ---------------------------------------------------------------------------
# Client spreadsheet
# ---------------------------------------------------------------------------
SERVICE_INFO_COLS = {  # column letter -> service type
    "B": "LCS Data Meeting", "C": "TOT Meeting", "D": "Email Q&A", "E": "NDT Program",
    "F": "Free Planning Consult", "G": "Consults", "H": "Monthly Learning Program", "I": "Audit Trainings",
    "J": "Customized Trainings", "K": "Fixed Trainings", "L": "Mini Trainings", "M": "Data Submissions",
    "N": "Fall 1 Audit", "O": "EOY Audit", "P": "SEIS Data Audit",
}
ONBOARD_COLS = {
    "C": "Created/verified LEA business folder", "D": "Created/verified service folder",
    "E": "Created/verified client folder", "F": "Welcome email sent", "G": "Received SEIS login",
    "H": "Received CALPADS login",
    "I": "Received participants - LCS Data", "J": "Received participants - TOT",
    "K": "Received participants - Email Q&A", "L": "Received participants - NDT",
    "M": "Emails documented in list - LCS Data", "N": "Emails documented in list - TOT",
    "O": "Emails documented in list - Email Q&A", "P": "Emails documented in list - NDT",
    "Q": "Participant emails sent out - LCS Data", "R": "Participant emails sent out - TOT",
    "S": "Participant emails sent out - Email Q&A", "T": "Participant emails sent out - NDT",
    "U": "Participants added to calendar invites - LCS Data", "V": "Participants added to calendar invites - TOT",
    "W": "Participants added to calendar invites - NDT training",
    "Y": "Contact NDT participants to schedule in-person meeting",
    "Z": "Using the NDT Program Schedule Set Up Sheet, create Zoom meetings & calendar invites",
    "AA": "Add trainings to the training schedule", "AB": "Add audits to the audit schedule",
    "AC": "Follow up on missing participant lists", "AD": "Follow up on missing calendars",
    "AE": "Follow up on missing SEIS logins",
}
LEAD_COLS = ["E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "X", "Y", "Z", "AA", "AB"]
PAPER_COLS = "BCDEFGHIJKLMNOPR"  # matches PAPERWORK_STEPS order (Q is Notes)
TRAINING_TYPES = [("customized", "Customized Trainings"), ("monthly", "Monthly Learning Program"),
                  ("fixed", "Fixed Trainings"), ("mini", "Mini Trainings"), ("tot", "TOT Meeting"),
                  ("ndt", "NDT Program"), ("audit", "Audit Trainings"), ("lcs data", "LCS Data Meeting"),
                  ("consult", "Consults")]
AUDIT_TYPES = [("fall", "Fall 1 Audit"), ("eoy", "EOY Audit"), ("seis", "SEIS Data Audit"),
               ("calpads", "CALPADS Data Audit"), ("submission", "Submission Audit"),
               ("compliance", "Compliance Audit"), ("data sub", "Data Submissions")]
EMAIL_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def _rows(ws, first: int):
    for r in range(first, ws.max_row + 1):
        yield r, (lambda col, _r=r: ws[f"{col}{_r}"].value)


def import_client_spreadsheet(db, path, school_year: Optional[str] = None) -> ImportReport:
    if not school_year:
        m = re.search(r"(\d{2})-(\d{2})", str(path).replace("\\", "/").split("/")[-1])
        school_year = f"20{m.group(1)}-{m.group(2)}" if m else school_year_for(logic.today())
    rep = ImportReport(f"Client spreadsheet {school_year}")
    wb = openpyxl.load_workbook(path, data_only=True)
    sheets = set(wb.sheetnames)
    need = {"Service Info", "Contract Tracking"}
    if not need & sheets:
        raise ValueError("This doesn't look like the Client Spreadsheet (no 'Service Info' or 'Contract Tracking' tab).")
    today = logic.today()
    sy_start, sy_end = school_year_bounds(school_year)
    st = {x.service_name: x for x in db.scalars(select(ServiceType))}
    tags = {t.tag_name: t for t in db.scalars(select(ContactTag))}
    resolver = ClientResolver(db, rep)

    # 1. Resolve every client name, most formal tabs first --------------------------------
    order = [("Service Info", 3, "A"), ("Contract Tracking", 2, "A"), ("Invoice Info", 3, "A"),
             ("Client Onboarding", 3, "A"), ("Coach Capacity", 3, "A"), ("Lead & Quote Tracking", 3, "A"),
             ("Training Tracking", 2, "A"), ("Audit Tracking", 2, "A"), ("Client Email and Choices Sheet", 3, "D")]
    by_row: dict[tuple[str, int], dict] = {}
    for tab, first, col in order:
        if tab not in sheets:
            continue
        ws = wb[tab]
        for r, get in _rows(ws, first):
            v = get(col)
            if tab == "Lead & Quote Tracking" and not s(v):
                break  # the colour key and notes sit below the first blank row
            if tab == "Client Email and Choices Sheet" and not s(v):
                v = get("B")
            c = resolver.resolve(v, tab)
            if c:
                by_row[(tab, r)] = c
    resolver.materialize()

    # 2. Coaches & capacity -----------------------------------------------------------------
    coaches = {c.coach_name.lower(): c for c in db.scalars(select(Coach))}

    def coach(name) -> Optional[Coach]:
        n = s(name)
        if not n or n.lower() in NA_WORDS:
            return None
        n = n.split("/")[0].split("&")[0].strip().title()
        if n.lower() not in coaches:
            coaches[n.lower()] = Coach(coach_name=n, specialties="SEIS, CALPADS")
            db.add(coaches[n.lower()])
            rep.counts["Coaches created"] += 1
        return coaches[n.lower()]

    if "Drop Down Menus" in sheets:
        ws = wb["Drop Down Menus"]
        for col in ("G", "H"):
            cname = s(ws[f"{col}2"].value)
            if not cname:
                continue
            ch = coach(cname)
            db.flush()
            for r in range(3, 15):
                mon = MONTHS.get(s(ws[f"F{r}"].value).lower())
                hrs = to_float(ws[f"{col}{r}"].value)
                if mon and hrs is not None:
                    ex = db.scalar(select(CoachMonthCapacity).where(
                        CoachMonthCapacity.coach_id == ch.id, CoachMonthCapacity.school_year == school_year,
                        CoachMonthCapacity.month == mon))
                    if ex:
                        ex.hours = hrs
                    else:
                        db.add(CoachMonthCapacity(coach_id=ch.id, school_year=school_year, month=mon, hours=hrs))
            rep.counts["Coach capacity charts"] += 1
        for r in range(3, 25):
            if s(ws[f"D{r}"].value):
                coach(ws[f"D{r}"].value)
    db.flush()

    # 3. Contacts -----------------------------------------------------------------------------
    contacts_by_email: dict[str, Contact] = {}
    for c in db.scalars(select(Contact)):
        if c.email_id:
            contacts_by_email[c.email_id.lower()] = c

    def add_tag(contact: Contact, tag: str, note=None):
        t = tags.get(tag)
        if t and not any(ti.tag_id == t.id for ti in contact.tags):
            contact.tags.append(ContactTagItem(tag=t, note=note))

    def upsert_contact(party, name: str, email: Optional[str], tag: Optional[str] = None, note=None) -> Contact:
        email = (email or "").strip().strip(",;") or None
        name = re.sub(r"\s+", " ", name or "").strip()
        extra = None
        m = re.search(r"\(([^)]*)\)", name)
        if m:
            extra, name = m.group(1), (name[: m.start()] + name[m.end():]).strip()
        if not name and email:
            name = email.split("@")[0].replace(".", " ").title()
        con = contacts_by_email.get(email.lower()) if email else None
        if con is None and not email:
            con = db.scalar(select(Contact).where(Contact.full_name == name, Contact.party_id == party.id,
                                                  Contact.party_type == party.party_type))
        if con is None:
            con = Contact(full_name=name or "(unnamed)", email_id=email, party_type=party.party_type,
                          party_id=party.id, notes=extra)
            db.add(con)
            rep.counts["Contacts created"] += 1
            if email:
                contacts_by_email[email.lower()] = con
        elif extra and extra not in (con.notes or ""):
            con.notes = ((con.notes or "") + "\n" + extra).strip()
        if tag:
            add_tag(con, tag, note)
        return con

    def contacts_from(party, names_v, emails_v, tag) -> list[Contact]:
        names_t, emails = s(names_v), EMAIL_RE.findall(s(emails_v))
        if not names_t and not emails:
            return []
        names = [n.strip() for n in re.split(r"\s*(?:&|/|,|\band\b)\s*(?![^()]*\))", names_t) if n.strip()]
        if emails and len(names) == len(emails):
            return [upsert_contact(party, n, e, tag) for n, e in zip(names, emails)]
        if len(emails) == 1:
            return [upsert_contact(party, names_t, emails[0], tag)]
        if emails:
            return [upsert_contact(party, "", e, tag) for e in emails]
        return [upsert_contact(party, n, None, tag) for n in names]

    if "Client Email and Choices Sheet" in sheets:
        ws = wb["Client Email and Choices Sheet"]
        svc_tags = {"G": "Participant - Email Q&A", "H": "Participant - TOT", "I": "Participant - LCS Data",
                    "J": "Participant - NDT"}
        for r, get in _rows(ws, 3):
            c = by_row.get(("Client Email and Choices Sheet", r))
            if not c or not (s(get("A")) or s(get("E"))):
                continue
            emails = EMAIL_RE.findall(s(get("E")))
            con = upsert_contact(c["obj"], s(get("A")), emails[0] if emails else None)
            for col, tag in svc_tags.items():
                v = s(get(col))
                if v and v.lower() not in NA_WORDS:
                    add_tag(con, tag, None if v.lower() in ("x", "y", "yes") else v)
            if s(get("K")):
                con.notes = ((con.notes or "") + "\n" + s(get("K"))).strip()
            selpa_name = s(get("C"))
            if selpa_name and isinstance(c["obj"], LcsDistrict) and not c["obj"].selpa_id:
                sc = resolver.resolve(selpa_name, "Client Email and Choices Sheet (SELPA column)", create=False)
                if sc and isinstance(sc["obj"], LcsSelpa):
                    c["obj"].selpa = sc["obj"]
    db.flush()

    # 4. Contracts: one per client with 26-27 business -----------------------------------------
    contracts: dict[int, TrainingContract] = {}  # id(canon) -> contract
    skipped = set()

    def contract_for(c) -> Optional[TrainingContract]:
        key = id(c)
        if key in contracts:
            return contracts[key]
        if key in skipped:
            return None
        p = c["obj"]
        ex = db.scalar(select(TrainingContract).where(TrainingContract.party_type == p.party_type,
                                                      TrainingContract.party_id == p.id,
                                                      TrainingContract.school_year == school_year))
        if ex:
            skipped.add(key)
            rep.warnings.append(f"{p.name} already has a {school_year} contract in the app; its spreadsheet rows were "
                                f"not imported again.")
            return None
        k = TrainingContract(party_type=p.party_type, party_id=p.id, school_year=school_year,
                             contract_start_date=sy_start, contract_end_date=sy_end, status="Draft",
                             billing_frequency="Monthly", total_contract_value=0)
        k.paperwork = [ContractPaperwork(step=st_, sort_order=i) for i, st_ in enumerate(PAPERWORK_STEPS)]
        db.add(k)
        contracts[key] = k
        rep.counts["Contracts created"] += 1
        return k

    # Service Info -> service lines
    if "Service Info" in sheets:
        ws = wb["Service Info"]
        for r, get in _rows(ws, 3):
            c = by_row.get(("Service Info", r))
            if not c:
                continue
            k = contract_for(c)
            if not k:
                continue
            for col, svc_name in SERVICE_INFO_COLS.items():
                v = get(col)
                if v is None or s(v) == "" or s(v) == "0":
                    continue
                svc = st[svc_name]
                qty, note = _qty(v, svc)
                k.services.append(ContractService(
                    service_type=svc, contracted_quantity=qty, hours_per_unit=svc.default_hours_per_unit,
                    effective_date=sy_start, description=svc_name, notes=note, frequency="per contract term"))
                rep.counts["Service lines"] += 1
            extra = "\n".join(x for x in (s(get("Q")), s(get("R"))) if x)
            if extra:
                k.notes = ((k.notes or "") + "\n" + extra).strip()

    # Contract Tracking -> paperwork + contacts
    if "Contract Tracking" in sheets:
        ws = wb["Contract Tracking"]
        for r, get in _rows(ws, 2):
            c = by_row.get(("Contract Tracking", r))
            if not c:
                continue
            k = contract_for(c)
            if not k:
                continue
            for step, col in zip(k.paperwork, PAPER_COLS):
                step.status, step.step_date, step.note = to_step(get(col))
            if s(get("Q")):
                k.notes = ((k.notes or "") + "\n" + s(get("Q"))).strip()
            mains = contacts_from(c["obj"], get("S"), get("T"), "Main Contact")
            cons = contacts_from(c["obj"], get("U"), get("V"), "Contracts")
            if mains:
                k.main_contact = mains[0]
            if cons:
                k.contracts_contact = cons[0]

    # Invoice Info -> value, PO, billing schedule
    invoice_rows = defaultdict(list)
    if "Invoice Info" in sheets:
        ws = wb["Invoice Info"]
        for r, get in _rows(ws, 3):
            c = by_row.get(("Invoice Info", r))
            if c and to_float(get("B")):
                invoice_rows[id(c)].append((c, {col: get(col) for col in "ABCDEFGHIJKLM"}))
    for key, rows in invoice_rows.items():
        c = rows[0][0]
        k = contract_for(c)
        if not k:
            continue
        k.total_contract_value = round(sum(to_float(x["B"]) or 0 for _, x in rows), 2)
        k.amount_other = k.total_contract_value
        k.billing_frequency = "Custom" if len(rows) > 1 else (
            "Annual-Upfront" if (to_float(rows[0][1]["C"]) or 1) <= 1 else "Monthly")
        notes = []
        for _, x in rows:
            months = int(to_float(x["C"]) or 1)
            start = to_date(x["G"], school_year) or sy_start
            amount = to_float(x["B"]) or 0
            if k.billing_start_date is None or start < k.billing_start_date:
                k.billing_start_date = start
            end_txt = s(x["H"])
            if to_date(x["H"], school_year):
                k.billing_end_date = to_date(x["H"], school_year)
            if len(rows) == 1:
                k.number_of_installments = months
            each = round(amount / months, 2)
            for i in range(months):
                due = logic.add_months(start, i)
                amt = round(amount - each * (months - 1), 2) if i == months - 1 else each
                past = due <= today
                k.billing_schedule.append(BillingInstallment(
                    due_date=due, amount=amt, amount_other=amt, status="Invoiced" if past else "Planned",
                    note="Assumed already invoiced (imported)" if past else None))
                rep.counts["Installments"] += 1
            if s(x["I"]):
                k.po_number = s(x["I"])
            if s(x["L"]):
                k.invoice_email = s(x["L"])
                for e in EMAIL_RE.findall(s(x["L"])):
                    upsert_contact(c["obj"], "", e, "Billing")
            if s(x["J"]) or s(x["K"]):
                cons = contacts_from(c["obj"], x["J"], x["K"], "Contracts")
                if cons and not k.contracts_contact:
                    k.contracts_contact = cons[0]
            if to_float(x["E"]):
                notes.append(f"Spreadsheet shows a new monthly amount of ${to_float(x['E']):,.2f}"
                             + (f" (adjusted {fmt_cell(x['F'])})" if s(x['F']) else "") + "; check the schedule.")
                rep.warnings.append(f"{c['obj'].name}: Invoice Info has a 'New Monthly Invoice Amount'. The schedule "
                                    f"uses the original split; review it on the contract's Billing tab.")
            elif s(x["F"]):
                notes.append(f"Adjustment note: {fmt_cell(x['F'])}")
            if end_txt and not to_date(x["H"], school_year):
                notes.append(f"Contract end note: {end_txt}")
            if s(x["M"]):
                notes.append(s(x["M"]))
        if notes:
            k.invoice_notes = "\n".join(notes)
        if k.billing_end_date is None and k.billing_schedule:
            k.billing_end_date = max(i.due_date for i in k.billing_schedule)
    for key, k in contracts.items():
        if not k.total_contract_value:
            p = logic.get_party(db, k.party_type, k.party_id) if k.party_id else None
            rep.warnings.append(f"{(p.name if p else '?')}: no amount in Invoice Info, so the contract value is $0.")
    rep.notes.append("Invoice Info has no category split, so imported contract amounts are filed under 'Other'. "
                     "Edit the split on a contract to divide it into Consultation / Training / Travel / Zone.")
    db.flush()

    # Coach Capacity -> coach assignment, consult hours provided, consult plan dates
    consult_dates = {}
    if "Coach Capacity" in sheets:
        ws = wb["Coach Capacity"]
        for r, get in _rows(ws, 3):
            c = by_row.get(("Coach Capacity", r))
            if not c:
                continue
            k = contract_for(c)
            if not k:
                continue
            ch = coach(get("B"))
            db.flush()
            consult_hours = to_float(get("C"))
            lines = [ln for ln in k.services if ln.service_type.service_name == "Consults"]
            free = sum(ln.contracted_quantity for ln in k.services
                       if ln.service_type.service_name == "Free Planning Consult")
            if consult_hours and not lines:
                ln = ContractService(service_type=st["Consults"], contracted_quantity=consult_hours,
                                     hours_per_unit=st["Consults"].default_hours_per_unit, effective_date=sy_start,
                                     description="Consults", notes="From Coach Capacity tab")
                k.services.append(ln)
                lines = [ln]
            elif consult_hours and lines and abs(lines[0].contracted_quantity + free - consult_hours) > 0.01:
                rep.warnings.append(f"{c['obj'].name}: Service Info says {lines[0].contracted_quantity:g} consult "
                                    f"hours (+{free:g} free planning), Coach Capacity says {consult_hours:g}. "
                                    f"Kept Service Info.")
            if ch:
                for ln in k.services:
                    if not ln.assigned_coach_id and ln.assigned_coach is None:
                        ln.assigned_coach = ch
            provided = to_float(get("F"))
            if provided and lines:
                db.flush()
                db.add(ServiceDeliveryLog(
                    contract=k, contract_service=lines[0], coach=ch, status="Completed",
                    date_performed=min(today, sy_end), duration=provided, units=provided,
                    subject="Consult hours provided to date (imported total)",
                    notes=f"Most recently completed month: {s(get('H'))}" if s(get("H")) else None))
                rep.counts["Delivery entries"] += 1
            if to_float(get("G")):
                rep.notes.append(f"{c['obj'].name}: Coach Capacity lists {to_float(get('G')):g} training hours "
                                 f"provided; trainings come from the Training Tracking tab instead, so this total was "
                                 f"not added again.")
            consult_dates[id(k)] = {"plan_sent": get("L"), "pre_consult": get("M"), "booked": get("N"),
                                    "reminder": get("O"), "note": s(get("P"))}
    db.flush()

    # Status, onboarding checklist
    onboard_rows = {}
    if "Client Onboarding" in sheets:
        ws = wb["Client Onboarding"]
        for r, get in _rows(ws, 3):
            c = by_row.get(("Client Onboarding", r))
            if c:
                onboard_rows[id(c)] = (c, {col: get(col) for col in list(ONBOARD_COLS) + ["B", "X", "AF"]})
    for key, k in contracts.items():
        executed = next((p for p in k.paperwork if p.step.startswith("Completed (fully")), None)
        c_canon = next(c for c in resolver.canon if id(c) == key)
        if (executed and executed.status == "Done") or key in onboard_rows:
            k.status = "Pending Assignment"
            logic.generate_onboarding(db, k)
            db.flush()
        if key in onboard_rows:
            c, row = onboard_rows[key]
            cl = logic.checklist_for(db, k)
            tasks = {t.step: t for t in cl.tasks}
            for col, task_name in ONBOARD_COLS.items():
                v = row.get(col)
                if v is None or s(v) == "":
                    continue
                status, d, note = to_step(v)
                targets = [t for name, t in tasks.items() if name == task_name]
                if not targets:
                    if status == "N/A":
                        continue
                    t = OnboardingTask(step=task_name, stage="Imported (service not on contract)", status=status,
                                       step_date=d, note=note, sort_order=len(cl.tasks))
                    cl.tasks.append(t)
                    tasks[task_name] = t
                    continue
                for t in targets:
                    t.status, t.step_date, t.note = status, d, note
            ch = coach(row.get("X"))
            if ch:
                db.flush()
                for ln in k.services:
                    if not ln.assigned_coach_id and ln.assigned_coach is None:
                        ln.assigned_coach = ch
                for t in cl.tasks:
                    if t.step == "Primary coach assigned":
                        t.status, t.note = "Done", ch.coach_name
            if s(row.get("B")):
                for n in re.split(r"\s*(?:&|/|,)\s*", s(row["B"])):
                    if n:
                        upsert_contact(c["obj"], n, None, "Onboarding")
            if s(row.get("AF")):
                k.notes = ((k.notes or "") + "\n" + s(row["AF"])).strip()
            dates = consult_dates.get(id(k), {})
            for t in cl.tasks:
                src = {"Book consult hours": dates.get("booked"), "Reminder sent out": dates.get("reminder"),
                       "Book pre-consult meeting": dates.get("pre_consult"),
                       "Complete pre-consult plan and send training plan": dates.get("plan_sent")}.get(t.step)
                if src is not None and s(src):
                    t.status, t.step_date, t.note = to_step(src)
            logic.refresh_onboarding_status(cl)
        db.flush()
        logic.maybe_activate(db, k)
        logic.reconcile_categories(k)
        _ = c_canon

    # Consult plans for every contract with consult hours
    for key, k in contracts.items():
        for ln in k.current_services:
            if ln.service_type.service_name == "Consults" and ln.contracted_quantity:
                d = consult_dates.get(id(k), {})
                plan = ConsultPlan(contract=k, contract_service=ln, status="Draft",
                                   consult_date=to_date(d.get("pre_consult")))
                sent = to_date(d.get("plan_sent"))
                if sent:
                    plan.status, plan.plan_sent_date = "Sent", sent
                if d.get("note"):
                    plan.structure_description = f"(Note from Coach Capacity: {d['note']})"
                db.add(plan)
                rep.counts["Consult plans"] += 1
    db.flush()

    # Training & audit tracking -> delivery log
    def line_for(k: TrainingContract, svc_name: str) -> Optional[ContractService]:
        return next((ln for ln in k.current_services if ln.service_type.service_name == svc_name), None)

    for tab, types, kind in (("Training Tracking", TRAINING_TYPES, "training"), ("Audit Tracking", AUDIT_TYPES, "audit")):
        if tab not in sheets:
            continue
        ws = wb[tab]
        for r, get in _rows(ws, 2):
            c = by_row.get((tab, r))
            if not c:
                continue
            k = contract_for(c)
            if not k:
                continue
            raw_type = s(get("B"))
            svc_name = next((v for key, v in types if key in raw_type.lower()), None)
            line = line_for(k, svc_name) if svc_name else None
            if svc_name and not line:
                rep.warnings.append(f"{c['obj'].name}: {tab} has a '{raw_type}' entry but the contract has no "
                                    f"{svc_name} line; logged without a service line.")
            lg = ServiceDeliveryLog(contract=k, contract_service=line, units=1)
            if kind == "training":
                d = to_date(get("F"))
                lg.date_performed, lg.time_text = d, fmt_cell(get("G"))
                lg.duration = parse_duration(s(get("G")))
                lg.audience, lg.mode, lg.subject = s(get("C")) or None, s(get("D")) or None, s(get("E")) or None
                lg.coach = coach(get("H"))
                lg.pre_meeting, lg.participants_received = fmt_cell(get("I")), fmt_cell(get("J"))
                lg.folder_link_sent, lg.placement_assessment = fmt_cell(get("K")), fmt_cell(get("L"))
                lg.pre_assessment, lg.post_assessment = fmt_cell(get("M")), fmt_cell(get("N"))
                lg.evaluations_received = fmt_cell(get("O"))
                lg.status = "Planned" if not d else ("Completed" if d < today else "Scheduled")
                lg.notes = f"Type in spreadsheet: {raw_type}" if raw_type and not svc_name else None
            else:
                d = to_date(get("D"))
                finish = get("G")
                lg.date_performed = d
                lg.subject = raw_type or "Audit"
                lg.calpads_login, lg.go_ahead = fmt_cell(get("C")), fmt_cell(get("F"))
                lg.coach = coach(get("E"))
                lg.follow_up_requested, lg.follow_up_completed = fmt_cell(get("H")), fmt_cell(get("I"))
                fd = to_date(finish) if isinstance(finish, (dt.date, dt.datetime, str)) else None
                if fd:
                    lg.end_date, lg.status = fd, "Completed"
                else:
                    lg.status = "Planned" if not d else "Scheduled"
                    if finish not in (None, ""):
                        lg.notes = f"'Audit Finish Date' cell held {finish!r} (not a date)."
            if lg.coach is None and line and line.assigned_coach:
                lg.coach = line.assigned_coach
            db.add(lg)
            rep.counts["Delivery entries"] += 1
    db.flush()

    # 5. Leads ---------------------------------------------------------------------------------
    if "Lead & Quote Tracking" in sheets:
        ws = wb["Lead & Quote Tracking"]
        colour_key = {}
        in_key = False
        for r in range(3, ws.max_row + 1):
            a = s(ws[f"A{r}"].value)
            if a.lower() == "color key":
                in_key = True
            elif in_key and a and ws[f"A{r}"].fill.fill_type and ws[f"A{r}"].fill.fgColor.type == "rgb":
                colour_key.setdefault(ws[f"A{r}"].fill.fgColor.rgb, []).append(a)

        def colour_labels(cell, months: bool) -> list[str]:
            if not cell.fill.fill_type or cell.fill.fgColor.type != "rgb":
                return []
            labels = [x for x in colour_key.get(cell.fill.fgColor.rgb, [])
                      if (x.lower().split()[0] in MONTHS) == months]
            return labels if len(labels) == 1 else []  # a colour used twice in the key can't be read
        for r, get in _rows(ws, 3):
            c = by_row.get(("Lead & Quote Tracking", r))
            if not c:
                continue
            p = c["obj"]
            lead = db.scalar(select(Lead).where(Lead.party_type == p.party_type, Lead.party_id == p.id,
                                                Lead.school_year == school_year))
            if lead:
                continue
            lead = Lead(party_type=p.party_type, party_id=p.id, school_year=school_year,
                        contact_text=s(get("B")) or None, source=s(get("D")) or None, notes=s(get("W")) or None)
            lead.initial_query_date = to_date(get("C"))
            if s(get("C")) and not isinstance(get("C"), (dt.date, dt.datetime)):
                lead.initial_query_note = s(get("C"))
            lead.steps = logic.new_lead_steps()
            for step, col in zip(lead.steps, LEAD_COLS):
                step.status, step.step_date, step.note = to_step(get(col))
            flags = [f"First contact: {m}" for m in colour_labels(ws[f"C{r}"], True)]
            for col in ("A", "B") + tuple(LEAD_COLS):
                for lab in colour_labels(ws[f"{col}{r}"], False):
                    if lab not in flags:
                        flags.append(lab)
            if flags:
                lead.notes = ("".join(f"[{f}] " for f in flags) + "\n" + (lead.notes or "")).strip()
            lead.legacy_quote_count = int(to_float(get("P")) or 0) or None
            lead.legacy_amendment_count = int(to_float(get("Q")) or 0) or None
            lead.legacy_highest_quoted, lead.legacy_lowest_quoted = to_float(get("R")), to_float(get("S"))
            lead.legacy_total_quoted, lead.legacy_accepted_amount = to_float(get("T")), to_float(get("U"))
            lead.legacy_quotes_won = int(to_float(get("V")) or 0) or None
            done = {st_.step for st_ in lead.steps if st_.status == "Done"}
            if "Try again next school year" in done:
                lead.status = "Try Again Next Year"
            elif {"Quote accepted", "Quote signed & returned"} & done:
                lead.status = "Won"
            elif "Moved to promotional list" in done:
                lead.status = "Promotional List"
            elif "Quotes written & sent" in done:
                lead.status = "Quoted"
            db.add(lead)
            rep.counts["Leads"] += 1

    ignored = sorted(t for t in sheets if "invoice" in t.lower() and t not in ("Invoice Info",))
    if ignored:
        rep.notes.append(f"{len(ignored)} per-client invoice tabs were not imported; the app builds each "
                         f"invoice from the contract's billing schedule instead.")
    if "Specialist Capacity Tracker" in sheets:
        rep.notes.append("Specialist Capacity Tracker: its time assumptions are built into the service types and "
                         "price list hours; its per-quote columns were not imported.")
    rep.notes.append("Past-due installments were marked 'Invoiced' on the assumption they were already billed. "
                     "Change any that weren't on the contract's Billing tab.")
    db.commit()
    return rep


def _qty(v, svc: ServiceType) -> tuple[float, Optional[str]]:
    if isinstance(v, (int, float)):
        return float(v), None
    t = s(v)
    if svc.delivery_unit == "subscription":
        return 1.0, t
    low = t.lower()
    if low.startswith("cont"):
        return 1.0, t
    if "ndt" in low and svc.service_name != "NDT Program":
        return 0.0, f"Included with NDT program ({t})"
    n = to_float(t)
    if n is not None:
        return n, t
    return 1.0, t
