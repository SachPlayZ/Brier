# Lessons

## Chat is not a credential store
- Mistake: Treating a pasted deployment token as a usable handoff path risks replaying a credential already preserved in chat history.
- Rule: Never use chat-exposed credentials; prefer existing Keychain auth or have the user enter replacements directly in the platform secret manager.

Patterns worth keeping, recorded as they came up.

## Measure before tuning a threshold
The perceptual-hash rule was set to Hamming ≤ 4 by analogy with photo
deduplication. Measuring the actual distance distribution on this corpus showed
d ≤ 4 covers 1.2% of *random* receipt pairs — because receipts are all
dark-on-light monospace blocks, so phash has little to key on. It was producing
~34 of 51 false positives. Fix: tighten to ≤ 2 **and** require a corroborating
field.
**Rule:** before setting a similarity threshold, plot the positive and negative
distributions. A threshold borrowed from another domain is a guess.

## Ground truth must be self-consistent
Two separate truth bugs each looked like a detector failure:
1. Honest claims wrapped around a shorter receipt list, so 20 receipts were
   claimed twice with no duplicate label. The detector was correctly flagging
   real duplicates and being scored wrong for it.
2. Duplicate-ness is transitive. The generator recorded only the edges it
   injected — (A,B) and (A,C) but never (B,C) — so a correct discovery of B~C
   counted as a false positive.
**Rule:** when precision looks bad, inspect the actual false positives before
touching the model. Ask whether the label is wrong before assuming the code is.

## Don't evaluate on inputs that are too clean to be informative
Extraction scored F1 = 1.000 on every field against the text sidecar, because
the sidecar *is* the exact rendered text. The number was meaningless and the
confidence scores had nothing to discriminate. Adding a deterministic OCR-noise
simulator keyed to the render's noise tier produced a real signal, and the
empirical auto-accept cutoff (0.845) then landed close to the value that had
been guessed (0.85) — which is only worth knowing because it was measured.
**Rule:** if a metric is perfect on the first run, suspect the test set.

## Missing evidence is not negative evidence
Duplicate signals return `None` when they cannot be computed, and the weights
are renormalized over the signals that exist. Imputing 0.0 for a missing invoice
number would have manufactured evidence that two claims *differ*.

## Put the safety rule in the service layer
"Cannot approve while a duplicate flag is open" lives in `services.approve`, not
in the view. A view-level check is one forgotten code path away from being
bypassed; the service-layer check is covered by a test that calls it directly
*and* one that goes through the HTTP endpoint.

## SQLite commits dominate bulk writes
Ingest took ~1.5s per receipt because each receipt writes ~20 rows and every one
was its own transaction (one fsync each). Batching 100 receipts per
`transaction.atomic()` removed nearly all of it.

## Optional dependencies need to be *found*, not just installed
Installing Tesseract was not enough: the Windows installer does not reliably add
it to PATH, and a running server process would not see a PATH change anyway.
`core/ocr.py` now checks `TESSERACT_CMD`, then PATH, then the known install
locations, and `manage.py check_ocr` reports exactly which step succeeded.
**Rule:** for an optional native dependency, locate the binary explicitly and
ship a diagnostic command. "Install it and it'll work" is not a design.

## A fallback that hides a feature is a bug report waiting to happen
The text-first OCR design was deliberate and correct, but the first thing the
user did was upload an image and hit a wall. Graceful degradation still needs
the primary path to actually work for the obvious use case.

## Real OCR found four bugs that synthetic noise did not
My `degrade.py` simulated character confusions, but not *structural* damage:
- `₹18,554.00` → `118,554.00`: the currency glyph read as a digit, inflating a
  claim sixfold. Now caught by arithmetic, or by the fact that a total can never
  exceed ~1.5x its subtotal under GST.
- Worse, reconciliation then "repaired" CGST to 102,822 on a 15,723 subtotal to
  make the bad total balance, and reported `arithmetic_ok=True`. A repair that
  implies an absurd tax rate is evidence another field is wrong, not that this
  one was missing. Repairs are now plausibility-checked.
- `Jan'19, 2025`: an apostrophe hid the date entirely.
- `SGST_@ 9%`: underscore is a word character, so `\bSGST\b` never matched.
**Rule:** a noise simulator only contains the corruptions you thought of. Run the
real thing before believing the numbers.

## Repair must be able to say "I don't know"
The GSTIN repair searched glyph confusions and accepted the first checksum-valid
result — and produced a *valid but wrong* GSTIN by flipping position 13, which is
a literal `Z`. A confidently wrong identifier is worse than a missing one: it
would silently link two unrelated vendors. Pinning the fixed position made the
search return None on that input instead.
**Rule:** for any repair or inference, check that the failure mode is "gives up",
not "guesses plausibly". Add a test asserting it returns nothing on hopeless input.

## Whitespace can hide a regex bug
`Total Before Tax: 12,135.82` was being captured as the grand total: the 18-char
label-to-value gap is wide enough for `total` to step straight over
`Before Tax:`. On the clean renders the column spacing was wider than the gap,
so the pattern never reached the value and the bug stayed invisible. OCR
collapses runs of spaces, and the bug appeared on *tier 0* — clean images.
**Rule:** a metric that is good on clean input and bad on noisy input is not
always a noise problem. Check whether the clean input was accidentally
protecting you, especially where a pattern has a bounded-width gap.

## Diagnose by field and tier, not in aggregate
Real-OCR extraction averaged respectably, but the per-tier table showed `total`
at 0.853 and `gstin` at 0.615 on *tier 0*. Clean renders should be near-perfect,
so those two numbers said "systematic bug", not "hard input" — and they were:
a label-boundary bug and a separator class missing `.` ("GST No.: ..."). The
aggregate would have been written off as OCR noise.

## "database is locked" is usually a transaction-scope bug, not a SQLite limit
Claim submission held a write transaction across ~5s of pure-Python dedup work
(dominated by a lazy `import sklearn` on first use), which is past SQLite's
default 5s lock timeout. Three fixes, in order of importance:
1. Move the scoring out of the transaction. It touches no rows; only the writes
   need to be atomic.
2. `transaction_mode="IMMEDIATE"`. This is the subtle one: a DEFERRED
   transaction takes a read lock at BEGIN and upgrades on first write, and that
   upgrade **cannot wait** (two upgrading readers would deadlock), so SQLite
   returns "database is locked" *instantly*, ignoring `busy_timeout`. The
   instant failure is the tell.
3. WAL + a real busy timeout so readers stop blocking writers.
**Rule:** if a lock error returns in 0.00s, no timeout setting will fix it —
the transaction mode is wrong.

## `max(id) + 1` is a race, and concurrency is how you find it
Six simultaneous uploads produced `UNIQUE constraint failed: receipt_id`. It was
invisible in every sequential test. Creation now retries with a bumped offset.
**Rule:** load-test the write path, not just the read path. One concurrent run
found two bugs that the whole sequential suite missed.

## Parse structure, not locale
Multi-currency amount parsing looked like it needed a locale per currency
(`1.234,56` vs `1,234.56`). It does not. The decimal separator is identifiable
from *structure*: the last `.` or `,` followed by one or two digits is the
decimal point; three trailing digits mean grouping. One rule covers US, European,
French and Indian conventions, and it works even when the currency is unknown --
which matters, because the currency is often what you are trying to detect.

## A symbol is only a symbol next to a number
Currency detection read "CAFE ZUR POST GmbH" as South African: ZAR's symbol is a
bare "R", which matches inside almost any word. Requiring the symbol to sit
against a digit fixed it. Single-character tokens need positional evidence
before they mean anything.

## A generic fallback stops a specific field from absorbing everything
Without a generic `tax_total`, a US "Sales Tax" line was never extracted -- and
reconciliation then attributed the unexplained gap to CGST, inventing an Indian
tax on a San Francisco coffee receipt. Arithmetically consistent, completely
wrong. When a model is specific to one jurisdiction, the generic case needs its
own field, not the nearest specific one.

## Broadening a pattern silently narrows another
Adding a bare `tax` alternative to the tax pattern made it capture
"Total Before Tax" -- a *subtotal* label -- which dropped corpus reconciliation
from 93.5% to 90.8%. Caught only because the INR benchmark was re-run after the
multi-currency change.
**Rule:** after broadening any pattern, re-run the existing benchmark. The new
cases passing says nothing about the old ones.

## A notebook that has not been executed is a draft
Writing `ml_analysis.ipynb` and running it found two defects the prose would have
shipped with:
1. A hardcoded narrative ("the measurement says 0.8433") that the live cell then
   contradicted with 0.8254 -- the extractor had changed underneath the constant,
   and `settings.CONFIDENCE_AUTO_ACCEPT` was stale with it.
2. "Tuned beat the defaults 5/5" now reads 0/5, because the shipped defaults ARE
   the tuned means. Same number, opposite meaning; the narrative had to change,
   not the code.
**Rule:** execute notebooks in CI-style before claiming they work, and never
hardcode a number the notebook itself computes.

## Derived constants drift silently
`CONFIDENCE_AUTO_ACCEPT` is a measurement copied into settings. Every pattern
change since moved it, and nothing complained -- the threshold just quietly
stopped matching the data it was derived from.
**Rule:** for any constant derived from a measurement, record how to re-derive it
next to the value, and re-derive after changing its inputs.

## Headless browsers clamp narrow windows
`msedge --headless --window-size=390,...` lays out at ~500px, so "mobile" shots
showed clipped content that was not a real bug. Wrap the page in an
`<iframe style="width:390px">` to get a true 390px viewport.
**Rule:** before trusting a mobile screenshot, confirm the layout viewport width.

## CSS grid children need `min-width: 0`
A `display:grid` stack of panels let a wide table push the whole column past the
viewport on mobile. `grid-template-columns: minmax(0, 1fr)` lets the inner
`overflow-x:auto` wrapper do its job.

## Check sed edits that add attributes
A sed replace added a second `class=` to an `<input>` that already had one; the
browser silently ignores the second. Grep for the result after scripted edits.

## Never `disabled` a submit button that carries name/value
A disabled submitter is left out of the form data, so `name="action" value="approve"`
would silently vanish. Use `aria-disabled` + `pointer-events:none` + a pending flag
that `preventDefault()`s further submits. Verified by asserting `action=reject` in
the intercepted POST body.

## A time cap must be measured from where the user's wait began
`setTimeout(leave, 1500)` inside a script that ran 2s in (behind a CDN script that
blocks parsing) is a 3.5s wait. Use `cap - performance.now()`, and never put a
third-party script ahead of your own in the parser path (`defer` it).

## `form.submit()` fires no submit event
Anything hooked on `submit` (progress bar, pending state) is skipped. Use
`requestSubmit()` in inline handlers.

## Test in-flight state from inside the page
Playwright queues `page.evaluate`/locator calls while a navigation is pending, so
polling from outside reads the state after it is gone. Log from an in-page
`setInterval` probe to the console instead. Also: route handlers with two
parameters are called as `(route, request)`, so `async def h(route, secs=1)`
silently gets a Request as `secs`.

## Do not carry a workaround for a flaky browser feature you can drop
Cross-document view transitions gave a nice fade but aborted intermittently and
raised unhandled rejections. Two mitigations still leaked; removing the feature
was cheaper and the page already had its own fade.

## `animation:` shorthand resets play-state
`.x { animation: fill 4s }` at higher specificity silently overrides
`.y { animation-play-state: paused }`, so "pause on hover" did nothing while the
timer kept running. Put the paused rule at equal or higher specificity than the rule
that starts the animation, and test that a paused timer actually holds.

## Full-page screenshots do not fire IntersectionObserver
Lazy images, reveals and count-ups looked broken (empty frames, "0.000") in a
full-page capture. Scroll the page in steps before asserting on or shooting it.

## Non-text contrast counts too
The 2px tab progress fill passed as "obviously teal" but was 2.88:1 on its track in
light mode. Compute ratios for fills, borders and progress, not only for text.

## Open sign-up is an authorization change, not just a form
Adding /signup/ turned "logged in" from "a trusted colleague" into "anyone". Existing
views only checked login, so every claim, receipt and money total was readable by any
new account. Before opening registration, audit each view for object-level access
(who owns this row?) and default to 404 for rows the user does not own.

## Two themed images: hide with CSS, load lazily, track state per <img>
A `display:none` lazy image is never fetched, so the inactive theme costs nothing until
the toggle. Frame-level "loaded" state breaks when the visible image changes; keep it on
each <img> and let the frame mirror the visible one (MutationObserver on the theme attribute).

## `{% url %}` has no inline `if`
`{% url 'a' if x else 'b' %}` is parsed as arguments and raises NoReverseMatch. Use
`{% if x %}{% url 'a' %}{% else %}{% url 'b' %}{% endif %}`.

## Hairline fonts need a heavier variant, not just darker ink
Geist Pixel Line reads pale on a light page whatever colour it is. Contrast ratios only
measure colour, not stroke weight, so pair a light-mode palette change with a heavier
font weight or variant and judge the screenshot, not just the number.

## Decorative textures must be masked away from text
Darkening a texture for visibility made it collide with the nav and body copy. Fade it
with a mask over text regions rather than lowering its opacity everywhere.

## Port the reference's actual mechanism, not a lookalike
I copied the reference's *sphere* character ramp for a background that in the reference is a
block-shade wave, so it read as "ASCII text" instead of blocks. When matching a reference,
read the exact component that draws the element you are copying (chars, sizes, colour, alpha).

## Assert on substrings that random tokens cannot contain
`"CB" not in html` fails about 1 run in 60 because CSRF tokens are random alphanumerics. Use
IDs long and odd enough that they cannot occur by chance.

## Measure legibility over an animated background from pixels
Hide the text, screenshot the region over several frames, take the darkest and lightest
pixels, and compute contrast against each text colour. A palette that passes on paper can
still fail under the densest frame.

## "Dark" and "distinguishable" pull against each other for a brand colour
A green pushed dark for contrast collapses toward the ink (2.1:1 apart) and reads as black.
Pick the brightest colour that still meets the contrast floor, and check its separation from
the neighbouring ink as well as from the background.

## Sample the pixels under the text, and know what you are sampling
The darkest pixel behind the headline was a grid-line crossing, not a block; hiding the
canvas proved it. Measure per layer, and report a low percentile next to the single worst pixel.

## Measure the text's own extent, not its container
A block-level heading spans its whole grid column; sampling that box included empty space where
the background is meant to be strong. Use a Range's client rects.

## A shaded pattern is judged by its common shades, not its densest one
Darkening the peak colour barely changed how the wave looked, because most cells sit in the
lighter shades. Adjust the whole opacity curve (a per-theme lift), not just the maximum.

## Check that a scripted edit actually landed
A `sed` with special characters silently matched nothing while the paired test edit succeeded,
so code and guard disagreed. Grep the result after every scripted change.

## A harmless heuristic becomes a bug the day it gets a consequence
- `_SYMBOL_RE` misread "Round Off" as ZAR "R" for a long time because currency only picked a date order. Once a currency mismatch rejected uploads, it would have turned away a plain Indian receipt.
- Rule: when a detector's output starts to gate something (reject, block, route), first run it on the clean, ordinary inputs (the app's own happy-path fixture) and look at what it reports, not just on the inputs it was built for. Add the false-positive as a regression test.
- Also: check the premise before deleting data. I assumed the synthetic generator made foreign receipts; a one-line grep showed they were 14 web uploads.

## Clean synthetic data hid a truncation bug in the shared number regex
- `NUM` took its first alternation branch, so "1174.00" (no thousands comma) became 117. The generator always wrote "1,174.00", so 0.96 total F1 looked fine while real receipts were wrong.
- Rule: when a regex is shared by many patterns, test it directly on the shapes real receipts print (ungrouped, zero-padded, 3-decimal, lakh-grouped) before trusting an evaluation built from one generator. Regex alternation returns the FIRST match, not the longest: put the more specific branch first or make the loose one require a group.
- Rule: a receipt layout the generator does not produce (pump slips) is invisible to the evaluation. Add the layout as its own small seeded set instead of editing the shared generator (one RNG stream: any change reshuffles every receipt).
- Rule: numbers that are not money (meter readings, ids, phone, quantities) must be excluded by the label in front of them, not by hoping the biggest number is the total.

## One report is a sample of a class: sweep the class before saying "fixed"
- The same class of error kept coming back, one receipt at a time. Cause: each fix was aimed at the single receipt reported, never at the invariant it broke. "Total GST" as the total is one instance of "an impossible set of amounts".
- Rule: after fixing a report, write down the rule the bad output violated (a bill cannot be smaller than its tax), then sweep EVERY text available (main corpus at all noise tiers, generated variants, all stored uploads) for violations of that rule, and add the sweep as a test with zero tolerance. Do it in the same change, before closing the report.
- Rule: first check whether two "same" inputs really are the same (U00042 was an older image, which is why it looked right).
- Rule: a fix that changes selection logic (which OCR reading, which candidate) must be scored against ground truth on real data (uploads that are dataset pages have truth) before applying it to the database. My first "best reading" metric (doc_confidence = a MIN) chose worse text; the truth comparison caught it.
- Rule: never let an arithmetic bonus reward a degenerate sum (subtotal == total, no tax): it "reconciles" trivially.
- Rule: check that a validation error is actually SHOWN. The upload form had rendered non_field_errors only, so every rejected file failed silently.

## An absolute path in a database row is a path to exactly one computer
`Receipt.image_path` held `F:\Project 5\data\receipts_images\R00001.png`. Renaming
the folder to `F:\Brier` broke all 480 rows and nothing complained, because no view
or template ever reads that column. It would also have been meaningless on any
other machine, which matters now the database ships with the repo.
Two further traps in the same bug: `receipts_truth.csv` wrote the same paths with
backslashes, which are ordinary filename characters on macOS and Linux; and patching
the rows alone would have been undone by the next `ingest`, because the writer was
the actual defect.
**Rule:** any path that leaves the process (into a database, a CSV, a config file)
is relative and forward-slashed. Fix the writer, not just the rows, and sweep the
other writers for the same shape.

## A command can finish its work and still crash on the way out
`manage.py evaluate` wrote all three report files, then died with
`UnicodeEncodeError` printing the same report to the console, because a Windows
console is cp1252 and the markdown contains the "less than or equal" character.
The artifacts were perfect; the command still exited non-zero with a traceback, and
its "Wrote ..." confirmations never appeared. Anyone running it would call it broken.
**Rule:** set the output encoding once in `manage.py` rather than policing which
characters each command is allowed to print. And when a command crashes, check
whether it crashed before or after its real work: those are different bugs.

## One setting cannot express a rule that depends on who is asking
`LOGIN_REDIRECT_URL` is a single value, so every successful login landed on the
employee claim list, reviewers included. `home_for()` already encoded the correct
rule and was used when an authenticated user visited `/login/`, but not for the
login POST itself. The rule existed and was applied on one path out of two.
**Rule:** when a helper encodes a routing or permission rule, grep for every branch
that routes and confirm each one calls it. A rule honoured in one place and skipped
in another is harder to spot than no rule at all, because the helper's existence
reads as proof it is being used.
