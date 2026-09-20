"""Render evaluation results as console text, Markdown and JSON."""
from __future__ import annotations

import json
from pathlib import Path

from core.eval.eval_dedup import DedupReport
from core.eval.eval_extraction import ExtractionReport

#: Targets the build is held to. Printed alongside the measured value so a
#: regression is visible without anyone remembering what "good" was.
TARGETS = {
    "extraction_f1_tier0": 0.95,
    "extraction_f1_tier2": 0.85,
    "ece": 0.08,
    "blocking_recall": 0.98,
    "reduction_ratio": 0.99,
    "dedup_precision_high": 0.95,
}


def render_markdown(extraction: ExtractionReport | None,
                    dedup: DedupReport | None,
                    *, meta: dict | None = None) -> str:
    out: list[str] = ["# Evaluation report", ""]
    if meta:
        out += ["| Setting | Value |", "|---|---|"]
        out += [f"| {k} | {v} |" for k, v in meta.items()]
        out.append("")

    if extraction is not None:
        out += _extraction_md(extraction)
    if dedup is not None:
        out += _dedup_md(dedup)
    return "\n".join(out)


def _extraction_md(rep: ExtractionReport) -> list[str]:
    out = ["## Field extraction", "",
           f"Receipts evaluated: **{rep.n_receipts}** · "
           f"arithmetic reconciled: **{rep.arithmetic_ok_rate:.1%}**", "",
           "| Field | n | Extracted | Precision | Recall | F1 | Lenient recall |",
           "|---|---:|---:|---:|---:|---:|---:|"]
    for r in rep.fields.values():
        d = r.as_dict()
        out.append(f"| {d['field']} | {d['n']} | {d['extraction_rate']:.1%} | "
                   f"{d['precision']:.3f} | {d['recall']:.3f} | {d['f1']:.3f} | "
                   f"{d['lenient_recall']:.3f} |")

    out += ["", "### By noise tier", "",
            "Tier 0 is a clean render; tier 3 is a crumpled, blurred, speckled phone photo.",
            "", "| Tier | " + " | ".join(_tier_fields(rep)) + " |",
            "|---" * (len(_tier_fields(rep)) + 1) + "|"]
    for tier, reports in sorted(rep.by_tier.items()):
        cells = [f"{reports[f].f1:.3f}" if f in reports else "-" for f in _tier_fields(rep)]
        out.append(f"| {tier} | " + " | ".join(cells) + " |")

    cal = rep.calibration
    out += ["", "### Confidence calibration", "",
            f"ECE **{cal.get('ece')}** (target ≤ {TARGETS['ece']}) · "
            f"Brier **{cal.get('brier')}** · n = {cal.get('n')}", "",
            "| Confidence bin | n | Mean conf | Accuracy | Gap |", "|---|---:|---:|---:|---:|"]
    for row in cal.get("reliability", []):
        if not row["n"]:
            continue
        out.append(f"| {row['bin']} | {row['n']} | {row['mean_conf']:.3f} | "
                   f"{row['accuracy']:.3f} | {row['gap']:+.3f} |")

    out += ["", "### Risk / coverage", "",
            "| Coverage | n | Accuracy | Min confidence |", "|---|---:|---:|---:|"]
    for row in cal.get("risk_coverage", []):
        out.append(f"| {row['coverage']:.0%} | {row['n']} | {row['accuracy']:.3f} | "
                   f"{row['min_confidence']:.3f} |")

    auto = cal.get("auto_accept_at_99")
    if auto:
        out += ["", f"**Empirical auto-accept cutoff:** confidence ≥ **{auto['threshold']}** "
                    f"gives {auto['accuracy']:.1%} accuracy over {auto['coverage']:.0%} "
                    f"of fields. Set `CONFIDENCE_AUTO_ACCEPT` to this rather than guessing."]
    return out + [""]


def _tier_fields(rep: ExtractionReport) -> list[str]:
    return [f for f in ("vendor", "date", "total", "gstin", "invoice_no")
            if f in rep.fields]


def _dedup_md(rep: DedupReport) -> list[str]:
    b = rep.blocking
    out = ["## Duplicate detection", "", "### Blocking", "",
           "Measured independently of scoring: a duplicate never proposed as a "
           "candidate can never be caught, however good the scorer is.", "",
           f"- Candidate pairs: **{b.get('n_candidates'):,}** of "
           f"{int(b.get('all_pairs', 0)):,} possible "
           f"({b.get('candidates_per_claim')} per claim)",
           f"- Reduction ratio: **{b.get('reduction_ratio')}** "
           f"(target ≥ {TARGETS['reduction_ratio']})",
           f"- Blocking recall: **{b.get('blocking_recall')}** "
           f"(target ≥ {TARGETS['blocking_recall']}), {b.get('missed_pairs')} pair(s) missed",
           "", "### Precision / recall by band", "",
           "| Band | TP | FP | FN | Precision | Recall | F1 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for row in rep.by_band:
        out.append(f"| {row['band']} | {row['tp']} | {row['fp']} | {row['fn']} | "
                   f"{row['precision']:.3f} | {row['recall']:.3f} | {row['f1']:.3f} |")

    out += ["", f"Average precision: **{rep.average_precision}**", "",
            "### Recall by duplicate type (MEDIUM and above)", "",
            "| Type | Found | Total | Recall |", "|---|---:|---:|---:|"]
    for row in rep.by_dup_type:
        out.append(f"| {row['dup_type']} | {row['found']} | {row['total']} | "
                   f"{row['recall']:.3f} |")

    w = rep.workload
    out += ["", "### Reviewer workload", "",
            f"- Pairs surfaced per 1000 claims: **{w.get('pairs_per_1000_claims')}**",
            f"- Precision in the top 50 by score: **{w.get('precision_at_top_50')}**",
            f"- Pairs at MEDIUM or above: **{w.get('reviewer_pairs_medium_plus')}**"]

    if rep.ablation:
        out += ["", "### Signal ablation", "",
                "Each signal removed in turn; delta is the F1 it was worth at the HIGH cutoff.",
                "", "| Signal removed | F1 | Delta |", "|---|---:|---:|"]
        for row in rep.ablation:
            out.append(f"| {row['signal']} | {row['f1']:.3f} | {row['delta']:+.3f} |")
    return out + [""]


def write_reports(path_stem: Path, extraction: ExtractionReport | None,
                  dedup: DedupReport | None, *, meta: dict | None = None) -> tuple[Path, Path]:
    path_stem = Path(path_stem)
    path_stem.parent.mkdir(parents=True, exist_ok=True)

    md_path = path_stem.with_suffix(".md")
    md_path.write_text(render_markdown(extraction, dedup, meta=meta), encoding="utf-8")

    json_path = path_stem.with_suffix(".json")
    payload = {"meta": meta or {},
               "extraction": extraction.as_dict() if extraction else None,
               "dedup": dedup.as_dict() if dedup else None,
               "targets": TARGETS}
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return md_path, json_path
