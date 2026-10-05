"""Record-time locator generation: several candidates per target, each verified unique
(and verified to hit the element the agent actually chose) at that moment.

Default priority (most semantic first):
  1 role + accessible name     2 programmatic label      3 adjacent label cell
  4 visible text anchor        5 table structure          6 CSS path (last resort)
Exception: a control inside a data-table row (e.g. a "View" link repeated per row) is
identified by its row, so the structural anchor goes first - role+name is only unique
by accident there (one search result) and would silently pick the wrong row tomorrow.
Data cells are never anchored by their own text: the text IS the data and changes.
"""

from __future__ import annotations

from pydantic import BaseModel

from cua.artifact.schema import (
    Css,
    Label,
    Locator,
    NearLabel,
    RoleName,
    TableCell,
    TextAnchor,
)
from cua.surface.types import CONTROL_ROLES, UIElement

NAMEABLE_ROLES = CONTROL_ROLES | {"heading"}


class CandidateReport(BaseModel):
    locator: Locator
    count: int
    same_element: bool | None
    kept: bool
    error: str | None = None


def _key_columns(el: UIElement, typed_values: list[str], hint: str | None) -> list[str]:
    if not el.row:
        return []
    cols = [c for c in el.row if c and c != el.column]
    ordered: list[str] = []
    if hint and hint in el.row:
        ordered.append(hint)
    ordered += [c for c in cols if el.row[c] in typed_values and c not in ordered]
    ordered += [c for c in cols if c not in ordered]
    return ordered


def propose(el: UIElement, typed_values: list[str], row_key_hint: str | None, is_data: bool) -> list[Locator]:
    out: list[Locator] = []
    structural: list[Locator] = []
    for key in _key_columns(el, typed_values, row_key_hint)[:3]:
        if el.role in CONTROL_ROLES:
            structural.append(TableCell(row_key_column=key, row_key_value=el.row[key],
                                        column_header=el.column or None, control_role=el.role))
        elif el.column:
            structural.append(TableCell(row_key_column=key, row_key_value=el.row[key], column_header=el.column))
    row_scoped_control = el.role in CONTROL_ROLES and bool(structural)
    if row_scoped_control:
        out += structural
    if el.role in NAMEABLE_ROLES and el.name:
        out.append(RoleName(role=el.role, name=el.name))
    if el.label:
        out.append(Label(text=el.label))
    if el.near_label and el.role in CONTROL_ROLES and el.role not in ("link", "button"):
        out.append(NearLabel(label_text=el.near_label, control_role=el.role))
    elif el.near_label and el.role in ("text", "cell") and not el.column:
        out.append(NearLabel(label_text=el.near_label))  # "Label: | value" pairs
    if el.name and not is_data and el.role in ("link", "button", "text", "cell", "heading") and len(el.name) <= 60:
        out.append(TextAnchor(text=el.name))
    if not row_scoped_control:
        out += structural
    if el.css:
        out.append(Css(selector=el.css))
    return out


def build_candidates(
    surface, el: UIElement, *, typed_values: list[str] | None = None, row_key_hint: str | None = None, is_data: bool = False
) -> tuple[list[Locator], list[CandidateReport]]:
    """Return verified candidates (unique AND pointing at `el`), in priority order."""
    kept: list[Locator] = []
    report: list[CandidateReport] = []
    for loc in propose(el, typed_values or [], row_key_hint, is_data):
        if loc.strategy == "table_cell" and any(k.strategy == "table_cell" for k in kept):
            continue  # one verified structural anchor is enough
        m = surface.match(el.frame_path, loc, ref=el.ref)
        ok = m.count == 1 and m.same_as_ref is True and loc not in kept
        report.append(CandidateReport(locator=loc, count=m.count, same_element=m.same_as_ref, kept=ok, error=m.error))
        if ok:
            kept.append(loc)
    return kept, report
