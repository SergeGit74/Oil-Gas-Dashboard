"""Analyst feedback analysis: a golden-set export and a tagger-agreement report. Read-only on the store."""
import csv
from collections import Counter

import storage

GOLDEN_COLUMNS = ["feedback_id", "item_type", "item_id", "created_at", "rating", "analyst_label", "comment",
                  "article_title", "source", "category", "model_direction", "model_magnitude", "model_confidence"]


def golden_rows():
    """One row per feedback entry; article-tag entries are joined with the model's tag at the time of export."""
    rows = []
    for f in storage.all_feedback():
        a = storage.article_by_id(int(f["item_id"])) if f["item_type"] == "article_tag" and f["item_id"].isdigit() else None
        rows.append({
            "feedback_id": f["id"], "item_type": f["item_type"], "item_id": f["item_id"],
            "created_at": f["created_at"], "rating": f["rating"], "analyst_label": f["analyst_label"] or "",
            "comment": f["comment"] or "",
            "article_title": a["title"] if a else "", "source": a["source"] if a else "",
            "category": a["category"] if a else "", "model_direction": a["direction"] if a else "",
            "model_magnitude": a["magnitude"] if a else "", "model_confidence": a["confidence"] if a else ""})
    return rows


def export_golden_csv(path):
    rows = golden_rows()
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:  # BOM so Excel reads UTF-8
        writer = csv.DictWriter(fh, fieldnames=GOLDEN_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def agreement_report():
    """How often the model's direction matches the analyst on article-tag feedback.

    Agree: thumbs up with no correction, or a correction equal to the model's direction. Disagree: a different
    corrected direction. Thumbs down with no correction is counted separately because the right label is unknown.
    The latest feedback per article wins."""
    latest = {}
    for f in storage.all_feedback():
        if f["item_type"] == "article_tag":
            latest[f["item_id"]] = f
    agree = disagree = down_no_label = missing = 0
    confusion = Counter()
    for item_id, f in latest.items():
        a = storage.article_by_id(int(item_id)) if item_id.isdigit() else None
        if not a or not a["direction"]:
            missing += 1
            continue
        model, label = a["direction"], f["analyst_label"]
        if label:
            confusion[(model, label)] += 1
            agree, disagree = agree + (model == label), disagree + (model != label)
        elif f["rating"] == "up":
            confusion[(model, model)] += 1
            agree += 1
        else:
            down_no_label += 1
    judged = agree + disagree
    return {"articles_with_feedback": len(latest), "judged": judged, "agree": agree, "disagree": disagree,
            "agreement_rate": round(agree / judged, 3) if judged else None,
            "thumbs_down_without_correction": down_no_label, "article_not_found": missing,
            "confusion": {f"{model} -> {label}": n for (model, label), n in sorted(confusion.items())}}
