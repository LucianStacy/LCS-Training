# LCS-Training

A standalone app for running LCS-Training's SEIS and CALPADS training business. It covers leads, quotes, contracts, onboarding, coach capacity, consult plans, trainings and audits, and billing schedules.

It is built to the **LCS-Training Platform Blueprint**: tables and fields use the Blueprint's DocType names (`LCS Training Contract`, `LCS Contract Service`, …). That way the data can move into the future `lcs_training` ERPNext app without being reworked. Until then the app runs on its own.

No student information is stored. The only student-related value is a district's *size tier* (Small/Medium/Large/XL), which sets audit pricing.

## Run it

### With Docker (recommended)

```bash
docker compose up -d --build
```

Open <http://localhost:8765>. The data lives in the `lcs-data` Docker volume, so it survives restarts and rebuilds. Stop the app with `docker compose down`. Leave off `-v`: `docker compose down -v` deletes the data volume.

To back up the database:

```bash
docker compose cp lcs-training:/data/lcs_training.db ./lcs_training-backup.db
```

### Without Docker

Install Python 3.11+ and double-click `run.bat`, or run:

```bash
pip install -r requirements.txt
python -m lcs_training
```

The data is saved in `data/lcs_training.db`.

## First steps

1. **Import / Export → Price list**: upload a Quote Pricing Worksheet (.xlsx). Every price is marked *needs review* until you confirm it for the year.
2. **Import / Export → Client spreadsheet**: upload *Client Spreadsheet - Contracts and Services*. It reads every tracking tab and brings in:
   - clients, contacts and tags
   - leads
   - contract paperwork and service quantities
   - onboarding status
   - Coach Capacity, Training Tracking and Audit Tracking
   - Invoice Info
3. Check the import report. Each time a name was matched to an existing client (for example "AV SELPA" → "Antelope Valley SELPA"), the report lists it. Fix any wrong match with **Merge into another client** on the client's page.

## How the pieces connect

| Step | Screen | What happens automatically |
|---|---|---|
| Lead | Leads | Step checklist (triage, meeting, quote sent, follow-ups) |
| Quote | Quotes | Priced from the year's price list: size tiers, Email Q&A deals, the NDT 4-or-more discount, hotel/per diem, zone charges by mileage. Subtotals for Consultation / Training / Travel / Zone / Other |
| Accept quote | Quotes | Creates the contract, its service lines (with coach hours) and a monthly billing schedule split by category |
| Paperwork | Contract → Paperwork | Marking the fully executed contract as received moves it to *Pending Assignment* and generates the onboarding checklist. The checklist only has tasks for the services purchased |
| Coaches | Contract → Services | Each service line gets a coach. When all have one, the contract becomes *Active*. Each coach shows their remaining headroom |
| Consult plan | Contract → Consult plans | The Pre-Consult Meeting Set Up Sheet. Planned hours are checked against contracted hours, "Mark held" logs delivery, and you can download it as a branded Word document |
| Delivery | Trainings & Audits | Replaces the Training Tracking and Audit Tracking tabs. Feeds utilization ("8 of 12 delivered") and renewal risk |
| Capacity | Coaches | Monthly capacity, less time off, compared with workload. Workload is the larger of contracted and delivered hours on each line |
| Amendments | Contract → Amendments | Either an amendment quote or a direct value change. Service lines are effective-dated (closed and replaced, never overwritten). Invoiced installments are kept and the remaining balance is spread over the remaining months |
| Billing | Contract → Billing | Monthly / Quarterly / Annual-Upfront / Custom schedules, plus a printable invoice sheet per installment |

## Moving to ERPNext later

**Import / Export → Download ERPNext export** produces one CSV per DocType, plus `manifest.json` and a mapping README. Each CSV uses the Blueprint's names and puts the linked record's *name* in link fields, which is how ERPNext links records. The export can go through ERPNext's Data Import tool, or be the starting point for a sync into the `lcs_training` custom app.

| This app | ERPNext |
|---|---|
| Clients | `LCS District`, `LCS SELPA` |
| Contacts + tags | `Contact` (+ `lcs_tags` → Email Group sync) |
| Leads | `Lead` |
| Price list | `Item` / `Item Price` (categories → Item Groups / income accounts) |
| Quotes | `Quotation` |
| Contracts | `LCS Training Contract`, `LCS Contract Service`, `LCS Contract Amendment`, `LCS Contract Billing Schedule` |
| Onboarding | `LCS Training Task Template`, `LCS Onboarding Checklist` |
| Coaches | `LCS Coach`, `LCS Coach Unavailability` |
| Delivery | `LCS Service Delivery Log` |
| Consult plans | `LCS Consult Plan`, `LCS Consult Session` (new; not in the Blueprint yet) |

## Notes

- **Repository privacy:** this repository is public. `.gitignore` keeps the database, uploads and every spreadsheet/Word/CSV/zip file out of git, so client data stays on this computer.
- **No logins yet:** the app has no login screen. Docker publishes it on `127.0.0.1` only, so only this computer can reach it.
- **Branding:** it follows the LCS-Training Brand Style Guide: LCS Purple `#74439A`, Magenta `#E9166E` for primary buttons, Montserrat headings, Calibri body text.
