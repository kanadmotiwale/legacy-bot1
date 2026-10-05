"""Locator candidate ordering, record-time verification, and replay-time fallback + drift."""

from __future__ import annotations

from cua.artifact.locators import build_candidates, propose
from cua.artifact.schema import Css, NearLabel, RoleName, Target, TableCell, TextAnchor
from cua.replay import resolve
from cua.replay.resolve import resolve_target
from cua.surface.types import MatchInfo, UIElement


class FakeSurface:
    """Counts per locator strategy; optionally changes after N polls (page still loading)."""

    def __init__(self, counts, later=None, after=0, same=True):
        self.counts, self.later, self.after, self.same = counts, later, after, same
        self.polls = 0

    def match(self, frame_path, locator, ref=None):
        table = self.later if (self.later and self.polls >= self.after) else self.counts
        n = table.get(locator.strategy, 0)
        return MatchInfo(count=n, same_as_ref=(self.same if (ref and n == 1) else None))

    def wait(self, ms):
        self.polls += 1


def el(**kw):
    base = dict(ref="e1", frame_path=[], role="textbox", name="", css="body > input")
    return UIElement(**{**base, **kw})


def test_semantic_first_order():
    cands = propose(el(role="button", name="Search"), [], None, is_data=False)
    assert [c.strategy for c in cands] == ["role_name", "text", "css"]
    cands = propose(el(near_label="Member ID:"), [], None, is_data=False)
    assert [c.strategy for c in cands] == ["near_label", "css"]


def test_row_scoped_control_prefers_structural_anchor():
    view = el(role="link", name="View", column=None, row={"Member ID": "10042", "Name": "X"})
    cands = propose(view, ["10042"], None, is_data=False)
    assert cands[0] == TableCell(row_key_column="Member ID", row_key_value="10042", control_role="link")
    assert RoleName(role="link", name="View") in cands


def test_data_cells_never_anchor_on_their_own_text():
    cell = el(role="cell", name="$12,345.67", column="Balance", row={"Type": "Share Savings", "Balance": "$12,345.67"})
    cands = propose(cell, [], "Type", is_data=True)
    assert not any(isinstance(c, TextAnchor) for c in cands)
    assert cands[0] == TableCell(row_key_column="Type", row_key_value="Share Savings", column_header="Balance")


def test_record_time_verification_drops_non_unique_or_wrong_element():
    surface = FakeSurface({"role_name": 2, "text": 1, "css": 1}, same=True)
    kept, report = build_candidates(surface, el(role="button", name="Go"))
    assert [k.strategy for k in kept] == ["text", "css"]
    assert report[0].count == 2 and not report[0].kept
    wrong = FakeSurface({"role_name": 1, "css": 1}, same=False)
    assert build_candidates(wrong, el(role="button", name="Go"))[0] == []


TARGET = Target(description="field", candidates=[NearLabel(label_text="Member ID:", control_role="textbox"),
                                                 RoleName(role="textbox", name="Member ID"),
                                                 Css(selector="input")])


def test_resolution_takes_first_unique_and_reports_no_drift():
    r = resolve_target(FakeSurface({"near_label": 1, "role_name": 1, "css": 1}), TARGET, 1000)
    assert r.index == 0 and r.target.locator.strategy == "near_label"


def test_lower_priority_match_is_drift_after_grace(monkeypatch):
    monkeypatch.setattr(resolve, "LOWER_PRIORITY_GRACE_MS", 0)
    r = resolve_target(FakeSurface({"near_label": 0, "role_name": 3, "css": 1}), TARGET, 1000)
    assert r.index == 2 and r.target.locator.strategy == "css"
    assert [a.match_count for a in r.attempts] == [0, 3, 1]


def test_grace_period_waits_for_higher_priority_on_loading_page():
    # css matches immediately, near_label appears on the 2nd poll -> we must pick near_label
    s = FakeSurface({"css": 1}, later={"near_label": 1, "css": 1}, after=2)
    r = resolve_target(s, TARGET, 3000)
    assert r.index == 0


def test_ambiguous_vs_not_found():
    amb = resolve_target(FakeSurface({"near_label": 2, "role_name": 2, "css": 2}), TARGET, 200)
    assert amb.target is None and amb.ambiguous
    nf = resolve_target(FakeSurface({}), TARGET, 200)
    assert nf.target is None and not nf.ambiguous
