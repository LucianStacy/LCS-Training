"""ERPNext hand-off: one CSV per DocType, named and laid out after the Platform
Blueprint so the future ``lcs_training`` Frappe app (or ERPNext's Data Import
tool) can take it in directly. Link fields carry the linked record's *name*,
which is how ERPNext links records."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import zipfile

from sqlalchemy import inspect, select

from . import logic
from .models import Base

NAME_ATTRS = ("name", "service_name", "coach_name", "full_name", "tag_name", "quote_number", "title", "item_code")


def display_name(obj) -> str:
    if obj is None:
        return ""
    for a in NAME_ATTRS:
        v = getattr(obj, a, None)
        if isinstance(v, str) and v:
            return v
    return f"{getattr(obj, '__doctype__', type(obj).__name__)}-{obj.id}"


def _rows(db, model):
    mapper = inspect(model)
    cols = [c.key for c in mapper.column_attrs]
    rels = [r for r in mapper.relationships if r.direction.name == "MANYTOONE"]
    has_party = "party_type" in cols and "party_id" in cols
    header = ["name"] + cols + [r.key for r in rels] + (["party"] if has_party else [])
    out = []
    for obj in db.scalars(select(model)):
        row = {"name": f"{model.__doctype__}-{obj.id}" if not hasattr(obj, "name") or not isinstance(
            getattr(obj, "name", None), str) else obj.name}
        for c in cols:
            v = getattr(obj, c)
            row[c] = v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v
        for r in rels:
            row[r.key] = display_name(getattr(obj, r.key))
        if has_party:
            row["party"] = logic.party_name(db, obj.party_type, obj.party_id) if obj.party_id else ""
        out.append(row)
    return header, out


def erpnext_export(db) -> bytes:
    buf = io.BytesIO()
    manifest = {"exported_at": dt.datetime.now().isoformat(timespec="seconds"), "doctypes": {}}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for mapper in Base.registry.mappers:
            model = mapper.class_
            doctype = getattr(model, "__doctype__", None)
            if not doctype:
                continue
            header, rows = _rows(db, model)
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=header, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
            fname = doctype.replace(" ", "_") + ".csv"
            z.writestr(fname, s.getvalue())
            manifest["doctypes"][doctype] = {"file": fname, "table": model.__tablename__, "rows": len(rows)}
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        z.writestr("README.txt", README)
    return buf.getvalue()


README = """LCS-Training -> ERPNext export
================================
One CSV per DocType. DocType and field names follow the LCS-Training Platform
Blueprint (section 2.2). Columns ending in _id are this app's internal ids; the
column without _id next to them holds the linked record's name, which is what
ERPNext uses for Link fields.

Standard ERPNext DocTypes: Contact, Lead, Item, Quotation, Quotation Item.
Everything prefixed "LCS" belongs in the lcs_training custom app.

Mapping notes
- Contract amounts are split into Consultation / Training / Travel / Zone /
  Other; map those to Item Groups / income accounts under the LCS-Training
  Company's Chart of Accounts.
- LCS Contract Billing Schedule rows become Sales Invoices (or a Subscription
  Plan) when billing moves into ERPNext.
- party_type is "LCS District" or "LCS SELPA", party is the client's name.
"""
