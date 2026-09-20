# Evaluation report

| Setting | Value |
|---|---|
| mode | simulated |
| receipts | 480 |
| ocr_available | True |
| elapsed_s | 13.1 |

## Field extraction

Receipts evaluated: **480** · arithmetic reconciled: **92.5%**

| Field | n | Extracted | Precision | Recall | F1 | Lenient recall |
|---|---:|---:|---:|---:|---:|---:|
| vendor | 480 | 100.0% | 0.990 | 0.990 | 0.990 | 0.990 |
| gstin | 480 | 93.1% | 0.998 | 0.929 | 0.962 | 0.929 |
| invoice_no | 480 | 96.9% | 0.923 | 0.894 | 0.908 | 0.894 |
| date | 480 | 94.6% | 1.000 | 0.946 | 0.972 | 0.946 |
| subtotal | 480 | 96.5% | 0.914 | 0.881 | 0.897 | 0.881 |
| cgst | 352 | 94.0% | 0.979 | 0.920 | 0.949 | 0.920 |
| sgst | 352 | 94.0% | 0.976 | 0.918 | 0.946 | 0.918 |
| total | 480 | 99.8% | 0.983 | 0.981 | 0.982 | 0.981 |
| igst | 71 | 94.4% | 0.955 | 0.901 | 0.927 | 0.901 |

### By noise tier

Tier 0 is a clean render; tier 3 is a crumpled, blurred, speckled phone photo.

| Tier | vendor | date | total | gstin | invoice_no |
|---|---|---|---|---|---|
| 0 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| 1 | 1.000 | 0.992 | 1.000 | 0.962 | 0.922 |
| 2 | 0.980 | 0.935 | 0.990 | 0.907 | 0.817 |
| 3 | 0.929 | 0.833 | 0.819 | 0.880 | 0.571 |

### Confidence calibration

ECE **0.0581** (target ≤ 0.08) · Brier **0.0269** · n = 3655

| Confidence bin | n | Mean conf | Accuracy | Gap |
|---|---:|---:|---:|---:|
| 0.0-0.1 | 138 | 0.000 | 0.000 | +0.000 |
| 0.2-0.3 | 3 | 0.260 | 0.000 | -0.260 |
| 0.3-0.4 | 1 | 0.309 | 0.000 | -0.309 |
| 0.4-0.5 | 55 | 0.466 | 0.582 | +0.116 |
| 0.5-0.6 | 45 | 0.555 | 0.689 | +0.134 |
| 0.6-0.7 | 42 | 0.652 | 0.809 | +0.157 |
| 0.7-0.8 | 126 | 0.768 | 0.849 | +0.081 |
| 0.8-0.9 | 667 | 0.863 | 0.948 | +0.085 |
| 0.9-1.0 | 2578 | 0.949 | 0.998 | +0.049 |

### Risk / coverage

| Coverage | n | Accuracy | Min confidence |
|---|---:|---:|---:|
| 50% | 1827 | 1.000 | 0.932 |
| 80% | 2924 | 0.998 | 0.869 |
| 90% | 3289 | 0.987 | 0.793 |
| 100% | 3655 | 0.933 | 0.000 |

**Empirical auto-accept cutoff:** confidence ≥ **0.8216** gives 99.0% accuracy over 86% of fields. Set `CONFIDENCE_AUTO_ACCEPT` to this rather than guessing.

## Duplicate detection

### Blocking

Measured independently of scoring: a duplicate never proposed as a candidate can never be caught, however good the scorer is.

- Candidate pairs: **5,881** of 128,778 possible (23.15 per claim)
- Reduction ratio: **0.954332** (target ≥ 0.99)
- Blocking recall: **0.9895** (target ≥ 0.98), 1 pair(s) missed

### Precision / recall by band

| Band | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| EXACT+ | 61 | 0 | 34 | 1.000 | 0.642 | 0.782 |
| HIGH+ | 94 | 1 | 1 | 0.990 | 0.990 | 0.990 |
| MEDIUM+ | 94 | 23 | 1 | 0.803 | 0.990 | 0.887 |
| LOW+ | 94 | 219 | 1 | 0.300 | 0.990 | 0.461 |

Average precision: **0.9895**

### Recall by duplicate type (MEDIUM and above)

| Type | Found | Total | Recall |
|---|---:|---:|---:|
| PHOTO_REUSE | 30 | 30 | 1.000 |
| RESUBMIT_AFTER_REJECT | 10 | 10 | 1.000 |
| RETYPED | 19 | 20 | 0.950 |
| SPLIT | 18 | 18 | 1.000 |
| TRANSITIVE | 17 | 17 | 1.000 |

### Reviewer workload

- Pairs surfaced per 1000 claims: **230.31**
- Precision in the top 50 by score: **1.0**
- Pairs at MEDIUM or above: **117**
