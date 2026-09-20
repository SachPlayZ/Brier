# Project 5 — Receipt Extraction + Duplicate Claims Detection

## Plan
- [x] Phase 0 — environment (Python 3.12, venv, deps)
- [x] Phase 1 — repo skeleton, Django config, SQLite/Postgres switch
- [x] Phase 2 — synthetic dataset generator (4 templates, 4 noise tiers, labelled dups)
- [x] Phase 3 — extraction: patterns, validators, reconciliation, confidence scoring
- [x] Phase 4 — dedup: blocking, signals, rules, scoring, clustering
- [x] Phase 5 — Django app: models, workflow services, Bootstrap review UI, admin
- [x] Phase 6 — evaluation harness: accuracy, calibration, blocking, ablation, tuning
- [x] Tests: extraction, dedup, workflow
- [x] README + lessons

## Results

See `data/eval_report.md` for the generated report. Headline numbers are pasted
into the "Measured results" section below after each run.

## Notes
- OCR is optional. Evaluation defaults to `--mode simulated`, which applies
  deterministic OCR-style damage to the text sidecar keyed to the render's noise
  tier. `--mode clean` is the no-noise upper bound; `--mode ocr` uses Tesseract
  when installed.
- Duplicate weights ship as hand-set defaults. `evaluate --tune` random-searches
  them on a dev split and reports both splits; the defaults are kept unless the
  tuned set wins on the held-out test split.

## Measured results (seed 42, 480 receipts / 508 claims, `--mode simulated`)

### Field extraction

| Field | n | Precision | Recall | F1 |
|---|---:|---:|---:|---:|
| vendor | 480 | 0.981 | 0.981 | 0.981 |
| date | 480 | 1.000 | 0.946 | 0.972 |
| total | 480 | 0.956 | 0.956 | 0.956 |
| gstin | 480 | 0.991 | 0.906 | 0.947 |
| invoice_no | 480 | 0.923 | 0.894 | 0.908 |
| subtotal | 480 | 0.897 | 0.875 | 0.886 |
| cgst / sgst / igst | 352/352/71 | ~0.94 | ~0.91 | ~0.92 |

Amounts reconcile on 94.2% of receipts.

By noise tier (F1): tier 0 is 1.000 across every field; tier 2 runs 0.82–0.97;
tier 3 falls to 0.57–0.86. The tier-0/tier-3 spread is the point — it shows the
pipeline degrades gracefully and that the per-tier breakdown is worth reporting.

### Confidence calibration
- ECE **0.0539** (target ≤ 0.08), Brier **0.0318**, n = 3,655
- Accuracy at 50% coverage **1.000**, at 80% **0.995**, at 100% **0.923**
- **Empirical auto-accept cutoff: 0.8433** → 99.0% accuracy over 84% of fields.
  The hand-guessed seed was 0.85, so the guess was close — but that is only
  worth knowing because it was measured.

### Duplicate detection
- Blocking: 5,881 candidate pairs of 128,778 possible (23 per claim),
  reduction **0.954**, recall **0.9895** (1 pair missed of 95)
- **HIGH+: precision 0.990, recall 0.990, F1 0.990** — this is the band that
  blocks approval
- MEDIUM+: precision 0.810, recall 0.990 (MEDIUM does not block approval)
- Average precision **0.9895**, precision in the top 50 **1.000**
- Recall by type at MEDIUM+: PHOTO_REUSE 1.00 · RESUBMIT_AFTER_REJECT 1.00 ·
  SPLIT 1.00 · TRANSITIVE 1.00 · RETYPED 0.95
- Signal ablation (rules off): invoice −0.069, amount −0.050, vendor −0.005

### Against the plan's targets
| Target | Result |
|---|---|
| Tier 0 F1 ≥ 0.95 (total/gstin/invoice), ≥ 0.92 vendor | **met** — 1.000 across the board |
| Tier 2 F1 ≥ 0.85 all fields | **partly met** — vendor 0.970, date 0.935, total 0.889, gstin 0.878 pass; invoice_no 0.817 falls short |
| ECE ≤ 0.08 | **met** (0.0539) |
| Blocking recall ≥ 0.98 | **met** (0.9895) |
| Reduction ratio ≥ 0.99 | **not met** (0.954) — see below |
| EXACT+HIGH precision ≥ 0.95 | **met** (0.990) |

Two honest shortfalls:
- **Reduction ratio 0.954 vs the 0.99 target.** Tightening further cost blocking
  recall, and recall is the more expensive thing to lose — a duplicate that is
  never a candidate can never be caught. At 23 candidate comparisons per claim
  the cost is negligible in practice, so recall was kept.
- **invoice_no at tier 3 (F1 0.571).** Invoice numbers are high-entropy strings
  with no redundancy, so a single OCR character error is unrecoverable — unlike
  an amount, which the arithmetic check can repair. The confidence score does
  reflect this correctly, which is what routes those receipts to manual review.

### Verification performed
- `pytest` — 103 tests pass (extraction, dedup, workflow, views)
- `gen_dataset --seed 42` twice → byte-identical CSVs and sidecar text
- 16/16 HTTP end-to-end checks against the running server
- Screenshots reviewed: dashboard, review queue, claim detail, duplicate compare
- Approval guard verified three ways: direct service call, HTTP POST, and the
  disabled Approve button with its "Approval blocked" banner

---

## Follow-up: real OCR enabled (2026-09-13)

Reported: uploading an image returned *"Tesseract is not installed, so an
uploaded image cannot be read."* This was the deferred decision in the original
plan; hitting it settled the question.

- [x] Install Tesseract 5.4.0
- [x] Locate the binary explicitly (`TESSERACT_CMD` → PATH → known install
      dirs). The Windows installer skips PATH, and a running server would not
      see a PATH change regardless.
- [x] `manage.py check_ocr` — reports binding, binary, version, and OCRs a
      sample so the quality is visible
- [x] `pytesseract` promoted from commented-out to a real requirement

Running real OCR immediately exposed four extraction bugs that the synthetic
noise simulator never produced, because it corrupted characters but not
structure:

| Bug | Impact | Fix |
|---|---|---|
| `₹18,554.00` read as `118,554.00` | claim inflated 6× | strip the leading glyph digit when arithmetic or the GST ceiling says so |
| tax "repaired" to 102,822 on a 15,723 subtotal, reported `arithmetic_ok=True` | a bad total *validated* by a fabricated tax | repairs rejected above a believable tax rate |
| `Jan'19, 2025` | date missed entirely | loosened month/day separator |
| `SGST_@ 9%` | tax missed (`_` is a word char, so `\b` fails) | `(?![A-Za-z0-9])` |
| `SGST: 89.55` read as rate 89.5 / amount 5 | regression from the above | percent marker made mandatory |
| GSTIN glyph confusions | GSTIN unusable under OCR | bounded search over confusable positions, checksum decides |

The GSTIN repair initially produced a checksum-*valid but wrong* GSTIN by
flipping position 13 (a literal `Z`). That is worse than no GSTIN — it would
silently link unrelated vendors — so the position is pinned and the search now
returns nothing when it cannot recover. 10 regression tests cover all of this.

**Effect on the same upload** (R00004, real Tesseract):

| Field | Before | After | Truth |
|---|---|---|---|
| total | 118554.00 | **18554.00** | 18554.00 |
| date | *missing* | **2025-01-19** | 2025-01-19 |
| cgst | 102822.19 (invented) | 1407.09 (repaired) | 1415.10 |
| sgst | *missing* | **1415.10** | 1415.10 |
| arithmetic_ok | True (on garbage) | True (genuinely) | — |

Corpus regression (`--mode simulated`): no field regressed; GSTIN F1 improved
0.947 → **0.955** with precision now 1.000. `arithmetic reconciled` fell
94.2% → 92.7%, which is the correct direction — those were false reconciliations.

### Second wave: bugs only real OCR exposed

`Total Before Tax: 12,135.82` was being captured as the **grand total**. The
18-character label-to-value gap is wide enough for `total` to step over
`Before Tax:`. On the clean renders the column spacing exceeded that gap, so the
pattern never reached the value — the bug was invisible until OCR collapsed the
whitespace, and it showed up on *tier 0*. Also fixed: punctuation inserted
inside a label (`Amount: Payable`), the GSTIN separator class missing `.`
(`GST No.: ...`), and GSTIN length repair for a split glyph (`B1Z7` → `B12Z7`).

Widening the GSTIN capture to 14-17 chars raised recall but dropped precision
0.813 → 0.754, because failed repairs emitted wrong-length garbage. A
wrong checksummed identifier is worse than a missing one, so a non-repairable
wrong-length value is now discarded: precision back to 0.780 with recall held.

### Real-OCR results (Tesseract 5.4.0, `--mode ocr`, 480 receipts)

| Field | F1 before fixes | F1 after | Simulated |
|---|---:|---:|---:|
| vendor | 0.998 | **0.998** | 0.990 |
| igst | 0.950 | **0.950** | 0.927 |
| date | 0.969 | **0.969** | 0.972 |
| invoice_no | 0.945 | **0.945** | 0.908 |
| subtotal | 0.945 | **0.945** | 0.886 |
| sgst | 0.937 | **0.937** | 0.920 |
| cgst | 0.885 | **0.885** | 0.927 |
| total | 0.825 | **0.860** | 0.963 |
| gstin | 0.558 | **0.589** | 0.962 |

`total` by tier: 0.853 → **0.926** on tier 0, 0.879 on tier 2, 0.333 on tier 3.
Arithmetic reconciliation 87.7% → **90.4%**.

Two honest observations:

- **The noise simulator is miscalibrated, in both directions.** It is *harsher*
  than real Tesseract on `invoice_no` (0.908 vs 0.945) and `subtotal` (0.886 vs
  0.945), while completely missing the structural damage that actually matters
  (currency glyphs, punctuation inserted into labels, split glyphs). Simulated
  mode remains the default because it needs no binary, but the real-OCR numbers
  are the ones to quote.
- **GSTIN is the weak field under real OCR** (F1 0.589, extracted on 61% of
  receipts). It is a high-entropy 15-character string with no redundancy beyond
  its checksum, so multi-character damage is unrecoverable — and the repair is
  deliberately built to give up rather than guess. The confidence score reflects
  this correctly, which is what routes those receipts to manual review.

Corpus regression on the text path: no field regressed; vendor 0.983 → 0.990,
total 0.954 → 0.963, gstin 0.955 → 0.962.

---

# UI refresh (Kokonut-style shell)

Templates + static only; no Python changes.

- [x] `static/expenses/app.css` tokens + components, `app.js`
- [x] `base.html` shell (sidebar, topbar, login variant)
- [x] dashboard, claim_list, review_queue
- [x] claim_detail, dup_compare
- [x] claim_submit, login, `_confidence`
- [x] copy audit (no em-dash, no `·`, only dynamic bar widths inline)
- [x] verify: pytest 157 pass, route sweep (finance/employee/anon), light/dark screenshots at 1440 and 390

## Review
- Bootstrap 5.3 kept; `app.css` maps zinc tokens onto `--bs-*`. Theme via `data-bs-theme`, saved in localStorage, defaults to OS.
- Fixed a latent bug: claim_list/review_queue selects called `this.form.submit()` outside any `<form>`; now real GET forms.
- Input borders use a separate `--field-line` (3:1) since card borders are deliberately faint.
- `landing.html` untouched (own stylesheet, marketing page).
- Not changed (data/view, not UI): flag-band order on dashboard follows view order; review queue caps at 200.

---

# Loading animations

CSS + vanilla JS, Django templates.

- [x] `loading.css` + `loading.js`
- [x] `_preloader.html`, `_skeleton.html`, `_receipt_image.html`, `templatetags/expenses_ui.py`
- [x] wire `base.html`; strip old loading code from `app.js` / `app.css`
- [x] receipt images -> `.img-frame` (claim_detail, dup_compare)
- [x] button labels (`data-loading-label`), filter selects -> `requestSubmit()`
- [x] verify: pytest 162 pass (5 new in `tests/test_ui.py`), route sweep, 36 real-Edge checks (x2 runs), visuals in light/dark/mobile

## Review
- Deviations from plan: cross-document view transitions **removed** (Chromium aborted them intermittently with an unhandled rejection after POST-to-same-URL redirects and when a skeleton was up; the existing `page-in` fade stays). No `_spinner.html` (a `.spinner` class is enough). Skeleton only for known GET routes, never after a POST. Preloader is `aria-hidden` but not `inert` (it would break login `autofocus`).
- Bugs found by testing, all fixed: preloader hard cap was measured from script start and the CDN Bootstrap script blocked parsing (5.6s stall, now `defer` + cap from navigation start, 1.7s worst case); a failed image whose `error` fired before JS ran never showed the fallback; filter selects used `form.submit()` which skips the submit event (no progress bar).
- Key constraint: never `disabled` the submitter (drops `name="action"` from the POST). `aria-disabled` + a pending flag instead.
- Not verified: real screen reader, Safari/Firefox, real-device 60fps, real Slow 3G throttling (used route delays).

---

# Landing page (Nexus-inspired) + loading animations

Native CSS + vanilla JS on the Django template.

- [x] real screenshots -> `static/expenses/landing/*.webp` (6, dark, demo data)
- [x] `_boot.html` shared by base + landing; `_shot.html`, `_cta.html`
- [x] landing view reads `data/eval_report.json` (no invented numbers)
- [x] `landing.css`, `landing.js` (nav, reveal, tabs, count-up, ASCII canvas, glyph)
- [x] `landing.html` rewrite (hero, bento, tabs, accuracy, rules, CTA, footer)
- [x] loading.js: link-buttons pending state
- [x] verify: pytest 175 (13 new), 45 real-Edge checks x3 runs, contrast audit both themes, CSS/JS audits

## Review
- Reference kept: dark tech feel, teal accent, Geist Pixel Line display type, mono `//` labels, ASCII wave texture, bento, auto-cycling tabs, count-up stats, CTA panel. Dropped: every invented number and compliance claim, fake live feed, stats inside the hero, emoji, glows, two signup CTAs.
- Numbers on the page come from `data/eval_report.json` at request time. It has `dedup: null` right now, so duplicate-detection metrics are not shown (re-run `manage.py evaluate` to get them, then add a section).
- Bugs found by testing: tab pause never worked (the `animation` shorthand out-ranked the paused rule and reset play-state); light-mode progress fill was 2.88:1 against its track (now 3.47).
- Loading system reused: preloader (once per session, shared key with the app), top bar, CTA link-button pending state, screenshot frames with shimmer + fade. No skeleton on navigation (landing has no `main.page`, and `/login/` has none).
- Not verified: real screen reader, Safari/Firefox, real-device fps for the ASCII canvas (measured ~20 frames/s on desktop Edge, paused offscreen).

---

# Notebook refresh (2026-09-19)

- [x] `manage.py evaluate` re-run: `data/eval_report.*` now includes duplicate detection (was `dedup: null`)
- [x] `notebooks/ml_analysis.ipynb` re-executed in place (backup: `ml_analysis.ipynb.bak`), 17/17 code cells, 0 errors
- [x] All 17 text outputs identical to the Sep 16 run (fixed seeds), so no numbers moved
- [x] Prose fixed to match measurements: "roughly 1.2%" -> ~1.1% (1.08% measured, ~1,400 spurious pairs, 508 claims); summary row "5/5 seeds" now says it was measured against the original hand-set weights and a fresh search wins 0/5
- Checked, unchanged: 8 hand features, phash floor 0.90 (1 - 6/64), tuned weights image 0.0265 / text 0.2497, ECE 0.055 vs 0.08 target, confidence falls with noise tier (0.893, 0.861, 0.667, 0.487)

---

# Streamlit end-to-end deployment (2026-09-20)

## Plan

- [x] Add a Streamlit runtime adapter that boots Django, applies migrations, and provisions the finance account from secrets.
- [x] Add authenticated employee flows: sign up, sign in, upload invoice/PDF, OCR/extract, submit, list, and inspect claims.
- [x] Add finance flows: dashboard, review queue, field correction, duplicate resolution, and guarded approve/reject/request-info actions.
- [x] Persist uploaded receipt bytes in the database so Streamlit Cloud restarts do not lose evidence.
- [x] Add Streamlit Cloud config, Tesseract system dependency, Python dependency, secrets template, and deployment documentation.
- [x] Add tests for Streamlit-specific storage/bootstrap helpers and critical workflows.
- [x] Run migrations, tests, Django checks, Streamlit headless smoke test, and inspect the final diff.
- [x] Commit the deployment changes locally.
- [ ] Push, configure Streamlit Community Cloud secrets, deploy, and smoke-test the public URL.

## Verification

- [x] `pytest -q` — 391 passed, 7 skipped
- [x] `python manage.py check`
- [x] `python manage.py makemigrations --check`
- [x] Streamlit server starts and health endpoint responds
- [x] Upload -> extraction -> duplicate screen -> dismiss -> approve verified with `AppTest`
- [x] Git diff contains no credentials or unrelated changes

## Review

### Changed

- Streamlit UI and runtime over the existing Django service layer.
- Durable private receipt evidence in PostgreSQL rows; blob columns deferred from list/dedup queries.
- Cloud config, Tesseract package, production secrets template, migration, and deployment docs.
- Production hardening: fail-closed startup, collision-safe employee/admin provisioning, escaped dynamic HTML, bounded uploads, ordered risk queue, connection cleanup, and operational field corrections.

### Verified

- 391 tests pass; 7 real-OCR cases skipped in the test environment.
- Ruff checks, Django checks, migration drift check, HTTP health check, and Streamlit end-to-end workflow pass.

### Risks

- PostgreSQL blob storage is appropriate for this first deployment, not high-volume archival storage.
- Streamlit session auth has no password reset or cross-session rate limiter; deploy private/invite-only.
- Streamlit Community Cloud data residency and resource limits must suit the invoice data.

### Follow-ups

- Move evidence to private object storage if volume grows.
- Add an identity provider and rate limiting before broad public access.

## Unresolved questions

- PostgreSQL `DATABASE_URL` for durable hosted data.
- Streamlit Community Cloud deployment access/session and desired app subdomain.
- Stale elsewhere, not touched: `tasks/lessons.md` still says d<=4 covers 1.2% of random pairs

---

# Object storage + employee access (2026-09-21)

## Plan

- [x] Add an S3-compatible receipt storage adapter with private-object uploads and downloads.
- [x] Add receipt storage metadata/migration and use it for Streamlit uploads/previews/OCR.
- [x] Preserve local/database fallback for development and legacy receipts.
- [x] Enable employee self-signup in the Streamlit deployment configuration and verify login/signup.
- [x] Add focused storage/auth tests and run the full verification suite.
- [x] Commit the deployment update locally.
- [ ] Push the deployment update and configure the hosted storage secrets.

## Verification

- [x] Storage adapter unit tests with a fake client.
- [x] Django checks and migration drift check.
- [x] Full pytest suite and Streamlit workflow smoke test.

## Review

### Changed

- S3-compatible private receipt storage, durable object keys, cleanup, and OCR/preview downloads.
- Streamlit employee self-registration remains employee-only and is enabled by `BRIER_ALLOW_SIGNUP=1`.

### Verified

- 392 tests pass; 7 real-OCR cases skipped.
- AppTest startup, Ruff, Django checks, migration drift, and storage round-trip pass.

### Risks

### Follow-ups

---

# Rename to Brier (2026-09-19)

- [x] Brand text "Expense Claims" -> "Brier": sidebar, breadcrumb root, page titles (`X | Brier`), login, preloader, landing (title, nav, footer, aria-labels, copyright), Django admin headers, README title + one-line origin (Brier score)
- Kept: generic phrase "expense claims" in the landing meta description; brand-mark icon (receipt)
- Not changed: repo folder name `Project 5`, Python package `expenses`, URLs, model names

---

# Sign up + themed previews (2026-09-19)

Decisions (asked): employee-only self-serve sign-up, active immediately; 4 preview tabs (Log in, Submit, Extract, Review), every preview has dark + light; Log in + Sign up on every CTA, "Open dashboard" when signed in.

- [x] `SignUpForm` + `signup` view + `/signup/` + template; login page links to it; wording Log in / Log out everywhere
- [x] ownership rules (see below), `roles` context processor
- [x] landing CTA group (Sign up primary, Log in secondary); hero drops "See how it works" (nav has it)
- [x] themed `_shot.html` (dark + light `<img>`), per-image load state, hero preload for the active theme
- [x] 14 previews recaptured (7 pages x 2 themes) after the rename and text changes; 6 old single-theme files deleted
- [x] tests: `tests/test_signup.py` (21), landing tests updated; 200 pass. Real Edge: 62/62 (toggle swaps every frame, light theme never downloads dark files, no CLS, nav fits at 900/1024, mobile menu has both buttons)

## Review
- **Found while planning sign-up:** any logged-in user could read every claim (`claim_list?scope=all`, no owner check on `claim_detail`, org-wide dashboard). With open sign-up that is a data leak, so fixed here: non-finance users see only claims linked to their login; other people's claims 404; `?scope=all` is finance-only; dashboard is finance-only (employees are redirected to My claims); a login with no Employee record sees nothing. Behaviour change: seeded `e001` no longer sees everyone's claims.
- Sign-up cannot grant reviewer rights (group membership stays admin-only). Employee codes continue the seed style (E031...).
- `claim_submit` fallback now links its auto-created Employee to the login, otherwise "My claims" would not find those claims.
- Not done, worth knowing: no rate limit or CAPTCHA on sign-up, no email (so no password reset), "username is taken" reveals which usernames exist, `/media/` receipt files are served without auth in DEBUG.

---

# Landing light mode: darker palette (2026-09-19)

Decisions (asked): solid pixel font for headings in light mode only, "clearly darker, still soft", visible-but-calm textures.

- [x] light tokens re-chosen by measured contrast (`scratchpad/contrast_light.py`): ink 17:1, ink-2 14:1, muted 5.9 -> 8.7:1, accent text 5.1 -> 8.1:1, card edge 1.2 -> 1.6:1, ghost border 6:1; all new pairs pass
- [x] h1/h2 use Geist Pixel Square in light mode (Line stays in dark); headings still <= 2 lines at 1440 and 390
- [x] grid and ASCII use a dedicated `--grid-line` / darker green; ASCII masked clear of the nav and calm behind the copy column; ghost button has a backing so texture never shows through its label
- [x] dark theme unchanged (only a new `--grid-line` variable set to its old value); 62/62 real-browser checks, 200 pytest pass

---

# Landing: block-wave background (2026-09-19)

- Cause of the "ASCII" look: I had ported the reference's *sphere* ramp (`.:-=+*#%@`); its hero wave uses shade blocks (`█▓▒░`) with per-cell colour and opacity.
- [x] `landing.js`: wave now draws 4 shades as filled squares (size and opacity grow with intensity), same three overlapping waves as the reference, no glyphs so it is font-independent; ~1.5-2ms per frame
- [x] colour per theme from CSS (`--block-rgb`, `--block-a`): dark = bright teal, light = deep green (6,63,57) at max 26% opacity; softer on phones
- [x] masks: calm behind the copy column on desktop, calm at the top on phones, clear of the nav
- [x] worst-case text contrast over the blocks measured from screenshots across 8 animation frames: headline 10-12:1, accent line 4.8-7.8:1, lead 4.7-5.2:1, at 1440 and 390, both themes
- [x] tests: 3 new guards (203 pass), real-browser suite 62/62
- Not changed: the small receipt-matching glyph in the "Duplicates come with evidence" tile is still text art (it is foreground content, not the background)
- Flaky test fixed: ownership tests searched HTML for "CA"/"CB", which random CSRF tokens can contain; IDs are now AL-CLAIM-1 / BO-CLAIM-2

---

# Landing light mode: visible green and darker blocks (2026-09-20)

- Problem: green text `#094f47` was 2.1:1 from the near-black ink, so it read as black; the block wave (deep green at 26%) was too pale.
- [x] text green chosen by search, not by eye: `#006c51` (hsl 165, 100%, 21%) is the brightest green that keeps 5:1 on every light surface; 3.1:1 from the ink (was 2.1:1). Hue close to the dark theme's teal.
- [x] fill green (progress, blocks) `#057e60`, brighter than the text green (3.0:1 on the progress track)
- [x] block wave: colour (5,126,96), max opacity 26% -> 74% (phones 16% -> 42%); grid lines slightly softer (16%, 11% on phones) so their crossings do not dip behind the copy
- [x] legibility from pixels over 8 animation frames, hero and closing panel, 1440 and 390: text >= 4.5:1 against the darkest 1% of the background; single worst pixel >= 3.0:1. Blocks alone behind the green line never drop below 5.3:1
- Trade-off recorded: accent text is 5.0-6.3:1 (still AA), down from 8-9:1, because a green that is clearly green cannot also be near-black
- Not rerun: the full 62-check browser suite (dev server was stopped for memory and I did not restart it). Verified instead on a locally rendered copy; pytest 203 pass

---

# Landing light mode: darker block greens (2026-09-20)

- Cause: the block colour was a mid-green (`#057e60`) alpha-blended over the pale page, and most of the wave sits in the lighter shades, so it read minty/grey-teal.
- [x] block colour `rgb(0, 92, 68)` (deep green), max opacity 86% (phones 52%): densest block lightness 43% -> 28%, still clearly green (green channel 79 above red)
- [x] new `--block-lift` (light 0.40, dark 0) raises the lighter shades toward the dense one, so the mid shades are darker too
- [x] legibility re-measured from pixels (8 frames): hero and closing panel, 1440 and 390, text >= 4.5:1 against the darkest 1% of the background; green headline line is the tightest at 4.53:1
- [x] 204 tests pass (1 new guard). Dark theme unchanged.

## Cream light background (#FFF9F2)
- [x] `--page: #fff9f2` in app.css and landing.css (light only; dark untouched); loading.css inherits via `var(--page)`.
- [x] Neutrals re-tinted warm (surface #fffdf9, surface-2, lines, track, ink, muted, buttons, shadows) so the page is one palette, not cream + cool grey.
- [x] Landing light: ink 18.7:1, muted 9.4:1, accent text 6.2:1, ghost border 7.5:1 on the cream. App light: ink 16.8:1, muted 5.7:1, field border 3.9:1. `--warn` darkened (#a84a07) for 4.5:1 on surface-2.
- [x] 7 light preview webps recaptured (they showed the old grey UI).
- [x] Verified: body bg rgb(255,249,242) on 8 pages in Edge; block-wave legibility 8/8 at 1440 and 390; pytest 204 passed.
- Note: Django admin (/admin/) has its own theme, not changed.
- [x] Follow-up: surfaces (cards, sidebar, topbar, inputs) set to true `#ffffff` in light mode (app.css + landing.css); page stays `#fff9f2`; surface-2 (hover, table head) stays warm. Previews recaptured.

## INR-only receipts
- [x] `ALLOWED_CURRENCIES = ("INR",)`; `services.foreign_currency()` (real signal only: code, symbol, symbol+marker; bare locale marker and bare letter symbols never reject; `$` does). `extract_into_receipt` pins non-allowed to INR.
- [x] `claim_submit` rejects foreign receipts ("Brier handles INR receipts only. This one looks like USD."), deletes the receipt row + image, keeps the pasted text in the form. No claim created.
- [x] Landing cell C: "Made for Indian tax receipts" (GSTIN/CGST/SGST/IGST chips) replaces "Multi-currency"; `n_currencies` context removed.
- [x] Data: synthetic set was already INR-only; deleted the 14 web-upload foreign receipts + claims (U00048-U00061, DB backup in scratchpad), groups recomputed: 535->521 receipts, 559->545 claims, 442->369 flags. Eval report unchanged (synthetic only).
- [x] Root-cause fix found by the new tests: `core/currency.py` `_SYMBOL_RE` used `\s*`, so the "R" of a "Round Off" line after a line ending in a digit read as ZAR. Now same-line only (`[ \t]*`).
- [x] Verified: pytest 217 passed (13 new); Edge: USD paste rejected with banner, form text kept, DB unchanged; INR paste accepted; landing cell dark/light 1440+390 no h-scroll; previews recaptured.

## Pump slips / any oddly labelled receipt (reported: IndianOil slip total read as 1730171.27)
- [x] Root causes (OCR was fine): (1) no total pattern accepted `Amount(Rs)`, so the positional fallback took the meter reading `Vtrd: 0001730171.270`; (2) `ANY_AMOUNT` matched 2 of 3 decimals; (3) `INVOICE_LABELLED` missed `Inv. No`; (4) vendor took the dealer line, not the brand.
- [x] Found on the way, wider than this receipt: `NUM` read any amount printed **without thousands commas** as its first 3 digits (`Total: 2207.00` -> 220). Synthetic data always used commas, so the evaluation never saw it.
- [x] Fix (core/, no LLM): `TOTAL_AMOUNT_LABEL` (line-start, ranked under an explicit Total), `NON_MONEY_LINE`, 3-decimal guard, `Inv. No`, `brands.py` (brand beats dealer, fuzzy for OCR damage), rate x volume cross-check + derivation, lost-decimal repair, CGST=SGST plausibility, subtotal plausibility.
- [x] Separate pump-slip set (`gen_pump_slips`, seed 43, 60 slips, `data/pump_slips/`; main corpus/DB/line model untouched). Baseline -> now: total F1 0.067 -> 0.933, invoice 0.581 -> 0.904, vendor 0.650 -> 0.983, date 0.983 -> 0.983. Remaining misses are OCR-damaged digits, all scored 0.47-0.61 (routed to review).
- [x] Main corpus (480, simulated): no field has fewer correct values; total F1 0.9604 -> 0.9708, subtotal 0.8885 -> 0.8912, cgst 0.9280 -> 0.9293, sgst 0.9197 -> 0.9264, arithmetic reconciled 93.5% -> 92.7% (fewer unjustified "repairs"), ECE 0.0546 -> 0.0538. Auto-accept cutoff re-derived: 0.8254 -> 0.8218 (settings + models).
- [x] Tests: `tests/test_pump_slips.py` (73), suite 217 -> 290. Edge: real JPG uploaded through the form reads 590.00 / invoice / Indian Oil, duplicate check flags it EXACT against the first upload.
- [x] Data: re-read only U00048 (claim U00041: 1730171.27 -> 590.00, NEEDS_INFO -> SUBMITTED, audit RE_EXTRACT). Dry run on the other 41 uploads: 9 differ (U00003-7, U00011/12/46/47), NOT applied.
- Backup of the old eval report + pump baseline: `data/_backup_before_pump/`.

## Reasoning over the amounts + any file format (reported: U00043 total read as 283 = "Total GST")
- [x] Why: `TOTAL` matched "Total GST" (bare "total" + gap); OCR wrote `283 .00` and `Taxable Val : 2 1572.20` (rupee glyph -> "2"); each field was picked alone so 283 looked like a fine total. U00042 was a DIFFERENT image (older VAT layout), so it happened to work.
- [x] Direct fixes (patterns/fields): TOTAL excludes gst/tax/vat/cess/discount/volume..., "Sub Total" is not a total, `283 .00`, stray-glyph skip, `Taxable Val`, `Amount (%)`, rate line uses the LAST number, greeting-anchored brand for unknown fuel brands.
- [x] New `core/extraction/reasoning.py`: picks the best COMBINATION of total/subtotal/CGST/SGST/IGST/tax_total. Hard rules: total > tax (and != any tax), taxable <= total+Rs1, CGST == SGST. Fixes an impossible set by giving up the LEAST trusted reading (trust lost = cost), never returns one; corroboration (rate x volume) raises trust; re-checked after every repair. Acts only on impossible sets: forcing sums on damaged text made totals worse (measured), so arithmetic-only mismatches stay with reconcile_amounts.
- [x] Any format: PDF (text layer, else rendered pages; pypdfium2), JPG/JPEG/PNG/WEBP/GIF/BMP/TIFF; type by magic bytes, 15 MB cap, HEIC gets a clear message; non-web formats stored as PNG; PDF preview = stacked pages PNG. Form errors were never displayed (a rejected file showed nothing): fixed.
- [x] OCR: EXIF, alpha flattened to white, up to 2 readings (as-is / preprocessed), the fuller and more consistent one wins (`_score`, not doc_confidence, which is a min and scored a complete reading 0 for one missing date).
- [x] UI: labels "Taxable value", "Total tax", "Total amount" (payable) in reading order, money with rupee sign, vendor by name, plain-language notes, and an equation with the real numbers (`1,572.20 + 141.50 + 141.50 = 1,855.20`, or the gap).
- [x] Proof: invariant sweep (main sim all tiers + clean, pump, uploads) = ZERO hard-rule violations (before: tax>total 7, CGST!=SGST 28, total==tax 1); main corpus no field with fewer correct, total F1 0.9708 -> 0.9822, cgst/sgst 0.929/0.926 -> 0.949/0.946; pump set extended (GST/VAT/plain layouts, unknown brands, OCR glyph artefacts), 100 slips: total 0.925, subtotal 0.913, cgst 0.959, vendor 0.940 (rest is damaged digits, scored low). Real-OCR uploads with ground truth (35 dataset pages): 207 -> 234 correct fields, 0 regressions. Cutoff re-derived 0.8216.
- [x] Data: all 42 unreviewed uploads re-read (U00002 skipped: decided), audit RE_EXTRACT each, claimed_amount refreshed, flags rebuilt (rescore --keep-resolved). U00043 = 1,855.20 / 1,572.20 / 141.50 / 141.50 / 283.00. U00042 and U00043 are flagged EXACT duplicates of each other (correct).
- [x] Tests 290 -> 376 (reasoning, invariants, formats incl. real OCR per format, detail page). Backups: `data/_backup_before_reasoning/`, DB copies in the scratchpad.
- Not verified in a browser: the dev server was stopped for low memory and not restarted. The page is covered by rendered-HTML tests.

## Brand visibility: the name "Brier" and its mark on the landing page
Problem: an average visitor does not learn the project is called "Brier".
Diagnosis: size is not the bug. The nav lockup is already the largest thing in
the bar (`landing.css:101` even says "Sized to be seen first"). The name has
nowhere to stick and no meaning attached: the favicon is blank (`data:,`), there
is no share card, the mark is a stock Phosphor `ph-receipt` glyph that reads
"receipt app" and carries no recall, and the word never appears in the hero, so
the reading path is headline, lead, button, gone.
Direction: one mark app-wide; a calibration-staircase silhouette; preloader
restyled but its gating untouched; a brand lockup above the H1; a real rendered
1200x630 share card.

### A. The mark, one source of truth
- [ ] `_brand_mark.html`: inline SVG, 16x16 viewBox, 10 blocks in columns of
      1/2/3/4 (the reliability diagram reduced to a silhouette), `currentColor`,
      `aria-hidden`. 16px grid so the favicon lands on whole pixels.
- [ ] Replace `<i class="ph ph-receipt">` in all six places: `landing.html:38`
      (nav), `landing.html:273` (footer), `_preloader.html:6`, `base.html:26`
      (app sidebar), `login.html:5`, `signup.html:5`.
- [ ] Size the SVG inside the existing tiles: `landing.css:103` (46px tile) and
      `app.css:118` (32px tile). Tiles, radii and colours unchanged.

### B. Tab and bookmark
- [ ] `brand/favicon.svg`: accent tile, staircase knocked out in `--btn-ink`.
      One file, legible on both light and dark browser chrome.
- [ ] `brand/apple-touch-icon.png` at 180px.
- [ ] Link both from `landing.html` (replaces `href="data:,"`) and from
      `base.html`, which currently declares no icon at all.

### C. Hero lockup
- [ ] Lockup above the H1: mark + "Brier" + "named for the Brier score", inline
      on one row. Becomes `hero-in --i:0`; H1, lead and CTA shift to 1/2/3.
- [ ] Sentence case, NOT an uppercase-tracking eyebrow. The page has 6 sections
      and 2 eyebrows already ("// Product", "// Review rules"), which is exactly
      the ceil(6/3) cap. A third would break it.
- [ ] Gate: if nav mark and hero mark read as duplicated at 1440x900, drop the
      mark from the hero lockup and keep wordmark + meaning line.

### D. Preloader
- [ ] Restyle only. Larger lockup, add the meaning line. The first-page-only and
      reduced-motion gating in `_boot.html` stays exactly as it is.

### E. Share card
- [ ] `manage.py make_og_card`: renders `brand/og-card.png` at 1200x630 with
      Playwright (already installed) from an HTML template, so it can be
      regenerated when the mark changes.
- [ ] `og:title`, `og:description`, `og:image`, `og:url`,
      `twitter:card=summary_large_image` in `landing.html`.

### F. Verification (nothing is done until these pass)
- [ ] Screenshots at 1440x900 and 390x844, both themes. Hero must still fit with
      the CTAs visible and no scroll.
- [ ] Render the favicon SVG at 16px and look at it. The staircase must still
      read as a staircase, not a blur.
- [ ] Measure contrast of the staircase on the accent tile in both themes
      (non-text, 3:1 floor). Light mode uses `--accent-fill: #057e60`, dark uses
      `#2dd4bf`, so the knockout colour has to be checked per theme.
- [ ] Open the app sidebar, login and signup and confirm the mark renders at
      32px without clipping. The lockup is shared; this is the blast radius.
- [ ] `manage.py check` and the full test suite (387 tests).
