# Brier

Invoice and receipt field extraction with calibrated confidence, plus explainable
duplicate-claim detection. The name comes from the Brier score, the measure this
project uses to check that its confidence scores can be trusted.

Employees submit expense reimbursements with receipt images. Finance needs three
things: the fields read off each receipt automatically, duplicate claims caught
*before* money goes out, and — when something is flagged — a straight answer to
"why?".

Two things make this more than a regex script:

- **Per-field confidence scoring.** Every extracted value carries a weighted sum
  of *named* components (pattern specificity, position on the page, OCR clarity,
  whether the amounts reconcile, validator verdict, vendor-match strength,
  candidate margin, a line-role model, GST-rate consistency). The breakdown is
  stored and shown in the UI, so a low score can be interrogated rather than
  merely distrusted. The scores are calibrated and measured, not decorative.
- **Explainable duplicate detection.** Blocking narrows the search, independent
  signals score each pair, and deterministic rules short-circuit the cases where
  the evidence is conclusive. Every flag lists the signals that produced it.

```
core/                   pure Python, never imports Django
  extraction/           patterns, candidate generation, confidence scoring
  dedup/                blocking, signals, rules, scoring, clustering
  datagen/              synthetic receipt generator (images + ground truth)
  eval/                 accuracy, calibration, blocking quality, ablation
expenses/               Django app: models, workflow services, review UI
data/                   generated dataset and evaluation reports
```

`core/` has no Django dependency, so the same code is exercised by the unit
tests, the evaluation harness and the web app.

---

## Setup

Requires Python 3.11+ (3.12 is what this was built on). Pick your platform
below; the two blocks are equivalent, only the paths differ.

### Windows

The `python` on PATH may be the Microsoft Store stub, which will not work:

```powershell
winget install --id Python.Python.3.12 -e --source winget
# then, in a NEW terminal:
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

```powershell
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py gen_dataset      # ~2 min: images + CSVs
.venv\Scripts\python manage.py train_models     # optional line-role model
.venv\Scripts\python manage.py ingest --flush
.venv\Scripts\python manage.py seed_users
.venv\Scripts\python manage.py runserver
```

### macOS / Linux

```bash
# macOS, with Homebrew:
brew install python@3.12
# Debian / Ubuntu:
# sudo apt install python3.12 python3.12-venv

python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

```bash
.venv/bin/python manage.py migrate
.venv/bin/python manage.py gen_dataset          # ~2 min: images + CSVs
.venv/bin/python manage.py train_models         # optional line-role model
.venv/bin/python manage.py ingest --flush
.venv/bin/python manage.py seed_users
.venv/bin/python manage.py runserver
```

Sign in at http://127.0.0.1:8000/ as `finance` / `demo12345` (reviewer) or
`e001` / `demo12345` (employee).

Every step is required except `train_models`. The repository ships no database
and no receipt images, so `gen_dataset` and `ingest` are what give you something
to look at, and `seed_users` is what creates those two logins. Skip it and there
is no account to sign in with.

### Database

SQLite by default, so a clone runs with no database to set up. PostgreSQL is a
one-line switch: everything is driven by `DATABASE_URL` and no Postgres-only
field types are used anywhere.

```
DATABASE_URL=postgres://user:pass@localhost:5432/brier
```

**Both backends are tested, not just supported.** Run the whole thing on
PostgreSQL:

```powershell
# Windows:
winget install -e --id PostgreSQL.PostgreSQL.17
& "C:\Program Files\PostgreSQL\17\bin\psql.exe" -U postgres -c "create database brier;"
```

```bash
# macOS:
brew install postgresql@17 && brew services start postgresql@17
createdb brier
```

Then point `DATABASE_URL` at it and run the same commands as usual:

```powershell
$env:DATABASE_URL = "postgres://postgres:postgres@127.0.0.1:5432/brier"
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py ingest --flush
.venv\Scripts\python manage.py seed_users
.venv\Scripts\python -m pytest -q
```

Measured on PostgreSQL 17.11, with the same corpus the SQLite default ships:

| | SQLite | PostgreSQL 17.11 |
|---|---|---|
| `migrate` | clean | clean |
| `ingest --flush` | 480 receipts, 508 claims | 480 receipts, 508 claims, 311 flags in 62 groups, 40s |
| `seed_users` | finance, e001..e005, admin | identical |
| Test suite | **387 passed** | **387 passed** |
| Dashboard, review queue, claims, landing | 200 | 200 |

The SQLite-specific tuning in `config/settings.py` (`IMMEDIATE` transactions,
WAL, a busy timeout, and no persistent connections) is applied only when the
engine is SQLite, so Postgres keeps `CONN_MAX_AGE` and its own locking.

### OCR

**Uploading a receipt image requires Tesseract.** Without it, pasting the
receipt text still works and everything downstream is identical.

```powershell
winget install --id UB-Mannheim.TesseractOCR -e
```

```bash
# macOS:
brew install tesseract
# Debian / Ubuntu:
sudo apt install tesseract-ocr
```

Then restart the server and confirm:

```powershell
.venv\Scripts\python manage.py check_ocr        # Windows
```

```bash
.venv/bin/python manage.py check_ocr            # macOS / Linux
```

That prints whether the Python binding and the binary were both found, and OCRs
a sample receipt so you can see the quality. The binary is located via
`TESSERACT_CMD`, then PATH, then the usual install directories. Homebrew and apt
put it on PATH for you; the Windows installer often does not, and a running
server would not pick up a PATH change anyway. Set `TESSERACT_CMD` in `.env` for
a non-standard location.

Because the sidecar is the *exact* rendered text, evaluating against it would be
trivially perfect and would tell you nothing. `core/degrade.py` therefore
simulates realistic OCR damage (character confusions, dropped and doubled
characters, lowered per-character confidences) keyed to the same noise tier used
to render the image. That is the default evaluation mode; `--mode ocr` runs the
real engine, and `--mode clean` is the no-noise upper bound.

Real OCR damages receipts in ways a character-level simulator does not, and the
extractor handles the structural cases specifically:

| Corruption | Example | Handling |
|---|---|---|
| Currency glyph read as a digit | `₹18,554.00` → `118,554.00` | Arithmetic, or the fact that a total cannot exceed ~1.5× its subtotal under GST |
| Implausible reconciliation | tax "repaired" to 102,822 on a 15,723 subtotal | Repairs are rejected above a believable tax rate |
| Punctuation inside a date | `Jan'19, 2025` | Loosened separator |
| Word char after a tax label | `SGST_@ 9%` | `(?![A-Za-z0-9])` instead of `\b` |
| Glyph confusion in a GSTIN | `27AAACRSOSSK1Z7` | Bounded search over confusable positions, checksum decides |

The GSTIN repair returns nothing rather than guessing when it cannot find a
checksum-valid reading — a confidently wrong identifier would silently link two
unrelated vendors, which is worse than a missing one.

---

## Commands

| Command | What it does |
|---|---|
| `gen_dataset` | Synthetic receipts: PNGs, text sidecars, and four ground-truth CSVs |
| `train_models` | Trains the optional line-role classifier from the generator's free labels |
| `ingest` | Loads the CSVs into the DB **through the real pipeline** |
| `rescore` | Rebuilds duplicate flags after a rule or weight change |
| `evaluate` | Measures extraction accuracy, calibration and dedup quality |
| `seed_users` | Creates the finance group and demo logins |
| `check_ocr` | Diagnoses the OCR setup and OCRs a sample image |

```powershell
.venv\Scripts\python manage.py evaluate --ablation --tune
```

writes `data/eval_report.md`, `data/eval_report.json` and `data/eval_errors.csv`.

---

## How extraction works

```
image ──(optional OCR)──┐
text sidecar ───────────┴─► normalize ─► per-field candidates ─► line-model
    re-rank ─► cross-field reconciliation ─► confidence scoring ─► result
```

Each field generates *many* candidates and keeps the losers, because the margin
between the winner and the runner-up is itself a confidence component (`uniq`).
Labelled matches outrank positional guesses via a specificity tier — which is
how `Grand Total: ₹1,174.00` beats the `Total Qty: 16` decoy sitting three lines
above it.

Reconciliation checks `subtotal + taxes + round_off == total`. When exactly one
component is missing it is solved for algebraically and marked
`repaired:<field>`, which carries both a lower tier and a multiplicative
penalty — a repaired value never masquerades as one that was actually read.

Hard validators (GSTIN mod-36 checksum, date plausibility window, invoice-number
charset) cap confidence at 0.35 and force manual review when they fail.

## Currencies

Receipts are not all Indian, and the system does not assume they are. Currency is
a first-class field: detected per receipt, stored, used to format every amount in
the UI, and used to keep duplicate detection honest.

**Amounts** are parsed *structurally*, not by locale. Whichever of `.` or `,`
comes last and is followed by one or two digits is the decimal point; three
trailing digits mean grouping. That one rule reads every convention without being
told which applies:

| Written | Parsed | Convention |
|---|---|---|
| `1,234.56` | 1234.56 | US / UK / India |
| `1.234,56` | 1234.56 | Germany, Spain, Italy |
| `1 234,56` | 1234.56 | France, Scandinavia |
| `1,23,456.78` | 123456.78 | Indian lakh grouping |
| `34,39` | 34.39 | bare euro decimal comma |
| `(25.00)` | -25.00 | accounting negative |

**Detection** prefers an explicit ISO code, then a symbol that identifies one
currency, then a shared symbol like `$` disambiguated by locale markers (`EIN`
and `Sales Tax` imply USD, `MwSt` implies EUR, `GSTIN` implies INR). A symbol
only counts when it sits against a number — otherwise ZAR's bare `R` matches
inside any word, and "CAFE ZUR POST" reads as a South African receipt.

**Tax** is generalised. India splits GST into CGST/SGST/IGST; most jurisdictions
print one line. `tax_total` matches `Sales Tax`, `VAT`, `MwSt`, `TVA`, `IVA`,
`HST/PST/QST` and others, and reconciliation uses it when no GST component is
present — so a US "Sales Tax" line is read as tax rather than left unexplained
and silently attributed to CGST.

**Dates** follow the currency: `03/04/2025` is 3 April on a euro receipt and
4 March on a US one.

**Duplicate detection never compares across currencies.** 100 USD and 100 INR are
different claims; the amount signal returns 0 and the amount-based block keys are
namespaced by currency. A matching invoice number still forces EXACT, because one
bill with a misread currency is still that one bill. Dashboard totals are grouped
per currency rather than summed into a meaningless figure.

Adding a currency is one entry in `CURRENCIES` in `core/currency.py`.

## How duplicate detection works

**Blocking** — a claim is only compared against claims sharing a cheap key:
invoice number, vendor + rounded amount, employee + date ±3d, employee + amount
bucket + week, perceptual-hash LSH bands, or a genuinely rare text token. Blocks
that grow beyond 40 members are dropped rather than trimmed: receipts share
layout, so such a block carries no information, and any real pair inside it also
shares a selective key.

**Signals** — invoice match, amount (with tolerance), date proximity, vendor
fuzzy similarity, image perceptual hash, TF-IDF text cosine. A signal that
cannot be computed returns `None`, not zero, and the weights are renormalized
over what exists — otherwise a missing invoice number would manufacture evidence
of *innocence*.

**Rules** short-circuit the weighted score when the evidence is conclusive: same
invoice + same vendor; same employee + vendor + date + amount; an identical
image *corroborated* by vendor or amount; everything matching except the invoice
number (the shape of a doctored resubmission). Plus resubmission-after-rejection
and split-claim patterns.

Pairs land in `EXACT / HIGH / MEDIUM / LOW` bands. Union-find over the strong
bands assigns a duplicate group, so three submissions of one receipt are one
decision rather than three.

### On perceptual hashing

phash is far weaker on receipts than on photographs — every receipt is a
dark-on-light block of monospace text, so the low-frequency structure phash keys
on is nearly identical across unrelated bills. Measured on this corpus, a
Hamming distance of 4 covers 1.2% of *random* pairs (~1,500 spurious pairs in a
500-claim corpus). So the identical-image rule requires distance ≤ 2 **and** a
corroborating field, and the soft signal is floored at distance 6.

## Workflow

`DRAFT → SUBMITTED → UNDER_REVIEW → APPROVED | REJECTED`, plus `NEEDS_INFO`.

Every transition goes through `expenses/services.py`, which writes an
`AuditEvent` and enforces the rule the project exists for: **a claim cannot be
approved while an EXACT or HIGH duplicate flag is open.** Finance must confirm
or dismiss the flag first. The guard lives in the service layer, not in a view,
so no code path can approve around it.

Receipts whose document confidence falls below 0.60 are routed to `NEEDS_INFO`
rather than pushed into the queue as a confident-looking guess.

---

## Notebook

`notebooks/ml_analysis.ipynb` documents the learned and statistically-justified parts,
with the evidence rather than the assertion:

| Part | Question |
|---|---|
| Line-role classifier | Can a model tell TOTAL from ITEM, and is it worth the dependency? |
| Confidence calibration | Does a confidence of 0.9 actually mean 90% correct? |
| Duplicate signals | Which signals carry information, and what should they weigh? |
| Perceptual hashing | Is phash usable on text documents at all? |

```powershell
.venv\Scripts\python -m jupyter lab notebooks/ml_analysis.ipynb
```

It imports from `core/` — the same code the app and tests use, not a reimplementation —
and runs top to bottom in about two minutes. It reports the auto-accept threshold as a
live measurement, so re-run it after changing any extraction pattern and copy the value
into `CONFIDENCE_AUTO_ACCEPT`; that number drifts whenever the extractor does.

## Testing

```powershell
.venv\Scripts\python -m pytest -q
```

Covers normalizers, every field extractor, the GSTIN checksum, each duplicate
signal and rule, blocking, clustering, and every workflow transition — including
that approval is refused while a duplicate flag is open.
