"""Deterministic verification-report construction from structured comparisons."""

import html
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


_MATCH_STATUSES = {"MATCH", "MATCHED", "PASS", "PASSED", "VERIFIED"}


def parse_comparison_json(response_content: str) -> List[Dict[str, Any]]:
    """Parse field-level verification rows returned by a comparator LLM.

    Accepts both the current wrapper object:
      {"verification_analysis_report": [{...}]}

    and the older bare array form:
      [{...}]
    """
    if not isinstance(response_content, str):
        raise ValueError("Comparison output must be a string")

    stripped = response_content.strip()

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        array_start = response_content.find("[")
        array_end = response_content.rfind("]")
        if array_start < 0 or array_end < array_start:
            raise ValueError("Comparison output did not contain a JSON array")
        try:
            parsed = json.loads(response_content[array_start:array_end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"Comparison output contained invalid JSON: {exc}") from exc

    if isinstance(parsed, dict):
        parsed = parsed.get("verification_analysis_report")

    if not isinstance(parsed, list) or not parsed:
        raise ValueError("Comparison output must be a non-empty JSON array")
    if not all(isinstance(item, dict) for item in parsed):
        raise ValueError("Every comparison result must be a JSON object")
    return parsed


def _confidence_value(value: Any) -> int:
    if isinstance(value, bool):
        return 0

    if isinstance(value, (int, float)):
        numeric = float(value)
    else:
        match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
        numeric = float(match.group()) if match else 0.0

    # Some prompts express a confidence adjustment as -40 instead of the
    # resulting confidence of 60. Preserve the intended business meaning.
    if numeric < 0:
        numeric = 100 + numeric
    return int(max(0, min(100, numeric)))


def _normalized_field_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _is_critical(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"true", "yes", "1"}


def _item_is_critical(item: Dict[str, Any]) -> bool:
    return _is_critical(item.get("_is_critical", item.get("is_critical")))


def _display_value(value: Any) -> str:
    if value is None or value == "":
        return "N/A"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _comparison_columns(comparisons: Sequence[Dict[str, Any]]) -> List[Tuple[str, str]]:
    if any("api_value" in item or "applicant_value" in item for item in comparisons):
        return [
            ("api_value", "API Value"),
            ("applicant_value", "Applicant Value"),
            ("document_value", "Document Value"),
        ]
    return [
        ("record_value", "Record Value"),
        ("document_value", "Document Value"),
    ]


def _status(item: Dict[str, Any]) -> str:
    return str(item.get("status") or "NOT_FOUND").strip().upper()


def _is_match(item: Dict[str, Any]) -> bool:
    return _status(item) in _MATCH_STATUSES


def _public_row(item: Dict[str, Any]) -> Dict[str, Any]:
    """Return the row shape exposed to Salesforce/UI consumers."""
    row = {
        "field_name": item.get("field_name"),
        "status": _status(item),
        "confidence": _confidence_value(item.get("confidence")),
        "notes": item.get("notes"),
    }
    for value_key in ("api_value", "applicant_value", "record_value", "document_value"):
        if value_key in item:
            row[value_key] = item.get(value_key)
    return row


def _apply_critical_field_map(
    comparisons: Sequence[Dict[str, Any]],
    critical_field_names: Optional[Iterable[str]],
) -> List[Dict[str, Any]]:
    if critical_field_names is None:
        return [dict(item) for item in comparisons]

    critical_names = {
        _normalized_field_name(field_name)
        for field_name in critical_field_names
    }
    prepared: List[Dict[str, Any]] = []
    for item in comparisons:
        copied = dict(item)
        if _normalized_field_name(copied.get("field_name")) in critical_names:
            copied["_is_critical"] = True
        prepared.append(copied)
    return prepared


def _render_html(comparisons: Sequence[Dict[str, Any]]) -> str:
    value_columns = _comparison_columns(comparisons)
    headers = ["Field Name", *(label for _, label in value_columns), "Status", "Confidence", "Notes"]

    header_html = "".join(
        f"<th style='border:1px solid #ddd;padding:8px;text-align:left;'>{html.escape(label)}</th>"
        for label in headers
    )
    rows = []
    for item in comparisons:
        status = _status(item)
        background = "#e8f5e9" if _is_match(item) else "#fde8e8"
        cells = [html.escape(_display_value(item.get("field_name")))]
        cells.extend(
            html.escape(_display_value(item.get(key)))
            for key, _ in value_columns
        )
        cells.extend([
            html.escape(status),
            f"{_confidence_value(item.get('confidence'))}%",
            html.escape(_display_value(item.get("notes"))).replace("\n", "<br>"),
        ])
        rows.append(
            f"<tr style='background:{background};'>"
            + "".join(
                f"<td style='border:1px solid #ddd;padding:8px;'>{cell}</td>"
                for cell in cells
            )
            + "</tr>"
        )

    return (
        "<div style='font-family:Arial;'>"
        "<table style='width:100%;border-collapse:collapse;border:1px solid #ddd;'>"
        f"<thead><tr style='background:#f2f2f2;'>{header_html}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def build_verification_report(
    comparisons: Sequence[Dict[str, Any]],
    *,
    critical_field_names: Optional[Iterable[str]] = None,
    extra_fields: Optional[Dict[str, Any]] = None,
    allowed_fields: Optional[Iterable[str]] = None,
    extra_allowed_fields: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Build the complete report contract without a synthesis LLM call.

    `allowed_fields` is the record's verifiable field list; `extra_allowed_fields`
    whitelists synthetic rows a prompt mandates (e.g. "Payslip Recency",
    "Number of Semesters") that are not record fields.
    """
    if not comparisons:
        raise ValueError("Cannot build a report without comparison results")

    # Server-side safety net: drop any LLM-hallucinated rows for fields not in
    # the verifiable_fields list (e.g. "Last Modified Date" from document metadata).
    # Matching is normalized (case/punctuation-insensitive) so a legitimate row
    # like "Employer Name" for record key "employerName" is not silently lost.
    if allowed_fields is not None:
        allowed_set = {_normalized_field_name(f) for f in allowed_fields}
        # Always allow explicitly injected LLM reporting fields
        allowed_set.add(_normalized_field_name("Number of Semesters"))
        for extra in (extra_allowed_fields or ()):
            allowed_set.add(_normalized_field_name(extra))

        comparisons = [
            item for item in comparisons
            if _normalized_field_name(item.get("field_name")) in allowed_set
        ]
        if not comparisons:
            raise ValueError("All comparison rows were filtered out by allowed_fields")

    comparisons = _apply_critical_field_map(comparisons, critical_field_names)

    score = 100.0
    for item in comparisons:
        if _item_is_critical(item):
            confidence = _confidence_value(item.get("confidence"))
            score -= (100 - confidence) / 2
    confidence_range = int(max(0, min(100, score)))

    issues = [item for item in comparisons if not _is_match(item)]
    critical_issues = [item for item in issues if _item_is_critical(item)]

    if critical_issues:
        names = ", ".join(_display_value(item.get("field_name")) for item in critical_issues)
        overall_feedback = f"Verification requires review due to critical issues in {names}."
    elif issues:
        names = ", ".join(_display_value(item.get("field_name")) for item in issues)
        overall_feedback = (
            "All critical fields verified successfully. "
            f"Non-critical issues require review in {names}."
        )
    else:
        overall_feedback = "All critical fields verified successfully."

    mismatched_field_list = ";".join(
        _display_value(item.get("field_name"))
        for item in issues
    ) or "N/A"

    if critical_issues:
        verification_status = "Failed" if confidence_range < 50 else "Needs Review"
    elif confidence_range >= 80:
        verification_status = "Passed"
    elif confidence_range >= 50:
        verification_status = "Needs Review"
    else:
        verification_status = "Failed"

    public_rows = [_public_row(item) for item in comparisons]

    report = {
        "field_comparison_summary": _render_html(public_rows),
        "overall_feedback": overall_feedback,
        "confidence_range": confidence_range,
        "overall_percentage_confidence": confidence_range,
        "mismatched_field_list": mismatched_field_list,
        "verification_analysis_report": public_rows,
        "verification_status": verification_status,
    }
    if extra_fields:
        report.update(extra_fields)
    return report
