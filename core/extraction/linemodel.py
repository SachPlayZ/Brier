"""Optional line-role classifier.

Labels a receipt line as VENDOR / TOTAL / TAX / ... The training labels are
free: the synthetic generator knows the role of every line it renders and
writes ``receipts_lines.csv``.

Everything here is optional at inference. ``load()`` returns ``None`` when no
model file exists and the pipeline falls back to the neutral ``ml`` component,
so the system never hard-depends on a trained artifact.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

ROLES = ["VENDOR", "ADDRESS", "GSTIN", "DATE", "INVOICE", "ITEM",
         "SUBTOTAL", "TAX", "TOTAL", "FOOTER", "OTHER"]

_N_HAND_FEATURES = 8


def hand_features(line: str, index: int, n_lines: int) -> list[float]:
    """Eight cheap positional/shape features that TF-IDF cannot see."""
    n_lines = max(n_lines, 1)
    stripped = line.strip()
    chars = [c for c in stripped if not c.isspace()]
    n_chars = max(len(chars), 1)
    letters = [c for c in chars if c.isalpha()]
    return [
        index / n_lines,
        sum(1 for c in chars if c.isdigit()) / n_chars,
        len(letters) / n_chars,
        1.0 if re.search(r"[₹]|\d+\.\d{2}", stripped) else 0.0,
        min(len(stripped.split()), 12) / 12.0,
        (sum(1 for c in letters if c.isupper()) / len(letters)) if letters else 0.0,
        1.0 if ":" in stripped else 0.0,
        1.0 if index >= n_lines - 3 else 0.0,
    ]


class LineRoleModel:
    """Thin wrapper over a TF-IDF + LogisticRegression pipeline."""

    def __init__(self, vectorizer, classifier, classes: list[str]):
        self._vectorizer = vectorizer
        self._clf = classifier
        self._classes = classes

    # ------------------------------------------------------------------ train
    @classmethod
    def train(cls, lines_csv: Path, out_path: Path | None = None,
              *, min_rows: int = 200) -> "LineRoleModel | None":
        import numpy as np
        from scipy.sparse import hstack, csr_matrix
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression

        texts, labels, feats = [], [], []
        grouped: dict[str, list[tuple[int, str, str]]] = {}
        with Path(lines_csv).open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                grouped.setdefault(row["receipt_id"], []).append(
                    (int(row["line_no"]), row["text"], row["role"]))

        for rows in grouped.values():
            rows.sort()
            n = len(rows)
            for index, text, role in rows:
                texts.append(text)
                labels.append(role)
                feats.append(hand_features(text, index, n))

        if len(texts) < min_rows or len(set(labels)) < 2:
            return None

        vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5),
                                     min_df=2, max_features=30000, lowercase=True)
        X_text = vectorizer.fit_transform(texts)
        X = hstack([X_text, csr_matrix(np.asarray(feats, dtype=float))]).tocsr()
        clf = LogisticRegression(max_iter=1000, C=4.0, n_jobs=None)
        clf.fit(X, labels)

        model = cls(vectorizer, clf, list(clf.classes_))
        if out_path is not None:
            model.save(Path(out_path))
        return model

    # ------------------------------------------------------------------- i/o
    def save(self, path: Path) -> None:
        import joblib

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"vectorizer": self._vectorizer, "clf": self._clf,
                     "classes": self._classes}, path)

    @classmethod
    def load(cls, path: Path) -> "LineRoleModel | None":
        """Return ``None`` when the model is absent or unloadable."""
        path = Path(path)
        if not path.exists():
            return None
        try:
            import joblib

            blob = joblib.load(path)
            return cls(blob["vectorizer"], blob["clf"], blob["classes"])
        except Exception:
            return None

    # ------------------------------------------------------------- inference
    def predict_proba(self, lines: list[str]) -> list[dict[str, float]]:
        if not lines:
            return []
        import numpy as np
        from scipy.sparse import hstack, csr_matrix

        n = len(lines)
        X_text = self._vectorizer.transform(lines)
        feats = np.asarray([hand_features(line, i, n) for i, line in enumerate(lines)],
                           dtype=float)
        X = hstack([X_text, csr_matrix(feats)]).tocsr()
        probs = self._clf.predict_proba(X)
        return [dict(zip(self._classes, row)) for row in probs]
