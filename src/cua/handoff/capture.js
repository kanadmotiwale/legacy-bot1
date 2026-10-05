// Human action capture. Installed in every frame. Reports what the operator
// clicked/changed in semantic terms (role, name, label, table context) so actions
// can be logged and, during discovery, turned into reviewable steps.
// The runner only records these while a human holds the control lock.
(() => {
  if (window.__cuaCaptureInstalled) return;
  window.__cuaCaptureInstalled = true;
  const send = (kind, el) => {
    try {
      if (!window.__cuaHuman || !window.__cua) return;
      const d = window.__cua.describe(el, null);
      if (kind === "change" && el.type === "password") d.value = "<secret>";
      window.__cuaHuman({ kind, ...d, at: Date.now() });
    } catch (e) { /* never break the page */ }
  };
  document.addEventListener("click", (e) => {
    const el = e.target.closest("a[href],button,input,select,textarea,td,th") || e.target;
    send("click", el);
  }, true);
  document.addEventListener("change", (e) => send("change", e.target), true);
})();
