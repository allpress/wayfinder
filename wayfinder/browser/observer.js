// Wayfinder observer. Read-only. Installed once per page via add_init_script,
// then invoked from Python via page.evaluate("window.__wayfinder__.snapshot(opts)").
//
// Output shape (JSON-safe):
//   {
//     url, title,
//     handles:    [{handle, role, name, value, label, placeholder,
//                   required, disabled, checked, editable,
//                   in_form, landmark, ordinal, bbox}],
//     landmarks:  [{handle, role, name}],
//     text_blocks:[{handle, tag, text, landmark}],
//     fingerprint: "sha1-ish 10-char string of role+name+path of every interactable",
//     truncated:  bool
//   }
//
// Handles: deterministic per-snapshot. sha1(role|name|ax_path|ordinal)[0..4]
// prefixed with "h". Collisions are resolved by bumping the ordinal.

(() => {
  if (window.__wayfinder__) return;

  const MAX_STR = 200;
  const DEFAULT_MAX_HANDLES = 400;
  const MAX_HANDLES_FULLPAGE = 1500;

  // Computed roles for native elements when aria-role is absent.
  const NATIVE_ROLE = {
    A:       (el) => el.hasAttribute("href") ? "link" : null,
    BUTTON:  () => "button",
    SELECT:  () => "combobox",
    TEXTAREA:() => "textbox",
    OPTION:  () => "option",
    SUMMARY: () => "button",
    FORM:    () => "form",
  };

  // INPUT type → role.
  const INPUT_ROLE = {
    button: "button", submit: "button", reset: "button",
    checkbox: "checkbox", radio: "radio",
    range: "slider",
    number: "spinbutton",
    search: "searchbox",
    password: "textbox",   // kept as textbox; we tag with __wf_password__
    text: "textbox", email: "textbox", tel: "textbox", url: "textbox",
    date: "textbox", "datetime-local": "textbox", month: "textbox",
    week: "textbox", time: "textbox",
    file: "button",
    color: "textbox",
  };

  const INTERACTABLE_ROLES = new Set([
    "button", "link", "textbox", "combobox", "checkbox", "radio",
    "tab", "menuitem", "option", "form", "searchbox", "switch",
    "slider", "spinbutton", "menuitemcheckbox", "menuitemradio",
  ]);

  const LANDMARK_ROLES = new Set([
    "main", "navigation", "banner", "contentinfo", "complementary",
    "region", "search", "form",
  ]);

  // Native tags → landmark role when no explicit role attr.
  const NATIVE_LANDMARK = {
    MAIN: "main", NAV: "navigation", HEADER: "banner",
    FOOTER: "contentinfo", ASIDE: "complementary", SECTION: "region",
  };

  const TEXT_TAGS = new Set(["H1", "H2", "H3", "H4", "H5", "H6", "P", "LI", "TD", "TH", "DT", "DD"]);

  // ---------- small helpers ----------

  const trunc = (s, n = MAX_STR) => {
    if (s == null) return "";
    const t = String(s).replace(/\s+/g, " ").trim();
    return t.length > n ? t.slice(0, n) : t;
  };

  const getComputedRole = (el) => {
    const explicit = el.getAttribute && el.getAttribute("role");
    if (explicit) return explicit.trim().toLowerCase();
    const tag = el.tagName;
    if (tag === "INPUT") {
      const t = (el.getAttribute("type") || "text").toLowerCase();
      return INPUT_ROLE[t] || "textbox";
    }
    const fn = NATIVE_ROLE[tag];
    return fn ? fn(el) : null;
  };

  // Accessible name, best-effort per the ARIA naming algorithm (simplified):
  // aria-labelledby → aria-label → associated <label> → placeholder → alt → text.
  const accessibleName = (el) => {
    const byIds = el.getAttribute && el.getAttribute("aria-labelledby");
    if (byIds) {
      const parts = [];
      byIds.split(/\s+/).forEach((id) => {
        const ref = document.getElementById(id);
        if (ref) parts.push(ref.innerText || ref.textContent || "");
      });
      const joined = parts.join(" ").trim();
      if (joined) return trunc(joined);
    }
    const aria = el.getAttribute && el.getAttribute("aria-label");
    if (aria) return trunc(aria);

    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab) return trunc(lab.innerText || lab.textContent || "");
    }
    if (el.closest) {
      const wrap = el.closest("label");
      if (wrap) return trunc(wrap.innerText || wrap.textContent || "");
    }

    const ph = el.getAttribute && el.getAttribute("placeholder");
    if (ph) return trunc(ph);

    const alt = el.getAttribute && el.getAttribute("alt");
    if (alt) return trunc(alt);

    const title = el.getAttribute && el.getAttribute("title");
    if (title) return trunc(title);

    if (el.tagName === "INPUT" && (el.type === "submit" || el.type === "reset" || el.type === "button")) {
      return trunc(el.value || el.type);
    }
    return trunc(el.innerText || el.textContent || "");
  };

  const isInputElement = (el) => {
    const tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
  };

  const fieldLabel = (el) => {
    if (!el.id) return null;
    const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    return lab ? trunc(lab.innerText || lab.textContent || "") : null;
  };

  const isVisible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none") return false;
    if (style.opacity === "0") return false;
    const r = el.getBoundingClientRect();
    return (r.width > 0 && r.height > 0);
  };

  const bboxOf = (el) => {
    try {
      const r = el.getBoundingClientRect();
      return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
    } catch (_) { return null; }
  };

  // Build the AX-path as a slash-delimited role/ordinal from <html>. Stable under
  // minor DOM mutations that don't reorder the ancestor chain.
  const axPath = (el) => {
    const parts = [];
    let node = el;
    while (node && node !== document.body && node !== document.documentElement) {
      const parent = node.parentElement;
      if (!parent) break;
      const siblings = Array.from(parent.children).filter((s) => s.tagName === node.tagName);
      const idx = siblings.indexOf(node);
      parts.push(`${node.tagName}[${idx}]`);
      node = parent;
    }
    return parts.reverse().join("/");
  };

  // sha1-ish deterministic hash → 4 hex chars. We use a FNV-1a variant because
  // it's cheap and stable; we only need collision avoidance within ~400 elements
  // per page, which FNV handles fine. Ordinal is bumped on collision anyway.
  const shortHash = (s) => {
    let h = 0x811c9dc5 >>> 0;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
    }
    return h.toString(16).padStart(8, "0").slice(0, 4);
  };

  const findLandmark = (el) => {
    let n = el.parentElement;
    while (n) {
      const explicit = n.getAttribute && n.getAttribute("role");
      if (explicit && LANDMARK_ROLES.has(explicit.toLowerCase())) return explicit.toLowerCase();
      const native = NATIVE_LANDMARK[n.tagName];
      if (native) return native;
      n = n.parentElement;
    }
    return null;
  };

  const findOwningForm = (el, formHandles) => {
    let n = el.parentElement;
    while (n) {
      if (n.tagName === "FORM") {
        return formHandles.get(n) || null;
      }
      n = n.parentElement;
    }
    return null;
  };

  // ---------- snapshot ----------

  const snapshot = (opts) => {
    const o = opts || {};
    const viewportOnly = o.viewport_only !== false; // default true
    const maxHandles = viewportOnly ? DEFAULT_MAX_HANDLES : MAX_HANDLES_FULLPAGE;

    const vw = window.innerWidth || document.documentElement.clientWidth;
    const vh = window.innerHeight || document.documentElement.clientHeight;

    const allElements = document.querySelectorAll("*");
    const handles = [];
    const landmarks = [];
    const textBlocks = [];
    const seen = new Map();        // handle → count (for ordinal bumping)
    const formHandles = new Map();  // element → handle

    // First pass: collect landmarks (we need their handles for interactable.landmark).
    const landmarkCounts = Object.create(null);
    allElements.forEach((el) => {
      let role = null;
      const explicit = el.getAttribute && el.getAttribute("role");
      if (explicit && LANDMARK_ROLES.has(explicit.toLowerCase())) {
        role = explicit.toLowerCase();
      } else if (NATIVE_LANDMARK[el.tagName]) {
        role = NATIVE_LANDMARK[el.tagName];
      }
      if (!role) return;
      const name = accessibleName(el);
      const ord = (landmarkCounts[role] = (landmarkCounts[role] || 0) + 1) - 1;
      const path = axPath(el);
      const handle = "l" + shortHash(`${role}|${name}|${path}|${ord}`);
      landmarks.push({ handle, role, name });
    });

    // Second pass: interactables.
    let truncated = false;
    const roleOrdCounts = Object.create(null);

    for (const el of allElements) {
      if (handles.length >= maxHandles) { truncated = true; break; }
      const role = getComputedRole(el);
      if (!role || !INTERACTABLE_ROLES.has(role)) continue;

      const rect = el.getBoundingClientRect();
      const visible = isVisible(el);
      if (viewportOnly) {
        // Keep if any part intersects viewport. (rect coordinates are viewport-relative.)
        const inVp = rect.bottom > 0 && rect.top < vh && rect.right > 0 && rect.left < vw;
        if (!inVp && !visible) continue;
      }

      const name = accessibleName(el);
      const key = `${role}|${name}`;
      const ordinal = (roleOrdCounts[key] = (roleOrdCounts[key] || 0) + 1) - 1;
      const path = axPath(el);
      let handle = "h" + shortHash(`${role}|${name}|${path}|${ordinal}`);
      // Ordinal-bump on collision.
      while (seen.has(handle)) {
        handle = "h" + shortHash(`${role}|${name}|${path}|${ordinal}|${seen.get(handle)}`);
        seen.set(handle, (seen.get(handle) || 0) + 1);
      }
      seen.set(handle, 0);

      const isForm = role === "form";
      if (isForm) formHandles.set(el, handle);

      // Password marker.
      const placeholder = trunc(el.getAttribute ? (el.getAttribute("placeholder") || "") : "");
      const nativeType = (el.tagName === "INPUT" && el.getAttribute) ? (el.getAttribute("type") || "").toLowerCase() : "";
      const labelText = isInputElement(el) ? fieldLabel(el) : null;
      const wfPasswordMarker = nativeType === "password" ? "__wf_password__" : null;

      handles.push({
        handle,
        role,
        name,
        value: isInputElement(el) ? trunc(el.value || "") : null,
        label: wfPasswordMarker || labelText,
        placeholder,
        required: !!(el.required || (el.getAttribute && el.getAttribute("aria-required") === "true")),
        disabled: !!(el.disabled || (el.getAttribute && el.getAttribute("aria-disabled") === "true")),
        checked:
          el.type === "checkbox" || el.type === "radio" ? !!el.checked :
          (el.getAttribute && el.getAttribute("aria-checked")) === "true" ? true :
          (el.getAttribute && el.getAttribute("aria-checked")) === "false" ? false :
          null,
        editable: isInputElement(el) && !el.disabled && !el.readOnly && nativeType !== "submit" && nativeType !== "button" && nativeType !== "reset",
        in_form: findOwningForm(el, formHandles),
        landmark: findLandmark(el),
        ordinal,
        bbox: bboxOf(el),
      });
    }

    // Third pass: readable text blocks (not interactable).
    const textCounts = Object.create(null);
    allElements.forEach((el) => {
      if (!TEXT_TAGS.has(el.tagName)) return;
      // Skip if the element is inside an interactable or is itself one.
      if (INTERACTABLE_ROLES.has(getComputedRole(el) || "")) return;
      const text = trunc(el.innerText || el.textContent || "", 600);
      if (!text) return;
      const tag = el.tagName.toLowerCase();
      const ord = (textCounts[tag] = (textCounts[tag] || 0) + 1) - 1;
      const handle = "t" + shortHash(`${tag}|${text}|${axPath(el)}|${ord}`);
      textBlocks.push({ handle, tag, text, landmark: findLandmark(el) });
    });

    // Fingerprint over all interactable handles PLUS text-block handles. We
    // sort first so two snapshots of the same page are equal regardless of
    // tiny reorderings. Text blocks are included so that adding a paragraph
    // or list item changes the fingerprint even when no new interactable
    // appears (common for dynamic content and chat-style UIs).
    const sig = []
      .concat(handles.map((h) => `h:${h.handle}:${h.role}:${h.name}`))
      .concat(textBlocks.map((t) => `t:${t.handle}:${t.tag}`))
      .sort();
    const joined = sig.join("|");
    const fingerprint = shortHash(joined) + shortHash(joined + "|salt");

    return {
      url: location.href,
      title: document.title || "",
      handles, landmarks, text_blocks: textBlocks,
      fingerprint, truncated,
    };
  };

  window.__wayfinder__ = { snapshot, version: 1 };
})();
