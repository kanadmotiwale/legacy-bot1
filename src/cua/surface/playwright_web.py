"""PlaywrightWebSurface: the one concrete Surface. The only module that touches Playwright pages."""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from playwright.sync_api import Error as PWError
from playwright.sync_api import Frame, Page

from cua.artifact.params import path_of, render, route_matches
from cua.artifact.schema import (
    Condition,
    Css,
    ElementVisible,
    FieldValue,
    FrameRef,
    HttpStatus,
    Label,
    Locator,
    NearLabel,
    RoleName,
    RouteMatches,
    TableCell,
    TextAnchor,
    TextVisible,
)
from cua.surface.types import (
    ActionRequest,
    ActionResult,
    FrameInfo,
    Location,
    MatchInfo,
    Observation,
    RefTarget,
    ResolvedTarget,
    UIElement,
)

DOM_JS = (Path(__file__).parent / "dom.js").read_text()


class TargetError(Exception):
    pass


class PlaywrightWebSurface:
    kind = "web"

    def __init__(self, page: Page, mask_rules: dict[str, dict[str, list[str]]] | None = None) -> None:
        self.page = page
        self.mask_rules = mask_rules or {}
        self._status: dict[int, int] = {}  # id(frame) -> last document HTTP status
        self._navs = 0
        page.on("response", self._on_response)
        page.on("framenavigated", self._on_nav)

    # ------------------------------------------------------------------ frames
    def _on_response(self, resp) -> None:
        try:
            if resp.request.resource_type == "document" and resp.frame:
                self._status[id(resp.frame)] = resp.status
        except PWError:
            pass

    def _on_nav(self, frame) -> None:
        self._navs += 1

    def nav_count(self) -> int:
        """Monotonic count of document navigations in any frame."""
        return self._navs

    def _ensure_lib(self, frame: Frame) -> None:
        if not frame.evaluate("() => !!window.__cua"):
            frame.evaluate(DOM_JS)

    @staticmethod
    def frame_ref(frame: Frame) -> FrameRef:
        return FrameRef(name=frame.name or None, url_pattern=None if frame.name else path_of(frame.url))

    def frame_path(self, frame: Frame) -> list[FrameRef]:
        path = []
        while frame.parent_frame is not None:
            path.insert(0, self.frame_ref(frame))
            frame = frame.parent_frame
        return path

    def frame(self, frame_path: list[FrameRef]) -> Frame | None:
        frame = self.page.main_frame
        for ref in frame_path:
            nxt = None
            for child in frame.child_frames:
                if child.is_detached():
                    continue
                if ref.name and child.name == ref.name:
                    nxt = child
                    break
                if not ref.name and ref.url_pattern and route_matches(ref.url_pattern, child.url):
                    nxt = child
                    break
            if nxt is None:
                return None
            frame = nxt
        return frame

    def _frames(self, frame_path: list[FrameRef] | None) -> list[Frame]:
        if frame_path is None:
            return [f for f in self.page.frames if not f.is_detached()]
        f = self.frame(frame_path)
        return [f] if f else []

    # ------------------------------------------------------------------ observe
    def observe(self, *, screenshot: bool = True, mask_level: str | None = "llm") -> Observation:
        elements: list[UIElement] = []
        frames: list[FrameInfo] = []
        for i, frame in enumerate(self.page.frames):
            if frame.is_detached():
                continue
            path = self.frame_path(frame)
            frames.append(FrameInfo(path=path, url=frame.url, status=self._status.get(id(frame))))
            try:
                self._ensure_lib(frame)
                raw = frame.evaluate("(p) => window.__cua.observe(p)", f"f{i}e")
            except PWError:
                continue  # frame navigated mid-observation; next observation will catch it
            for d in raw:
                elements.append(UIElement(frame_path=path, **d))
        shot = self.screenshot(mask_level=mask_level) if screenshot else None
        return Observation(url=self.page.url, frames=frames, elements=elements, screenshot=shot)

    def current_location(self) -> Location:
        return Location(
            url=self.page.url,
            frames=[
                FrameInfo(path=self.frame_path(f), url=f.url, status=self._status.get(id(f)))
                for f in self.page.frames
                if not f.is_detached()
            ],
        )

    # ------------------------------------------------------------------ locators
    def _locate(self, frame: Frame, loc: Locator):
        if isinstance(loc, RoleName):
            return frame.get_by_role(loc.role, name=loc.name, exact=True)
        if isinstance(loc, Label):
            return frame.get_by_label(loc.text, exact=True)
        if isinstance(loc, TextAnchor):
            base = frame.get_by_text(loc.text, exact=True)
            return base if not loc.role else frame.get_by_role(loc.role).filter(has_text=loc.text)
        if isinstance(loc, (NearLabel, TableCell)):
            self._ensure_lib(frame)
            nonce = secrets.token_hex(4)
            frame.evaluate("([s, n]) => window.__cua.resolve(s, n)", [loc.model_dump(), nonce])
            return frame.locator(f'[data-cua-hit="{nonce}"]')
        if isinstance(loc, Css):
            return frame.locator(loc.selector)
        raise TargetError(f"unsupported locator {loc}")

    def _locate_unique(self, frame_path: list[FrameRef] | None, locator: Locator):
        """Locator for `locator` within frame_path; with None, the single frame where it matches."""
        hits = []
        for frame in self._frames(frame_path):
            loc = self._locate(frame, locator)
            n = loc.count()
            if n:
                hits.append((loc, n))
        total = sum(n for _, n in hits)
        return (hits[0][0] if hits else None), total

    def _resolve(self, target: ResolvedTarget | RefTarget):
        if isinstance(target, RefTarget):
            for frame in self.page.frames:
                if frame.is_detached():
                    continue
                loc = frame.locator(f'[data-cua-ref="{target.ref}"]')
                if loc.count() == 1:
                    return loc
            raise TargetError(f"ref {target.ref} not found (stale observation?)")
        loc, n = self._locate_unique(target.frame_path, target.locator)
        if n != 1:
            raise TargetError(f"{target.locator.strategy} matched {n} elements")
        return loc

    def match(self, frame_path: list[FrameRef] | None, locator: Locator, ref: str | None = None) -> MatchInfo:
        if frame_path is not None and self.frame(frame_path) is None:
            return MatchInfo(count=0, error="frame not found")
        try:
            loc, n = self._locate_unique(frame_path, locator)
            same = None
            if ref is not None and n == 1:
                # same element, or the cell that wraps it (e.g. <td><b>value</b></td>)
                same = loc.evaluate(
                    "(e, r) => { const s = `[data-cua-ref=\"${r}\"]`; return e.matches(s) || !!e.querySelector(s) || !!e.closest(s); }", ref
                )
            return MatchInfo(count=n, same_as_ref=same)
        except PWError as e:
            return MatchInfo(count=0, error=str(e).splitlines()[0])

    def frame_text(self, frame_path: list[FrameRef]) -> str:
        f = self.frame(frame_path)
        try:
            return f.evaluate("() => document.body ? document.body.innerText : ''") if f else ""
        except PWError:
            return ""

    def read(self, frame_path: list[FrameRef] | None, locator: Locator) -> str:
        loc = self._resolve(ResolvedTarget(frame_path=frame_path, locator=locator))
        tag = loc.evaluate("e => e.tagName")
        if tag in ("INPUT", "TEXTAREA"):
            return loc.input_value()
        if tag == "SELECT":
            return loc.evaluate("e => e.selectedOptions[0] ? e.selectedOptions[0].text : ''")
        return loc.inner_text()

    def read_ref(self, ref: str) -> str:
        loc = self._resolve(RefTarget(ref=ref))
        return loc.inner_text() if loc.evaluate("e => !['INPUT','TEXTAREA','SELECT'].includes(e.tagName)") else loc.input_value()

    # ------------------------------------------------------------------ act
    def act(self, action: ActionRequest) -> ActionResult:
        t0 = time.monotonic()
        try:
            if action.kind == "navigate":
                url = action.value or ""
                if not url.startswith("http"):
                    url = urljoin(self.page.url, url)
                self.page.goto(url, wait_until="commit", timeout=action.timeout_ms)
            else:
                loc = self._resolve(action.target)
                if action.kind == "click":
                    loc.click(timeout=action.timeout_ms)
                elif action.kind == "type":
                    loc.fill(action.value or "", timeout=action.timeout_ms)
                elif action.kind == "select":
                    try:
                        loc.select_option(label=action.value, timeout=action.timeout_ms)
                    except PWError:
                        loc.select_option(value=action.value, timeout=action.timeout_ms)
            return ActionResult(ok=True, duration_ms=int((time.monotonic() - t0) * 1000))
        except (PWError, TargetError) as e:
            return ActionResult(ok=False, error=str(e).splitlines()[0], duration_ms=int((time.monotonic() - t0) * 1000))

    def describe_target(self, target: ResolvedTarget | RefTarget) -> dict[str, Any]:
        """Role, accessible name, link href and document URL of a resolved target (for policy)."""
        frames = self.page.frames if isinstance(target, RefTarget) else self._frames(target.frame_path)
        for frame in frames:
            try:
                if isinstance(target, RefTarget):
                    loc = frame.locator(f'[data-cua-ref="{target.ref}"]')
                else:
                    loc = self._locate(frame, target.locator)
                if loc.count() != 1:
                    continue
                self._ensure_lib(frame)
                info = loc.evaluate("e => ({href: e.href || null, role: window.__cua.role(e), name: window.__cua.accName(e)})")
                info["frame_url"] = frame.url
                return info
            except PWError:
                continue
        return {}

    def href_of(self, ref: str) -> str | None:
        """Absolute URL a link ref would navigate to (for policy checks)."""
        try:
            loc = self._resolve(RefTarget(ref=ref))
            return loc.evaluate("e => e.href || null")
        except (PWError, TargetError):
            return None

    def frame_url_of(self, target: ResolvedTarget | RefTarget) -> str | None:
        try:
            if isinstance(target, RefTarget):
                for frame in self.page.frames:
                    if not frame.is_detached() and frame.locator(f'[data-cua-ref="{target.ref}"]').count():
                        return frame.url
                return None
            f = self.frame(target.frame_path)
            return f.url if f else None
        except PWError:
            return None

    # ------------------------------------------------------------------ conditions
    def check(self, condition: Condition, params: dict[str, Any]) -> bool:
        try:
            return self._check(condition, params)
        except (PWError, TargetError, KeyError):
            return False

    def _check(self, c: Condition, params: dict[str, Any]) -> bool:
        if isinstance(c, RouteMatches):
            return any(route_matches(c.pattern, f.url, params) for f in self._frames(c.frame_path))
        if isinstance(c, TextVisible):
            text = render(c.text, params)
            for f in self._frames(c.frame_path):
                loc = f.get_by_text(text, exact=c.exact)
                n = loc.count()
                if any(loc.nth(i).is_visible() for i in range(min(n, 5))):
                    return True
            return False
        if isinstance(c, ElementVisible):
            for cand in c.target.candidates:
                loc, n = self._locate_unique(c.target.frame_path, _render_locator(cand, params))
                if n == 1 and loc.is_visible():
                    return True
            return False
        if isinstance(c, FieldValue):
            want = render(c.equals, params)
            for cand in c.target.candidates:
                loc, n = self._locate_unique(c.target.frame_path, _render_locator(cand, params))
                if n == 1:
                    if loc.evaluate("e => e.tagName") == "SELECT":
                        return loc.evaluate("e => e.selectedOptions[0] ? e.selectedOptions[0].text.trim() : ''") == want
                    return loc.input_value() == want
            return False
        if isinstance(c, HttpStatus):
            return any(
                c.min <= self._status.get(id(f), 200) <= c.max for f in self._frames(c.frame_path)
            )
        return False

    def read_lines_after(self, anchor_text: str, frame_path: list[FrameRef] | None) -> list[str]:
        for f in self._frames(frame_path):
            try:
                self._ensure_lib(f)
                lines = f.evaluate("(a) => window.__cua.linesAfter(a)", anchor_text)
                if lines is not None:
                    return lines
            except PWError:
                continue
        return []

    def reload_failed_documents(self) -> int:
        n = 0
        for f in list(self.page.frames):
            if f.is_detached() or self._status.get(id(f), 200) < 500:
                continue
            n += 1
            if f.parent_frame is None:
                self.page.reload(wait_until="commit")
            else:
                f.evaluate("() => location.reload()")
        return n

    # ------------------------------------------------------------------ evidence
    def screenshot(self, *, mask_level: str | None = "log") -> bytes:
        masks = []
        rules = self.mask_rules.get(mask_level) if mask_level else None
        if rules:
            for f in self.page.frames:
                if f.is_detached():
                    continue
                try:
                    self._ensure_lib(f)
                    if f.evaluate("(r) => window.__cua.markSensitive(r)", rules):
                        masks.append(f.locator("[data-cua-mask]"))
                except PWError:
                    continue
        return self.page.screenshot(mask=masks, mask_color="#222222")

    def settle(self, timeout_ms: int = 10_000, quiet_ms: int = 300) -> None:
        """Wait until no frame has navigated for `quiet_ms` and every frame finished loading."""
        deadline = time.monotonic() + timeout_ms / 1000
        last, quiet_since = self._navs, time.monotonic()
        while time.monotonic() < deadline:
            self.page.wait_for_timeout(50)
            if self._navs != last:
                last, quiet_since = self._navs, time.monotonic()
                continue
            if (time.monotonic() - quiet_since) * 1000 < quiet_ms:
                continue
            try:
                if all(f.is_detached() or f.evaluate("() => document.readyState") == "complete" for f in self.page.frames):
                    return
            except PWError:
                quiet_since = time.monotonic()

    def wait(self, ms: int) -> None:
        self.page.wait_for_timeout(ms)


def _render_locator(loc: Locator, params: dict[str, Any]) -> Locator:
    data = {k: (render(v, params) if isinstance(v, str) else v) for k, v in loc.model_dump().items()}
    return type(loc).model_validate(data)
