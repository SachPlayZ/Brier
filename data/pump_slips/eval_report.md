# Evaluation report

| Setting | Value |
|---|---|
| mode | simulated |
| receipts | 100 |
| ocr_available | True |
| elapsed_s | 2.1 |

## Field extraction

Receipts evaluated: **100** · arithmetic reconciled: **90.0%**

| Field | n | Extracted | Precision | Recall | F1 | Lenient recall |
|---|---:|---:|---:|---:|---:|---:|
| vendor | 100 | 100.0% | 0.940 | 0.940 | 0.940 | 0.980 |
| gstin | 56 | 89.3% | 0.980 | 0.875 | 0.924 | 0.875 |
| invoice_no | 100 | 90.0% | 0.967 | 0.870 | 0.916 | 0.870 |
| date | 100 | 96.0% | 1.000 | 0.960 | 0.980 | 0.960 |
| subtotal | 54 | 90.7% | 0.959 | 0.870 | 0.913 | 0.870 |
| cgst | 38 | 92.1% | 1.000 | 0.921 | 0.959 | 0.921 |
| sgst | 38 | 92.1% | 0.943 | 0.868 | 0.904 | 0.868 |
| igst | 0 | 0.0% | 0.000 | 0.000 | 0.000 | 0.000 |
| total | 100 | 99.0% | 0.929 | 0.920 | 0.925 | 0.920 |

### By noise tier

Tier 0 is a clean render; tier 3 is a crumpled, blurred, speckled phone photo.

| Tier | vendor | date | total | gstin | invoice_no |
|---|---|---|---|---|---|
| 0 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| 1 | 0.941 | 0.970 | 1.000 | 1.000 | 1.000 |
| 2 | 0.909 | 0.927 | 0.864 | 0.909 | 0.872 |
| 3 | 0.769 | 1.000 | 0.640 | 0.625 | 0.476 |

### Confidence calibration

ECE **0.1446** (target ≤ 0.08) · Brier **0.0557** · n = 586

| Confidence bin | n | Mean conf | Accuracy | Gap |
|---|---:|---:|---:|---:|
| 0.0-0.1 | 32 | 0.000 | 0.000 | +0.000 |
| 0.3-0.4 | 4 | 0.350 | 0.500 | +0.150 |
| 0.4-0.5 | 14 | 0.466 | 0.643 | +0.177 |
| 0.5-0.6 | 12 | 0.557 | 0.750 | +0.193 |
| 0.6-0.7 | 63 | 0.662 | 0.952 | +0.290 |
| 0.7-0.8 | 102 | 0.743 | 0.961 | +0.218 |
| 0.8-0.9 | 254 | 0.857 | 0.984 | +0.128 |
| 0.9-1.0 | 105 | 0.939 | 1.000 | +0.061 |

### Risk / coverage

| Coverage | n | Accuracy | Min confidence |
|---|---:|---:|---:|
| 50% | 293 | 0.997 | 0.831 |
| 80% | 468 | 0.983 | 0.686 |
| 90% | 527 | 0.977 | 0.595 |
| 100% | 586 | 0.910 | 0.000 |

**Empirical auto-accept cutoff:** confidence ≥ **0.8019** gives 99.2% accuracy over 61% of fields. Set `CONFIDENCE_AUTO_ACCEPT` to this rather than guessing.
