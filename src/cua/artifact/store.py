"""Artifact storage: artifacts/<capability_id>/<version>.json. Approved versions are immutable."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from cua.artifact.schema import Capability, Status, TenantOverride


def _ver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def versions(root: Path, capability_id: str) -> list[Path]:
    d = root / capability_id
    return sorted(d.glob("*.json"), key=lambda p: _ver(p.stem)) if d.exists() else []


def load(path: str | Path) -> Capability:
    return Capability.model_validate_json(Path(path).read_text())


def latest(root: Path, capability_id: str) -> Capability | None:
    vs = versions(root, capability_id)
    return load(vs[-1]) if vs else None


def next_version(root: Path, cap: Capability) -> tuple[str, str | None]:
    """Semver: MAJOR if the contract (inputs/outputs/outcomes) changed, else MINOR. Same fingerprint = same version."""
    prev = latest(root, cap.capability_id)
    if prev is None:
        return "1.0.0", None
    if prev.fingerprint == cap.fingerprint:
        return prev.version, prev.version
    major, minor, _ = _ver(prev.version)
    if prev.contract_signature() != cap.contract_signature():
        return f"{major + 1}.0.0", prev.version
    return f"{major}.{minor + 1}.0", prev.version


def save(root: Path, cap: Capability) -> Path:
    cap.fingerprint = cap.compute_fingerprint()
    path = root / cap.capability_id / f"{cap.version}.json"
    if path.exists():
        existing = load(path)
        if existing.fingerprint == cap.fingerprint:
            return path  # identical behaviour/contract: keep the existing file (and its approval)
        if existing.status == Status.APPROVED and existing.fingerprint != cap.fingerprint:
            raise PermissionError(f"{path} is approved and immutable; record a new version instead")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cap.model_dump_json(indent=2))
    return path


def approve(path: Path, reviewer: str, note: str | None = None) -> Capability:
    cap = load(path)
    if cap.compute_fingerprint() != cap.fingerprint:
        raise ValueError("artifact content does not match its fingerprint; re-save before approving")
    cap.status = Status.APPROVED
    cap.provenance.reviewed_by = reviewer
    cap.provenance.approved_at = datetime.now(timezone.utc)
    if note:
        cap.provenance.notes.append(f"review: {note}")
    path.write_text(cap.model_dump_json(indent=2))
    return cap


def export_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(Capability.model_json_schema(), indent=2))


def add_tenant_override(root: Path, path: Path, tenant: str, override: TenantOverride, source: str) -> Path:
    """Attach a per-tenant delta to a capability. Writes a new MINOR version, back to draft."""
    cap = load(path)
    cap.app.tenant_overrides[tenant] = override
    cap.fingerprint = cap.compute_fingerprint()
    cap.version, parent = next_version(root, cap)
    cap.status = Status.DRAFT
    cap.provenance.parent_version = parent
    cap.provenance.reviewed_by = None
    cap.provenance.approved_at = None
    cap.provenance.notes.append(f"tenant override '{tenant}' added from {source}")
    return save(root, cap)
