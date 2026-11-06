"""Data model for the LCS-Training standalone app.

Every table that will become an ERPNext DocType carries a ``__doctype__``
attribute with the DocType name from the Platform Blueprint (section 2.2), and
field names follow the Blueprint wherever it names them. That keeps a later
sync/import into the ``lcs_training`` Frappe app a one-to-one mapping.

No student information is stored anywhere: the only student-related value is
the *size tier* of a district (Small/Medium/Large/XL) used for audit pricing.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (Boolean, Date, Float, ForeignKey, Integer, String, Text,
                        create_engine, event)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column, relationship,
                            sessionmaker)

from .config import DB_PATH


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Shared vocabularies
# ---------------------------------------------------------------------------
PARTY_SELPA = "LCS SELPA"
PARTY_DISTRICT = "LCS District"
PARTY_TYPES = [PARTY_DISTRICT, PARTY_SELPA]

CLIENT_KINDS = ["District", "County Office of Education", "Charter", "SELPA", "Other"]
STUDENT_TIERS = ["Small", "Medium", "Large", "XL"]
TIER_HELP = {"Small": "up to 1,500 in SEIS", "Medium": "up to 3,000",
             "Large": "up to 5,000", "XL": "more than 5,000"}

STEP_STATUSES = ["Not Started", "In Progress", "Done", "N/A"]

CONTRACT_STATUSES = ["Draft", "Pending Assignment", "Active", "Renewal Pending", "Expired", "Cancelled"]
BILLING_FREQUENCIES = ["Monthly", "Quarterly", "Annual-Upfront", "Custom"]
INSTALLMENT_STATUSES = ["Planned", "Invoiced", "Paid", "Cancelled"]

QUOTE_STATUSES = ["Draft", "Sent", "Accepted", "Declined", "Expired"]
LEAD_STATUSES = ["Open", "Quoted", "Won", "Lost", "Try Again Next Year", "Promotional List"]

INCOME_CATEGORIES = ["Consultation", "Training", "Travel", "Zone", "Other"]

# How a price-list line turns quantity into money.
PRICE_METHODS = {
    "unit": "Quantity x rate",
    "tiered": "Quantity x rate for the client's size tier",
    "two_factor": "Quantity x second factor x rate (e.g. people x hours)",
    "percent_of_group": "Discount: rate (as %) of every line in the same group",
    "discount": "Discount: quantity x rate, subtracted",
    "zone": "Zone charge looked up from one-way mileage, per trip",
    "zone_additional": "Additional-person zone charge (half zone), per trip",
}

SERVICE_KINDS = ["Consult", "Training", "Audit", "Meeting", "Subscription", "Program", "Other"]
DELIVERY_UNITS = ["session", "hour", "audit", "month", "participant", "subscription"]
DELIVERY_STATUSES = ["Planned", "Scheduled", "Completed", "Cancelled"]
DELIVERY_MODES = ["Virtual", "In-Person"]
UNAVAILABILITY_TYPES = ["Vacation", "Sick", "Training", "Holiday", "Other"]

SCHOOL_MONTHS = [7, 8, 9, 10, 11, 12, 1, 2, 3, 4, 5, 6]
MONTH_NAMES = {i: dt.date(2000, i, 1).strftime("%B") for i in range(1, 13)}


def school_year_for(d: dt.date) -> str:
    start = d.year if d.month >= 7 else d.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def school_year_bounds(sy: str) -> tuple[dt.date, dt.date]:
    start = int(sy[:4])
    return dt.date(start, 7, 1), dt.date(start + 1, 6, 30)


# ---------------------------------------------------------------------------
# Parties
# ---------------------------------------------------------------------------
class LcsSelpa(Base):
    __tablename__ = "lcs_selpa"
    __doctype__ = "LCS SELPA"
    id: Mapped[int] = mapped_column(primary_key=True)
    selpa_name: Mapped[str] = mapped_column(String(200), unique=True)
    region: Mapped[Optional[str]] = mapped_column(String(100))
    student_tier: Mapped[Optional[str]] = mapped_column(String(10))
    aliases: Mapped[Optional[str]] = mapped_column(Text)  # one per line, used for matching
    notes: Mapped[Optional[str]] = mapped_column(Text)
    member_districts: Mapped[list["LcsDistrict"]] = relationship(back_populates="selpa")

    party_type = PARTY_SELPA

    @property
    def name(self) -> str:
        return self.selpa_name


class LcsDistrict(Base):
    __tablename__ = "lcs_district"
    __doctype__ = "LCS District"
    id: Mapped[int] = mapped_column(primary_key=True)
    district_name: Mapped[str] = mapped_column(String(200), unique=True)
    client_kind: Mapped[str] = mapped_column(String(40), default="District")
    selpa_id: Mapped[Optional[int]] = mapped_column(ForeignKey("lcs_selpa.id"))
    primary_contact_id: Mapped[Optional[int]] = mapped_column(ForeignKey("contact.id", use_alter=True))
    student_tier: Mapped[Optional[str]] = mapped_column(String(10))
    region: Mapped[Optional[str]] = mapped_column(String(100))
    aliases: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    selpa: Mapped[Optional[LcsSelpa]] = relationship(back_populates="member_districts")
    primary_contact: Mapped[Optional["Contact"]] = relationship(foreign_keys=[primary_contact_id])

    party_type = PARTY_DISTRICT

    @property
    def name(self) -> str:
        return self.district_name


# ---------------------------------------------------------------------------
# Contacts & tags  (Contact customization: lcs_tags)
# ---------------------------------------------------------------------------
class ContactTag(Base):
    __tablename__ = "lcs_contact_tag"
    __doctype__ = "LCS Contact Tag"
    id: Mapped[int] = mapped_column(primary_key=True)
    tag_name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[Optional[str]] = mapped_column(String(300))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Contact(Base):
    __tablename__ = "contact"
    __doctype__ = "Contact"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200))
    email_id: Mapped[Optional[str]] = mapped_column(String(200))
    phone: Mapped[Optional[str]] = mapped_column(String(50))
    designation: Mapped[Optional[str]] = mapped_column(String(120))
    party_type: Mapped[Optional[str]] = mapped_column(String(20))
    party_id: Mapped[Optional[int]] = mapped_column(Integer)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    tags: Mapped[list["ContactTagItem"]] = relationship(back_populates="contact", cascade="all, delete-orphan")

    @property
    def tag_names(self) -> list[str]:
        return sorted(t.tag.tag_name for t in self.tags)


class ContactTagItem(Base):
    __tablename__ = "lcs_contact_tag_item"
    __doctype__ = "LCS Contact Tag Item"
    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contact.id"))
    tag_id: Mapped[int] = mapped_column(ForeignKey("lcs_contact_tag.id"))
    note: Mapped[Optional[str]] = mapped_column(String(200))  # e.g. "1a of 2"
    contact: Mapped[Contact] = relationship(back_populates="tags")
    tag: Mapped[ContactTag] = relationship()


# ---------------------------------------------------------------------------
# Services, price list, zone table
# ---------------------------------------------------------------------------
class ServiceType(Base):
    __tablename__ = "lcs_service_type"
    __doctype__ = "LCS Service Type"
    id: Mapped[int] = mapped_column(primary_key=True)
    service_name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[str] = mapped_column(String(30), default="Other")
    delivery_unit: Mapped[str] = mapped_column(String(30), default="session")
    default_hours_per_unit: Mapped[float] = mapped_column(Float, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[Optional[str]] = mapped_column(Text)


class PriceItem(Base):
    """One priced line of the Quote Calculation Worksheet (ERPNext Item + Item Price)."""
    __tablename__ = "lcs_price_item"
    __doctype__ = "Item"
    id: Mapped[int] = mapped_column(primary_key=True)
    school_year: Mapped[str] = mapped_column(String(7), index=True)
    item_code: Mapped[str] = mapped_column(String(60))
    item_name: Mapped[str] = mapped_column(String(160))
    variant: Mapped[Optional[str]] = mapped_column(String(120))
    description: Mapped[Optional[str]] = mapped_column(String(300))
    income_category: Mapped[str] = mapped_column(String(20), default="Consultation")
    method: Mapped[str] = mapped_column(String(20), default="unit")
    rate: Mapped[float] = mapped_column(Float, default=0)
    rate_small: Mapped[float] = mapped_column(Float, default=0)
    rate_medium: Mapped[float] = mapped_column(Float, default=0)
    rate_large: Mapped[float] = mapped_column(Float, default=0)
    rate_xl: Mapped[float] = mapped_column(Float, default=0)
    group_key: Mapped[Optional[str]] = mapped_column(String(40))
    qty_label: Mapped[Optional[str]] = mapped_column(String(60))
    factor_label: Mapped[Optional[str]] = mapped_column(String(60))
    bulk_min_qty: Mapped[Optional[float]] = mapped_column(Float)
    bulk_discount_pct: Mapped[Optional[float]] = mapped_column(Float)
    service_type_id: Mapped[Optional[int]] = mapped_column(ForeignKey("lcs_service_type.id"))
    hours_per_unit: Mapped[Optional[float]] = mapped_column(Float)
    hours_small: Mapped[Optional[float]] = mapped_column(Float)
    hours_medium: Mapped[Optional[float]] = mapped_column(Float)
    hours_large: Mapped[Optional[float]] = mapped_column(Float)
    hours_xl: Mapped[Optional[float]] = mapped_column(Float)
    section: Mapped[str] = mapped_column(String(60), default="Consultation")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    service_type: Mapped[Optional[ServiceType]] = relationship()

    def tier_rate(self, tier: Optional[str]) -> float:
        return {"Small": self.rate_small, "Medium": self.rate_medium, "Large": self.rate_large,
                "XL": self.rate_xl}.get(tier or "", 0.0) or 0.0

    def hours_for(self, tier: Optional[str]) -> Optional[float]:
        if self.method == "tiered":
            return {"Small": self.hours_small, "Medium": self.hours_medium,
                    "Large": self.hours_large, "XL": self.hours_xl}.get(tier or "")
        return self.hours_per_unit


class ZoneRate(Base):
    __tablename__ = "lcs_zone_rate"
    __doctype__ = "LCS Zone Rate"
    id: Mapped[int] = mapped_column(primary_key=True)
    school_year: Mapped[str] = mapped_column(String(7), index=True)
    miles_from: Mapped[float] = mapped_column(Float)
    zone_no: Mapped[int] = mapped_column(Integer)
    zone_charge: Mapped[float] = mapped_column(Float)
    additional_person_charge: Mapped[float] = mapped_column(Float)


# ---------------------------------------------------------------------------
# Leads & quotes
# ---------------------------------------------------------------------------
class StepMixin:
    id: Mapped[int] = mapped_column(primary_key=True)
    step: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="Not Started")
    step_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    note: Mapped[Optional[str]] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Lead(Base):
    __tablename__ = "lcs_lead"
    __doctype__ = "Lead"
    id: Mapped[int] = mapped_column(primary_key=True)
    party_type: Mapped[Optional[str]] = mapped_column(String(20))
    party_id: Mapped[Optional[int]] = mapped_column(Integer)
    contact_text: Mapped[Optional[str]] = mapped_column(String(300))
    school_year: Mapped[str] = mapped_column(String(7))
    initial_query_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    initial_query_note: Mapped[Optional[str]] = mapped_column(String(300))
    source: Mapped[Optional[str]] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="Open")
    notes: Mapped[Optional[str]] = mapped_column(Text)
    # Totals carried over from the spreadsheet (quotes before this app existed)
    legacy_quote_count: Mapped[Optional[int]] = mapped_column(Integer)
    legacy_amendment_count: Mapped[Optional[int]] = mapped_column(Integer)
    legacy_highest_quoted: Mapped[Optional[float]] = mapped_column(Float)
    legacy_lowest_quoted: Mapped[Optional[float]] = mapped_column(Float)
    legacy_total_quoted: Mapped[Optional[float]] = mapped_column(Float)
    legacy_accepted_amount: Mapped[Optional[float]] = mapped_column(Float)
    legacy_quotes_won: Mapped[Optional[int]] = mapped_column(Integer)
    steps: Mapped[list["LeadStep"]] = relationship(back_populates="lead", cascade="all, delete-orphan",
                                                   order_by="LeadStep.sort_order")


class LeadStep(StepMixin, Base):
    __tablename__ = "lcs_lead_step"
    __doctype__ = "LCS Lead Step"
    lead_id: Mapped[int] = mapped_column(ForeignKey("lcs_lead.id"))
    lead: Mapped[Lead] = relationship(back_populates="steps")


class Quote(Base):
    __tablename__ = "lcs_quote"
    __doctype__ = "Quotation"
    id: Mapped[int] = mapped_column(primary_key=True)
    quote_number: Mapped[str] = mapped_column(String(20), unique=True)
    party_type: Mapped[str] = mapped_column(String(20))
    party_id: Mapped[int] = mapped_column(Integer)
    lead_id: Mapped[Optional[int]] = mapped_column(ForeignKey("lcs_lead.id"))
    school_year: Mapped[str] = mapped_column(String(7))
    quote_date: Mapped[dt.date] = mapped_column(Date)
    valid_until: Mapped[Optional[dt.date]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="Draft")
    student_tier: Mapped[Optional[str]] = mapped_column(String(10))
    one_way_mileage: Mapped[Optional[float]] = mapped_column(Float)
    in_person_trips: Mapped[Optional[float]] = mapped_column(Float)
    second_person_trips: Mapped[Optional[float]] = mapped_column(Float)
    client_details: Mapped[Optional[str]] = mapped_column(Text)   # "Details for the Client per discussion"
    internal_notes: Mapped[Optional[str]] = mapped_column(Text)
    is_amendment: Mapped[bool] = mapped_column(Boolean, default=False)
    amends_contract_id: Mapped[Optional[int]] = mapped_column(ForeignKey("lcs_training_contract.id"))
    grand_total: Mapped[float] = mapped_column(Float, default=0)
    total_consultation: Mapped[float] = mapped_column(Float, default=0)
    total_training: Mapped[float] = mapped_column(Float, default=0)
    total_travel: Mapped[float] = mapped_column(Float, default=0)
    total_zone: Mapped[float] = mapped_column(Float, default=0)
    total_other: Mapped[float] = mapped_column(Float, default=0)
    items: Mapped[list["QuoteItem"]] = relationship(back_populates="quote", cascade="all, delete-orphan",
                                                    order_by="QuoteItem.sort_order")
    lead: Mapped[Optional[Lead]] = relationship()


class QuoteItem(Base):
    __tablename__ = "lcs_quote_item"
    __doctype__ = "Quotation Item"
    id: Mapped[int] = mapped_column(primary_key=True)
    quote_id: Mapped[int] = mapped_column(ForeignKey("lcs_quote.id"))
    price_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("lcs_price_item.id"))
    description: Mapped[Optional[str]] = mapped_column(String(300))
    qty: Mapped[float] = mapped_column(Float, default=0)
    factor: Mapped[Optional[float]] = mapped_column(Float)
    rate: Mapped[float] = mapped_column(Float, default=0)   # rate used (manual override for unlisted)
    amount: Mapped[float] = mapped_column(Float, default=0)
    income_category: Mapped[str] = mapped_column(String(20), default="Consultation")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    quote: Mapped[Quote] = relationship(back_populates="items")
    price_item: Mapped[Optional[PriceItem]] = relationship()


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------
class TrainingContract(Base):
    __tablename__ = "lcs_training_contract"
    __doctype__ = "LCS Training Contract"
    id: Mapped[int] = mapped_column(primary_key=True)
    party_type: Mapped[str] = mapped_column(String(20))
    party_id: Mapped[int] = mapped_column(Integer)
    quote_id: Mapped[Optional[int]] = mapped_column(Integer)
    school_year: Mapped[str] = mapped_column(String(7))
    contract_start_date: Mapped[dt.date] = mapped_column(Date)
    contract_end_date: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(30), default="Draft")
    total_contract_value: Mapped[float] = mapped_column(Float, default=0)
    amount_consultation: Mapped[float] = mapped_column(Float, default=0)
    amount_training: Mapped[float] = mapped_column(Float, default=0)
    amount_travel: Mapped[float] = mapped_column(Float, default=0)
    amount_zone: Mapped[float] = mapped_column(Float, default=0)
    amount_other: Mapped[float] = mapped_column(Float, default=0)
    billing_frequency: Mapped[str] = mapped_column(String(20), default="Monthly")
    billing_start_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    billing_end_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    number_of_installments: Mapped[Optional[int]] = mapped_column(Integer)
    po_number: Mapped[Optional[str]] = mapped_column(String(80))
    invoice_email: Mapped[Optional[str]] = mapped_column(String(300))
    main_contact_id: Mapped[Optional[int]] = mapped_column(ForeignKey("contact.id"))
    contracts_contact_id: Mapped[Optional[int]] = mapped_column(ForeignKey("contact.id"))
    invoice_notes: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    main_contact: Mapped[Optional[Contact]] = relationship(foreign_keys=[main_contact_id])
    contracts_contact: Mapped[Optional[Contact]] = relationship(foreign_keys=[contracts_contact_id])
    services: Mapped[list["ContractService"]] = relationship(back_populates="contract", cascade="all, delete-orphan",
                                                             order_by="ContractService.id")
    amendments: Mapped[list["ContractAmendment"]] = relationship(back_populates="contract", cascade="all, delete-orphan",
                                                                 order_by="ContractAmendment.amendment_date")
    billing_schedule: Mapped[list["BillingInstallment"]] = relationship(back_populates="contract",
                                                                        cascade="all, delete-orphan",
                                                                        order_by="BillingInstallment.due_date")
    paperwork: Mapped[list["ContractPaperwork"]] = relationship(back_populates="contract", cascade="all, delete-orphan",
                                                                order_by="ContractPaperwork.sort_order")

    @property
    def current_services(self) -> list["ContractService"]:
        return [s for s in self.services if s.end_date is None]


class ContractPaperwork(StepMixin, Base):
    __tablename__ = "lcs_contract_paperwork"
    __doctype__ = "LCS Contract Paperwork"
    contract_id: Mapped[int] = mapped_column(ForeignKey("lcs_training_contract.id"))
    contract: Mapped[TrainingContract] = relationship(back_populates="paperwork")


class Coach(Base):
    __tablename__ = "lcs_coach"
    __doctype__ = "LCS Coach"
    id: Mapped[int] = mapped_column(primary_key=True)
    coach_name: Mapped[str] = mapped_column(String(120), unique=True)
    email: Mapped[Optional[str]] = mapped_column(String(200))
    employee: Mapped[Optional[str]] = mapped_column(String(60))
    specialties: Mapped[Optional[str]] = mapped_column(String(300))
    hours_per_day: Mapped[float] = mapped_column(Float, default=6)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    capacity: Mapped[list["CoachMonthCapacity"]] = relationship(back_populates="coach", cascade="all, delete-orphan")
    unavailability: Mapped[list["CoachUnavailability"]] = relationship(back_populates="coach",
                                                                      cascade="all, delete-orphan",
                                                                      order_by="CoachUnavailability.start_date")


class CoachMonthCapacity(Base):
    """base_capacity in the Blueprint, broken out per month the way the Drop Down Menus chart does it."""
    __tablename__ = "lcs_coach_capacity"
    __doctype__ = "LCS Coach Capacity"
    id: Mapped[int] = mapped_column(primary_key=True)
    coach_id: Mapped[int] = mapped_column(ForeignKey("lcs_coach.id"))
    school_year: Mapped[str] = mapped_column(String(7))
    month: Mapped[int] = mapped_column(Integer)
    hours: Mapped[float] = mapped_column(Float, default=0)
    coach: Mapped[Coach] = relationship(back_populates="capacity")


class CoachUnavailability(Base):
    __tablename__ = "lcs_coach_unavailability"
    __doctype__ = "LCS Coach Unavailability"
    id: Mapped[int] = mapped_column(primary_key=True)
    coach_id: Mapped[int] = mapped_column(ForeignKey("lcs_coach.id"))
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date] = mapped_column(Date)
    type: Mapped[str] = mapped_column(String(30), default="Vacation")
    note: Mapped[Optional[str]] = mapped_column(String(300))
    coach: Mapped[Coach] = relationship(back_populates="unavailability")


class ContractService(Base):
    __tablename__ = "lcs_contract_service"
    __doctype__ = "LCS Contract Service"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("lcs_training_contract.id"))
    service_type_id: Mapped[int] = mapped_column(ForeignKey("lcs_service_type.id"))
    price_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("lcs_price_item.id"))
    description: Mapped[Optional[str]] = mapped_column(String(300))
    contracted_quantity: Mapped[float] = mapped_column(Float, default=0)
    frequency: Mapped[Optional[str]] = mapped_column(String(60), default="per contract term")
    hours_per_unit: Mapped[float] = mapped_column(Float, default=0)
    amount: Mapped[Optional[float]] = mapped_column(Float)
    effective_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    assigned_coach_id: Mapped[Optional[int]] = mapped_column(ForeignKey("lcs_coach.id"))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    contract: Mapped[TrainingContract] = relationship(back_populates="services")
    service_type: Mapped[ServiceType] = relationship()
    assigned_coach: Mapped[Optional[Coach]] = relationship()
    deliveries: Mapped[list["ServiceDeliveryLog"]] = relationship(back_populates="contract_service")

    @property
    def committed_hours(self) -> float:
        return (self.contracted_quantity or 0) * (self.hours_per_unit or 0)


class ContractAmendment(Base):
    __tablename__ = "lcs_contract_amendment"
    __doctype__ = "LCS Contract Amendment"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("lcs_training_contract.id"))
    amendment_date: Mapped[dt.date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(Text)
    changed_by: Mapped[Optional[str]] = mapped_column(String(120))
    reason: Mapped[Optional[str]] = mapped_column(Text)
    value_change: Mapped[Optional[float]] = mapped_column(Float)
    contract: Mapped[TrainingContract] = relationship(back_populates="amendments")


class BillingInstallment(Base):
    __tablename__ = "lcs_contract_billing_schedule"
    __doctype__ = "LCS Contract Billing Schedule"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("lcs_training_contract.id"))
    due_date: Mapped[dt.date] = mapped_column(Date)
    amount: Mapped[float] = mapped_column(Float)
    amount_consultation: Mapped[float] = mapped_column(Float, default=0)
    amount_training: Mapped[float] = mapped_column(Float, default=0)
    amount_travel: Mapped[float] = mapped_column(Float, default=0)
    amount_zone: Mapped[float] = mapped_column(Float, default=0)
    amount_other: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(20), default="Planned")
    invoice_ref: Mapped[Optional[str]] = mapped_column(String(80))
    note: Mapped[Optional[str]] = mapped_column(String(300))
    contract: Mapped[TrainingContract] = relationship(back_populates="billing_schedule")


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------
class TaskTemplate(Base):
    __tablename__ = "lcs_training_task_template"
    __doctype__ = "LCS Training Task Template"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    source_document: Mapped[Optional[str]] = mapped_column(String(300))
    service_type_id: Mapped[Optional[int]] = mapped_column(ForeignKey("lcs_service_type.id"))  # None = every contract
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    service_type: Mapped[Optional[ServiceType]] = relationship()
    items: Mapped[list["TaskTemplateItem"]] = relationship(back_populates="template", cascade="all, delete-orphan",
                                                           order_by="TaskTemplateItem.sort_order")


class TaskTemplateItem(Base):
    __tablename__ = "lcs_task_template_item"
    __doctype__ = "LCS Task Template Item"
    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("lcs_training_task_template.id"))
    stage: Mapped[Optional[str]] = mapped_column(String(80))
    task: Mapped[str] = mapped_column(String(300))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    template: Mapped[TaskTemplate] = relationship(back_populates="items")


class OnboardingChecklist(Base):
    __tablename__ = "lcs_onboarding_checklist"
    __doctype__ = "LCS Onboarding Checklist"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("lcs_training_contract.id"), unique=True)
    generated_date: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="In Progress")
    tasks: Mapped[list["OnboardingTask"]] = relationship(back_populates="checklist", cascade="all, delete-orphan",
                                                         order_by="OnboardingTask.sort_order")
    contract: Mapped[TrainingContract] = relationship()


class OnboardingTask(StepMixin, Base):
    __tablename__ = "lcs_onboarding_task"
    __doctype__ = "LCS Onboarding Task"
    checklist_id: Mapped[int] = mapped_column(ForeignKey("lcs_onboarding_checklist.id"))
    stage: Mapped[Optional[str]] = mapped_column(String(80))
    template_item_id: Mapped[Optional[int]] = mapped_column(Integer)
    service_type_id: Mapped[Optional[int]] = mapped_column(ForeignKey("lcs_service_type.id"))
    checklist: Mapped[OnboardingChecklist] = relationship(back_populates="tasks")


# ---------------------------------------------------------------------------
# Delivery, consult plans
# ---------------------------------------------------------------------------
class ServiceDeliveryLog(Base):
    """One training, audit, consult session or meeting (Training Tracking + Audit Tracking tabs)."""
    __tablename__ = "lcs_service_delivery_log"
    __doctype__ = "LCS Service Delivery Log"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("lcs_training_contract.id"))
    contract_service_id: Mapped[Optional[int]] = mapped_column(ForeignKey("lcs_contract_service.id"))
    coach_id: Mapped[Optional[int]] = mapped_column(ForeignKey("lcs_coach.id"))
    status: Mapped[str] = mapped_column(String(20), default="Scheduled")
    date_performed: Mapped[Optional[dt.date]] = mapped_column(Date)
    end_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    time_text: Mapped[Optional[str]] = mapped_column(String(60))
    duration: Mapped[Optional[float]] = mapped_column(Float)  # hours
    units: Mapped[float] = mapped_column(Float, default=1)    # sessions/audits counted against the line
    mode: Mapped[Optional[str]] = mapped_column(String(20))
    audience: Mapped[Optional[str]] = mapped_column(String(120))
    subject: Mapped[Optional[str]] = mapped_column(String(300))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    # Training Tracking checklist
    pre_meeting: Mapped[Optional[str]] = mapped_column(String(120))
    participants_received: Mapped[Optional[str]] = mapped_column(String(120))
    folder_link_sent: Mapped[Optional[str]] = mapped_column(String(120))
    placement_assessment: Mapped[Optional[str]] = mapped_column(String(120))
    pre_assessment: Mapped[Optional[str]] = mapped_column(String(120))
    post_assessment: Mapped[Optional[str]] = mapped_column(String(120))
    evaluations_received: Mapped[Optional[str]] = mapped_column(String(120))
    # Audit Tracking checklist
    calpads_login: Mapped[Optional[str]] = mapped_column(String(120))
    go_ahead: Mapped[Optional[str]] = mapped_column(String(120))
    follow_up_requested: Mapped[Optional[str]] = mapped_column(String(120))
    follow_up_completed: Mapped[Optional[str]] = mapped_column(String(120))
    contract: Mapped[TrainingContract] = relationship()
    contract_service: Mapped[Optional[ContractService]] = relationship(back_populates="deliveries")
    coach: Mapped[Optional[Coach]] = relationship()


class ConsultPlan(Base):
    """The Pre-Consult Meeting Set Up Sheet."""
    __tablename__ = "lcs_consult_plan"
    __doctype__ = "LCS Consult Plan"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("lcs_training_contract.id"))
    contract_service_id: Mapped[Optional[int]] = mapped_column(ForeignKey("lcs_contract_service.id"))
    consult_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="Draft")
    plan_sent_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    structure_description: Mapped[Optional[str]] = mapped_column(Text)
    baseline: Mapped[Optional[str]] = mapped_column(Text)
    goals: Mapped[Optional[str]] = mapped_column(Text)
    objectives: Mapped[Optional[str]] = mapped_column(Text)
    contract: Mapped[TrainingContract] = relationship()
    contract_service: Mapped[Optional[ContractService]] = relationship()
    sessions: Mapped[list["ConsultSession"]] = relationship(back_populates="plan", cascade="all, delete-orphan",
                                                            order_by="ConsultSession.session_no")


class ConsultSession(Base):
    __tablename__ = "lcs_consult_session"
    __doctype__ = "LCS Consult Session"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("lcs_consult_plan.id"))
    session_no: Mapped[int] = mapped_column(Integer)
    duration: Mapped[Optional[float]] = mapped_column(Float)
    mode: Mapped[Optional[str]] = mapped_column(String(20))
    session_date: Mapped[Optional[dt.date]] = mapped_column(Date)
    date_text: Mapped[Optional[str]] = mapped_column(String(80))
    topic: Mapped[Optional[str]] = mapped_column(Text)
    delivery_log_id: Mapped[Optional[int]] = mapped_column(ForeignKey("lcs_service_delivery_log.id"))
    plan: Mapped[ConsultPlan] = relationship(back_populates="sessions")
    delivery_log: Mapped[Optional[ServiceDeliveryLog]] = relationship()


# ---------------------------------------------------------------------------
# Users, sign-in sessions, change log  (app-only; ERPNext has its own users)
# ---------------------------------------------------------------------------
ROLES = {
    "Admin": "Everything, including users, imports, the price list and setup.",
    "Office Staff": "Day-to-day work: clients, leads, quotes, contracts, billing, delivery, coaches.",
    "Coach": "Trainings, audits, consult plans, onboarding and their own time off. No dollar amounts.",
    "Viewer": "Can see everything but change nothing.",
}


class User(Base):
    __tablename__ = "app_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True)   # usually the work email
    full_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20), default="Viewer")
    password_hash: Mapped[str] = mapped_column(String(300))
    coach_id: Mapped[Optional[int]] = mapped_column(ForeignKey("lcs_coach.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(default=dt.datetime.now)
    last_login: Mapped[Optional[dt.datetime]] = mapped_column()
    coach: Mapped[Optional[Coach]] = relationship()


class UserSession(Base):
    __tablename__ = "app_user_session"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the cookie value
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[dt.datetime] = mapped_column()
    created_at: Mapped[dt.datetime] = mapped_column(default=dt.datetime.now)


class AuditLog(Base):
    __tablename__ = "app_audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(default=dt.datetime.now, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer)
    user_name: Mapped[Optional[str]] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(300))
    status: Mapped[int] = mapped_column(Integer)


class Setting(Base):
    __tablename__ = "app_setting"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text)


# ---------------------------------------------------------------------------
engine = create_engine(f"sqlite:///{DB_PATH}", future=True)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)
