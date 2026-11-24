/* Papigator Web-Frontend – vanilla JS, kein Build-Schritt */
const $ = (s) => document.querySelector(s);
const PAGE = 200;
const state = { me: null, docs: [], tags: [], users: [], q: "", tag: null, year: null, review: false, reviewCount: 0, due: false, dueCount: null, warranty: false, warrantyCount: null, sender: null, senders: [], trash: false, trashCount: 0, trashDays: 30, scope: "mine",
                selected: new Set(), current: null, poll: null, limit: PAGE, progressStart: null,
                sort: localStorage.getItem("bl.sort") || "date", dir: localStorage.getItem("bl.dir") || "desc",
                size: localStorage.getItem("bl.size") || "m", mode: startMode(), views: [],
                smart: null };   // Suche in Sätzen: { chips, dropped } vom Server (siehe smartSearch)
// Nach dem Neuladen im selben Ordner weitermachen, damit „Zurück“ zu den Einträgen im Verlauf passt
if (state.mode === "folders" && history.state && "folder" in history.state) state.tag = history.state.folder;
if (state.sort.includes("_")) state.sort = "date";  // alte gespeicherte Werte

// ---------- API ----------
async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: "same-origin", ...opts });
  if (r.status === 401 && !path.startsWith("/auth/")) { showLogin(); throw new Error("401"); }
  if (!r.ok) {
    let detail = null;
    try { detail = (await r.json()).detail; } catch {}
    const msg = typeof detail === "string" ? detail : (detail && detail.message) || r.statusText;
    const err = new Error(msg); err.status = r.status; err.detail = detail;
    throw err;
  }
  return r.status === 204 ? null : r.json();
}
// Startansicht: fest Heute/Kacheln/Ordner oder die zuletzt benutzte (Einstellungen → Darstellung); neu: Heute
function startMode() {   // läuft schon beim Anlegen von state (oben) – deshalb die Liste hier drin
  const MODES = ["today", "tiles", "folders", "money"];
  const s = localStorage.getItem("bl.start"), last = localStorage.getItem("bl.mode");
  return MODES.includes(s) ? s : MODES.includes(last) ? last : "today";
}
const json = (method, body, extra = {}) =>
  ({ method, headers: { "content-type": "application/json", ...extra }, body: JSON.stringify(body) });

// ---------- Screens ----------
function show(id) {
  for (const s of document.querySelectorAll(".screen")) s.classList.add("hidden");
  $(id).classList.remove("hidden");
}
function showLogin() { layers.length = 0; show("#login"); $("#login-user").focus(); }

// ---------- Browser-Zurück für Ebenen (Einstellungen, Fragen, Dialoge) ----------
// Alles, was sich wie eine eigene Seite oder ein Fenster über die Übersicht legt, bekommt einen
// Verlaufseintrag: Browser-Zurück (Taste, Maus, Wischgeste) schließt die oberste Ebene. Schließt man sie
// selbst (Knopf, Esc), wird ihr Eintrag mit history.go() zurückgenommen, damit Zurück danach nicht ins
// Leere führt. Dokument, Ordner und Papierkorb haben eigene Einträge (siehe dort).
const layers = [];          // offene Ebenen, die oberste zuletzt: { name, close }
let skipLayerPops = 0;
function openLayer(name, close) {
  if (layers.length && layers[layers.length - 1].name === name) return;   // z. B. Einstellungen neu laden
  layers.push({ name, close });
  const { id, ...keep } = history.state || {};   // Ordner/Papierkorb mitnehmen, das Dokument nicht
  history.pushState({ ...keep, layer: name, depth: layers.length }, "");
}
function closeLayer(name) {
  const i = layers.map((l) => l.name).lastIndexOf(name);
  if (i < 0) return;
  const n = layers.length - i;
  layers.splice(i);
  skipLayerPops++; history.go(-n);
}
window.addEventListener("popstate", (e) => {
  if (skipLayerPops) { skipLayerPops--; return; }
  const depth = e.state && e.state.layer ? e.state.depth : 0;
  while (layers.length > depth) layers.pop().close();
});
// Dialoge: beim Öffnen eine Ebene, beim Schließen (Knopf, Esc) wieder weg
function showDialog(d) {
  d._byHistory = false;
  d.showModal();
  // Manche Browser schließen den Dialog bei Zurück schon selbst – dann nichts mehr tun (sonst bliebe der Merker hängen)
  openLayer(d.id, () => { if (d.open) { d._byHistory = true; d.close(); } });
}
for (const d of document.querySelectorAll("dialog"))
  d.addEventListener("close", () => { if (d._byHistory) d._byHistory = false; else closeLayer(d.id); });
function toast(msg, ms = 2500, action = null) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  if (action) {
    const b = document.createElement("button"); b.className = "toast-act"; b.textContent = action.label;
    b.onclick = () => { t.classList.add("hidden"); action.fn(); };
    t.appendChild(b);
  }
  // Ein offener Dialog liegt in der obersten Ebene über allem – die Meldung muss dann in ihn hinein
  const host = document.querySelector("dialog[open]") || document.body;
  if (t.parentElement !== host) host.appendChild(t);
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.add("hidden"), ms);
}

// ---------- Login / Setup ----------
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/auth/login", json("POST", { username: $("#login-user").value, password: $("#login-pass").value, label: "Web" }));
    $("#login-error").classList.add("hidden");
    $("#login-pass").value = "";
    enterMain();
  } catch (err) { $("#login-error").textContent = err.message; $("#login-error").classList.remove("hidden"); }
});
$("#to-setup").onclick = (e) => { e.preventDefault(); show("#setup"); };
$("#to-login").onclick = (e) => { e.preventDefault(); showLogin(); };

$("#setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/users", json("POST", {
      username: $("#setup-user").value, display_name: $("#setup-name").value, password: $("#setup-pass").value, is_admin: true,
    }, { Authorization: "Bearer " + $("#setup-token").value.trim() }));
    await api("/auth/login", json("POST", { username: $("#setup-user").value, password: $("#setup-pass").value, label: "Web" }));
    toast("Nutzer angelegt");
    enterMain();
  } catch (err) { $("#setup-error").textContent = err.message; $("#setup-error").classList.remove("hidden"); }
});

// ---------- Einladung annehmen (/app/#invite=<token>) ----------
// Der Token steht hinter „#“ und geht deshalb nie im Seitenaufruf an den Server. Nach dem Einlösen (oder wenn
// die Einladung nicht mehr gilt) kommt er aus Adresszeile und Verlauf wieder heraus.
let inviteToken = null;
function inviteFromHash() {
  const m = location.hash.match(/^#invite=([A-Za-z0-9_-]{20,200})$/);
  return m ? m[1] : null;
}
function clearInviteHash() {
  if (location.hash.startsWith("#invite=")) history.replaceState(history.state, "", location.pathname + location.search);
}
function inviteFailed(msg) {
  inviteToken = null; clearInviteHash();
  $("#inv-fields").classList.add("hidden");
  $("#inv-title").textContent = "Einladung";
  $("#inv-sub").textContent = msg;
}
async function showInvite(token) {
  inviteToken = token;
  layers.length = 0; show("#invite");
  $("#inv-fields").classList.add("hidden"); $("#inv-error").classList.add("hidden");
  $("#inv-title").textContent = "Einladung"; $("#inv-sub").textContent = "Einladung wird geprüft …";
  let r;
  try { r = await fetch(`/invites/${encodeURIComponent(token)}`, { credentials: "omit" }); }
  catch { $("#inv-sub").textContent = "Keine Verbindung zum Server – bitte später noch einmal öffnen."; return; }
  if (!r.ok) {
    let detail = ""; try { detail = (await r.json()).detail || ""; } catch {}
    if (r.status === 429) { $("#inv-sub").textContent = detail || "Zu viele Versuche – bitte später noch einmal."; return; }
    inviteFailed(r.status === 404 ? "Diese Einladung gibt es nicht (mehr) – vielleicht wurde sie zurückgezogen. Bitte um eine neue." : `${detail || "Diese Einladung gilt nicht mehr"}. Bitte um eine neue.`);
    return;
  }
  const info = await r.json();
  $("#inv-title").textContent = `${info.display_name}, du wurdest zu Papigator eingeladen`;
  $("#inv-sub").textContent = `Server: ${info.server_name}. Wähle einen Benutzernamen und ein Passwort – damit meldest du dich später auch in der App an.`;
  $("#inv-fields").classList.remove("hidden");
  $("#inv-user").focus();
}
$("#invite-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!inviteToken) return;
  try {
    await api(`/invites/${encodeURIComponent(inviteToken)}/redeem`, json("POST", {
      username: $("#inv-user").value.trim(), password: $("#inv-pass").value, label: "Web" }));
    $("#inv-pass").value = ""; inviteToken = null; clearInviteHash();
    toast("Willkommen bei Papigator!");
    enterMain();
  } catch (err) {
    if (err.status === 404 || err.status === 410) { inviteFailed(`${err.message}. Bitte um eine neue Einladung.`); return; }
    $("#inv-error").textContent = err.message; $("#inv-error").classList.remove("hidden");
  }
});
$("#inv-to-login").onclick = (e) => { e.preventDefault(); inviteToken = null; clearInviteHash(); showLogin(); };
// Link in einen offenen Tab eingefügt: Seite lädt nicht neu, nur der Hash ändert sich
window.addEventListener("hashchange", () => { const t = inviteFromHash(); if (t) showInvite(t); });

// ---------- Main ----------
let settingsGen = 0;   // zählt openSettings hoch (siehe enterMain)
async function enterMain() {
  const gen = settingsGen;
  state.me = await api("/auth/me");
  state.scope = state.me.show_partner_in_overview ? "all" : "mine";
  // Nutzer, denen man Dokumente zuordnen darf (ich selbst, Partner mit Schreibrecht, als Admin alle)
  state.owners = await api("/documents/owners").catch(() => []);
  renderOwnerSelects();
  // Inzwischen wieder in die Einstellungen (Zurück, dann gleich wieder ⚙): nicht nachträglich überdecken –
  // beim Verlassen der Einstellungen kommt ohnehin ein neues enterMain
  if (gen !== settingsGen) return;
  show("#main");
  $("#demo-banner").classList.toggle("hidden", !state.me.demo);
  renderHello();
  renderScopeBar();
  applyMode();
  await Promise.all([loadTags(), loadYears(), loadDocs(), loadViews()]);
  startPolling();
}

/// „Guten Abend, Alex“ – nur der Vorname (wie „Heute“ in der App); darüber das Datum
function renderHello() {
  const now = new Date(), h = now.getHours();
  const first = (state.me?.display_name || state.me?.username || "").trim().split(/\s+/)[0];
  const hi = h >= 5 && h < 11 ? "Guten Morgen" : h >= 11 && h < 18 ? "Guten Tag" : "Guten Abend";
  $("#hello-title").textContent = first ? `${hi}, ${first}` : hi;
  $("#hello-date").textContent = now.toLocaleDateString("de-DE", { weekday: "long", day: "numeric", month: "long" });
}

async function loadYears() {
  state.years = await api("/documents/years?scope=" + state.scope);
  if (state.year && !state.years.some((y) => y.year === state.year)) state.year = null;
  renderTagBar();
}

function renderScopeBar() {
  const hasPartner = state.me.shared_with_me.length > 0;
  $("#scopebar").classList.toggle("hidden", !hasPartner);
  if (!hasPartner) { state.scope = "mine"; return; }
  const seg = $("#scope-seg"); seg.innerHTML = "";
  const opts = state.me.show_partner_in_overview
    ? [["all", "Alle"], ["mine", "Meine"], ["partner", "Partner"]]
    : [["mine", "Meine"], ["partner", "Partner"]];
  for (const [val, label] of opts) {
    const b = document.createElement("button");
    b.textContent = label; b.className = state.scope === val ? "active" : "";
    b.onclick = () => { state.scope = val; state.tag = null; state.year = null; state.review = false; state.due = false; state.warranty = false; state.sender = null; renderScopeBar(); loadTags(); loadYears(); loadDocs(); };
    seg.appendChild(b);
  }
}

async function loadTags() {
  let tc;
  [state.tags, { count: state.reviewCount }, tc, state.dueCount, state.warrantyCount] = await Promise.all([
    api("/tags?scope=" + state.scope), api("/documents/review_count?scope=" + state.scope), api("/trash/count"),
    api("/documents/due_count?scope=" + state.scope).catch(() => ({ count: 0, overdue: 0 })),
    api("/documents/warranty_count?scope=" + state.scope).catch(() => ({ count: 0, soon: 0 }))]);
  state.trashCount = tc.count; state.trashDays = tc.days;

  renderTagBar();
  renderFolderView();
  $("#tag-list").innerHTML = state.tags.map((t) => `<option value="${esc(t.name)}">`).join("");
}

/* Eine Filterzeile wie in der App: „Alle“ nur bei aktivem Filter, „Zu prüfen“, Tag ▾ und Jahr ▾.
   Die Auswahl passiert in einem Popover; ein aktiver Filter wird zum gefüllten Chip mit ✕. */
function renderTagBar() {
  renderSmartBar();
  const bar = $("#tagbar"); bar.innerHTML = "";
  // Während einer Suche in Sätzen stehen ihre Chips oben; die Filter hier bleiben gemerkt und kommen nach dem Leeren zurück
  bar.classList.toggle("hidden", smartOn());
  // mit Dokumenten – oder ganz unbenutzt (gerade als Ordner angelegt), sonst fänden sich neue Ordner hier nicht
  const used = (state.tags || []).filter((t) => t.count || t.unused).sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  const years = state.years || [];
  // Ordner-Ansicht = blättern und im Ordner suchen; Filter (Fristen, Tag, Jahr, Absender) gehören zu den Kacheln.
  // „Zu prüfen“ und „Papierkorb“ bleiben: das sind Orte, keine Filter.
  // „Heute“ zeigt wie die Ordner nur die Orte – gleiche Leiste, damit beim Umschalten nichts springt
  const folders = state.mode === "folders" || todayBrowsing() || moneyBrowsing();
  $("#search").placeholder = state.mode === "money" ? "Buchungen suchen, z. B. Amazon September 2026 …"
    : folders && state.tag && !state.trash ? `In „${state.tag}“ suchen …` : "Suchen …";

  if (folders && state.tag && state.q && !state.trash && !state.review) {
    bar.appendChild(chip(`In „${state.tag}“`, !state.searchAll, () => { state.searchAll = false; renderTagBar(); loadDocs(); }));
    bar.appendChild(chip("Überall", !!state.searchAll, () => { state.searchAll = true; renderTagBar(); loadDocs(); }));
  }
  if (!folders && (state.tag || state.year || state.review || state.due || state.warranty || state.sender)) {
    bar.appendChild(chip("Alle", false, () => { state.tag = null; state.year = null; state.review = false; state.due = false; state.warranty = false; state.sender = null; renderTagBar(); loadDocs(); }));
  }
  if (state.reviewCount || state.review) {
    const r = chip(`Zu prüfen · ${state.reviewCount}`, state.review, () => { state.review = !state.review; state.due = false; state.warranty = false; state.tag = null; renderTagBar(); loadDocs(); }, "#ffb347");
    r.classList.add("review"); bar.appendChild(r);
  }
  // Fristen: überfällig oder in den nächsten 30 Tagen fällig
  const dc = state.dueCount || { count: 0, overdue: 0 };
  if (!folders && (dc.count || state.due)) {
    const f = chip(`Fristen · ${dc.count}${dc.overdue ? ` (${dc.overdue} überfällig)` : ""}`, state.due,
      () => { state.due = !state.due; state.warranty = false; state.review = false; state.tag = null; renderTagBar(); loadDocs(); }, dc.overdue ? "#e5484d" : "#ffb347");
    f.classList.add("review"); bar.appendChild(f);
  }
  // Garantie: Käufe, deren Gewährleistung oder Garantie noch läuft (nach Ende sortiert)
  const wc = state.warrantyCount || { count: 0, soon: 0 };
  if (!folders && (wc.count || state.warranty)) {
    const w = chip(`Garantie · ${wc.count}${wc.soon ? ` (${wc.soon} endet bald)` : ""}`, state.warranty,
      () => { state.warranty = !state.warranty; state.due = false; state.review = false; state.tag = null; renderTagBar(); loadDocs(); }, "#3ba7a0");
    w.classList.add("review"); bar.appendChild(w);
  }
  if (folders) {
    // keine weiteren Filter
  } else if (state.tag) {
    const cur = state.tags.find((t) => t.name === state.tag);
    const c = chip(state.tag, true, () => { state.tag = null; renderTagBar(); loadDocs(); }, tagColor(state.tag, cur?.color));
    c.appendChild(closeX()); bar.appendChild(c);
  } else if (used.length) {
    const c = chip("Tag", false, (e) => openTagPopover(e.currentTarget, used)); c.appendChild(caret()); bar.appendChild(c);
  }
  if (folders) {
    // –
  } else if (state.year) {
    const c = chip(String(state.year), true, () => { state.year = null; renderTagBar(); loadDocs(); });
    c.appendChild(closeX()); bar.appendChild(c);
  } else if (years.length > 1) {
    const c = chip("Jahr", false, (e) => openYearPopover(e.currentTarget, years)); c.appendChild(caret()); bar.appendChild(c);
  }
  if (folders) {
    // –
  } else if (state.sender) {
    const c = chip(state.sender, true, () => { state.sender = null; renderTagBar(); loadDocs(); });
    c.appendChild(closeX()); bar.appendChild(c);
  } else if (!state.trash) {
    const c = chip("Absender", false, (e) => openSenderPopover(e.currentTarget)); c.appendChild(caret()); bar.appendChild(c);
  }
  if (!folders && !state.trash && (state.views.length || Object.keys(currentViewParams()).some((k) => k !== "scope"))) {
    const c = chip(state.views.length ? `Gespeichert · ${state.views.length}` : "Suche speichern", false, (e) => openViewsPopover(e.currentTarget));
    c.appendChild(caret()); bar.appendChild(c);
  }
  const t = chip(state.trashCount ? `Papierkorb · ${state.trashCount}` : "Papierkorb", state.trash, () => openTrash(!state.trash));
  t.classList.add("more"); t.style.marginLeft = "auto";
  t.insertBefore(trashIcon(), t.firstChild);
  bar.appendChild(t);
}
function caret() {
  const s = document.createElement("span"); s.className = "caret";
  s.innerHTML = `<svg width="10" height="10" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  return s;
}
function closeX() {
  const s = document.createElement("span"); s.className = "x";
  s.innerHTML = `<svg width="10" height="10" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"/></svg>`;
  return s;
}

/* Popover unter einem Chip; schließt bei Klick daneben oder Escape */
function openPopover(anchor, build) {
  const pop = $("#popover");
  if (!pop.classList.contains("hidden") && pop.dataset.anchor === anchor.textContent) { closePopover(); return; }
  pop.innerHTML = ""; pop.dataset.anchor = anchor.textContent; build(pop);
  pop.classList.remove("hidden");
  const r = anchor.getBoundingClientRect();
  pop.style.top = `${r.bottom + window.scrollY + 6}px`;
  pop.style.left = `${Math.max(8, Math.min(r.left + window.scrollX, window.innerWidth - pop.offsetWidth - 8))}px`;
  setTimeout(() => {
    document.addEventListener("click", onDocClick); document.addEventListener("keydown", onKey);
  }, 0);
  function onDocClick(e) { if (!pop.contains(e.target)) closePopover(); }
  function onKey(e) { if (e.key === "Escape") closePopover(); }
  function closePopover() {
    pop.classList.add("hidden"); pop.dataset.anchor = "";
    document.removeEventListener("click", onDocClick); document.removeEventListener("keydown", onKey);
  }
  pop._close = closePopover;
}
function closePopover() { $("#popover")._close?.(); }

function openTagPopover(anchor, used) {
  openPopover(anchor, (pop) => {
    const input = document.createElement("input");
    input.type = "search"; input.placeholder = "Tag suchen …"; input.className = "pop-search"; input.autocomplete = "off";
    const list = document.createElement("div"); list.className = "pop-chips";
    const render = () => {
      const q = input.value.trim().toLowerCase(); list.innerHTML = "";
      const show = used.filter((t) => !q || t.name.toLowerCase().includes(q));
      if (!show.length) list.innerHTML = `<span class="muted small">Kein Tag passt.</span>`;
      for (const t of show) {
        const c = chip(t.name, state.tag === t.name, () => { state.tag = t.name; state.review = false; closePopover(); renderTagBar(); loadDocs(); }, tagColor(t.name, t.color));
        const cnt = document.createElement("span"); cnt.className = "cnt"; cnt.textContent = t.count; c.appendChild(cnt);
        list.appendChild(c);
      }
    };
    input.oninput = render;
    input.onkeydown = (e) => { if (e.key === "Enter") { const first = list.querySelector(".chip"); if (first) first.click(); } };
    if (used.length > 10) pop.appendChild(input);
    pop.appendChild(list); render();
    if (used.length > 10) setTimeout(() => input.focus(), 0);
  });
}

async function openSenderPopover(anchor) {
  const senders = await api("/senders?scope=" + state.scope).catch(() => []);
  openPopover(anchor, (pop) => {
    const input = document.createElement("input");
    input.type = "search"; input.placeholder = "Absender suchen …"; input.className = "pop-search"; input.autocomplete = "off";
    const list = document.createElement("div"); list.className = "pop-chips";
    const render = () => {
      const q = input.value.trim().toLowerCase(); list.innerHTML = "";
      const show = senders.filter((s) => !q || s.name.toLowerCase().includes(q)).slice(0, 60);
      if (!show.length) list.innerHTML = `<span class="muted small">Kein Absender.</span>`;
      for (const s of show) {
        const c = chip(s.name, state.sender === s.name, () => { state.sender = s.name; state.review = false; state.due = false; state.warranty = false; closePopover(); renderTagBar(); loadDocs(); });
        const cnt = document.createElement("span"); cnt.className = "cnt"; cnt.textContent = s.count; c.appendChild(cnt);
        list.appendChild(c);
      }
    };
    input.oninput = render;
    if (senders.length > 8) pop.appendChild(input);
    pop.appendChild(list); render();
    if (senders.length > 8) setTimeout(() => input.focus(), 0);
  });
}

function openYearPopover(anchor, years) {
  openPopover(anchor, (pop) => {
    const list = document.createElement("div"); list.className = "pop-chips";
    for (const y of years) {
      const c = chip(String(y.year), state.year === y.year, () => { state.year = y.year; closePopover(); renderTagBar(); loadDocs(); });
      const cnt = document.createElement("span"); cnt.className = "cnt"; cnt.textContent = y.count; c.appendChild(cnt);
      list.appendChild(c);
    }
    pop.appendChild(list);
  });
}
function trashIcon() {
  const w = document.createElement("span"); w.style.display = "inline-flex";
  w.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  return w;
}
// Der Papierkorb wirkt wie eine eigene Seite – also mit Verlaufseintrag: Browser-Zurück schließt ihn
// (und führt in den Ordner, aus dem man kam). fromHistory: Aufruf aus popstate, nicht selbst im Verlauf blättern.
let skipTrashPop = false;
function openTrash(on, fromHistory = false, tag = null) {
  if (!fromHistory) {
    if (on && !state.trash) history.pushState({ trash: true }, "");
    else if (!on && state.trash && history.state && history.state.trash) { skipTrashPop = true; history.back(); }
  }
  state.trash = on; state.tag = tag; state.review = false; state.due = false; state.warranty = false; state.sender = null; state.selected.clear();
  $("#selection-bar").classList.add("hidden");
  $("#trash-bar").classList.toggle("hidden", !on);
  $("#trash-empty-btn").classList.toggle("hidden", !!state.me?.demo);   // Demo: nichts endgültig löschen
  $("#trash-info").textContent = on ? `Papierkorb – wird nach ${state.trashDays} Tagen automatisch geleert` : "";
  $("#restore-selected-btn").classList.toggle("hidden", !on);
  $("#merge-btn").classList.toggle("hidden", on);
  $("#share-selected-btn").classList.toggle("hidden", on);
  loadDocs();
}
$("#trash-empty-btn").onclick = async () => {
  if (!confirm(`${state.trashCount} Dokument(e) endgültig löschen?`)) return;
  await api("/trash", { method: "DELETE" }); toast("Papierkorb geleert"); openTrash(false); loadTags();
};
$("#restore-selected-btn").onclick = async () => {
  for (const id of [...state.selected]) await api(`/documents/${id}/restore`, { method: "POST" }).catch(() => {});
  state.selected.clear(); $("#selection-bar").classList.add("hidden"); toast("Wiederhergestellt");
  await Promise.all([loadDocs(), loadTags(), loadYears()]);
};

function chip(label, active, onClick, color) {
  const c = document.createElement("span");
  c.className = "chip" + (active ? " active" : "");
  if (color) { c.style.setProperty("--tag", color); c.innerHTML = `<span class="dot"></span>`; }
  c.appendChild(document.createTextNode(label)); c.onclick = onClick;
  return c;
}

let docsSeq = 0;   // nur die zuletzt angeforderte Liste zeigen (schnelles Tippen, Polling, Satz-Suche)
async function loadDocs() {
  const seq = ++docsSeq;
  if ((folderBrowsing() && !state.tag) || todayBrowsing() || moneyBrowsing()) {   // Ordner-Übersicht, Heute, Geld: keine Kacheln
    state.docs = [];
    $("#more-btn").classList.add("hidden");
    renderGrid(); renderTagBar(); renderFolderView(); renderToday(); renderMoney(); renderBankHint();
    if (todayBrowsing() || moneyBrowsing()) $("#empty").classList.add("hidden");
    return;
  }
  const p = new URLSearchParams({ limit: state.limit, sort: state.sort, dir: state.dir });
  if (smartOn()) {
    // Suche in Sätzen: nur die Chips zählen – Sortierung und „Mehr laden“ wie sonst
    for (const [k, v] of Object.entries(smartParams())) p.set(k, v);
  } else {
    p.set("scope", state.scope);
    if (state.q && !state.smart) p.set("q", state.q);   // im Papierkorb gilt ein Satz nicht als Suchwort
    if (state.tag && !(state.mode === "folders" && state.q && state.searchAll)) p.set("tag", state.tag);
    if (state.year) p.set("year", state.year);
    if (state.review) p.set("review", "true");
    if (state.due) p.set("due", "true");
    if (state.warranty) p.set("warranty", "true");
    if (state.sender) p.set("sender", state.sender);
    if (state.trash) p.set("trash", "true");
  }
  const docs = await api("/documents?" + p);
  if (seq !== docsSeq) return;
  state.docs = docs;
  $("#more-btn").classList.toggle("hidden", state.docs.length < state.limit);
  renderGrid();
  renderTagBar();
  renderFolderView();
  renderToday();
  renderMoney();
  renderBankHint();
}
// Zur Dokumentsuche passende Buchungen: eine Zeile „Im Konto: 7 Buchungen · 214,30 €“, Klick öffnet sie in „Geld“
let bankHintSeq = 0;
async function renderBankHint() {
  const el = $("#bank-hint"), text = state.q && !state.trash && state.mode !== "money" ? state.q.trim() : "";
  const seq = ++bankHintSeq;
  if (!text) { el.classList.add("hidden"); return; }
  const r = await api("/bank/search?" + new URLSearchParams({ q: text })).catch(() => null);
  if (seq !== bankHintSeq) return;
  if (!r || !r.count) { el.classList.add("hidden"); return; }
  const when = r.range ? ` · ${fmtDate(r.range.from)} – ${fmtDate(r.range.to)}` : "";
  el.innerHTML = `<span style="flex:1">Im Konto: <b>${r.count === 1 ? "1 Buchung" : `${r.count} Buchungen`} · ${eurC(r.cents)}</b>${esc(when)}</span><span aria-hidden="true">›</span>`;
  el.dataset.q = text;
  el.classList.remove("hidden");
}
$("#bank-hint").onclick = () => {
  const q = $("#bank-hint").dataset.q || "";
  $("#search").value = q;
  $('#mode-seg button[data-mode="money"]').click();
};

// ---------- Ordner (Tags in Tags) ----------
// Ordner-Modus ohne aktive Suche/Filter: state.tag ist der geöffnete Ordner, null = oberste Ebene.
function folderBrowsing() {
  return state.mode === "folders" && !state.q && !state.smart && !state.year && !state.review && !state.due && !state.warranty && !state.sender && !state.trash;
}
function applyMode() {
  for (const b of document.querySelectorAll("#mode-seg button")) b.classList.toggle("active", b.dataset.mode === state.mode);
}
for (const b of document.querySelectorAll("#mode-seg button"))
  b.onclick = () => {
    const leaving = state.mode;
    state.mode = b.dataset.mode; localStorage.setItem("bl.mode", state.mode); applyMode();
    // Suche mitnehmen: in „Geld“ sucht das Feld die Buchungen, sonst die Dokumente
    if (state.mode === "money" && leaving !== "money") {
      money.q = $("#search").value.trim(); money.search = null;
      if (state.q || state.smart) { state.q = ""; state.smart = null; smartSeq++; renderSmartBar(); }
    } else if (leaving === "money" && state.mode !== "money" && money.q) {
      money.q = ""; $("#search").value = "";
    }
    state.tag = null;   // der geöffnete Ordner gehört zur Ordner-Ansicht, nicht als Filter in die Kacheln
    if (state.mode !== "tiles") { state.year = null; state.due = false; state.warranty = false; state.sender = null; }
    if (state.mode === "today" || state.mode === "money") state.review = false;   // „Zu prüfen“ bleibt in den Ordnern ein Ort, in „Heute“ ist es die Karte
    state.searchAll = false;
    loadDocs();
  };
function tagChildren() {
  const kids = new Map();
  for (const t of state.tags || []) {
    const k = t.parent_id ?? null;
    if (!kids.has(k)) kids.set(k, []);
    kids.get(k).push(t);
  }
  return kids;
}
function tagPath(name) {
  const byId = new Map((state.tags || []).map((t) => [t.id, t]));
  let t = (state.tags || []).find((x) => x.name === name), out = [], seen = new Set();
  while (t && !seen.has(t.id)) { seen.add(t.id); out.unshift(t); t = byId.get(t.parent_id); }
  return out;
}
const FOLDER_SVG = `<svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>`;
const SMART_SVG = `<svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z" fill="currentColor"/></svg>`;
// Passendes Symbol nach dem Ordnernamen (wie FolderTile.symbol in der App); Spezielles zuerst
// („Autoversicherung“ → Schild, „Hausarzt“ → Herz). Deutsche Wortteile irgendwo im Namen, englische nur als ganzes Wort.
const FOLDER_ICONS = [
  [["versicherung"], ["insurance"], `<path d="M12 3l7 4v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V7l7-4z"/>`],
  [["steuer", "finanzamt"], ["tax", "taxes"], `<path d="M3 10l9-6 9 6M5 10v8M9.5 10v8M14.5 10v8M19 10v8M3 20.5h18"/>`],
  [["bank", "konto", "kredit"], ["credit"], `<rect x="3" y="5.5" width="18" height="13" rx="2.5"/><path d="M3 10h18M7 15h4"/>`],
  [["gesundheit", "arzt", "ärzt", "kranken", "zahn", "medizin"], ["health", "doctor", "medical"], `<path d="M12 20s-7-4.5-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.5-7 10-7 10z"/>`],
  [["schule", "kita", "kindergarten"], ["school"], `<path d="M3 10l9-6 9 6-9 6-9-6zM7 12.5V17c3 2 7 2 10 0v-4.5"/>`],
  [["auto", "kfz", "fahrzeug"], ["car", "cars"], `<path d="M5 16h14l-1.5-5h-11L5 16zM7 16v3M17 16v3M8 11l1.5-4h5L16 11"/>`],
  [["arbeit", "gehalt", "lohn", "beruf"], ["job", "work", "salary"], `<rect x="3" y="6" width="18" height="13" rx="3"/><path d="M8 6V4h8v2"/>`],
  [["haus", "wohnung", "miete"], ["house", "home"], `<path d="M4 21V9l8-6 8 6v12M9 21v-6h6v6"/>`],
];
function folderIcon(name) {
  const n = String(name || "").toLowerCase();
  const words = new Set(n.split(/[^\p{L}]+/u));
  const hit = FOLDER_ICONS.find(([de, en]) => de.some((w) => n.includes(w)) || en.some((w) => words.has(w)));
  if (!hit) return FOLDER_SVG;
  return `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${hit[2]}</svg>`;
}
function renderFolderView() {
  const box = $("#folderview");
  const on = folderBrowsing();
  box.classList.toggle("hidden", !on);
  if (!on) return;
  const kids = tagChildren();
  // Ein Ordner zählt, wenn er selbst oder ein Unterordner Dokumente hat
  const total = new Map();
  const sum = (t, seen = new Set()) => {
    if (total.has(t.id)) return total.get(t.id);
    if (seen.has(t.id)) return 0; seen.add(t.id);
    const n = (t.count || 0) + (kids.get(t.id) || []).reduce((a, c) => a + sum(c, seen), 0);
    total.set(t.id, n); return n;
  };
  // Sichtbar: mit Dokumenten darin oder darunter – oder ganz unbenutzt (frisch über „Neuer Ordner“ angelegt)
  const shown = (t, seen = new Set()) => {
    if (seen.has(t.id)) return false; seen.add(t.id);
    return sum(t) > 0 || !!t.unused || (kids.get(t.id) || []).some((c) => shown(c, seen));
  };
  const cur = state.tag ? (state.tags || []).find((t) => t.name === state.tag) : null;
  const subs = (kids.get(cur ? cur.id : null) || []).filter((t) => shown(t)).sort((a, b) => a.name.localeCompare(b.name, "de"));
  let html = `<div class="crumbs">` + (cur ? `<button class="btn ghost up" id="folder-up" title="Eine Ebene höher">‹ Zurück</button>` : "") +
    (cur ? `<a data-folder="">Alle Ordner</a>` : `<b>Alle Ordner</b>`);
  if (cur) {
    const path = tagPath(cur.name);
    path.forEach((t, i) => { html += ` <span>›</span> ` + (i < path.length - 1 ? `<a data-folder="${esc(t.name)}">${esc(t.name)}</a>` : `<b>${esc(t.name)}</b>`); });
  }
  html += `<button class="btn ghost newf" id="folder-new" title="${cur ? `Unterordner in „${esc(cur.name)}“ anlegen` : "Ordner auf oberster Ebene anlegen"}">+ Neuer Ordner</button>`;
  if (cur) html += `<a class="btn ghost" id="folder-dl" href="/export/tag.zip?tag=${enc(cur.name)}&scope=${enc(state.scope)}" download title="Als ZIP mit Unterordnern nach den übrigen Tags und Inhaltsverzeichnis – zum Weitergeben">Herunterladen</a>`;
  if (cur && state.me?.is_admin) html += `<button class="btn ghost icon-btn" id="folder-rename" title="Ordner umbenennen" aria-label="Ordner umbenennen"><svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4L19 9l-4-4L4 16zM14 6l4 4" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg></button>` +
    `<button class="btn ghost icon-btn danger" id="folder-delete" title="Ordner löschen" aria-label="Ordner löschen"><svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg></button>`;
  if (cur) html += `<button class="btn share" data-share-folder="${esc(cur.name)}">Ordner teilen</button>`;
  html += `</div>`;
  if (!cur && state.views.length) {
    html += `<div class="folder-h">Gespeicherte Suchen</div><div class="folders">` + state.views.map((v) =>
      `<button class="folder smart" data-view="${v.id}"><span class="ic">${SMART_SVG}</span><span><div class="nm">${esc(v.name)}</div><div class="ct">${v.count} Dokument${v.count === 1 ? "" : "e"}</div></span></button>`).join("") + `</div>`;
    html += `<div class="folder-h">Ordner</div>`;
  }
  if (subs.length) {
    html += `<div class="folders">` + subs.map((t) => {
      const n = (kids.get(t.id) || []).filter((c) => shown(c)).length;
      const ct = [t.count ? `${t.count} Dokument${t.count === 1 ? "" : "e"}` : "", n ? `${n} Ordner` : ""].filter(Boolean).join(" · ") || "leer";
      return `<button class="folder" data-folder="${esc(t.name)}" style="--tag:${esc(tagColor(t.name, t.color))}"><span class="ic">${folderIcon(t.name)}</span><span><div class="nm">${esc(t.name)}</div><div class="ct">${ct}</div></span></button>`;
    }).join("") + `</div>`;
  } else if (!cur) {
    html += `<p class="muted">Noch keine Ordner – lege mit „+ Neuer Ordner“ einen an oder vergib Tags, dann erscheinen sie hier.</p>`;
  }
  box.innerHTML = html;
  for (const el of box.querySelectorAll("[data-folder]"))
    el.onclick = () => openFolder(el.dataset.folder || null);
  box.querySelector("#folder-new").onclick = () => newFolder(cur);
  if (cur && state.me?.is_admin) {
    box.querySelector("#folder-rename").onclick = () => renameFolder(cur);
    box.querySelector("#folder-delete").onclick = () => deleteFolder(cur, cur.count || 0, (kids.get(cur.id) || []).length);
  }
  // Ordner per Drag&Drop in einen anderen ziehen – auf eine Kachel oder einen Brotkrumen („Alle Ordner“ = oberste Ebene)
  if (state.me?.is_admin) {
    const byName = (n) => (state.tags || []).find((t) => t.name === n);
    for (const el of box.querySelectorAll(".folder[data-folder]")) {
      el.draggable = true;
      el.ondragstart = (e) => { e.dataTransfer.setData("text/folder", el.dataset.folder); e.dataTransfer.effectAllowed = "move"; };
    }
    for (const el of box.querySelectorAll(".folder[data-folder], .crumbs a[data-folder]")) {
      el.ondragover = (e) => { if ([...e.dataTransfer.types].includes("text/folder")) { e.preventDefault(); el.classList.add("drop-target"); } };
      el.ondragleave = () => el.classList.remove("drop-target");
      el.ondrop = (e) => {
        if (![...e.dataTransfer.types].includes("text/folder")) return;
        e.preventDefault(); e.stopPropagation(); el.classList.remove("drop-target");
        moveFolder(byName(e.dataTransfer.getData("text/folder")), el.dataset.folder ? byName(el.dataset.folder) : null);
      };
    }
  }
  const up = box.querySelector("#folder-up");
  if (up) up.onclick = () => {
    const parent = (state.tags || []).find((t) => t.id === cur.parent_id)?.name ?? null;
    // Kam ich aus dem Elternordner, ist das derselbe Schritt wie die Zurück-Taste des Browsers
    if (history.state && "folder" in history.state && history.state.folder === cur.name && history.state.from === parent) history.back();
    else openFolder(parent);
  };
  for (const el of box.querySelectorAll("[data-share-folder]"))
    el.onclick = () => openShareDialog({ kind: "tag", target: el.dataset.shareFolder, label: `Ordner „${el.dataset.shareFolder}“ samt Unterordnern` });
  for (const el of box.querySelectorAll("[data-view]"))
    el.onclick = () => applyView(state.views.find((v) => String(v.id) === el.dataset.view));
  // Im Ordner ohne eigene Dokumente keine „Keine Dokumente“-Meldung
  if (!cur || (!state.docs.length && subs.length)) $("#empty").classList.add("hidden");
}

// Ordner verschieben, umbenennen, löschen (Admins – der Tag-Bestand ist gemeinsam)
async function patchFolder(t, parentId) {
  await api(`/tags/${t.id}`, json("PATCH", { name: t.name, color: t.color, parent_id: parentId || 0 }));
}
async function moveFolder(t, target) {
  if (!t || (target && target.id === t.id)) return;
  const old = t.parent_id ?? null, to = target ? target.id : null;
  if (old === to) return;
  try { await patchFolder(t, to); } catch (e) { return toast("Geht nicht: " + e.message, 4000); }
  await loadTags();
  toast(`„${t.name}“ liegt jetzt ${target ? `in „${target.name}“` : "auf oberster Ebene"}`, 8000, {
    label: "Rückgängig",
    fn: async () => { try { await patchFolder(t, old); await loadTags(); toast("Rückgängig gemacht"); } catch (e) { toast("Fehler: " + e.message); } },
  });
}
async function renameFolder(t) {
  const name = (prompt("Neuer Name für den Ordner:", t.name) || "").trim();
  if (!name || name === t.name) return;
  const clash = (state.tags || []).find((x) => x.id !== t.id && x.name.toLowerCase() === name.toLowerCase());
  if (clash) return toast(`„${clash.name}“ gibt es schon – Ordnernamen sind eindeutig`, 4500);
  try { await api(`/tags/${t.id}`, json("PATCH", { name, color: t.color })); } catch (e) { return toast("Fehler: " + e.message); }
  state.tag = name;
  history.replaceState({ ...(history.state || {}), folder: name }, "");
  await loadTags(); loadDocs();
  toast("Umbenannt");
}
async function deleteFolder(t, docs, subs) {
  const parent = (state.tags || []).find((x) => x.id === t.parent_id) || null;
  const parts = [`Ordner „${t.name}“ löschen?`, "",
    docs ? `Die Dokumente bleiben erhalten – sie verlieren nur den Tag „${t.name}“.` : "Darin liegen keine Dokumente.",
    subs ? `${subs} Unterordner rück${subs === 1 ? "t" : "en"} eine Ebene hoch${parent ? ` (nach „${parent.name}“)` : " (auf die oberste Ebene)"}.` : ""];
  if (!confirm(parts.filter((x, i) => x || i === 1).join("\n"))) return;
  try { await api(`/tags/${t.id}`, { method: "DELETE" }); } catch (e) { return toast("Fehler: " + e.message); }
  state.tag = parent ? parent.name : null;
  history.replaceState({ ...(history.state || {}), folder: state.tag }, "");
  await loadTags(); loadDocs();
  toast(`Ordner „${t.name}“ gelöscht`);
}

// Neuer Ordner = neuer Tag, auf Wunsch in einem anderen; Dokumente kommen per Tag hinein
async function newFolder(parent) {
  const name = (prompt(parent ? `Name des neuen Ordners in „${parent.name}“:` : "Name des neuen Ordners:") || "").trim();
  if (!name) return;
  const have = (state.tags || []).find((t) => t.name.toLowerCase() === name.toLowerCase());
  if (have) {
    const where = tagPath(have.name).map((t) => t.name).join(" › ");
    toast(`„${have.name}“ gibt es schon (${where}) – Ordnernamen sind eindeutig`, 4500);
    return;
  }
  try {
    await api("/tags", json("POST", { name, parent_id: parent ? parent.id : null }));
    await loadTags();   // Tag-Leiste, Vorschlagsliste und Ordner-Ansicht kennen ihn damit
    toast(`Ordner „${name}“ angelegt – Dokumente kommen über den Tag „${name}“ hinein`, 4000);
  } catch (e) { toast("Fehler: " + e.message); }
}

// Ordnerwechsel landen im Browser-Verlauf: „Zurück“ (Knopf, Maustaste, Wischgeste) geht in den vorigen Ordner
function openFolder(name, push = true) {
  if (name === state.tag) return;
  const from = state.tag;
  state.tag = name; state.searchAll = false;
  if (push) history.pushState({ folder: name, from }, "");
  loadDocs(); scrollTo({ top: 0, behavior: "smooth" });
}
let skipFolderPop = false;   // closeDetail geht selbst per history.back() zurück – das ist kein Ordnerwechsel
window.addEventListener("popstate", (e) => {
  if (skipFolderPop) { skipFolderPop = false; return; }
  if (!$("#detail").classList.contains("hidden") || state.mode !== "folders" || !folderBrowsing()) return;
  if (e.state && "id" in e.state) return;           // Eintrag eines Dokuments (z. B. „Vor“)
  openFolder(e.state?.folder ?? null, false);
});

// ---------- Gespeicherte Suchen ----------
async function loadViews() {
  state.views = await api("/views").catch(() => []);
  renderTagBar(); renderFolderView();
}
function currentViewParams() {
  const p = {};
  if (state.q) p.q = state.q;
  if (state.tag) p.tag = state.tag;
  if (state.year) p.year = state.year;
  if (state.sender) p.sender = state.sender;
  if (state.due) p.due = true;
  if (state.warranty) p.warranty = true;
  if (state.review) p.review = true;
  if (state.scope !== "mine") p.scope = state.scope;
  return p;
}
function applyView(v) {
  if (!v) return;
  const p = v.params || {};
  state.q = p.q || ""; state.tag = p.tag || null; state.year = p.year || null; state.sender = p.sender || null;
  state.due = !!p.due; state.warranty = !!p.warranty; state.review = !!p.review; state.trash = false;
  state.smart = null; smartSeq++; clearTimeout(searchTimer);
  if (p.scope && $("#scope-seg").querySelector("button")) state.scope = p.scope;
  $("#search").value = state.q;
  // Eine gespeicherte Suche zeigt Ergebnisse mit ihren Filtern – die gibt es nur in den Kacheln
  if (state.mode === "folders") { state.mode = "tiles"; applyMode(); }
  renderScopeBar(); loadDocs();
}
function openViewsPopover(anchor) {
  openPopover(anchor, (pop) => {
    const list = document.createElement("div"); list.className = "pop-chips"; list.style.flexDirection = "column"; list.style.alignItems = "stretch";
    for (const v of state.views) {
      const row = document.createElement("div"); row.style.cssText = "display:flex;align-items:center;gap:6px";
      const c = chip(`${v.name} · ${v.count}`, false, () => { pop._close(); applyView(v); });
      c.style.flex = "1";
      const del = document.createElement("button"); del.className = "btn ghost icon-btn"; del.title = "Löschen"; del.textContent = "✕";
      del.onclick = async (e) => { e.stopPropagation(); if (!confirm(`„${v.name}“ löschen?`)) return; await api(`/views/${v.id}`, { method: "DELETE" }); pop._close(); loadViews(); };
      const sh = document.createElement("button"); sh.className = "btn ghost icon-btn"; sh.title = "Freigabe-Link"; sh.textContent = "🔗";
      sh.onclick = (e) => { e.stopPropagation(); pop._close(); openShareDialog({ kind: "view", target: v.id, label: `Gespeicherte Suche „${v.name}“` }); };
      row.append(c, sh, del); list.appendChild(row);
    }
    const p = currentViewParams();
    if (Object.keys(p).some((k) => k !== "scope")) {
      const save = document.createElement("button"); save.className = "btn"; save.textContent = "Aktuelle Suche speichern …";
      save.onclick = async () => {
        const suggestion = [p.q, p.tag, p.year, p.sender, p.due ? "Fristen" : "", p.review ? "Zu prüfen" : ""].filter(Boolean).join(" · ");
        const name = prompt("Name der gespeicherten Suche:", suggestion);
        if (!name || !name.trim()) return;
        try { await api("/views", json("POST", { name: name.trim(), params: p })); toast("Gespeichert"); pop._close(); loadViews(); }
        catch (e) { toast("Fehler: " + e.message); }
      };
      list.appendChild(save);
    } else if (!state.views.length) {
      list.innerHTML = `<span class="muted small">Suche oder filtere etwas, dann lässt es sich hier speichern.</span>`;
    }
    pop.appendChild(list);
  });
}

function renderGrid() {
  const g = $("#grid"); g.innerHTML = "";
  $("#empty").classList.toggle("hidden", state.docs.length > 0);
  if (!state.docs.length) renderEmpty();
  g.classList.toggle("selecting", state.selected.size > 0);
  const showOwner = (smartOn() ? "all" : state.scope) !== "mine";
  for (const d of state.docs) {
    const c = document.createElement("div");
    c.className = "card" + (state.selected.has(d.id) ? " selected" : "");
    const done = d.status === "done" || d.status === "tagging";
    c.innerHTML = `
      <div class="check">${state.selected.has(d.id) ? "✓" : ""}</div>
      ${d.status !== "done" ? `<span class="badge ${d.status === "error" ? "error" : ""}">${statusLabel(d.status)}</span>` : d.ext_pending ? `<span class="badge warn" title="${esc(d.ext_from || "")}">von außen</span>` : (d.llm_status === "failed" ? `<span class="badge warn">ohne Tags</span>` : dueBadge(d))}
      <div class="thumb">${done ? `<img loading="lazy" src="${d.thumbnail_url}?v=${enc(d.updated_at)}" alt="">${d.page_count ? `<span class="pages">${d.page_count} S.</span>` : ""}` : `<span class="pending">${d.status === "processing" ? "OCR läuft …" : "Wartet"}</span>`}</div>
      <div class="info">
        <div class="title">${esc(d.title)}</div>
        <div class="sub">${state.sort.startsWith("added") ? "hinzugefügt " + fmtDate(d.created_at) : docDateHtml(d)}${d.sender ? " · " + esc(d.sender) : ""}</div>
        ${state.warranty && d.warranty_until ? `<div class="sub warranty">${esc(warrantyText(d))}</div>` : ""}
        ${state.review && reviewReasons(d).length ? `<div class="reasons">${esc(reviewReasons(d).join(" · "))}</div>` : ""}
        ${d.snippet ? `<div class="snippet">${esc(d.snippet).replaceAll("[[", "<mark>").replaceAll("]]", "</mark>")}</div>` : ""}
        ${showOwner && !d.is_own ? `<div class="owner">${esc(d.owner_name)}</div>` : ""}
        ${d.tags.length ? `<div class="tags">${d.tags.slice(0, 2).map(pill).join("")}${d.tags.length > 2 ? `<span class="pill more">+${d.tags.length - 2}</span>` : ""}</div>` : ""}
      </div>`;
    c.querySelector(".check").onclick = (e) => { e.stopPropagation(); if (d.can_edit) toggleSelect(d.id); };
    c.onclick = () => { if (state.selected.size || state.trash) { if (d.can_edit) toggleSelect(d.id); } else openDetail(d.id); };
    g.appendChild(c);
  }
}
$("#more-btn").onclick = () => { state.limit += PAGE; loadDocs(); };

/// Leere Ansicht mit dem Alligator: Titel und ein warmer Satz je nach Lage
function renderEmpty() {
  let title, text, upload = false;
  if (state.trash) {
    title = "Papierkorb ist leer";
    text = `Hier landet, was du löschst – nach ${state.trashDays || 30} Tagen räumt der Alligator es endgültig weg.`;
  } else if (state.review) {
    title = "Alles geprüft!";
    text = "Der kleine Alligator hat gerade nichts für euch. Neue Briefe landen hier, wenn er sich bei etwas nicht sicher ist.";
  } else if (state.q || smartOn()) {
    title = "Nichts gefunden";
    text = "Der Alligator hat überall nachgeschaut. Versuch es mit einem anderen Wort – oder nimm einen Filter weg.";
  } else if (state.due) {
    title = "Keine offenen Fristen";
    text = "Gerade ist nichts fällig. Zeit für einen Kaffee.";
  } else if (state.warranty) {
    title = "Keine laufende Garantie";
    text = "Gerade hat kein Kaufbeleg mehr Gewährleistung oder Garantie.";
  } else if (state.tag || state.year || state.sender) {
    title = "Hier ist noch nichts";
    text = "In dieser Auswahl liegen keine Dokumente.";
  } else {
    title = "Noch ganz leer hier";
    text = "Keine Dokumente bisher. Lade ein PDF hoch oder zieh es einfach ins Fenster – der Alligator sortiert es ein.";
    upload = true;
  }
  $("#empty-title").textContent = title;
  $("#empty-text").textContent = text;
  $("#empty-upload").classList.toggle("hidden", !upload);
}

/// Dokumentdatum für Karten – nie das Hochladedatum stattdessen (ein Vertrag von 2018 sah sonst aus wie von heute)
function docDateHtml(d) { return d.document_date ? fmtDate(d.document_date) : `<span class="no-date">Ohne Datum</span>`; }
/// Warum ein Dokument unter „Prüfen“ steht (Server: review_reasons; ältere Server schicken nichts, unbekannte Codes fallen weg)
const REVIEW_REASONS = { error: "Verarbeitung fehlgeschlagen", llm_failed: "Erkennung fehlgeschlagen", no_date: "Kein Datum erkannt", date_check: "Datum prüfen",
                         no_tags: "Keine Tags", external: "Von außen eingeworfen" };
function reviewReasons(d) {
  return (Array.isArray(d.review_reasons) ? d.review_reasons : []).filter((c) => Object.hasOwn(REVIEW_REASONS, c)).map((c) => REVIEW_REASONS[c]);
}

/// Frist-Marke auf der Karte: rot wenn überfällig, gelb in den nächsten 7 Tagen, sonst im Akzent.
function dueDays(d) {
  const [y, m, day] = d.due_date.slice(0, 10).split("-").map(Number);   // als Ortsdatum, nicht UTC
  return Math.round((new Date(y, m - 1, day) - new Date(new Date().toDateString())) / 86400000);
}
// Gewährleistung/Garantie: „Gewährleistung bis 05.03.2027“, abgelaufen „Garantie abgelaufen (05.03.2023)“
function warrantyText(d) {
  if (!d.warranty_until) return "";
  const what = d.warranty_kind === "garantie" ? "Garantie" : "Gewährleistung";
  return d.warranty_until >= new Date().toLocaleDateString("sv") ? `${what} bis ${fmtDate(d.warranty_until)}`
    : `${what} abgelaufen (${fmtDate(d.warranty_until)})`;
}
// Im Blick im Dokument: nur zeigen, was erkannt oder gesetzt ist; gespeichert wird nur Geändertes
function fillBlick(d) {
  $("#m-expires-row").classList.toggle("hidden", !d.expires_what && !d.expires_on);
  $("#m-expires-k").textContent = d.expires_what ? `${d.expires_what} gültig bis` : "Gültig bis";
  $("#m-expires").value = isoToDmy(d.expires_on); $("#m-expires")._initial = $("#m-expires").value;
  $("#m-service-row").classList.toggle("hidden", !d.service_what);
  $("#m-service-k").textContent = d.service_what ? `Nächste ${d.service_what === "Schornsteinfeger" ? "Kehrung" : d.service_what === "Rauchmelder" ? "Rauchmelder-Prüfung" : d.service_what}` : "Nächster Termin";
  $("#m-service").value = isoToDmy(d.service_next); $("#m-service")._initial = $("#m-service").value;
  $("#m-refund-row").classList.toggle("hidden", !d.refund_state);
  $("#m-refund").value = d.refund_state || "open"; $("#m-refund")._initial = $("#m-refund").value;
}
function warrantyValue(d) {
  return d.warranty_months == null ? "" : d.warranty_months === 0 ? "0" : `${d.warranty_months}:${d.warranty_kind || "garantie"}`;
}
function fillWarranty(d) {
  const sel = $("#m-warranty"), v = warrantyValue(d);
  sel.querySelectorAll("option[data-extra]").forEach((o) => o.remove());
  if (v && !sel.querySelector(`option[value="${v}"]`)) {   // seltene Dauer (z. B. 4 Jahre): als eigene Option
    const o = document.createElement("option"); o.value = v; o.dataset.extra = "1";
    const y = d.warranty_months % 12 ? `${d.warranty_months} Monate` : `${d.warranty_months / 12} Jahre`;
    o.textContent = `${y} ${d.warranty_kind === "legal" ? "Gewährleistung" : "Garantie"}`; sel.appendChild(o);
  }
  sel.value = v; sel._initial = sel.value;   // „nicht festgelegt“ bleibt beim Speichern unberührt
  const hint = $("#m-warranty-hint");
  const text = d.warranty_months > 0 ? (d.warranty_until ? warrantyText(d) + " · gerechnet ab Belegdatum" : "Ohne Datum lässt sich das Ende nicht berechnen") : "";
  hint.textContent = text; hint.classList.toggle("hidden", !text);
}

function dueBadge(d) {
  if (!d.due_date || d.due_done) return "";
  const days = dueDays(d);
  const cls = days < 0 ? "due-over" : days <= 7 ? "due-soon" : "due-later";
  // kurz, damit es auch auf schmalen Kacheln in eine Zeile passt; das Datum steht im Tooltip
  const label = days < 0 ? "überfällig" : days === 0 ? "heute fällig" : days === 1 ? "morgen fällig"
    : days <= 7 ? `in ${days} Tagen` : `fällig ${fmtDate(d.due_date)}`;
  const who = d.due_assignee_name ? ` · ${d.due_assignee_name}` : "";
  return `<span class="badge ${cls}" title="${esc(`${d.due_note || "Frist"} · ${fmtDate(d.due_date)}${who}`)}">${label}</span>`;
}

// Sortierung & Kachelgröße (im Browser gemerkt)
$("#sort-select").value = state.sort;
$("#sort-select").onchange = (e) => { state.sort = e.target.value; localStorage.setItem("bl.sort", state.sort); loadDocs(); };
function applyDir() {
  $("#dir-icon").style.transform = state.dir === "asc" ? "rotate(180deg)" : "";
  const labels = { date: ["älteste zuerst", "neueste zuerst"], added: ["zuerst hinzugefügt", "zuletzt hinzugefügt"],
                   title: ["A–Z", "Z–A"], sender: ["A–Z", "Z–A"], amount: ["niedrigster zuerst", "höchster zuerst"] };
  $("#dir-btn").title = (labels[state.sort] || ["aufsteigend", "absteigend"])[state.dir === "asc" ? 0 : 1];
}
$("#dir-btn").onclick = () => { state.dir = state.dir === "asc" ? "desc" : "asc"; localStorage.setItem("bl.dir", state.dir); applyDir(); loadDocs(); };
$("#sort-select").addEventListener("change", applyDir);
applyDir();
function applySize() {
  $("#grid").dataset.size = state.size;
  for (const b of $("#size-seg").querySelectorAll("button")) b.classList.toggle("active", b.dataset.size === state.size);
}
for (const b of $("#size-seg").querySelectorAll("button"))
  b.onclick = () => { state.size = b.dataset.size; localStorage.setItem("bl.size", state.size); applySize(); };
applySize();

function statusLabel(s) { return { pending: "Wartet", processing: "OCR …", tagging: "Tagging …", error: "Fehler" }[s] || s; }

function toggleSelect(id) {
  state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
  renderSelectionBar();
  renderGrid();
}
function renderSelectionBar() {
  $("#selection-bar").classList.toggle("hidden", state.selected.size === 0);
  $("#selection-count").textContent = `${state.selected.size} ausgewählt`;
  $("#merge-btn").disabled = state.selected.size < 2;
  $("#select-all-btn").classList.toggle("hidden", state.selected.size >= state.docs.length);
  $("#assign-select").classList.toggle("hidden", state.trash || (state.owners || []).length < 2);
  $("#assign-select").value = "";
  $("#nodate-selected-btn").classList.toggle("hidden", !state.review || state.trash);
}
function renderOwnerSelects() {
  const owners = state.owners || [];
  const opts = owners.map((u) => `<option value="${u.id}">${esc(u.display_name)}</option>`).join("");
  $("#assign-select").innerHTML = `<option value="">Zuordnen …</option>` + opts;
  $("#m-owner-select").innerHTML = opts;
}
$("#clear-selection-btn").onclick = () => { state.selected.clear(); $("#selection-bar").classList.add("hidden"); renderGrid(); };
// Alle geladenen Treffer auswählen – so lässt sich z. B. eine Suche nach dem Partnernamen komplett zuordnen
$("#select-all-btn").onclick = () => { for (const d of state.docs) state.selected.add(d.id); renderSelectionBar(); renderGrid(); };
$("#assign-select").onchange = async (e) => {
  const owner = Number(e.target.value); if (!owner) return;
  const name = state.owners.find((u) => u.id === owner)?.display_name || "";
  const ids = [...state.selected];
  if (!confirm(`${ids.length} Dokument(e) ${name} zuordnen?`)) { e.target.value = ""; return; }
  try {
    await api("/documents/owner", json("POST", { document_ids: ids, owner_id: owner }));
    state.selected.clear(); $("#selection-bar").classList.add("hidden");
    toast(`${ids.length} Dokument(e) gehören jetzt ${name}`);
    await Promise.all([loadDocs(), loadTags(), loadYears()]);
  } catch (err) { toast("Fehler: " + err.message, 4000); e.target.value = ""; }
};

$("#merge-btn").onclick = async () => {
  const ids = state.docs.filter((d) => state.selected.has(d.id)).map((d) => d.id);
  if (ids.length < 2) return;
  if (!confirm(`${ids.length} Dokumente zu einem zusammenführen? Die Originale werden gelöscht.`)) return;
  try {
    await api("/documents/merge", json("POST", { document_ids: ids, delete_sources: true }));
    state.selected.clear(); $("#selection-bar").classList.add("hidden");
    toast("Zusammengeführt"); await Promise.all([loadDocs(), loadTags(), loadYears()]);
  } catch (e) { toast("Fehler: " + e.message, 4000); }
};
// „Hat kein Datum“ für alle ausgewählten (nur unter „Zu prüfen“): nur Dokumente ohne Datum, die ich bearbeiten darf –
// ein vorhandenes, nur fragliches Datum bleibt stehen
$("#nodate-selected-btn").onclick = async () => {
  const docs = state.docs.filter((d) => state.selected.has(d.id));
  const todo = docs.filter((d) => !d.document_date && d.can_edit);
  if (!todo.length) { toast("Alle ausgewählten Dokumente haben ein Datum oder sind nur lesbar"); return; }
  let ok = 0;
  for (const d of todo) {
    try { await api(`/documents/${d.id}`, json("PATCH", { no_date: true })); ok++; } catch {}
  }
  state.selected.clear(); $("#selection-bar").classList.add("hidden");
  const rest = docs.length - todo.length;
  toast(`${ok} Dokument(e) ohne Datum abgelegt` + (rest ? ` · ${rest} mit Datum oder nur lesbar nicht geändert` : ""), 4000);
  await loadDocs();
};
// Herunterladen: ein Dokument direkt als PDF, mehrere als ZIP (Anmeldung über das Cookie)
$("#download-selected-btn").onclick = () => {
  const ids = [...state.selected];
  if (!ids.length) return;
  const a = document.createElement("a");
  a.href = ids.length === 1 ? `/documents/${ids[0]}/file?download=1` : `/export/selection.zip?ids=${ids.join(",")}`;
  a.download = ""; document.body.appendChild(a); a.click(); a.remove();
  toast(ids.length === 1 ? "PDF wird heruntergeladen" : `${ids.length} PDFs werden als ZIP heruntergeladen`);
};
// Teilen: direkt als Dateien (Teilen-Menü des Geräts) oder als Freigabe-Link für genau diese Dokumente
$("#share-selected-btn").onclick = () => {
  const docs = state.docs.filter((d) => state.selected.has(d.id));
  if (!docs.length) return;
  const label = docs.length === 1 ? `Dokument „${docs[0].title}“` : `${docs.length} ausgewählte Dokumente`;
  openShareDialog({ kind: docs.length === 1 ? "doc" : "docs", target: docs.length === 1 ? docs[0].id : docs.map((d) => d.id),
                    label, files: docs, own: docs.every((d) => d.is_own) });
};
$("#delete-selected-btn").onclick = async () => {
  const ids = [...state.selected];
  const perm = state.trash;
  if (perm && state.me?.demo) { toast("In der Demo nicht möglich – Papierkorb bleibt bis zum Zurücksetzen"); return; }
  if (!confirm(perm ? `${ids.length} Dokument(e) endgültig löschen?` : `${ids.length} Dokument(e) in den Papierkorb legen?`)) return;
  for (const id of ids) await api(`/documents/${id}${perm ? "?permanent=true" : ""}`, { method: "DELETE" }).catch(() => {});
  state.selected.clear(); $("#selection-bar").classList.add("hidden");
  await Promise.all([loadDocs(), loadTags(), loadYears()]);
};

// ---------- Heute ----------
// Wie der Start-Tab der App (iPad, Variante A): links die offenen Fristen zum Abhaken, rechts „Zum Prüfen“ und die
// Familie (Konten, die mit mir teilen, und Personen ohne Konto), darunter das Neueste. Eine Suche, ein Filter oder ein
// Klick auf eine Person zeigt Kacheln – ✕ an der Suche bzw. „Alle“ führt zurück. Gesucht wird über alles (scope=all).
function todayBrowsing() {
  return state.mode === "today" && !state.q && !state.smart && !state.tag && !state.year && !state.review && !state.due && !state.warranty && !state.sender && !state.trash;
}
const today = { seq: 0, data: null, family: null, familyAt: 0, showAllDue: false, checked: new Set() };
const TODAY_DUE_SHOWN = 6;
const AV_COLORS = ["#3a7452", "#c2410c", "#7c3aed", "#0e7490", "#db2777", "#92400e"];
const T_ALARM = `<svg width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="13" r="7" stroke="currentColor" stroke-width="2.2"/><path d="M12 10v3l2 2M5 4L3 6M19 4l2 2" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>`;
const T_CHECK = `<svg width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="4" stroke="currentColor" stroke-width="2.2"/><path d="M8.5 12l2.5 2.5 4.5-5" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const T_PEOPLE = `<svg width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="9" cy="8" r="3.5" stroke="currentColor" stroke-width="2.2"/><circle cx="17" cy="9.5" r="2.5" stroke="currentColor" stroke-width="2.2"/><path d="M3 19c.8-3.3 3.2-5 6-5s5.2 1.7 6 5M15.5 14.3c2.6-.4 4.6 1.2 5.5 4.2" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>`;
const T_REPEAT = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" aria-label="wiederholt sich"><path d="M17 2l4 4-4 4M3 11V9a3 3 0 013-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 01-3 3H3" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const T_CROSS = `<svg width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="3" y="7" width="18" height="13" rx="3" stroke="currentColor" stroke-width="2.1"/><path d="M9 7V5h6v2M12 10.5v6M9 13.5h6" stroke="currentColor" stroke-width="2.1" stroke-linecap="round"/></svg>`;

async function renderToday() {
  const box = $("#todayview"), on = todayBrowsing();
  $("#main").classList.toggle("today-on", on);
  box.classList.toggle("hidden", !on);
  if (!on) return;
  if (today.data) drawToday();   // sofort das Bekannte zeigen, dann auffrischen
  const seq = ++today.seq;
  const familyOld = Date.now() - today.familyAt > 60000;   // Familie höchstens einmal pro Minute nachzählen
  const [due, recent, rc, fam, up] = await Promise.all([
    api("/documents?scope=all&due=true&limit=50").catch(() => null),
    api("/documents?scope=all&sort=added&dir=desc&limit=12").catch(() => null),
    // Zählen wie die Liste, die der Klick öffnet (Bereich oben: nur meine oder alle)
    api("/documents/review_count?scope=" + state.scope).catch(() => null),
    familyOld ? Promise.all([api("/persons").catch(() => []), api("/documents/owner_counts").catch(() => null),
                             api("/emergency").catch(() => null)]) : null,
    api("/upcoming?scope=all").catch(() => ({ items: [] })),
  ]);
  if (seq !== today.seq || !todayBrowsing()) return;
  if (fam) { today.family = { persons: fam[0] || [], counts: fam[1], emergency: fam[2] }; today.familyAt = Date.now(); }
  today.data = { due, recent, review: rc ? rc.count : 0, upcoming: (up && up.items) || [] };
  today.checked.clear();
  drawToday();
}

function todayMembers() {
  const me = state.me, f = today.family || { persons: [], counts: null };
  const counts = new Map((f.counts || []).map((r) => [r.owner_id, r.count]));
  const out = [], seen = new Set();
  for (const u of [{ id: me.id, display_name: me.display_name || me.username }, ...(me.shared_with_me || [])]) {
    if (seen.has(u.id)) continue; seen.add(u.id);
    out.push({ key: `a${u.id}`, account: u.id, name: u.display_name, count: f.counts ? (counts.get(u.id) || 0) : null });
  }
  for (const p of f.persons) out.push({ key: `p${p.id}`, person: p.id, name: p.name, count: p.count, color: p.color });
  return out.map((m, i) => ({ ...m, color: m.color || AV_COLORS[i % AV_COLORS.length] }));
}

function todayDueRow(d) {
  const n = dueDays(d), u = n < 0 ? "over" : n <= 7 ? "soon" : "later";
  const [y, mo, day] = d.due_date.slice(0, 10).split("-").map(Number);
  const label = n < 0 ? "überfällig" : n === 0 ? "heute" : n === 1 ? "morgen" : n <= 7 ? `in ${n} Tagen`
    : new Date(y, mo - 1, day).toLocaleDateString("de-DE", { day: "numeric", month: "short" });
  const sub = d.due_note || d.sender || "";
  const who = d.due_assignee != null && d.due_assignee === state.me.id ? "Ich" : d.due_assignee_name;
  const done = today.checked.has(d.id);
  return `<div class="t-row ${u}${done ? " done" : ""}">
    <button class="t-open" data-open="${d.id}"><span class="t-stripe"></span><span class="t-txt"><span class="t-title">${esc(d.title)}</span>${
      sub || who || d.due_repeat ? `<span class="t-sub">${sub ? `<span class="t-note">${esc(sub)}</span>` : ""}${d.due_repeat ? T_REPEAT : ""}${who ? `<span class="t-who">${esc(who)}</span>` : ""}</span>` : ""
    }</span><span class="t-pill" title="${esc(`${d.due_note || "Frist"} · ${fmtDate(d.due_date)}`)}">${esc(label)}</span></button>
    ${d.can_edit ? `<button class="t-check" data-done="${d.id}" aria-label="Erledigt: ${esc(d.title)}" title="Erledigt"${done ? " disabled" : ""}></button>` : ""}
  </div>`;
}

// „Im Blick“: Ausweis läuft ab, Prüfung/Wartung fällig, teurer geworden, Erstattung offen (Server: /upcoming)
function blickRow(i) {
  const n = i.date ? Math.round((new Date(i.date.slice(0, 10) + "T00:00") - new Date(new Date().toDateString())) / 864e5) : null;
  const when = n == null ? "" : n < 0 ? (i.type === "expiry" ? "abgelaufen" : "überfällig") : n === 0 ? "heute"
    : n <= 60 ? `in ${n} Tagen` : fmtDate(i.date);
  const eur = (v) => (v == null ? "" : Number(v).toLocaleString("de-DE", { style: "currency", currency: "EUR" }));
  let title, sub, pill = when, u = n != null && n < 0 ? "over" : n != null && n <= 30 ? "soon" : "later";
  if (i.type === "expiry") { title = i.what ? `${i.what} läuft ab` : "Läuft ab"; sub = i.title; }
  else if (i.type === "service") { title = `${i.what} fällig`; sub = `zuletzt: ${i.title}`; }
  else if (i.type === "price") {
    title = `${i.what} ist teurer`; sub = `${eur(i.from)} → ${eur(i.amount)} (+${i.percent} %)` + (i.cancel_by ? ` · kündbar bis ${fmtDate(i.cancel_by)}` : "");
    pill = `seit ${fmtDate(i.date)}`; u = "later";
  } else { title = "Erstattung offen"; sub = `${i.title}${i.amount ? " · " + eur(i.amount) : ""}`; pill = i.date ? fmtDate(i.date) : ""; u = "later"; }
  // aus dem Konto erkannt (kein Beleg): führt in „Geld“
  return `<div class="t-row ${u}"><button class="t-open" ${i.document_id ? `data-open="${i.document_id}"` : `data-act="money"`}><span class="t-stripe"></span><span class="t-txt"><span class="t-title">${esc(title)}</span>
    <span class="t-sub"><span class="t-note">${esc(sub || "")}</span></span></span>${pill ? `<span class="t-pill">${esc(pill)}</span>` : ""}</button></div>`;
}

function drawToday() {
  const box = $("#todayview"), t = today.data;
  if (!t) { box.innerHTML = ""; return; }
  const dues = (t.due || []).filter((d) => d.due_date && !d.due_done);
  const shown = today.showAllDue ? dues : dues.slice(0, TODAY_DUE_SHOWN);
  const dueCard = `<section class="tcard t-due" aria-labelledby="t-due-h">
    <div class="t-head"><span class="t-ic orange">${T_ALARM}</span><h2 id="t-due-h">Fristen</h2>${dues.length ? `<span class="t-count">${dues.length} offen</span>` : ""}</div>
    ${t.due === null ? `<p class="t-none">Fristen ließen sich gerade nicht laden.</p>` : dues.length ? "" : `<p class="t-none">Keine offenen Fristen – Zeit für einen Kaffee.</p>`}
    ${shown.map(todayDueRow).join("")}
    ${dues.length > TODAY_DUE_SHOWN ? `<button class="t-more" data-act="more">${today.showAllDue ? "Weniger zeigen" : `Alle ${dues.length} Fristen zeigen`}</button>` : ""}
  </section>`;
  const review = t.review ? `<button class="t-review" data-act="review"><span class="t-ic">${T_CHECK}</span>
    <span class="t-rv"><b>${t.review === 1 ? "1 Dokument" : `${t.review} Dokumente`} zum Prüfen</b><small>Kurz drüberschauen – dauert keine Minute</small></span><span aria-hidden="true">›</span></button>` : "";
  const up = t.upcoming || [];
  const blick = up.length ? `<section class="tcard t-blick" aria-labelledby="t-blick-h">
    <div class="t-head"><span class="t-ic">${T_ALARM}</span><h2 id="t-blick-h">Im Blick</h2></div>
    ${up.map(blickRow).join("")}
  </section>` : "";
  const members = todayMembers(), em = today.family?.emergency;
  const family = members.length > 1 || em ? `<section class="tcard t-family" aria-labelledby="t-fam-h">
    <div class="t-head"><span class="t-ic">${T_PEOPLE}</span><h2 id="t-fam-h">Familie</h2></div>
    ${members.length > 1 ? `<div class="t-people">${members.map((m) => `<button class="t-person" data-member="${m.key}" title="Dokumente von ${esc(m.name)}">
      <span class="t-av" style="--av:${esc(m.color)}">${esc((m.name || "?").charAt(0).toUpperCase())}</span><b>${esc(m.name)}</b>
      <small>${m.count == null ? "&nbsp;" : m.count === 1 ? "1 Dokument" : `${m.count} Dokumente`}</small></button>`).join("")}</div>` : ""}
    ${em ? `<button class="t-emerg" data-act="emergency">${T_CROSS}<b>Notfallmappe</b><small>${em.documents.length ? (em.documents.length === 1 ? "1 Dokument" : `${em.documents.length} Dokumente`) : "Noch leer"}</small></button>` : ""}
  </section>` : "";
  const recent = (t.recent || []).length ? `<div class="t-head t-new-h"><span class="t-ic small">${SMART_SVG}</span><h2>Neu</h2><button class="t-link" data-act="all">Alle Dokumente</button></div>
    <div class="t-new">${t.recent.map((d) => {
      const ready = d.status === "done" || d.status === "tagging";
      return `<button class="t-doc" data-open="${d.id}"><span class="t-thumb">${ready ? `<img loading="lazy" src="${d.thumbnail_url}?v=${enc(d.updated_at)}" alt="">` : `<span class="pending">${d.status === "error" ? "Fehler" : "OCR läuft …"}</span>`}</span>
        <span class="t-dtitle">${esc(d.title)}</span>${d.tags.length ? pill(d.tags[0]) : `<span class="t-dsub">${docDateHtml(d)}</span>`}</button>`;
    }).join("")}</div>` : "";
  const empty = !dues.length && !t.review && !up.length && !(t.recent || []).length && t.due !== null;
  box.innerHTML = empty ? "" : `<div class="t-cols">${dueCard}${review || blick || family ? `<div class="t-side">${review}${blick}${family}</div>` : ""}</div>${recent}`;
  $("#empty").classList.toggle("hidden", !empty);
  if (empty) { state.docs = []; renderEmpty(); }
}

/// Dokumente einer Person zeigen – als Chip „von Sam“ wie bei der Suche in Sätzen; ✕ führt zurück zu „Heute“
function showMember(m) {
  const c = m.account != null ? { kind: "owner", label: m.name, owner_id: m.account }
    : { kind: "person", label: m.name, value: m.name, tag: true };
  showChip(c, `von ${m.name}`);
}
function showChip(c, text) {
  clearTimeout(searchTimer); smartSeq++;
  state.smart = { chips: [c], dropped: [] };
  state.q = text; $("#search").value = text;
  renderSmartBar(); loadDocs();
}

$("#todayview").addEventListener("click", async (e) => {
  const el = e.target.closest("button"); if (!el) return;
  if (el.dataset.open) return openDetail(Number(el.dataset.open));
  if (el.dataset.member) { const m = todayMembers().find((x) => x.key === el.dataset.member); if (m) showMember(m); return; }
  if (el.dataset.done) {
    const id = Number(el.dataset.done);
    today.checked.add(id); drawToday();
    try {
      const d = await api(`/documents/${id}`, json("PATCH", { due_done: true }));
      toast(d && d.due_date && !d.due_done ? `Erledigt – nächste Frist ${fmtDate(d.due_date)}` : "Erledigt");
    } catch (err) { today.checked.delete(id); toast("Fehler: " + err.message); }
    setTimeout(() => { if (todayBrowsing()) renderToday(); }, 600);
    return;
  }
  switch (el.dataset.act) {
    case "more": today.showAllDue = !today.showAllDue; drawToday(); break;
    case "review": state.review = true; renderTagBar(); loadDocs(); break;
    case "emergency": {   // als Ordner-Chip über alles, was ich sehe (wie die Mappe selbst) – nicht nur im Bereich „Meine“
      const em = today.family?.emergency;
      if (em) showChip({ kind: "word", label: em.name, value: em.name, tag: true }, em.name);
      break;
    }
    case "all": $('#mode-seg button[data-mode="tiles"]').click(); break;
    case "money": $('#mode-seg button[data-mode="money"]').click(); break;
  }
});

// ---------- Geld (Kontoumsätze aus CSV, Server: /bank) ----------
// Eigene Ansicht wie „Heute“: Zeitraum, Kacheln, „Wofür“ als Balken, eine Kategorie im Detail mit Monaten und Buchungen,
// „Einsortieren“ für offene Buchungen (Regel „immer so“), Import als Dialog mit Vorschau. Kategorie und Einsortieren
// sind Ebenen – Browser-Zurück führt zur Übersicht.
const money = { yearly: null, yearlyOpen: false, q: "", search: null, period: "6m", account: "", cat: null, sorting: false, data: null, cats: null, sort: null, accounts: null, seq: 0, lastImport: null };
function moneyBrowsing() { return state.mode === "money" && !state.trash && !state.review && !state.due && !state.warranty; }
const eurC = (c) => (c / 100).toLocaleString("de-DE", { style: "currency", currency: "EUR" });
function moneyRange() {
  const d = new Date(), pad = (n) => String(n).padStart(2, "0");
  const iso = (y, m, day = 1) => `${y}-${pad(m)}-${pad(day)}`;
  if (money.period === "month") return { from: iso(d.getFullYear(), d.getMonth() + 1) };
  if (money.period === "6m") { const x = new Date(d.getFullYear(), d.getMonth() - 5, 1); return { from: iso(x.getFullYear(), x.getMonth() + 1) }; }
  if (money.period === "year") return { from: iso(d.getFullYear(), 1) };
  return {};
}
function moneyQuery(extra = {}) {
  const p = new URLSearchParams({ ...moneyRange(), ...extra });
  if (money.account) p.set("account", money.account);
  return p.toString();
}
async function renderMoney() {
  const box = $("#moneyview"), on = moneyBrowsing(), appearing = on && box.classList.contains("hidden");
  $("#main").classList.toggle("money-on", on);
  box.classList.toggle("hidden", !on);
  if (!on) { money.lastKey = null; return; }
  if (money.data && appearing) drawMoney();
  const seq = ++money.seq;
  const [acc, sum, cat, srt, ins, rules, found, yearly] = await Promise.all([
    api("/bank/accounts").catch(() => null),
    api("/bank/summary?" + moneyQuery()).catch(() => null),
    money.cat ? api("/bank/category?" + moneyQuery({ name: money.cat })).catch(() => null) : null,
    money.sorting ? api("/bank/to_sort").catch(() => null) : null,
    api("/bank/insights").catch(() => null),
    api("/bank/rules").catch(() => []),
    money.q ? api("/bank/search?" + new URLSearchParams({ q: money.q })).catch(() => null) : null,
    api("/bank/yearly").catch(() => null),   // ältere Server ohne /bank/yearly: einfach keine Karte
  ]);
  if (seq !== money.seq || !moneyBrowsing()) return;
  // Das Nachladen im Hintergrund (Verarbeitung läuft) ruft hier alle paar Sekunden an: nur neu zeichnen, wenn sich
  // etwas geändert hat – sonst klappen offene Auswahlen zu und die Seite springt
  const key = JSON.stringify([acc, sum, cat, srt, ins, rules, found, yearly, money.cat, money.sorting, money.yearlyOpen, money.period, money.account, money.q]);
  if (key === money.lastKey && !appearing) return;
  money.lastKey = key;
  money.accounts = acc; money.data = sum; money.catData = cat; money.sort = srt; money.insights = ins ? ins.items : []; money.rules = rules || []; money.search = found; money.yearly = yearly;
  drawMoney();
}
function moneyTxRow(t, edit = false) {
  const [y, m, d] = t.booked.split("-");
  const who = t.counterparty || "–";
  const doc = t.document_id ? `<button class="m-pill" data-open="${t.document_id}">Beleg</button>` : "";
  // in der Kategorie: falsch einsortiert? hier umstellen
  const cats = (money.accounts && money.accounts.categories) || [];
  // Noch nicht einsortiert: Platzhalter statt der ersten Kategorie – sonst sähe es aus, als wäre schon gewählt
  const sel = edit ? `<select data-mrecat="${t.id}" data-key="${esc(t.key || "")}" aria-label="Kategorie von ${esc(who)}" class="m-recat${t.category ? "" : " empty"}">${
    t.category ? "" : `<option value="" selected disabled>Kategorie wählen …</option>`}${cats.map((c) =>
    `<option${c === t.category ? " selected" : ""}>${esc(c)}</option>`).join("")}</select>` : "";
  const year = edit && t.cents < 0 && money.yearly ? `<button class="m-pill" data-myear="${t.id}" data-val="true" title="Kommt einmal im Jahr – zu den jährlichen Kosten">Jährlich</button>` : "";
  return `<div class="m-tx"><span class="d">${d}.${m}.</span><span class="w"><b>${esc(who)}</b><small>${esc(t.purpose || "")}</small></span>${sel}${doc}${year}
    <span class="a${t.cents > 0 ? " plus" : ""}">${t.cents > 0 ? "+" : ""}${eurC(t.cents)}</span></div>`;
}
function drawMoney() {
  const box = $("#moneyview"), a = money.accounts, s = money.data;
  const accs = (a && a.accounts) || [];
  const periods = [["month", "Monat"], ["6m", "6 Monate"], ["year", "Jahr"], ["all", "Alles"]];
  // Konten nach Umsätzen sortiert (Gemeinschaftskonto zuerst, kleine Pockets hinten)
  const byUse = [...accs].sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  const accSel = accs.length > 1 ? `<select id="m-account" class="m-accsel" aria-label="Konto"><option value="">Alle ${accs.length} Konten</option>${byUse.map((x) =>
    `<option value="${x.id}"${String(x.id) === String(money.account) ? " selected" : ""}>${esc(x.name)}</option>`).join("")}</select>` : "";
  const picked = accs.find((x) => String(x.id) === String(money.account));
  const which = picked ? picked.name : accs.length === 1 ? accs[0].name : accs.length ? `${accs.length} Konten` : "Noch kein Konto";
  const covered = s && s.covered ? `Umsätze vom ${fmtDate(s.covered.first)} bis ${fmtDate(s.covered.last)}` : "";
  const from = moneyRange().from;
  const gap = s && s.covered && from && s.covered.first > from && accs.length
    ? `<p class="m-warn">Der Zeitraum beginnt am ${fmtDate(from)}, die Umsätze erst am ${fmtDate(s.covered.first)} – ältere fehlen, die Summen sind zu klein.</p>` : "";
  const undo = money.lastImport ? `<p class="m-note">${money.lastImport.new} Umsätze importiert · <button class="linkish" data-mact="undo">Zurücknehmen</button></p>` : "";
  // Kopf in zwei ruhigen Zeilen: Titel mit kurzer Unterzeile und Import rechts; darunter Zeitraum und Konto
  const head = `<div class="m-head"><div class="grow"><h2>Geld</h2><p class="m-sub">${esc(which)}${covered ? " · " + covered : ""}</p></div></div>
    <div class="m-controls">${accs.length ? `<div class="segmented" role="group" aria-label="Zeitraum">${periods.map(([k, l]) =>
      `<button data-mperiod="${k}" class="${money.period === k ? "active" : ""}">${l}</button>`).join("")}</div>${accSel}` : ""}
      <span class="spacer"></span><button class="btn primary" data-mact="import">CSV importieren</button></div>${gap}${undo}`;
  if (!accs.length) {
    box.innerHTML = head + `<section class="tcard"><h3 style="margin:0 0 6px">Wofür geht euer Geld?</h3>
      <p class="muted" style="margin:0 0 10px">Ladet die Umsätze als CSV-Export aus dem Online-Banking eurer Bank hoch. Papigator sortiert sie in Kategorien,
      findet die passenden Belege und zeigt, was teurer geworden ist. Kein Bank-Login, alles bleibt auf eurem Server.</p>
      <button class="btn primary" data-mact="import">CSV importieren</button></section>`;
    return;
  }
  if (!s) { box.innerHTML = head + `<p class="muted">Umsätze ließen sich gerade nicht laden.</p>`; return; }
  if (money.sorting) { box.innerHTML = head + drawMoneySort(); return; }
  if (money.yearlyOpen) { box.innerHTML = head + drawMoneyYearly(); return; }
  if (money.q) { box.innerHTML = head + drawMoneySearch(); return; }
  const cats = s.categories || [];
  const max = Math.max(1, ...cats.map((c) => c.cents));
  const sel = money.cat ? cats.find((c) => c.name === money.cat) : null;
  const kpis = `<div class="m-kpis">
    <div class="m-kpi"><span class="k">Ausgaben</span><b>${eurC(s.spent)}</b><small>${s.months.length ? `ø ${eurC(Math.round(s.spent / s.months.length))} im Monat` : "&nbsp;"}</small></div>
    <div class="m-kpi"><span class="k">Einnahmen</span><b>${eurC(s.income)}</b><small>${s.income - s.spent >= 0 ? `${eurC(s.income - s.spent)} übrig` : `${eurC(s.spent - s.income)} mehr ausgegeben`}</small></div>
    ${sel ? `<div class="m-kpi sel"><span class="k">${esc(sel.name)}</span><b>${eurC(sel.cents)}</b><small>${s.spent ? Math.round(sel.cents / s.spent * 100) : 0} % der Ausgaben</small></div>` : ""}
    <div class="m-kpi"><span class="k">Mit Beleg</span><b>${s.out_count ? Math.round(s.with_doc / s.out_count * 100) : 0} %</b><small>${s.with_doc} von ${s.out_count} Abbuchungen</small></div></div>`;
  const bars = `<section class="tcard" aria-labelledby="m-cat-h"><div class="t-head"><h2 id="m-cat-h">Wofür</h2><span class="m-note">Rücksendungen sind abgezogen, Umbuchungen zählen nicht</span></div>
    ${cats.map((c) => `<button class="m-bar${c.name === money.cat ? " on" : ""}" data-mcat="${esc(c.name)}"><span class="n">${esc(c.name)}</span>
      <span class="track"><span class="fill" style="width:${Math.max(1, Math.round(c.cents / max * 100))}%"></span></span><span class="v">${eurC(c.cents)}</span></button>`).join("") || `<p class="muted">Keine Ausgaben in diesem Zeitraum.</p>`}</section>`;
  const cd = money.catData;
  const detail = money.cat && cd ? (() => {
    const mmax = Math.max(1, ...cd.months.map((m) => Math.abs(m.cents)));
    const mon = (k) => new Date(k + "-01T00:00").toLocaleDateString("de-DE", { month: "short" });
    return `<section class="tcard m-detail" aria-labelledby="m-det-h"><button class="btn m-backcat" data-mact="closecat">‹ Alle Kategorien</button>
      <div class="t-head"><h2 id="m-det-h">${esc(cd.name)}</h2>
      <span class="t-count">${eurC(cd.cents)} · ${cd.count} Buchungen</span></div>
      ${cd.top ? `<p class="m-note" style="margin:0 0 4px">Am meisten bei ${esc(cd.top.name)} (${eurC(cd.top.cents)})</p>` : ""}
      <label class="toggle m-note" style="margin:0 0 6px"><input type="checkbox" id="m-recat-all"${money.recatAll === false ? "" : " checked"}> Umstellen gilt für alle Buchungen desselben Händlers</label>
      <div class="m-months">${cd.months.map((m) => `<div class="mo"><b>${eurC(m.cents).replace(/,\d\d/, "")}</b><span class="col" style="height:${Math.round(Math.abs(m.cents) / mmax * 100)}px"></span>${mon(m.month)}</div>`).join("")}</div>
      ${cd.transactions.map((t) => moneyTxRow(t, true)).join("")}
      <button class="btn m-backcat" data-mact="closecat" style="margin-top:12px">‹ Alle Kategorien</button></section>`;
  })() : "";
  const side = `<div class="t-side">${s.to_sort ? `<button class="m-sortbtn" data-mact="sort"><span style="flex:1"><b>${s.to_sort === 1 ? "1 Buchung" : `${s.to_sort} Buchungen`} einsortieren</b>
      <small>Unbekannte Händler, PayPal, Amazon – einmal zugeordnet, merkt Papigator sich das</small></span><span aria-hidden="true">›</span></button>` : ""}
    ${moneyYearlyCard()}
    ${(money.insights || []).length ? `<section class="tcard"><div class="t-head"><h2>Aus dem Konto erkannt</h2></div>
      <p class="m-note" style="margin:-4px 0 6px">Abbuchungen sind verlässlicher als Rechnungen</p>
      ${money.insights.map((i) => `<div class="m-tx"><span class="w"><b>${esc(i.name)} ${i.type === "price" ? "ist teurer" : "– neue Abbuchung"}</b>
        <small>${i.type === "price" ? `${eurC(Math.round(i.from * 100))} → ${eurC(Math.round(i.to * 100))} seit ${fmtDate(i.since)} (+${i.percent} %)`
          : `${eurC(Math.round(i.to * 100))} ${esc(i.interval)} seit ${fmtDate(i.since)} – kein Beleg im Archiv`}</small></span></div>`).join("")}</section>` : ""}
    <section class="tcard"><div class="t-head"><h2>Konten</h2><span class="t-count">${accs.length}</span></div>${(() => {
      // kompakt: die vier meistgenutzten, der Rest aufklappbar
      const people = (money.accounts && money.accounts.people) || [];
      const seen = (x) => { const n = people.filter((p) => (x.visible_to || []).includes(p.id)).map((p) => p.name);
        return !x.is_own ? "mitgeteilt" : n.length ? `sichtbar für ${n.join(", ")}` : "nur du"; };
      // Wer ein Konto sieht, lässt sich hier jederzeit ändern (Sparkonto privat, Gemeinschaftskonto geteilt)
      const share = (x) => x.is_own && people.length ? `<div class="m-share${money.shareOpen === x.id ? "" : " hidden"}" data-mshare="${x.id}">
        ${people.map((p) => `<label class="toggle small"><input type="checkbox" data-mvis="${x.id}" value="${p.id}"${(x.visible_to || []).includes(p.id) ? " checked" : ""}> ${esc(p.name)} sieht das Konto</label>`).join("")}</div>` : "";
      const row = (x) => `<div class="m-tx"><span class="w"><b>${esc(x.name)}</b>
        <small>${x.count} Umsätze · ${esc(seen(x))}</small></span>
        ${x.is_own && people.length ? `<button class="m-pill" data-mvisbtn="${x.id}" aria-expanded="${money.shareOpen === x.id}">Wer sieht's?</button>` : ""}
        ${x.is_own ? `<button class="m-pill" data-mdel="${x.id}" title="Konto samt Umsätzen löschen">Löschen</button>` : ""}</div>${share(x)}`;
      const top = byUse.slice(0, 4), rest = byUse.slice(4);
      return top.map(row).join("") + (rest.length ? `<details class="m-rules"><summary>${rest.length} weitere Konten</summary>${rest.map(row).join("")}</details>` : "");
    })()}
      ${(money.rules || []).length ? `<details class="m-rules"><summary>${money.rules.length} eigene Regeln</summary>${money.rules.map((r) =>
        `<div class="m-tx"><span class="w"><b>${r.pattern.startsWith("kat:") ? `Bank-Kategorie „${esc(r.pattern.slice(4))}“` : `„${esc(r.pattern)}“`}</b><small>→ ${esc(r.category)}</small></span><button class="m-pill" data-mrule="${r.id}">Entfernen</button></div>`).join("")}</details>` : ""}
      <p class="m-note">Die Umsätze bleiben auf eurem Server und erscheinen nie in Freigabe-Links.</p></section></div>`;
  box.innerHTML = head + kpis + `<div class="m-cols"><div style="display:flex;flex-direction:column;gap:16px;min-width:0">${detail}${bars}</div>${side}</div>`;
  // Kategorie geöffnet: zu ihr springen (sie steht über den Balken); geschlossen: zurück zu „Wofür“
  const jump = money.jump; money.jump = null;
  const target = jump === "detail" ? box.querySelector(".m-detail") : jump === "bars" ? box.querySelector("#m-cat-h") : null;
  if (target) target.scrollIntoView({ block: "start", behavior: "smooth" });
}
// Suche in „Geld“ (Feld oben): Zeitraum und Suchwörter als Chips, Summe, die Buchungen (umsortierbar)
// Jährliche Kosten (Server: /bank/yearly): Karte in der Seitenspalte und eigene Ebene mit Vorjahr, Topf und Vorschau
const fmtMonth = (iso) => new Date(iso.slice(0, 7) + "-01T00:00").toLocaleDateString("de-DE", { month: "long", year: "numeric" });
function moneyPotLine(y) {
  const need = y.totals.monthly_needed, pot = y.pot;
  if (!pot) return `Nötig im Monat: ${eurC(need)}`;
  const diff = need - pot.monthly;
  return `${esc(pot.name)}: ${eurC(pot.monthly)} im Monat – ` + (Math.abs(diff) < 100 ? "passt" : diff > 0 ? `${eurC(diff)} zu wenig (nötig ${eurC(need)})` : `${eurC(-diff)} mehr als nötig`);
}
function moneyYearlyCard() {
  const y = money.yearly;
  if (!y || (!y.items.length && !y.pot)) return "";
  const top = y.items.filter((i) => i.in_sum && i.active).slice(0, 3);
  return `<section class="tcard"><div class="t-head"><h2>Jährliche Kosten</h2><span class="t-count">${eurC(y.totals.per_year)} im Jahr</span></div>
    <p class="m-note" style="margin:-4px 0 6px">${moneyPotLine(y)}</p>
    ${top.map((i) => `<div class="m-tx"><span class="w"><b>${esc(i.label || i.name)}</b><small>nächste ${fmtMonth(i.next)}</small></span><span class="a">${eurC(i.per_year)}</span></div>`).join("")}
    <button class="btn" data-mact="yearly" style="margin-top:8px">Alle ${y.items.length} ansehen</button></section>`;
}
function drawMoneyYearly() {
  const y = money.yearly;
  if (!y) return `<section class="tcard"><p class="muted" style="margin:0">Jährliche Kosten lassen sich gerade nicht laden.</p></section>`;
  const Y = y.year, pot = y.pot;
  const potSel = `<select id="m-pot" aria-label="Rücklagen-Topf"><option value="">kein Topf</option>${y.accounts.map((a) =>
    `<option value="${a.id}"${pot && pot.id === a.id ? " selected" : ""}>${esc(a.name)}</option>`).join("")}</select>`;
  const live = y.items.filter((i) => i.in_sum && i.active).length;
  const kpis = `<div class="m-kpis">
    <div class="m-kpi"><span class="k">Pro Jahr</span><b>${eurC(y.totals.per_year)}</b><small>${live} Posten, je der aktuelle Betrag</small></div>
    <div class="m-kpi"><span class="k">Nötig im Monat</span><b>${eurC(y.totals.monthly_needed)}</b><small>Jahressumme ÷ 12</small></div>
    <div class="m-kpi"><span class="k">Rücklagen-Topf</span><b>${pot ? eurC(pot.monthly) : "–"}</b><small>${pot ? moneyPotLine(y).replace(/^[^:]*: [^–]*– /, "") : "Konto wählen:"} ${potSel}</small></div>
    <div class="m-kpi"><span class="k">${Y - 1} → ${Y}</span><b>${eurC(y.totals.last)} → ${eurC(y.totals.this)}</b><small>abgebucht im Kalenderjahr</small></div></div>`;
  const short = y.covered && (new Date(y.today) - new Date(y.covered.first)) / 864e5 < 400
    ? `<p class="m-warn">Die Umsätze beginnen erst am ${fmtDate(y.covered.first)}. Jahresposten erkennt Papigator an einer Abbuchung gut ein Jahr vorher – oder ihr markiert eine Buchung als „Jährlich“.</p>` : "";
  const row = (i) => {
    const ch = i.change_pct ? ` <span class="m-chg${i.change_pct > 0 ? " up" : ""}">${i.change_pct > 0 ? "+" : ""}${i.change_pct} %</span>` : "";
    const state = !i.in_sum ? "nur Beleg, keine Abbuchung gefunden – zählt nicht mit" : !i.active ? "weggefallen" : `nächste ${fmtMonth(i.next)}`;
    const doc = i.document_id ? `<button class="m-pill" data-open="${i.document_id}">Beleg</button>` : "";
    const act = i.source === "document" ? "" : i.source === "marked"
      ? `<button class="m-pill" data-myear="${i.mark_tx}" data-val="null" title="Markierung zurücknehmen">Markierung weg</button>`
      : `<button class="m-pill" data-myear="${i.mark_tx}" data-val="false" title="Ist kein Jahresposten – ausblenden">Nicht jährlich</button>`;
    const yv = (k) => i.years[String(k)] ? eurC(i.years[String(k)]) : "–";
    return `<div class="m-yrow${i.active && i.in_sum ? "" : " dim"}"><span class="w"><b>${esc(i.label || i.name)}</b><small>${i.label ? esc(i.name) + " · " : ""}${state}${i.source === "marked" ? " · von Hand" : ""}${i.source === "pot" ? " · aus dem Topf genommen" : i.from_pot ? " · aus dem Topf bezahlt" : ""}</small></span>
      <span class="v">${yv(Y - 1)}</span><span class="v">${yv(Y)}</span><span class="v strong">${eurC(i.per_year)}${ch}</span><span class="acts">${doc}${act}</span></div>`;
  };
  const head = `<div class="m-yrow head"><span class="w">Posten</span><span class="v">${Y - 1}</span><span class="v">${Y}</span><span class="v">pro Jahr</span><span class="acts"></span></div>`;
  const sched = y.schedule.length ? `<section class="tcard"><div class="t-head"><h2>Die nächsten 12 Monate</h2></div>${y.schedule.map((m) =>
    `<div class="m-tx"><span class="d" style="flex-basis:120px">${fmtMonth(m.month)}</span><span class="w"><b>${m.items.map((x) => esc(x.name)).join(", ")}</b></span><span class="a">${eurC(m.cents)}</span></div>`).join("")}</section>` : "";
  return `<div class="m-layer"><div class="t-head m-layer-h"><button class="m-back" data-mact="closeyearly" aria-label="Zurück zu Geld">‹ Geld</button><h2>Jährliche Kosten</h2></div>${short}${kpis}
    <section class="tcard m-ylist">${y.items.length ? head + y.items.map(row).join("") : `<p class="muted" style="margin:0">Noch keine Jahresposten erkannt.</p>`}
      <p class="m-note">Erkannt an Abbuchungen, die es etwa ein Jahr vorher schon einmal gab. Was erst einmal abgebucht wurde (neues Jahresabo), markiert ihr in einer Kategorie an der Buchung mit „Jährlich“.
      Was ihr aus dem Rücklagen-Topf herausnehmt, zählt immer mit – auch wenn es von einem anderen Konto bezahlt wird.</p></section>${sched}</div>`;
}
function drawMoneySearch() {
  const r = money.search;
  if (!r) return `<section class="tcard"><p class="muted" style="margin:0">Suche läuft …</p></section>`;
  const range = r.range ? `<span class="chip active">${fmtDate(r.range.from)} – ${fmtDate(r.range.to)}</span>` : "";
  const words = r.words.map((w) => `<span class="chip active">${esc(w)}</span>`).join("");
  return `<section class="tcard"><div class="t-head m-search-h"><h2>Suche</h2>${range}${words}
      <span class="t-count">${r.count === 1 ? "1 Buchung" : `${r.count} Buchungen`} · ${eurC(r.cents)}</span>
      <button class="t-link" data-mact="clearsearch">✕ Suche löschen</button></div>
    ${r.words.length ? "" : `<p class="muted">Gib einen Händler oder eine Kategorie ein, z. B. „Amazon September 2026“.</p>`}
    ${r.count > r.transactions.length ? `<p class="m-note">Die neuesten ${r.transactions.length} von ${r.count}:</p>` : ""}
    ${r.transactions.map((t) => moneyTxRow(t, true)).join("") || (r.words.length ? `<p class="muted">Keine Buchungen gefunden.</p>` : "")}</section>`;
}
const MONEY_QUICK = ["Online-Shopping", "Kleidung", "Lebensmittel", "Haushalt", "Kinder", "Freizeit & Ausgehen", "Sonstiges"];
function drawMoneySort() {
  const list = (money.sort && money.sort.transactions) || [];
  const all = (money.accounts && money.accounts.categories) || [];
  const cards = list.map((t) => {
    // Gutschrift: meist Einnahme oder Geld vom eigenen Konto/Pocket; Abbuchung: die häufigen Ausgaben, Umbuchung zuletzt
    const base = t.cents > 0 ? ["Einnahmen", "Umbuchung", "Sonstiges"] : [...MONEY_QUICK.slice(0, 5), "Umbuchung"];
    const quick = [...new Set([t.category, ...base].filter(Boolean))].slice(0, 6);
    return `<section class="tcard m-card" data-tx="${t.id}"><div class="m-tx" style="border:0;padding:0">${moneyTxRow(t).replace(/^<div class="m-tx">|<\/div>$/g, "")}</div>
      ${t.cat_source === "ai" ? `<p class="m-note" style="margin:0">Vorschlag der KI: <b>${esc(t.category)}</b> – bitte bestätigen</p>` : ""}
      ${t.bank_category ? `<label class="toggle m-note" style="margin:0"><input type="checkbox" data-mbank checked> Alle mit Bank-Kategorie „${esc(t.bank_category)}“ genauso</label>` : ""}
      <div class="m-choices">${quick.map((c) => `<button class="m-choice${c === t.category ? " sug" : ""}" data-mpick="${esc(c)}">${esc(c)}</button>`).join("")}
        <select data-mother aria-label="Andere Kategorie"><option value="">Andere …</option>${all.filter((c) => !quick.includes(c)).map((c) => `<option>${esc(c)}</option>`).join("")}</select>
        ${t.key ? `<label class="toggle" style="margin-left:auto"><input type="checkbox" data-mremember="${esc(t.key)}"${t.counterparty && /paypal|amazon|amzn|klarna/i.test(t.counterparty) && !/paypal/i.test(t.key) ? "" : " checked"}> Immer so für „${esc(t.key)}“</label>` : ""}</div></section>`;
  }).join("");
  return `<div class="m-layer"><div class="t-head m-layer-h"><button class="m-back" data-mact="closesort" aria-label="Zurück zu Geld">‹ Geld</button><h2>Einsortieren</h2><span class="t-count">${list.length} offen</span></div>
    ${cards || `<section class="tcard"><p class="muted" style="margin:0">Alles einsortiert.</p></section>`}</div>`;
}
async function moneyPick(card, category) {
  const id = Number(card.dataset.tx), rem = card.querySelector("[data-mremember]"), bk = card.querySelector("[data-mbank]");
  try {
    const r = await api(`/bank/tx/${id}`, json("PATCH", { category, remember: rem && rem.checked ? rem.dataset.mremember : null,
                                                         remember_bank: !!(bk && bk.checked) }));
    card.remove();
    toast(r.applied ? `${category} – ${r.applied} weitere gleich mit` : category);
    if (r.applied) renderMoney();
  } catch (e) { toast("Fehler: " + e.message); }
}
$("#moneyview").addEventListener("change", (e) => {
  if (e.target.id === "m-account") { money.account = e.target.value; money.cat = null; renderMoney(); }
  if (e.target.id === "m-pot") {
    api("/bank/yearly/pot", json("PUT", { account_id: e.target.value ? Number(e.target.value) : null }))
      .then(() => { toast(e.target.value ? "Rücklagen-Topf gespeichert" : "Kein Rücklagen-Topf"); renderMoney(); })
      .catch((err) => toast("Fehler: " + err.message));
  }
  if (e.target.matches("[data-mother]") && e.target.value) moneyPick(e.target.closest("[data-tx]"), e.target.value);
  if (e.target.id === "m-recat-all") money.recatAll = e.target.checked;
  if (e.target.matches("[data-mrecat]")) {
    const all = money.recatAll !== false && e.target.dataset.key;
    api(`/bank/tx/${e.target.dataset.mrecat}`, json("PATCH", { category: e.target.value, remember: all ? e.target.dataset.key : null }))
      .then((r) => { toast(`Umsortiert: ${e.target.value}` + (r.applied ? ` – ${r.applied} weitere von „${e.target.dataset.key}“` : "")); renderMoney(); })
      .catch((err) => toast("Fehler: " + err.message));
  }
});
$("#moneyview").addEventListener("change", async (e) => {
  const id = e.target.dataset && e.target.dataset.mvis; if (!id) return;
  const box = e.target.closest("[data-mshare]");
  const ids = [...box.querySelectorAll("input:checked")].map((i) => Number(i.value));
  try {
    await api(`/bank/accounts/${id}`, json("PATCH", { visible_to: ids }));
    toast(ids.length ? "Gespeichert – das Konto ist jetzt mitgeteilt" : "Gespeichert – das Konto siehst nur du");
    renderMoney();
  } catch (err) { e.target.checked = !e.target.checked; toast("Fehler: " + err.message); }
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || !moneyBrowsing() || document.querySelector("dialog[open]")) return;
  if (money.yearlyOpen) { money.yearlyOpen = false; closeLayer("money-yearly"); renderMoney(); }
  else if (money.sorting) { money.sorting = false; closeLayer("money-sort"); renderMoney(); }
});
$("#moneyview").addEventListener("click", async (e) => {
  const el = e.target.closest("button"); if (!el) return;
  if (el.dataset.open) return openDetail(Number(el.dataset.open));
  if (el.dataset.mperiod) { money.period = el.dataset.mperiod; renderMoney(); return; }
  if (el.dataset.myear) {
    const v = el.dataset.val, val = v === "true" ? true : v === "false" ? false : null;
    try {
      await api("/bank/yearly/mark", json("PUT", { tx_id: Number(el.dataset.myear), yearly: val }));
      toast(val === true ? "Zu den jährlichen Kosten genommen" : val === false ? "Ausgeblendet – zählt nicht mehr als jährlich" : "Markierung zurückgenommen");
      renderMoney();
    } catch (err) { toast("Fehler: " + err.message); }
    return;
  }
  if (el.dataset.mcat) {
    const was = money.cat; money.cat = was === el.dataset.mcat ? null : el.dataset.mcat;
    money.jump = money.cat ? "detail" : "bars";
    if (money.cat && !was) openLayer("money-cat", () => { money.cat = null; money.jump = "bars"; renderMoney(); });
    if (!money.cat && was) closeLayer("money-cat");
    renderMoney(); return;
  }
  if (el.dataset.mpick) return moneyPick(el.closest("[data-tx]"), el.dataset.mpick);
  if (el.dataset.mrule) {
    try { await api(`/bank/rules/${el.dataset.mrule}`, { method: "DELETE" }); toast("Regel entfernt – bereits Einsortiertes bleibt"); renderMoney(); }
    catch (err) { toast("Fehler: " + err.message); }
    return;
  }
  if (el.dataset.mvisbtn) {
    const id = Number(el.dataset.mvisbtn); money.shareOpen = money.shareOpen === id ? null : id;
    for (const box of document.querySelectorAll("#moneyview [data-mshare]")) box.classList.toggle("hidden", Number(box.dataset.mshare) !== money.shareOpen);
    for (const b of document.querySelectorAll("#moneyview [data-mvisbtn]")) b.setAttribute("aria-expanded", String(Number(b.dataset.mvisbtn) === money.shareOpen));
    return;
  }
  if (el.dataset.mdel) {
    if (!confirm("Konto samt allen importierten Umsätzen löschen? Belege im Archiv bleiben.")) return;
    try { await api(`/bank/accounts/${el.dataset.mdel}`, { method: "DELETE" }); money.account = ""; renderMoney(); } catch (err) { toast("Fehler: " + err.message); }
    return;
  }
  switch (el.dataset.mact) {
    case "import": openBankImport(); break;
    case "sort": money.sorting = true; openLayer("money-sort", () => { money.sorting = false; renderMoney(); }); renderMoney(); break;
    case "closesort": money.sorting = false; closeLayer("money-sort"); renderMoney(); break;
    case "yearly": money.yearlyOpen = true; openLayer("money-yearly", () => { money.yearlyOpen = false; renderMoney(); }); renderMoney(); window.scrollTo({ top: 0 }); break;
    case "closeyearly": money.yearlyOpen = false; closeLayer("money-yearly"); renderMoney(); break;
    case "clearsearch": money.q = ""; money.search = null; $("#search").value = ""; renderMoney(); break;
    case "closecat": money.cat = null; money.jump = "bars"; closeLayer("money-cat"); renderMoney(); break;
    case "undo":
      try { const r = await api(`/bank/imports/${money.lastImport.import_id}`, { method: "DELETE" }); toast(`${r.removed} Umsätze entfernt`); }
      catch (err) { toast("Fehler: " + err.message); }
      money.lastImport = null; renderMoney(); break;
  }
});
// Import: Datei wählen → Vorschau (erkannte Spalten, neu/schon da, erste Zeilen) → Konto wählen → importieren
let bankFile = null, bankMulti = null;   // bankMulti: Konten aus der Datei (C24: Girokonto, Pockets, Sparkonten)
function openBankImport() {
  bankFile = null; bankMulti = null; $("#bk-file").value = "";
  for (const id of ["#bk-preview", "#bk-target", "#bk-error"]) $(id).classList.add("hidden");
  $("#bk-go").disabled = true;
  const own = ((money.accounts && money.accounts.accounts) || []).filter((a) => a.is_own);
  $("#bk-account").innerHTML = own.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("") + `<option value="">Neues Konto …</option>`;
  $("#bk-people").innerHTML = ((money.accounts && money.accounts.people) || []).map((p) =>
    `<label class="toggle"><input type="checkbox" value="${p.id}" checked> ${esc(p.name)}</label>`).join("") || `<span class="muted small">Niemand – du teilst noch keine Dokumente.</span>`;
  bankTargetChanged();
  showDialog($("#bank-dialog"));
}
function bankTargetChanged() {
  const isNew = !$("#bk-account").value;
  // Nennt die Datei die Konten selbst, wählt man hier keins – nur, wer geteilte Konten sehen darf
  $("#bk-account").closest("label").classList.toggle("hidden", !!bankMulti);
  $("#bk-name-row").classList.toggle("hidden", !isNew || !!bankMulti);
  $("#bk-people-row").classList.toggle("hidden", !isNew && !bankMulti);
}
async function bankPreview() {
  if (!bankFile) return;
  const fd = new FormData(); fd.append("file", bankFile); fd.append("preview", "true");
  if ($("#bk-account").value) fd.append("account_id", $("#bk-account").value);
  $("#bk-error").classList.add("hidden"); $("#bk-go").disabled = true;
  try {
    const r = await api("/bank/import", { method: "POST", body: fd });
    const map = Object.entries(r.mapping).map(([k, v]) => `${{ date: "Datum", amount: "Betrag", counterparty: "Gegenüber", purpose: "Zweck", currency: "Währung", category: "Kategorie der Bank", kind: "Buchungsart", valuta: "Valuta", debit: "Soll", credit: "Haben", acc_no: "Konto-Nr.", acc_name: "Kontoname" }[k] || k}: „${esc(v)}“`).join(" · ");
    bankMulti = r.accounts || null;
    const accList = bankMulti ? `<div class="bk-accounts"><p class="small" style="margin:8px 0 4px"><b>${bankMulti.length} Konten in der Datei</b> – geteilte sehen die angehakten Personen unten, private nur du</p>
      ${bankMulti.map((a) => `<div class="m-tx"><span class="w"><b>${esc(a.name)}</b><small>··${esc(a.tail)} · ${a.count} Umsätze${a.existing ? ` · schon da, ${a.new} neu` : " · neu"}</small></span>
        ${a.existing ? `<span class="m-pill">wird ergänzt</span>` : `<label class="toggle small"><input type="checkbox" data-bkshare="${esc(a.key)}" checked> teilen</label>`}</div>`).join("")}</div>` : "";
    $("#bk-preview").innerHTML = `<p class="small" style="margin:0"><b>${r.bank ? esc(r.bank) + " · " : ""}${r.count} Umsätze</b> vom ${fmtDate(r.first)} bis ${fmtDate(r.last)}:
      ${r.new} neu${r.duplicates ? `, ${r.duplicates} schon da` : ""}${r.skipped ? `, ${r.skipped} Zeilen ohne Datum/Betrag übersprungen` : ""}${r.to_sort ? ` · ${r.to_sort} zum Einsortieren` : ""}</p>
      <p class="muted small" style="margin:4px 0 0">Erkannte Spalten: ${map}</p>${accList}
      <table class="bk-table">${(r.rows || []).map((x) => `<tr><td>${fmtDate(x.booked)}</td><td>${esc(x.counterparty || "")}<br><span class="muted">${esc((x.purpose || "").slice(0, 60))}</span></td>
        <td>${x.category ? esc(x.category) : `<span class="muted">offen</span>`}</td><td class="r">${eurC(x.cents)}</td></tr>`).join("")}</table>`;
    $("#bk-preview").classList.remove("hidden"); $("#bk-target").classList.remove("hidden"); bankTargetChanged();
    if (!$("#bk-name").value && r.bank) $("#bk-name").value = `${r.bank} Girokonto`;
    $("#bk-go").disabled = !r.new; $("#bk-go").textContent = r.new ? `${r.new} Umsätze importieren` : "Nichts Neues";
  } catch (e) { $("#bk-error").textContent = e.message; $("#bk-error").classList.remove("hidden"); }
}
$("#bk-file").onchange = (e) => { bankFile = e.target.files[0] || null; bankPreview(); };
$("#bk-account").onchange = () => { bankTargetChanged(); bankPreview(); };
$("#bk-go").onclick = async () => {
  const fd = new FormData(); fd.append("file", bankFile);
  const people = [...$("#bk-people").querySelectorAll("input:checked")].map((i) => i.value).join(",");
  if (bankMulti) {
    fd.append("visible_to", people);
    fd.append("shares", JSON.stringify(Object.fromEntries([...$("#bk-preview").querySelectorAll("[data-bkshare]")].map((i) => [i.dataset.bkshare, i.checked]))));
  } else if ($("#bk-account").value) fd.append("account_id", $("#bk-account").value);
  else {
    fd.append("account_name", $("#bk-name").value.trim());
    fd.append("visible_to", people);
  }
  $("#bk-go").disabled = true;
  try {
    const r = await api("/bank/import", { method: "POST", body: fd });
    money.lastImport = r; money.account = ""; money.cat = null;
    $("#bank-dialog").close();
    toast(`${r.new} Umsätze importiert` + (r.to_sort ? ` – ${r.to_sort} zum Einsortieren` : ""));
    renderMoney();
  } catch (e) { $("#bk-error").textContent = e.message; $("#bk-error").classList.remove("hidden"); $("#bk-go").disabled = false; }
};

// ---------- Suche ----------
// Ein Stichwort: schnelle Live-Suche wie bisher (state.q → /documents). Ein Satz („Gehalt letzte 6 Monate“,
// „Versicherungen von Sam“, „Was ist bald fällig?“): der Server (/search) zerlegt ihn in Filter, die als Chips
// unter dem Suchfeld stehen und sich einzeln mit ✕ entfernen lassen. Die Treffer lädt danach loadDocs() aus den
// Chips – so bleiben Sortierung, „Mehr laden“ und das Nachladen (Polling, nach dem Dokument) gleich. Wie die
// normale Suche ohne eigenen Verlaufseintrag: Zurück aus einem Dokument landet wieder bei Chips und Treffern.
let searchTimer, smartSeq = 0, smartMissing = false;
const SMART_WORD = /^(fällig\w*|faellig\w*|überfällig\w*|ueberfällig\w*|fristen?|deadlines?|due|overdue)$/i;
function smartOn() { return !!state.smart && !state.trash; }
function wantsSmart(text) {
  if (smartMissing || state.trash || !text) return false;
  if (state.mode === "folders" && state.tag) return false;   // im geöffneten Ordner: suchen in diesem Ordner (In …/Überall)
  const words = text.split(/\s+/);
  if (words.some((w) => w.startsWith("-"))) return false;   // „sam -alex“: Ausschließen kann nur die normale Suche
  return words.length > 1 || SMART_WORD.test(words[0]);
}
function plainSearch(text) {
  smartSeq++;   // eine noch laufende Satz-Suche ist damit überholt
  state.smart = null; state.q = text;
  loadDocs();
}
async function smartSearch(text) {
  const seq = ++smartSeq;
  let r;
  try { r = await api("/search?" + new URLSearchParams({ q: text, limit: 50 })); }
  catch (e) {
    if (seq !== smartSeq) return;
    if (e.status === 404) smartMissing = true;   // älterer Server ohne Satz-Suche: ab jetzt wie früher
    if (e.message !== "401") plainSearch(text);
    return;
  }
  if (seq !== smartSeq || $("#search").value.trim() !== text) return;   // inzwischen weitergetippt
  if (!r || !Array.isArray(r.chips) || !r.chips.length) return plainSearch(text);
  state.smart = { chips: r.chips, dropped: r.dropped || [] };
  state.q = text;
  renderSmartBar(); loadDocs();
}
// Parameter für /documents aus den verbliebenen Chips (Suchwörter mit ihrem Stamm, nicht der Anzeige)
function smartParams() {
  const p = { scope: "all" };
  const words = state.smart.chips.filter((c) => c.kind === "word" || c.kind === "person").map((c) => c.value || c.label);
  if (words.length) p.q = words.join(" ");
  for (const c of state.smart.chips) {
    if (c.kind === "range") { if (c.from) p.date_from = c.from; if (c.to) p.date_to = c.to; }
    else if (c.kind === "owner" && c.owner_ids && c.owner_ids.length) p.owner = c.owner_ids.join(",");
    else if (c.kind === "owner" && c.owner_id != null) p.owner = c.owner_id;
    else if (c.kind === "due") p.due = "true";
  }
  return p;
}
function renderSmartBar() {
  const bar = $("#smartbar"); bar.innerHTML = "";
  const on = smartOn();
  bar.classList.toggle("hidden", !on);
  if (!on) return;
  const lbl = document.createElement("span"); lbl.className = "bar-label"; lbl.textContent = "Gesucht:";
  bar.appendChild(lbl);
  for (const c of state.smart.chips) {
    const tagLike = (c.kind === "word" || c.kind === "person") && c.tag;
    const known = tagLike ? (state.tags || []).find((t) => t.name.toLowerCase() === String(c.value).toLowerCase()) : null;
    const color = tagLike ? tagColor(known ? known.name : c.value, known?.color) : null;
    const el = chip(c.kind === "owner" || c.kind === "person" ? `von ${c.label}` : c.label, true, () => removeSmartChip(c), color);
    el.classList.add("smart-chip"); el.dataset.kind = c.kind; el.title = "Filter entfernen";
    el.appendChild(closeX()); bar.appendChild(el);
  }
  if (state.smart.dropped.length) {
    const h = document.createElement("span"); h.className = "smart-hint muted small";
    h.textContent = `Ohne ${state.smart.dropped.map((d) => `„${d}“`).join(", ")} gesucht`;
    h.title = "Damit zusammen gab es keinen Treffer";
    bar.appendChild(h);
  }
}
function removeSmartChip(c) {
  const rest = state.smart.chips.filter((x) => x !== c);
  if (!rest.length) return clearSearch();   // ohne Filter bliebe nur „alles“ – das ist die normale Übersicht
  state.smart = { chips: rest, dropped: [] };
  renderSmartBar(); loadDocs();
}
function clearSearch() {
  clearTimeout(searchTimer);
  $("#search").value = "";
  plainSearch("");
}
// Im Reiter „Geld“ sucht das Feld oben in den Buchungen (nicht in den Dokumenten)
function moneySearch(text) { money.q = text; money.search = null; money.cat = null; renderMoney(); }
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const text = e.target.value.trim();
  if (state.mode === "money") { searchTimer = setTimeout(() => moneySearch(text), 350); return; }
  if (wantsSmart(text)) searchTimer = setTimeout(() => smartSearch(text), 500);   // erst wenn der Satz steht
  else searchTimer = setTimeout(() => plainSearch(text), text ? 250 : 0);
});
$("#search").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.isComposing) return;
  e.preventDefault();
  clearTimeout(searchTimer);
  const text = e.target.value.trim();
  if (state.mode === "money") return moneySearch(text);
  if (wantsSmart(text)) smartSearch(text); else plainSearch(text);
});

// ---------- KI-Erkennung pausieren ----------
function pauseText(until) {
  if (!until) return "";
  if (until === "forever") return "pausiert, bis du fortsetzt";
  const d = new Date(until), today = new Date();
  const time = d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
  const tomorrow = new Date(today); tomorrow.setDate(today.getDate() + 1);
  const day = d.toDateString() === today.toDateString() ? "" : d.toDateString() === tomorrow.toDateString() ? "morgen " : fmtDate(d.toISOString().slice(0, 10)) + " ";
  return `pausiert bis ${day}${time} Uhr`;
}
function renderLlmPause(until) {
  $("#s-llm-pause-status").textContent = until ? "Erkennung " + pauseText(until) : "Erkennung läuft.";
  $("#s-llm-pause-status").classList.toggle("warn", !!until);
  $("#s-llm-resume-btn").classList.toggle("hidden", !until);
}
function pauseEnd(choice) {
  const d = new Date();
  if (choice === "forever") return "forever";
  if (choice === "evening") { d.setHours(22, 0, 0, 0); if (d <= new Date()) d.setDate(d.getDate() + 1); }
  else if (choice === "morning") { d.setDate(d.getDate() + 1); d.setHours(7, 0, 0, 0); }
  else d.setMinutes(d.getMinutes() + Number(choice));
  return d.toISOString();
}
$("#s-llm-pause-btn").onclick = async () => {
  try {
    const r = await api("/settings/llm/pause", json("POST", { until: pauseEnd($("#s-llm-pause-for").value) }));
    renderLlmPause(r.llm_paused_until); toast("KI-Erkennung " + pauseText(r.llm_paused_until)); updateProgress();
  } catch (e) { toast("Fehler: " + e.message); }
};
$("#s-llm-resume-btn").onclick = async () => {
  try {
    const r = await api("/settings/llm/pause", json("POST", { until: null }));
    renderLlmPause(r.llm_paused_until); toast("KI-Erkennung läuft wieder"); updateProgress();
  } catch (e) { toast("Fehler: " + e.message); }
};

async function updateProgress() {
  const h = await fetch("/health").then((r) => (r.ok ? r.json() : null)).catch(() => null);
  if (!h) return false;
  const c = h.documents || {};
  const open = (c.pending || 0) + (c.processing || 0) + (c.tagging || 0);
  const total = Object.values(c).reduce((a, b) => a + b, 0);
  const box = $("#progress");
  if (!open) {
    box.classList.add("hidden"); state.progressStart = null;
    if (h.llm_failed) toastOnce(`${h.llm_failed} Dokument(e) ohne LLM-Tags – wird automatisch nachgeholt, sobald das LLM erreichbar ist`);
    else if (h.backup?.stale && state.me?.is_admin) toastOnce("Sicherung veraltet oder fehlgeschlagen – siehe Einstellungen → Ablage");
    return false;
  }
  const done = total - open;
  if (!state.progressStart) state.progressStart = { t: Date.now(), done };
  const elapsed = (Date.now() - state.progressStart.t) / 1000;
  const rate = (done - state.progressStart.done) / Math.max(elapsed, 1);
  const eta = rate > 0 ? Math.round(open / rate / 60) : null;
  box.classList.remove("hidden");
  $("#progress-label").textContent = `${done} von ${total} verarbeitet · ${c.pending || 0} warten · ${c.processing || 0} OCR · ${c.tagging || 0} Tagging`;
  // Pausiert: keine Restzeit schätzen, stattdessen sagen, bis wann
  $("#progress-eta").textContent = h.llm_paused_until && c.tagging ? "KI-Erkennung " + pauseText(h.llm_paused_until)
    : eta !== null && elapsed > 20 ? `noch ca. ${eta < 60 ? eta + " min" : (eta / 60).toFixed(1) + " h"}` : "";
  $("#progress-bar").style.width = `${Math.round(100 * done / Math.max(total, 1))}%`;
  return true;
}
let _toasted = "";
function toastOnce(msg) { if (_toasted !== msg) { _toasted = msg; toast(msg, 5000); } }

function startPolling() {
  clearInterval(state.poll);
  updateProgress();
  state.poll = setInterval(async () => {
    if ($("#main").classList.contains("hidden")) return;
    const busy = await updateProgress();
    // In „Heute“ gibt es keine Kacheln – dort hält „Neu“ fest, ob noch etwas verarbeitet wird
    const todayBusy = todayBrowsing() && (today.data?.recent || []).some((d) => d.status !== "done");
    if (busy || todayBusy || state.docs.some((d) => d.status !== "done")) {
      await Promise.all([loadDocs(), loadTags(), loadYears()]);
    }
  }, 5000);
}

// ---------- Fragen ----------
function closeAsk() { $("#ask-panel").classList.add("hidden"); closeLayer("ask"); }
$("#ask-btn").onclick = () => {
  if (!$("#ask-panel").classList.contains("hidden")) return closeAsk();
  $("#ask-panel").classList.remove("hidden"); $("#ask-input").focus();
  openLayer("ask", () => $("#ask-panel").classList.add("hidden"));
};
$("#ask-close").onclick = closeAsk;
$("#ask-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#ask-input").value.trim(); if (!q) return;
  const st = $("#ask-status"), an = $("#ask-answer"), so = $("#ask-sources");
  st.classList.remove("hidden"); st.textContent = "Suche passende Dokumente und frage das LLM … (10–40 s)";
  an.classList.add("hidden"); so.classList.add("hidden"); $("#ask-submit").disabled = true;
  try {
    const r = await api("/ask", json("POST", { question: q, scope: state.scope }));
    const span = r.range ? `Zeitraum ${fmtDate(r.range.from)} – ${fmtDate(r.range.to)}` : "";
    const kw = r.keywords?.length ? "Gesucht nach: " + r.keywords.join(", ") : "";
    st.textContent = [span, kw].filter(Boolean).join(" · ");
    an.classList.remove("hidden");
    an.innerHTML = esc(r.answer).replace(/\[Dok\s*(\d+)\]/g, (m, n) => `<span class="src" data-n="${n}">${n}</span>`);
    for (const el of an.querySelectorAll(".src")) el.onclick = () => { const src = r.sources.find((x) => String(x.n) === el.dataset.n); if (src) openDetail(src.id); };
    so.innerHTML = ""; so.classList.toggle("hidden", !r.sources.length);
    for (const src of r.sources) {
      const c = chip(`${src.title}${src.document_date ? " · " + fmtDate(src.document_date) : ""}`, false, () => openDetail(src.id));
      const b = document.createElement("b"); b.textContent = src.n; c.insertBefore(b, c.firstChild); so.appendChild(c);
    }
  } catch (err) { st.textContent = "Fehler: " + err.message; }
  $("#ask-submit").disabled = false;
});

// ---------- Upload ----------
$("#file-input").addEventListener("change", (e) => uploadFiles([...e.target.files]));
// Nur echte Dateien von außen – Ordner oder Tags, die man innerhalb der Seite zieht, sind kein Upload
let dragDepth = 0;
const isFileDrag = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
document.addEventListener("dragenter", (e) => { if (!isFileDrag(e)) return; e.preventDefault(); if (++dragDepth === 1) $("#dropzone").classList.remove("hidden"); });
document.addEventListener("dragleave", (e) => { if (!isFileDrag(e)) return; if (--dragDepth <= 0) { dragDepth = 0; $("#dropzone").classList.add("hidden"); } });
document.addEventListener("dragover", (e) => { if (isFileDrag(e)) e.preventDefault(); });
document.addEventListener("drop", (e) => {
  if (!isFileDrag(e)) return;
  e.preventDefault(); dragDepth = 0; $("#dropzone").classList.add("hidden");
  const all = [...e.dataTransfer.files];
  const pdfs = all.filter((f) => f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf"));
  if (all.length > pdfs.length) toast(`${all.length - pdfs.length} Datei(en) übersprungen – hochladen geht im Browser nur mit PDF`, 4500);
  uploadFiles(pdfs);
});
async function uploadFiles(files) {
  if (!files.length) return;
  // Im geöffneten Ordner bzw. bei gewähltem Tag: das Dokument bekommt diesen Tag und liegt damit dort
  const into = !state.trash && state.tag ? state.tag : null;
  let ok = 0;
  for (const f of files) {
    const fd = new FormData(); fd.append("file", f);
    if (into) fd.append("tags", into);
    try { await api("/documents", { method: "POST", body: fd }); ok++; }
    catch (e) { toast(`${f.name}: ${e.message}`, 4000); }
  }
  if (ok) toast(into ? `${ok} Dokument(e) hochgeladen – in „${into}“` :
    folderBrowsing() ? `${ok} Dokument(e) hochgeladen – noch ohne Ordner, zu finden unter „Kacheln“ oder „Zu prüfen“` : `${ok} Dokument(e) hochgeladen`, 4000);
  $("#file-input").value = "";
  if (state.scope === "partner") { state.scope = "mine"; renderScopeBar(); await loadTags(); }
  await loadDocs();
}

// ---------- Detail ----------
async function openDetail(id) {
  const d = await api(`/documents/${id}?include_text=true`);
  state.current = d;
  show("#detail");
  setPdf(`${d.file_url}#view=FitH`);
  $("#download-btn").href = d.file_url + "?download=1";
  $("#share-btn").classList.toggle("hidden", !d.is_own);
  $("#m-owner").classList.toggle("hidden", d.is_own);
  $("#m-owner").textContent = d.is_own ? "" : `Dokument von ${d.owner_name}${d.can_edit ? "" : " · nur lesen"}`;
  const canAssign = d.can_edit && (state.owners || []).some((u) => u.id === d.owner_id) && state.owners.length > 1;
  $("#m-owner-row").classList.toggle("hidden", !canAssign);
  $("#m-owner-select").value = String(d.owner_id);
  $("#m-title").value = d.title || "";
  $("#m-date").value = isoToDmy(d.document_date);
  updateDateHint(); renderDetailReasons(d);
  $("#m-sender").value = d.sender || "";
  $("#m-amount").value = d.amount ?? "";
  $("#m-currency").value = d.currency || "";
  $("#m-direction").value = d.direction || "";
  fillDue(d);
  $("#m-due-note").value = d.due_note || "";
  loadAssignees(d);
  fillWarranty(d); fillBlick(d);
  $("#m-summary").classList.toggle("hidden", !d.summary);
  $("#m-summary-text").textContent = d.summary || "";
  $("#m-llm-hint").classList.toggle("hidden", !d.tags.some((t) => t.source === "llm"));
  const src = { pdf: "Text aus PDF", ocr: "OCR", client: "Text vom Gerät" }[d.text_source] || "kein Text";
  $("#m-info").textContent = `${d.page_count} Seite(n) · ${src} · ${d.original_name || ""} · hinzugefügt ${fmtDate(d.created_at)}` +
    (d.ext_from && !d.ext_pending ? ` · über Briefkasten „${d.ext_from}“` : "");
  $("#m-ext").classList.toggle("hidden", !d.ext_pending);
  $("#m-ext-from").textContent = d.ext_from ? `über „${d.ext_from}“` : "";
  $("#m-ext-accept").disabled = $("#m-ext-delete").disabled = !d.can_edit;
  $("#save-state").textContent = "";
  for (const el of ["#m-title", "#m-date", "#m-sender", "#m-amount", "#m-currency", "#m-direction", "#m-due", "#m-due-note", "#m-due-done", "#m-due-repeat", "#m-due-assignee", "#m-warranty", "#m-expires", "#m-service", "#m-refund", "#m-tag-input", "#save-btn", "#delete-btn", "#reprocess-btn"])
    $(el).disabled = !d.can_edit;
  renderMetaTags(d.tags.map((t) => t.name));
  // Personen ohne Konto (Familie): eigene kleine Zeile über den Feldern
  const persons = d.tags.filter((t) => t.kind === "person");
  $("#m-persons").classList.toggle("hidden", !persons.length);
  $("#m-persons").innerHTML = persons.length ? `<span class="muted small">Für</span>` +
    persons.map((t) => `<span class="person-pill" style="--tag:${tagColor(t.name, t.color)}">${esc(t.name)}</span>`).join("") : "";
  history.pushState({ id }, "", `#doc-${id}`);
}
// Frist-Felder, die sich beim Speichern ändern können (wiederkehrend erledigt → nächster Termin, wieder offen)
function fillDue(d) {
  $("#m-due").value = isoToDmy(d.due_date);
  $("#m-due-done").checked = !!d.due_done;
  $("#m-due-repeat").value = d.due_repeat || "";
  $("#m-due-done").parentElement.title = d.due_last_done ? `zuletzt erledigt ${fmtDate(d.due_last_done)}` : "";
  $("#m-due-ics").classList.toggle("hidden", !d.due_date);
  $("#m-due-ics").href = `/documents/${d.id}/due.ics`;
}
// „Wer kümmert sich?“: nur Personen, die das Dokument sehen. Eine eingetragene Person, die ich nicht kenne,
// bleibt als „jemand anderes“ stehen und wird nur überschrieben, wenn ich etwas anderes wähle.
async function loadAssignees(d) {
  const row = $("#m-due-assignee-row"), sel = $("#m-due-assignee");
  row.classList.add("hidden");
  const list = await api(`/documents/${d.id}/assignees`).catch(() => []);
  if (state.current?.id !== d.id) return;
  const cur = state.current.due_assignee;
  const opts = [`<option value="">Niemand bestimmt</option>`,
    ...list.map((u) => `<option value="${u.id}">${esc(u.id === state.me?.id ? `Ich (${u.display_name})` : u.display_name)}</option>`)];
  if (cur != null && !list.some((u) => u.id === cur)) opts.push(`<option value="${cur}">${esc(state.current.due_assignee_name || "jemand anderes")}</option>`);
  sel.innerHTML = opts.join("");
  sel.value = cur == null ? "" : String(cur);
  sel._initial = sel.value;
  row.classList.toggle("hidden", list.length < 2 && cur == null);
}
function updateDateHint() {
  // „Hat kein Datum“: bestätigt leer – kein Aufruf zum Eintragen, nur der Vermerk; ein eingetragenes Datum hebt ihn auf
  const empty = !$("#m-date").value.trim(), none = !!state.current?.no_date;
  $("#m-date-hint").classList.toggle("hidden", !empty);
  $("#m-date-hint-text").textContent = none ? "Ohne Datum abgelegt" : "Kein Datum erkannt – bitte eintragen";
  $("#m-no-date").classList.toggle("hidden", none || !state.current?.can_edit);
}
$("#m-no-date").onclick = async () => {
  const d = state.current; if (!d) return;
  try {
    state.current = { ...(await api(`/documents/${d.id}`, json("PATCH", { no_date: true }))), text: d.text };
    $("#m-date").value = ""; updateDateHint(); renderDetailReasons(state.current);
    toast("Ohne Datum abgelegt – nicht mehr unter „Zu prüfen“");
    loadDocs();
  } catch (e) { toast("Fehler: " + e.message); }
};
$("#m-date").addEventListener("input", updateDateHint);
$("#m-date").closest(".datefield").querySelector(".dmy-picker").addEventListener("change", updateDateHint);
function renderDetailReasons(d) {
  const r = reviewReasons(d);
  $("#m-reasons").classList.toggle("hidden", !r.length);
  $("#m-reasons").textContent = r.length ? "Zu prüfen: " + r.join(" · ") : "";
}
function renderMetaTags(names) {
  const box = $("#m-tags"); box.innerHTML = ""; box._names = names;
  for (const n of names) {
    const known = state.tags.find((t) => t.name.toLowerCase() === n.toLowerCase());
    const c = chip(n, false, null, tagColor(n, known && known.color));
    if (state.current?.can_edit) {
      const x = document.createElement("span"); x.className = "x"; x.textContent = "✕";
      x.onclick = () => renderMetaTags(names.filter((m) => m !== n));
      c.appendChild(x);
    }
    box.appendChild(c);
  }
}
$("#m-tag-input").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  e.preventDefault();
  const v = e.target.value.trim(); if (!v) return;
  const names = $("#m-tags")._names;
  if (!names.some((n) => n.toLowerCase() === v.toLowerCase())) renderMetaTags([...names, v]);
  e.target.value = "";
});
// ---------- Datum im Format der Browsersprache ----------
// Das native <input type="date"> richtet sich nach der Oberflächensprache des Browsers, nicht nach der
// bevorzugten Sprache – ein deutscher Nutzer mit englischem Chrome sah MM/TT/JJJJ. Deshalb Textfelder
// im Format der ersten bevorzugten Sprache: Deutsch TT.MM.JJJJ, USA MM/DD/YYYY, UK DD/MM/YYYY usw.
// Einstellbar unter Einstellungen → Darstellung (pro Browser); „auto“ nimmt die erste bevorzugte Sprache.
var DATE_FORMATS = {
  "dmy.": { order: ["day", "month", "year"], sep: ".", placeholder: "TT.MM.JJJJ" },
  "dmy/": { order: ["day", "month", "year"], sep: "/", placeholder: "DD/MM/YYYY" },
  "mdy/": { order: ["month", "day", "year"], sep: "/", placeholder: "MM/DD/YYYY" },
  "ymd-": { order: ["year", "month", "day"], sep: "-", placeholder: "JJJJ-MM-TT" },
};
var _dateFmt;   // var statt const: fmtDate() kann schon vor dieser Zeile laufen
function autoDateFormat() {
  const lang = (navigator.languages && navigator.languages[0]) || navigator.language || "de-DE";
  let parts;
  try { parts = new Intl.DateTimeFormat(lang, { year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date(2026, 10, 15)); }
  catch { return DATE_FORMATS["dmy."]; }
  const order = parts.filter((p) => ["day", "month", "year"].includes(p.type)).map((p) => p.type);
  const sep = (parts.find((p) => p.type === "literal") || { value: "." }).value.trim() || ".";
  const de = lang.toLowerCase().startsWith("de");
  const letter = { day: de ? "TT" : "DD", month: "MM", year: de ? "JJJJ" : "YYYY" };
  return { order, sep, placeholder: order.map((t) => letter[t]).join(sep) };
}
function dateFmt() {
  if (!_dateFmt) _dateFmt = DATE_FORMATS[localStorage.getItem("bl.datefmt")] || autoDateFormat();
  return _dateFmt;
}
function isoToDmy(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
  if (!m) return "";
  const v = { year: m[1], month: m[2], day: m[3] };
  return dateFmt().order.map((t) => v[t]).join(dateFmt().sep);
}
/** "" bei leerem Feld, null bei ungültiger Eingabe, sonst JJJJ-MM-TT. Trennzeichen egal, zweistelliges Jahr = 20xx. */
function dmyToIso(s) {
  s = (s || "").trim();
  if (!s) return "";
  let v, m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(s);
  if (m) v = { year: m[1], month: m[2], day: m[3] };
  else {
    m = /^(\d{1,4})[./\-\s](\d{1,2})[./\-\s](\d{1,4})$/.exec(s);
    const nums = m ? [m[1], m[2], m[3]] : (/^\d{8}$/.test(s) ? null : undefined);
    if (nums === undefined) return null;
    if (nums === null) {  // 15112026 bzw. 11152026: Längen aus der Reihenfolge
      const len = { day: 2, month: 2, year: 4 }; let i = 0; v = {};
      for (const t of dateFmt().order) { v[t] = s.slice(i, i + len[t]); i += len[t]; }
    } else {
      v = {}; dateFmt().order.forEach((t, i) => (v[t] = nums[i]));
    }
    if (v.year.length === 2) v.year = "20" + v.year;
    if (v.year.length !== 4) return null;
  }
  const y = +v.year, mo = +v.month, d = +v.day;
  const dt = new Date(Date.UTC(y, mo - 1, d));
  if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== mo - 1 || dt.getUTCDate() !== d) return null;
  return `${v.year}-${String(mo).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}
for (const f of document.querySelectorAll(".datefield")) {
  const text = f.querySelector(".dmy"), pick = f.querySelector(".dmy-picker");
  f.querySelector(".dmy-btn").onclick = (e) => {
    e.preventDefault();
    if (text.disabled) return;
    pick.value = dmyToIso(text.value) || "";
    try { pick.showPicker(); } catch { pick.focus(); }
  };
  pick.onchange = () => { if (pick.value) { text.value = isoToDmy(pick.value); text.classList.remove("invalid"); } };
  text.onblur = () => {
    const iso = dmyToIso(text.value);
    text.classList.toggle("invalid", iso === null);
    if (iso) text.value = isoToDmy(iso);
  };
  text.oninput = () => text.classList.remove("invalid");
}

$("#save-btn").onclick = async () => {
  const d = state.current;
  const docDate = dmyToIso($("#m-date").value), dueDate = dmyToIso($("#m-due").value);
  const expDate = dmyToIso($("#m-expires").value), svcDate = dmyToIso($("#m-service").value);
  const bad = [[docDate, "#m-date"], [dueDate, "#m-due"], [expDate, "#m-expires"], [svcDate, "#m-service"]].find(([v]) => v === null);
  if (bad) {
    $(bad[1]).classList.add("invalid");
    toast(`Datum bitte als ${dateFmt().placeholder} eingeben`); return;
  }
  const body = {
    title: $("#m-title").value.trim() || d.title,
    document_date: docDate || null,
    sender: $("#m-sender").value.trim() || null,
    amount: $("#m-amount").value === "" ? null : Number($("#m-amount").value),
    currency: $("#m-currency").value.trim().toUpperCase() || null,
    ...($("#m-direction").value ? { direction: $("#m-direction").value } : {}),
    due_date: dueDate,
    due_note: $("#m-due-note").value.trim() || null,
    due_done: $("#m-due-done").checked,
    due_repeat: $("#m-due-repeat").value || null,
    tags: $("#m-tags")._names,
  };
  const who = $("#m-due-assignee");
  if (!$("#m-due-assignee-row").classList.contains("hidden") && who.value !== who._initial)
    body.due_assignee = who.value ? Number(who.value) : null;
  if ($("#m-expires").value !== $("#m-expires")._initial) body.expires_on = expDate || "";
  if ($("#m-service").value !== $("#m-service")._initial) body.service_next = svcDate || "";
  if (!$("#m-refund-row").classList.contains("hidden") && $("#m-refund").value !== $("#m-refund")._initial) body.refund_state = $("#m-refund").value;
  // Gewährleistung nur mitschicken, wenn geändert – sonst würde Speichern „nicht festgelegt“ zu einer Korrektur machen
  const wsel = $("#m-warranty");
  if (wsel.value !== wsel._initial && wsel.value !== "") {
    const [m, kind] = wsel.value.split(":");
    body.warranty_months = Number(m); if (kind) body.warranty_kind = kind;
  }
  try {
    state.current = await api(`/documents/${d.id}`, json("PATCH", body));
    // Besitzer getrennt, denn das Zuordnen prüft andere Rechte als das Bearbeiten
    const owner = Number($("#m-owner-select").value);
    if (!$("#m-owner-row").classList.contains("hidden") && owner && owner !== d.owner_id) {
      await api("/documents/owner", json("POST", { document_ids: [d.id], owner_id: owner }));
      // Ohne Freigabe des neuen Besitzers ist das Dokument für mich jetzt unsichtbar – dann zurück zur Liste
      const fresh = await api(`/documents/${d.id}?include_text=true`).catch(() => null);
      if (!fresh) { toast("Zugeordnet"); closeDetail(); await Promise.all([loadDocs(), loadTags(), loadYears()]); return; }
      state.current = fresh;
    }
    renderDetailReasons(state.current); updateDateHint();
    fillDue(state.current); fillWarranty(state.current); fillBlick(state.current);
    $("#m-due-note").value = state.current.due_note || "";
    loadAssignees(state.current);
    $("#save-state").textContent = "Gespeichert";
    toast(body.due_repeat && body.due_done && !state.current.due_done
      ? `Erledigt – nächste Frist ${fmtDate(state.current.due_date)}` : "Gespeichert");
  } catch (e) { toast("Fehler: " + e.message); }
};
$("#m-ext-accept").onclick = async () => {
  const d = state.current; if (!d) return;
  try {
    state.current = await api(`/documents/${d.id}/accept`, { method: "POST" });
    $("#m-ext").classList.add("hidden");
    toast("Übernommen");
  } catch (e) { toast("Fehler: " + e.message); }
};
$("#m-ext-delete").onclick = () => $("#delete-btn").click();
$("#delete-btn").onclick = async () => {
  if (!confirm("Dokument in den Papierkorb legen?")) return;
  await api(`/documents/${state.current.id}`, { method: "DELETE" });
  toast("Im Papierkorb"); closeDetail();
};
$("#reprocess-btn").onclick = async () => {
  await api(`/documents/${state.current.id}/reprocess`, { method: "POST" });
  toast("Wird erneut analysiert"); closeDetail();
};
$("#back-btn").onclick = () => history.back();
window.addEventListener("popstate", (e) => {
  if ($("#detail").classList.contains("hidden")) return;
  if (e.state && state.current && e.state.id === state.current.id) return;   // nur ein Dialog darüber wurde zu
  closeDetail(false);
});
window.addEventListener("popstate", (e) => {
  if (skipTrashPop) { skipTrashPop = false; return; }
  if (!$("#detail").classList.contains("hidden") || !$("#settings").classList.contains("hidden")) return;
  const back = e.state && "folder" in e.state ? e.state.folder : null;   // Ordner, in dem man vorher war
  if (state.trash && !(e.state && e.state.trash)) openTrash(false, true, back);
  else if (!state.trash && e.state && e.state.trash) openTrash(true, true);   // „Vor“
});
// PDF-Ansicht jedes Mal als neues iframe: ein Wechsel von iframe.src legt einen Eintrag im Browser-Verlauf
// an, und „Zurück“ blättert dann im PDF statt in der Seite (z. B. kam man so nicht mehr aus einem Ordner).
function setPdf(url) {
  const old = $("#pdf-view"), f = old.cloneNode(false);
  f.removeAttribute("src");
  if (url) f.src = url;
  old.replaceWith(f);
}
async function closeDetail(pop = true) {
  setPdf(null);
  show("#main");
  await Promise.all([loadDocs(), loadTags(), loadYears()]);
  if (pop && location.hash) { skipFolderPop = true; history.back(); }
}

// Logo oben links: zur Startseite „Heute“ – offene Ebenen (Einstellungen, Kategorie, Jährliche Kosten …), Dokument
// und Suche schließen wie von Hand
$("#brand-home").addEventListener("click", async () => {
  if (location.hash.startsWith("#doc-")) await closeDetail(true);
  const n = layers.length;
  while (layers.length) layers.pop().close();
  if (n) { skipLayerPops++; history.go(-n); }
  if ($("#search").value || state.q || money.q) {
    $("#search").value = ""; state.q = ""; state.smart = null; money.q = ""; money.search = null; smartSeq++; renderSmartBar();
  }
  $('#mode-seg button[data-mode="today"]').click();
  window.scrollTo({ top: 0, behavior: "smooth" });
});

// ---------- Einstellungen ----------
$("#settings-btn").onclick = openSettings;
// Zur Übersicht – per Knopf oder Browser-Zurück
function leaveSettings() { show("#main"); enterMain(); }
$("#settings-back").onclick = () => { closeLayer("settings"); leaveSettings(); };
$("#logout-btn").onclick = async () => { await api("/auth/logout", { method: "POST" }).catch(() => {}); showLogin(); };

async function openSettings() {
  settingsGen++;
  show("#settings");
  openLayer("settings", leaveSettings);
  [state.me, state.users] = await Promise.all([api("/auth/me"), api("/users")]);
  const me = state.me;
  $("#s-hello").textContent = me.display_name;
  fetch("/health").then((r) => r.json()).then((h) => { if (!h.version) return; $("#s-version").textContent = h.version ? `Server ${h.version}` : ""; }).catch(() => {});
  $("#s-show-partner").checked = me.show_partner_in_overview;
  $("#s-name").value = me.display_name;
  $("#s-pass").value = ""; $("#s-pass-old").value = "";
  $("#s-pass-old-row").classList.toggle("hidden", me.is_admin);
  // Demo: gemeinsames Konto – Name, Passwort, Geräte und Freigaben sperrt der Server (app/demo.py)
  $("#s-demo-account").classList.toggle("hidden", !me.demo);
  for (const id of ["#s-account-edit", "#s-sessions-revoke", "#s-share-add-row"]) $(id).classList.toggle("hidden", !!me.demo);

  renderLinks();
  $("#s-public-row").classList.toggle("hidden", !me.is_admin);
  if (me.is_admin) api("/settings").then((st) => { $("#s-public-url").value = st.public_url || ""; }).catch(() => {});

  // Freigaben, die ich erteilt habe
  const box = $("#s-shares"); box.innerHTML = "";
  if (!me.shared_by_me.length) box.innerHTML = `<p class="muted small">Noch niemand.</p>`;
  for (const s of me.shared_by_me) {
    const row = document.createElement("div"); row.className = "list-row";
    row.innerHTML = `<span>${esc(s.display_name)}</span><span class="muted small">${s.can_edit ? "darf bearbeiten" : "nur lesen"}</span><span class="spacer"></span>${me.demo ? "" : `<button class="btn ghost danger">Entfernen</button>`}`;
    if (!me.demo) row.querySelector("button").onclick = async () => { await api(`/shares/${s.id}`, { method: "DELETE" }); openSettings(); };
    box.appendChild(row);
  }
  const sel = $("#s-share-user"); sel.innerHTML = "";
  const candidates = state.users.filter((u) => u.id !== me.id && !me.shared_by_me.some((s) => s.id === u.id));
  for (const u of candidates) sel.innerHTML += `<option value="${u.id}">${esc(u.display_name)}</option>`;
  $("#s-share-add").disabled = !candidates.length;

  $("#s-shared-with-me").innerHTML = me.shared_with_me.length
    ? me.shared_with_me.map((s) => `<div class="list-row"><span>${esc(s.display_name)}</span><span class="muted small">${s.can_edit ? "bearbeiten" : "nur lesen"}</span></div>`).join("")
    : `<p class="muted small">Niemand hat dir seinen Pot freigegeben.</p>`;

  // Geräte
  const sessions = await api("/auth/sessions");
  $("#s-sessions").innerHTML = sessions.map((x) => `<div class="list-row"><span>${esc(x.label || "Gerät")}${x.current ? ' <span class="muted small">· dieses Gerät</span>' : ""}</span><span class="muted small">${esc(x.ip || "")} · zuletzt ${fmtDate(x.last_used)}</span><span class="spacer"></span>${x.current ? "" : `<button class="btn ghost danger" data-sid="${x.id}">Abmelden</button>`}</div>`).join("");
  for (const b of $("#s-sessions").querySelectorAll("button")) b.onclick = async () => { await api(`/auth/sessions/${b.dataset.sid}`, { method: "DELETE" }); openSettings(); };
  $("#s-sessions-revoke").onclick = async () => { if (confirm("Alle anderen Geräte abmelden?")) { await api("/auth/sessions", { method: "DELETE" }); toast("Abgemeldet"); openSettings(); } };

  // Tags verwalten, KI-Vorschläge zurücksetzen, Regeln
  state.tags = await api("/tags?scope=all");
  renderSettingsTags();
  $("#s-tag-suggestions").innerHTML = ""; $("#s-tag-suggest-status").textContent = "";
  if (me.is_admin) { await renderRules(); await renderSenders(); }
  await renderContracts(); renderPackageForm();

  // KI
  const cfg = me.is_admin ? await api("/settings") : {};   // Server-Einstellungen sind nur für Admins
  $("#s-ai").classList.toggle("hidden", !me.is_admin);
  $("#s-llm-enabled").checked = cfg.llm_enabled;
  if (me.is_admin) renderLlmPause(cfg.llm_paused_until);
  $("#s-llm-url").value = cfg.llm_url || "";
  $("#s-llm-model").value = cfg.llm_model || "";
  $("#s-llm-url2").value = cfg.llm_url2 || "";
  $("#s-llm-model2").value = cfg.llm_model2 || "";
  $("#s-llm-askmodel").value = cfg.llm_ask_model || "";
  $("#s-llm-askmodel2").value = cfg.llm_ask_model2 || "";
  if (me.is_admin) renderLlmCloud(cfg);
  $("#s-llm-restricted").checked = cfg.llm_tag_mode === "restricted";
  $("#s-return-window").checked = !!cfg.return_window;
  $("#s-ocr-langs").value = cfg.ocr_langs || "";
  $("#s-llm-result").classList.add("hidden");
  $("#s-llm-result2").classList.add("hidden");
  $("#s-storage").classList.toggle("hidden", !me.is_admin);
  $("#s-trash-days").value = String(cfg.trash_days);
  $("#s-dup").value = cfg.duplicate_mode;
  $("#s-thumb").value = String(cfg.thumb_width);
  if (me.is_admin) {
    $("#s-bk-dir").value = cfg.backup_dir || "";
    $("#s-bk-hour").innerHTML = Array.from({ length: 24 }, (_, i) => `<option value="${i}">${String(i).padStart(2, "0")}:00</option>`).join("");
    $("#s-bk-hour").value = String(cfg.backup_hour ?? 3);
    $("#s-bk-days").value = String(cfg.backup_keep_days ?? 14);
    $("#s-bk-weeks").value = String(cfg.backup_keep_weeks ?? 8);
    renderBackup();
  }
  $("#s-export-all").classList.toggle("hidden", !me.is_admin);
  $("#s-mail").classList.toggle("hidden", !me.is_admin);
  if (me.is_admin) renderMail();
  const ib = await api("/settings/inbox");
  $("#s-inbox").innerHTML = `<div class="list-row"><span>FTP-Dienst</span><span class="muted small">${ib.ftp ? "installiert" : "nicht gefunden"}</span><span class="spacer"></span><span class="muted small">alle ${ib.poll_seconds} s</span></div>` +
    ib.folders.map((f) => `<div class="list-row"><span>${esc(f.username)}</span><span class="muted small"><code>${esc(f.path)}</code></span><span class="spacer"></span><span class="small ${f.errors ? "error" : "muted"}">${f.waiting} wartend${f.errors ? ` · ${f.errors} Fehler` : ""}</span></div>`).join("");
  applyTheme();

  $("#s-admin").classList.toggle("hidden", !me.is_admin);
  // Gemeinsamen Tag-Pool, Absender und Regeln verwalten nur Admins
  for (const id of ["#s-sect-tags", "#s-sect-senders", "#s-sect-rules"]) $(id).classList.toggle("hidden", !me.is_admin);
  if (me.is_admin) {
    renderInvites();
    const ul = $("#s-users"); ul.innerHTML = "";
    for (const u of state.users) {
      const row = document.createElement("div"); row.className = "list-row";
      row.innerHTML = `<span>${esc(u.display_name)}</span><span class="muted small">${esc(u.username)}${u.is_admin ? " · Admin" : ""}</span><span class="spacer"></span>` +
        `<button class="btn ghost" data-act="pw">Passwort setzen</button>` +
        (u.id !== me.id ? `<button class="btn ghost danger" data-act="del">Löschen</button>` : "");
      row.querySelector('[data-act="pw"]').onclick = async () => {
        const pw = prompt(`Neues Passwort für ${u.display_name} (mindestens 8 Zeichen). Alle Geräte dieses Nutzers werden abgemeldet.`);
        if (!pw) return;
        try { await api(`/users/${u.id}`, json("PATCH", { password: pw })); toast("Passwort gesetzt"); }
        catch (e) { toast("Fehler: " + e.message, 4000); }
      };
      const b = row.querySelector('[data-act="del"]');
      if (b) b.onclick = async () => {
        if (!confirm(`${u.display_name} und alle seine Dokumente löschen?`)) return;
        await api(`/users/${u.id}`, { method: "DELETE" }); openSettings();
      };
      ul.appendChild(row);
    }
  }
}
$("#s-tag-new-btn").onclick = async () => {
  const name = $("#s-tag-new").value.trim(); if (!name) return;
  const have = state.tags.find((t) => t.name.toLowerCase() === name.toLowerCase());
  if (have) { toast(`„${have.name}“ gibt es schon – Ordnernamen sind eindeutig`, 4000); return; }
  try {
    await api("/tags", json("POST", { name, parent_id: Number($("#s-tag-new-parent").value) || null }));
    $("#s-tag-new").value = "";
    state.tags = await api("/tags?scope=all"); renderSettingsTags(); toast(`„${name}“ angelegt`);
  } catch (e) { toast("Fehler: " + e.message); }
};
$("#s-tag-new").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#s-tag-new-btn").click(); } });
const tagSel = new Set();
function renderSettingsTags() {
  const box = $("#s-tags"); box.innerHTML = "";
  const pathName = (t) => tagPath(t.name).map((x) => x.name).join(" › ");
  const sorted = [...state.tags].sort((a, b) => pathName(a).localeCompare(pathName(b), "de"));
  const ps = $("#s-tag-new-parent"), keep = ps.value;
  ps.innerHTML = ""; ps.append(new Option("oberste Ebene", ""));
  for (const t of sorted) ps.append(new Option("in " + pathName(t), String(t.id)));
  ps.value = [...ps.options].some((o) => o.value === keep) ? keep : "";
  const move = async (ids, parent) => {
    for (const id of ids) {
      const t = state.tags.find((x) => x.id === id);
      await api(`/tags/${id}`, json("PATCH", { name: t.name, color: t.color, parent_id: parent || 0 })).catch((err) => toast("Fehler: " + err.message));
    }
    state.tags = await api("/tags?scope=all"); renderSettingsTags();
  };
  box.ondragover = (e) => { if (e.target === box) { e.preventDefault(); box.classList.add("drop-root"); } };
  box.ondragleave = () => box.classList.remove("drop-root");
  box.ondrop = (e) => { box.classList.remove("drop-root"); if (e.target !== box) return; e.preventDefault(); move([Number(e.dataTransfer.getData("text/tag"))], null); };
  for (const t of sorted) {
    const c = chip(pathName(t), false, null, tagColor(t.name, t.color));
    c.draggable = true;
    c.ondragstart = (e) => e.dataTransfer.setData("text/tag", String(t.id));
    c.ondragover = (e) => { e.preventDefault(); c.classList.add("drop-target"); };
    c.ondragleave = () => c.classList.remove("drop-target");
    c.ondrop = (e) => {
      e.preventDefault(); e.stopPropagation(); c.classList.remove("drop-target");
      const id = Number(e.dataTransfer.getData("text/tag"));
      if (id && id !== t.id) move([id], t.id);
    };
    const cnt = document.createElement("span"); cnt.className = "cnt"; cnt.textContent = t.count; c.appendChild(cnt);
    c.classList.toggle("sel", tagSel.has(t.id));
    c.onclick = () => { tagSel.has(t.id) ? tagSel.delete(t.id) : tagSel.add(t.id); renderSettingsTags(); };
    c.oncontextmenu = (e) => { e.preventDefault(); tagSel.has(t.id) ? tagSel.delete(t.id) : tagSel.add(t.id); renderSettingsTags(); };
    box.appendChild(c);
  }
  const hint = document.createElement("span"); hint.className = "muted small"; hint.textContent = tagSel.size ? "" : "Anklicken zum Bearbeiten";
  box.appendChild(hint);
  $("#s-tag-merge").classList.toggle("hidden", tagSel.size === 0);
  $("#s-tag-merge-count").textContent = tagSel.size;
  $("#s-tag-rename-btn").classList.toggle("hidden", tagSel.size !== 1);
  const sel = $("#s-tag-merge-target"); sel.innerHTML = "";
  for (const t of sorted) sel.innerHTML += `<option value="${t.id}">${esc(pathName(t))}</option>`;
  const mv = $("#s-tag-move-target"); mv.innerHTML = `<option value="0">— oberste Ebene —</option>`;
  for (const t of sorted) if (!tagSel.has(t.id)) mv.innerHTML += `<option value="${t.id}">${esc(pathName(t))}</option>`;
  $("#s-tag-move-btn").onclick = async () => { const ids = [...tagSel]; tagSel.clear(); await move(ids, Number(mv.value)); toast("Verschoben"); };
}
$("#s-tag-merge-btn").onclick = async () => {
  const target = Number($("#s-tag-merge-target").value);
  const sources = [...tagSel].filter((id) => id !== target);
  if (!sources.length) return toast("Ziel muss ein anderer Tag sein");
  const tname = state.tags.find((t) => t.id === target)?.name;
  if (!confirm(`${sources.length} Tag(s) in „${tname}" zusammenführen?`)) return;
  await api("/tags/merge", json("POST", { source_ids: sources, target_id: target }));
  tagSel.clear(); toast("Zusammengeführt"); openSettings();
};
$("#s-tag-delete-btn").onclick = async () => {
  if (!confirm(`${tagSel.size} Tag(s) löschen? Die Dokumente bleiben, verlieren nur den Tag.`)) return;
  for (const id of tagSel) await api(`/tags/${id}`, { method: "DELETE" }).catch(() => {});
  tagSel.clear(); openSettings();
};
$("#s-tag-merge-cancel").onclick = () => { tagSel.clear(); renderSettingsTags(); };
$("#s-tag-rename-btn").onclick = async () => {
  const t = state.tags.find((x) => tagSel.has(x.id)); if (!t) return;
  const name = (prompt("Tag umbenennen:", t.name) || "").trim();
  if (!name || name === t.name) return;
  try { await api(`/tags/${t.id}`, json("PATCH", { name, color: t.color })); tagSel.clear(); toast("Umbenannt"); openSettings(); }
  catch (e) { toast("Fehler: " + e.message, 4000); }
};

// KI: ähnliche Tags gruppieren, je Gruppe zusammenführen oder ignorieren
$("#s-tag-suggest").onclick = async () => {
  const st = $("#s-tag-suggest-status"), box = $("#s-tag-suggestions"), btn = $("#s-tag-suggest");
  st.textContent = "Das LLM schaut die Tags durch …"; btn.disabled = true; box.innerHTML = "";
  try {
    const r = await api("/tags/suggest-merges", { method: "POST" });
    st.textContent = r.groups.length ? "" : "Keine ähnlichen Tags gefunden.";
    for (const g of r.groups) {
      const row = document.createElement("div"); row.className = "list-row";
      row.innerHTML = `<span>${g.sources.map((s) => esc(s.name)).join(", ")} <span class="muted">→</span> <b>${esc(g.target.name)}</b></span>` +
        `<span class="muted small">${esc(g.reason || "")}</span><span class="spacer"></span>` +
        `<button class="btn">Zusammenführen</button><button class="btn ghost">Ignorieren</button>`;
      const [merge, ignore] = row.querySelectorAll("button");
      merge.onclick = async () => {
        await api("/tags/merge", json("POST", { source_ids: g.sources.map((s) => s.id), target_id: g.target.id }));
        row.remove(); toast("Zusammengeführt");
        state.tags = await api("/tags?scope=all"); renderSettingsTags(); renderRules();
      };
      ignore.onclick = () => row.remove();
      box.appendChild(row);
    }
  } catch (e) { st.textContent = "Fehler: " + e.message; }
  btn.disabled = false;
};

// Schlagwort-Regeln: Wort im Text → Tag
async function renderRules() {
  const rules = await api("/rules");
  const box = $("#s-rules"); box.innerHTML = rules.length ? "" : `<p class="muted small">Noch keine Regeln.</p>`;
  for (const r of rules) {
    const row = document.createElement("div"); row.className = "list-row";
    row.innerHTML = `<span>„${esc(r.pattern)}“</span><span class="muted">→</span>`;
    row.appendChild(chip(r.tag, false, null, tagColor(r.tag, state.tags.find((t) => t.name === r.tag)?.color)));
    const sp = document.createElement("span"); sp.className = "spacer"; row.appendChild(sp);
    if (r.matches != null) {
      const n = document.createElement("span"); n.className = "muted small";
      n.textContent = r.matches === 1 ? "in 1 Dokument" : `in ${r.matches} Dokumenten`;
      row.appendChild(n);
    }
    const b = document.createElement("button"); b.className = "btn ghost danger"; b.textContent = "Löschen";
    b.onclick = async () => { await api(`/rules/${r.id}`, { method: "DELETE" }); renderRules(); };
    row.appendChild(b); box.appendChild(row);
  }
  $("#s-rule-tags").innerHTML = state.tags.map((t) => `<option value="${esc(t.name)}">`).join("");
}
// Vorschau beim Tippen: Wie viele vorhandene Dokumente enthalten das Schlagwort? Zeigt zu breite Regeln vorher.
let rulePreviewTimer;
$("#s-rule-pattern").addEventListener("input", () => {
  clearTimeout(rulePreviewTimer);
  const pattern = $("#s-rule-pattern").value.trim(), out = $("#s-rule-preview");
  if (pattern.length < 3) { out.textContent = ""; return; }
  rulePreviewTimer = setTimeout(async () => {
    try {
      const r = await api(`/rules/preview?pattern=${encodeURIComponent(pattern)}`);
      if ($("#s-rule-pattern").value.trim() !== pattern) return;
      out.textContent = !r.matches ? "Steht in keinem vorhandenen Dokument – greift nur bei neuen."
        : `Steht in ${r.matches === 1 ? "1 Dokument" : `${r.matches} Dokumenten`}, z. B. ${r.examples.slice(0, 3).map((t) => `„${t}“`).join(", ")}.`;
    } catch { out.textContent = ""; }
  }, 300);
});
$("#s-rule-add").onclick = async () => {
  const pattern = $("#s-rule-pattern").value.trim(), tag = $("#s-rule-tag").value.trim();
  if (!pattern || !tag) return toast("Schlagwort und Tag angeben");
  try {
    await api("/rules", json("POST", { pattern, tag }));
    $("#s-rule-pattern").value = ""; $("#s-rule-tag").value = ""; $("#s-rule-preview").textContent = "";
    state.tags = await api("/tags?scope=all"); renderSettingsTags(); renderRules(); toast("Regel angelegt");
  } catch (e) { toast("Fehler: " + e.message); }
};
$("#s-rules-apply").onclick = async () => {
  try {
    const r = await api("/rules/apply", { method: "POST" });
    toast(r.tagged ? `${r.tagged} Tag(s) vergeben` : "Nichts Neues – passende Dokumente hatten den Tag schon");
  } catch (e) { toast("Fehler: " + e.message); }
};
// KI-Anbieter: Vorlagen füllen Adresse (und, wo es ein gutes Standardmodell gibt, das Modell)
const LLM_PRESETS = {
  anthropic: { url: "https://api.anthropic.com/v1", model: "claude-haiku-5-5" },
  openai: { url: "https://api.openai.com/v1", model: "" },
  mistral: { url: "https://api.mistral.ai/v1", model: "mistral-small-latest" },
  openrouter: { url: "https://openrouter.ai/api/v1", model: "" },
};
const presetOf = (url) => Object.keys(LLM_PRESETS).find((k) => url && url.startsWith(LLM_PRESETS[k].url)) || "local";
const llmKeyDel = { "": false, "2": false };   // „Entfernen“ gedrückt, wird beim Speichern geschickt
function renderLlmCloud(cfg) {
  for (const sfx of ["", "2"]) {
    $(`#s-llm-preset${sfx}`).value = presetOf(cfg[`llm_url${sfx}`]);
    const isSet = cfg[`llm_key${sfx}_set`];
    $(`#s-llm-key${sfx}`).value = "";
    $(`#s-llm-key${sfx}`).placeholder = isSet ? "gespeichert – leer lassen zum Behalten" : "nur für Cloud-Anbieter nötig";
    $(`#s-llm-key${sfx}-state`).textContent = isSet ? "· gespeichert ✓" : "";
    $(`#s-llm-key${sfx}-del`).classList.toggle("hidden", !isSet);
    llmKeyDel[sfx] = false;
    const note = $(`#s-llm-cloudnote${sfx}`);
    note.classList.toggle("hidden", !cfg[`llm_cloud${sfx}`]);
    note.textContent = cfg[`llm_cloud${sfx}`] ? `☁️ Cloud: ${hostOf(cfg[`llm_url${sfx}`])} liegt außerhalb deines Heimnetzes.` : "";
  }
  const hosts = [...new Set(["", "2"].filter((x) => cfg[`llm_cloud${x}`]).map((x) => hostOf(cfg[`llm_url${x}`])))];
  $("#s-llm-cloud").classList.toggle("hidden", !hosts.length);
  $("#s-llm-cloud-hosts").textContent = hosts.join(" und ");
  $("#s-llm-cloud-tag").checked = cfg.llm_cloud_tag;
  $("#s-llm-cloud-ask").checked = cfg.llm_cloud_ask;
}
const hostOf = (u) => { try { return new URL(u).hostname; } catch { return u || ""; } };
for (const sfx of ["", "2"]) {
  $(`#s-llm-preset${sfx}`).onchange = (e) => {
    const p = LLM_PRESETS[e.target.value];
    if (!p) { if (presetOf($(`#s-llm-url${sfx}`).value) !== "local") { $(`#s-llm-url${sfx}`).value = ""; $(`#s-llm-model${sfx}`).value = ""; } $(`#s-llm-url${sfx}`).focus(); return; }
    $(`#s-llm-url${sfx}`).value = p.url; $(`#s-llm-model${sfx}`).value = p.model;
    $(`#s-llm-key${sfx}`).focus();
  };
  $(`#s-llm-key${sfx}-del`).onclick = () => {
    llmKeyDel[sfx] = true; $(`#s-llm-key${sfx}`).value = "";
    $(`#s-llm-key${sfx}-state`).textContent = "· wird beim Speichern entfernt";
    $(`#s-llm-key${sfx}-del`).classList.add("hidden");
  };
}
// Zustimmung zur Cloud-KI: ein eigener Dialog, kein Häkchen im Vorbeigehen
function confirmCloud(hosts) {
  const d = $("#cloud-dialog");
  $("#cd-hosts").textContent = hosts.join(", ");
  return new Promise((resolve) => { d.onclose = () => resolve(d.returnValue === "ok"); d.returnValue = ""; showDialog(d); });
}

// Verbindungstest für Haupt- und Ersatz-Server (suffix "" bzw. "2")
async function testLlm(suffix) {
  const out = $(`#s-llm-result${suffix}`); out.className = "small"; out.textContent = "Teste …";
  $(`#s-llm-test${suffix}`).disabled = true;
  try {
    const body = { url: $(`#s-llm-url${suffix}`).value.trim(), model: $(`#s-llm-model${suffix}`).value.trim(), slot: suffix ? 2 : 1 };
    const key = $(`#s-llm-key${suffix}`).value.trim();
    if (key) body.key = key;
    const r = await api("/settings/llm/test", json("POST", body));
    $(`#s-llm-models${suffix}`).innerHTML = r.models.map((m) => `<option value="${esc(m)}">`).join("");
    if (r.ok) {
      out.classList.add("ok");
      out.textContent = `Verbunden · ${r.models.length} Modell(e)` + (r.reply !== null ? ` · Antwort „${r.reply}" in ${(r.ms / 1000).toFixed(1)} s` : " · Modell wählen und erneut testen");
    } else { out.classList.add("err"); out.textContent = r.error || "Fehler"; }
  } catch (e) { out.classList.add("err"); out.textContent = "Fehler: " + e.message; }
  $(`#s-llm-test${suffix}`).disabled = false;
}
$("#s-llm-test").onclick = () => testLlm("");
$("#s-llm-test2").onclick = () => testLlm("2");
$("#s-ai-save").onclick = async () => {
  const body = {
    llm_enabled: $("#s-llm-enabled").checked, llm_url: $("#s-llm-url").value.trim(), llm_model: $("#s-llm-model").value.trim(),
    llm_url2: $("#s-llm-url2").value.trim(), llm_model2: $("#s-llm-model2").value.trim(),
    llm_ask_model: $("#s-llm-askmodel").value.trim(), llm_ask_model2: $("#s-llm-askmodel2").value.trim(),
    llm_tag_mode: $("#s-llm-restricted").checked ? "restricted" : "free", ocr_langs: $("#s-ocr-langs").value.trim(),
    llm_cloud_tag: $("#s-llm-cloud-tag").checked, llm_cloud_ask: $("#s-llm-cloud-ask").checked,
    return_window: $("#s-return-window").checked,
  };
  for (const sfx of ["", "2"]) {
    const key = $(`#s-llm-key${sfx}`).value.trim();
    if (key) body[`llm_key${sfx}`] = key; else if (llmKeyDel[sfx]) body[`llm_key${sfx}`] = "";
  }
  let cfg;
  try { cfg = await api("/settings", json("PATCH", body)); }
  catch (e) {
    if (e.status !== 409 || !e.detail || !e.detail.cloud_confirm) { toast("Fehler: " + e.message, 5000); return; }
    if (!(await confirmCloud(e.detail.cloud_confirm))) { toast("Nicht gespeichert – kein Cloud-Server ohne Zustimmung"); return; }
    try {
      // Neu freigegebene Cloud-Server: beide Aufgaben an, abschalten geht danach einzeln
      cfg = await api("/settings", json("PATCH", { ...body, llm_cloud_ack: true, llm_cloud_tag: true, llm_cloud_ask: true }));
    } catch (e2) { toast("Fehler: " + e2.message, 5000); return; }
  }
  renderLlmCloud(cfg);
  toast("KI-Einstellungen gespeichert");
};
$("#s-storage-save").onclick = async () => {
  await api("/settings", json("PATCH", {
    trash_days: Number($("#s-trash-days").value), duplicate_mode: $("#s-dup").value, thumb_width: Number($("#s-thumb").value),
    backup_dir: $("#s-bk-dir").value.trim(), backup_hour: Number($("#s-bk-hour").value),
    backup_keep_days: Number($("#s-bk-days").value), backup_keep_weeks: Number($("#s-bk-weeks").value),
  }));
  toast("Gespeichert"); renderBackup();
};

// ---------- Absender, Verträge, Steuer-Paket ----------
const senderSel = new Set();
async function renderSenders() {
  const list = await api("/senders?scope=all").catch(() => []);
  state.senders = list;
  const box = $("#s-senders"); box.innerHTML = "";
  if (!list.length) box.innerHTML = `<p class="muted small">Noch keine Absender.</p>`;
  for (const s of list) {
    const c = chip(s.name, senderSel.has(s.name), () => { senderSel.has(s.name) ? senderSel.delete(s.name) : senderSel.add(s.name); renderSenders(); });
    const cnt = document.createElement("span"); cnt.className = "cnt"; cnt.textContent = s.count; c.appendChild(cnt);
    box.appendChild(c);
  }
  $("#s-sender-merge").classList.toggle("hidden", senderSel.size < 2);
  $("#s-sender-merge-count").textContent = senderSel.size;
  const sel = $("#s-sender-merge-target"); sel.innerHTML = [...senderSel].map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  $("#s-pkg-senders").innerHTML = list.map((s) => `<option value="${esc(s.name)}">`).join("");
}
$("#s-sender-merge-btn").onclick = async () => {
  const target = $("#s-sender-merge-target").value; const sources = [...senderSel].filter((n) => n !== target);
  if (!sources.length || !confirm(`${sources.length} Schreibweise(n) zu „${target}“ zusammenführen?`)) return;
  const r = await api("/senders/merge", json("POST", { sources, target }));
  toast(`${r.moved} Dokument(e) umbenannt`); senderSel.clear(); await renderSenders(); loadDocs();
};
$("#s-sender-merge-cancel").onclick = () => { senderSel.clear(); renderSenders(); };
$("#s-sender-suggest").onclick = async () => {
  const st = $("#s-sender-suggest-status"), box = $("#s-sender-suggestions"), btn = $("#s-sender-suggest");
  btn.disabled = true; st.textContent = "Das LLM vergleicht die Absender …"; box.innerHTML = "";
  try {
    const r = await api("/senders/suggest-merges?scope=all", { method: "POST" });
    st.textContent = r.groups.length ? `${r.groups.length} Vorschlag/Vorschläge` : "Keine gleichen Absender gefunden.";
    for (const g of r.groups) {
      const row = document.createElement("div"); row.className = "list-row";
      row.innerHTML = `<span>${g.sources.map((s) => esc(s.name)).join(", ")} <span class="muted">→</span> <b>${esc(g.target.name)}</b><br><span class="muted small">${esc(g.reason)}</span></span><span class="spacer"></span><button class="btn">Zusammenführen</button><button class="btn ghost">Ignorieren</button>`;
      const [merge, ignore] = row.querySelectorAll("button");
      merge.onclick = async () => { await api("/senders/merge", json("POST", { sources: g.sources.map((s) => s.name), target: g.target.name })); row.remove(); renderSenders(); loadDocs(); };
      ignore.onclick = () => row.remove();
      box.appendChild(row);
    }
  } catch (e) { st.textContent = "Fehler: " + e.message; }
  btn.disabled = false;
};
async function renderContracts() {
  const list = await api("/contracts?scope=all").catch(() => []);
  const box = $("#s-contracts"); box.innerHTML = "";
  if (!list.length) { box.innerHTML = `<p class="muted small">Noch nichts erkannt – ein Vertrag erscheint, sobald ein Vertrag oder Bescheid mit regelmäßiger Zahlung abgelegt ist oder drei Belege desselben Absenders mit ähnlichem Betrag.</p>`; return; }
  const sum = list.reduce((a, c) => a + c.per_month, 0);
  for (const c of list) {
    const el = document.createElement("div"); el.className = "contract";
    const nextCls = c.overdue ? "error" : "";
    // Aus einem Vertrag/Bescheid: nächste Zahlung laut Rhythmus; aus Belegen: nächster Beleg erwartet
    const when = c.source === "document"
      ? `laut Dokument vom ${fmtDate(c.last)} · nächste Zahlung ${fmtDate(c.next_expected)}`
      : `zuletzt ${fmtDate(c.last)} · <span class="${nextCls}">nächster Beleg erwartet ${fmtDate(c.next_expected)}${c.overdue ? " (ausgeblieben?)" : ""}</span>`;
    const src = c.source === "document" ? "aus Vertrag/Bescheid" : `${c.count} Belege`;
    el.innerHTML = `<span><b>${esc(c.sender)}</b> <span class="muted small">· ${c.interval}, ${src}</span></span>
      <span class="amt">${c.amount.toFixed(2).replace(".", ",")} €</span>
      <span class="meta">${c.increase ? `<span class="warn">seit ${fmtDate(c.increase.since)} teurer: ${c.increase.from.toFixed(2).replace(".", ",")} → ${c.increase.to.toFixed(2).replace(".", ",")} € (+${c.increase.percent} %)</span> · ` : ""}${c.per_month.toFixed(2).replace(".", ",")} €/Monat · ${when}${c.cancel_by ? ` · <span class="warn">kündbar bis ${fmtDate(c.cancel_by.date)}</span>` : ""}${c.due ? ` · <span class="warn">Frist ${fmtDate(c.due.date)}</span>` : ""}</span>`;
    el.style.cursor = "pointer"; el.onclick = async () => { state.sender = c.sender; await enterMain(); };
    box.appendChild(el);
  }
  const total = document.createElement("p"); total.className = "small"; total.style.marginTop = "10px";
  total.innerHTML = `<b>${sum.toFixed(2).replace(".", ",")} € pro Monat</b> für ${list.length} erkannte Verträge/Abos, ${(sum * 12).toFixed(0)} € im Jahr.`;
  box.appendChild(total);
}
function renderPackageForm() {
  const years = (state.years || []).map((y) => y.year ?? y);
  const now = new Date().getFullYear();
  const all = [...new Set([now, now - 1, ...years])].sort((a, b) => b - a);
  $("#s-pkg-year").innerHTML = all.map((y) => `<option value="${y}">${y}</option>`).join("") + `<option value="">alle Jahre</option>`;
  $("#s-pkg-year").value = String(now - 1);
}
$("#s-pkg-btn").onclick = () => {
  const p = new URLSearchParams({ scope: state.scope });
  if ($("#s-pkg-year").value) p.set("year", $("#s-pkg-year").value);
  if ($("#s-pkg-tags").value.trim()) p.set("tags", $("#s-pkg-tags").value.trim());
  if ($("#s-pkg-sender").value.trim()) p.set("sender", $("#s-pkg-sender").value.trim());
  window.location.href = "/export/package.zip?" + p;
};

// ---------- E-Mail-Eingang ----------
function mailForm() {
  const f = {
    mail_enabled: $("#s-mail-enabled").checked, mail_host: $("#s-mail-host").value.trim(), mail_port: Number($("#s-mail-port").value) || 993,
    mail_user: $("#s-mail-user").value.trim(), mail_folder: $("#s-mail-folder").value.trim() || "INBOX",
    mail_done_folder: $("#s-mail-done").value.trim(), mail_owner: Number($("#s-mail-owner").value) || 0,
    mail_poll: Number($("#s-mail-poll").value), mail_body_pdf: $("#s-mail-body").checked,
  };
  if ($("#s-mail-password").value) f.mail_password = $("#s-mail-password").value;
  return f;
}
async function renderMail() {
  const m = await api("/settings/mail").catch(() => null);
  if (!m) return;
  $("#s-mail-enabled").checked = m.mail_enabled === "1";
  $("#s-mail-host").value = m.mail_host; $("#s-mail-port").value = m.mail_port; $("#s-mail-user").value = m.mail_user;
  $("#s-mail-password").value = ""; $("#s-mail-password").placeholder = m.mail_password_set ? "gespeichert – nur zum Ändern eintragen" : "";
  $("#s-mail-folder").value = m.mail_folder; $("#s-mail-done").value = m.mail_done_folder;
  $("#s-mail-owner").innerHTML = `<option value="0">erster Admin</option>` + state.users.map((u) => `<option value="${u.id}">${esc(u.display_name)}</option>`).join("");
  $("#s-mail-owner").value = m.mail_owner || "0";
  $("#s-mail-poll").value = String(m.mail_poll || 300);
  $("#s-mail-body").checked = m.mail_body_pdf === "1";
  const st = $("#s-mail-status"); st.className = "small";
  if (!m.mail_host) { st.classList.add("muted"); st.textContent = "Noch nicht eingerichtet."; }
  else if (m.mail_last_error) { st.classList.add("error"); st.textContent = `Letzter Abruf ${fmtDate(m.mail_last)} fehlgeschlagen: ${m.mail_last_error}`; }
  else if (m.mail_last) { st.classList.add("ok"); st.textContent = `Letzter Abruf ${fmtDate(m.mail_last)} · ${m.mail_last_imported} neu · insgesamt ${m.mail_total} Dokumente aus Mails`; }
  else { st.classList.add("muted"); st.textContent = m.mail_enabled === "1" ? "Aktiv, noch kein Abruf." : "Gespeichert, aber nicht aktiv."; }
}
$("#s-mail-test").onclick = async () => {
  const st = $("#s-mail-status"); st.className = "small muted"; st.textContent = "Teste …";
  try {
    const r = await api("/settings/mail/test", json("POST", mailForm()));
    if (r.ok) { st.className = "small ok"; st.textContent = `Verbunden · ${r.unseen} ungelesene Mail(s) im Ordner`; $("#s-mail-folders").innerHTML = (r.folders || []).map((f) => `<option value="${esc(f)}">`).join(""); }
    else { st.className = "small error"; st.textContent = r.error; }
  } catch (e) { st.className = "small error"; st.textContent = "Fehler: " + e.message; }
};
$("#s-mail-save").onclick = async () => { await api("/settings/mail", json("PATCH", mailForm())); toast("E-Mail-Eingang gespeichert"); renderMail(); };
$("#s-mail-check").onclick = async () => {
  const st = $("#s-mail-status"); st.className = "small muted"; st.textContent = "Hole ab …";
  try { const r = await api("/settings/mail/check", { method: "POST" }); toast(`${r.checked} Mail(s) geprüft, ${r.imported} Dokument(e) neu`); await renderMail(); await loadDocs(); }
  catch (e) { st.className = "small error"; st.textContent = "Fehler: " + e.message; }
};

// ---------- Sicherung ----------
function fmtBytes(b) { b = b || 0; return b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b >= 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.round(b / 1e3)} KB`; }
let backupTimer;
async function renderBackup() {
  clearTimeout(backupTimer);
  const st = await api("/settings/backup").catch(() => null);
  if (!st) return;
  const status = $("#s-bk-status"); status.className = "small";
  if (!st.dir) { status.classList.add("muted"); status.textContent = "Sicherung aus – Zielordner eintragen und speichern."; }
  else if (st.running) { status.classList.add("muted"); status.textContent = "Sicherung läuft …"; backupTimer = setTimeout(renderBackup, 3000); }
  else if (!st.dir_ok) { status.classList.add("error"); status.textContent = `Zielordner ${st.dir} ist nicht beschreibbar oder existiert nicht.`; }
  else if (!st.last) { status.classList.add("muted"); status.textContent = `Noch keine Sicherung – erste läuft heute um ${String(st.hour).padStart(2, "0")}:00 oder jetzt per Knopf.`; }
  else if (!st.last_ok) { status.classList.add("error"); status.textContent = `Letzte Sicherung ${fmtDate(st.last)} fehlgeschlagen: ${st.last_error || ""}`; }
  else { status.classList.add(st.stale ? "error" : "ok"); status.textContent = `Letzte Sicherung ${fmtDate(st.last)} · ${fmtBytes(st.last_bytes)} in ${st.last_seconds < 1 ? "unter 1" : st.last_seconds} s${st.stale ? " · veraltet" : ""}`; }
  $("#s-bk-run").disabled = !st.dir || st.running;
  const box = $("#s-bk-list"); box.innerHTML = "";
  for (const s of st.snapshots.slice(0, 30)) {
    const row = document.createElement("div"); row.className = "list-row";
    row.innerHTML = `<span><code>${esc(s.name)}</code></span><span class="muted small">${s.documents ?? "?"} Dokumente · ${fmtBytes(s.bytes)}</span><span class="spacer"></span><button class="btn ghost danger">Wiederherstellen</button>`;
    row.querySelector("button").onclick = async () => {
      if (!confirm(`Stand ${s.name} wiederherstellen?\n\nDatenbank und Dateien werden mit diesem Snapshot überschrieben; alles, was seitdem dazukam, geht verloren.`)) return;
      if (prompt("Zur Bestätigung WIEDERHERSTELLEN eingeben:") !== "WIEDERHERSTELLEN") return;
      try {
        const r = await api("/settings/backup/restore", json("POST", { name: s.name, confirm: "WIEDERHERSTELLEN" }));
        toast(`Wiederhergestellt: ${r.documents} Dokumente, ${r.files} Dateien`, 6000);
        await Promise.all([loadDocs(), loadTags(), loadYears()]);
      } catch (e) { toast("Fehler: " + e.message, 5000); }
    };
    box.appendChild(row);
  }
}
$("#s-bk-run").onclick = async () => {
  try { await api("/settings/backup/run", { method: "POST" }); toast("Sicherung gestartet"); setTimeout(renderBackup, 800); }
  catch (e) { toast("Fehler: " + e.message, 5000); }
};
$("#s-retag-failed").onclick = async () => { const r = await api("/settings/llm/retag?scope=failed", { method: "POST" }); toast(`${r.queued} Dokument(e) eingereiht`); };
$("#s-retag-all").onclick = async () => {
  if (!confirm("Alle Dokumente neu taggen? Vorhandene LLM-Tags bleiben, manuelle Änderungen an Titel/Datum/Absender werden überschrieben. Dauert bei vielen Dokumenten Stunden.")) return;
  const r = await api("/settings/llm/retag?scope=all", { method: "POST" }); toast(`${r.queued} Dokument(e) eingereiht`);
};

// Datumsformat
function applyDateFormat() {
  _dateFmt = null;
  const v = localStorage.getItem("bl.datefmt") || "auto";
  $("#s-date-fmt").value = DATE_FORMATS[v] ? v : "auto";
  $("#s-date-fmt").options[0].textContent = `Automatisch (${autoDateFormat().placeholder})`;
  for (const el of document.querySelectorAll(".datefield .dmy")) el.placeholder = dateFmt().placeholder;
}
$("#s-date-fmt").onchange = (e) => {
  // Offene Felder im alten Format lesen und im neuen wieder hineinschreiben
  const open = [...document.querySelectorAll(".datefield .dmy")].map((el) => [el, dmyToIso(el.value)]);
  if (e.target.value === "auto") localStorage.removeItem("bl.datefmt"); else localStorage.setItem("bl.datefmt", e.target.value);
  applyDateFormat();
  for (const [el, iso] of open) if (iso) el.value = isoToDmy(iso);
  if (state.docs) renderGrid();
  toast("Gespeichert");
};
applyDateFormat();

// ---------- Freigabe-Links ----------
let shareTarget = null;
const SHARE_FILES_MAX = 30;
let shareFiles = null;   // vorab geladene PDFs – navigator.share muss direkt im Klick laufen, sonst blockt Safari
function canShareFiles() {
  try { return !!navigator.canShare && navigator.canShare({ files: [new File(["x"], "x.pdf", { type: "application/pdf" })] }); }
  catch { return false; }
}
function pdfName(d, used) {
  let base = (d.title || `Dokument ${d.id}`).replace(/[\\/:*?"<>|]+/g, "_").trim().slice(0, 100) || `Dokument ${d.id}`, name = base, i = 2;
  while (used.has(name.toLowerCase())) name = `${base} (${i++})`;
  used.add(name.toLowerCase()); return name + ".pdf";
}
async function prepareShareFiles(docs, token) {
  const used = new Set(), files = [];
  for (const d of docs) {
    const r = await fetch(`/documents/${d.id}/file`, { credentials: "same-origin" });
    if (!r.ok) throw new Error(`„${d.title}“ ließ sich nicht laden`);
    files.push(new File([await r.blob()], pdfName(d, used), { type: "application/pdf" }));
  }
  if (token === shareTarget) shareFiles = files;
}
const SH_NOTE_VIEW = "Wer den Link hat, kann die Dokumente ansehen und herunterladen – sonst nichts. Du kannst ihn jederzeit unter Einstellungen → Freigabe-Links zurückziehen.";
const SH_NOTE_UPLOAD = "Wer den Link hat, kann dir Dokumente einwerfen (PDF, JPEG, PNG) – sehen kann er nichts, auch nicht, was schon drin liegt. Eingeworfenes landet bei dir unter „Prüfen“.";
function openShareDialog(t) {
  shareTarget = t; shareFiles = null;
  $("#sh-what").textContent = t.label;
  $("#sh-setup").classList.remove("hidden"); $("#sh-result").classList.add("hidden");
  $("#sh-create").classList.remove("hidden"); $("#sh-password").value = ""; $("#sh-expiry").value = "30";
  const upload = t.kind === "upload";
  $("#sh-name-row").classList.toggle("hidden", !upload); $("#sh-folder-row").classList.toggle("hidden", !upload);
  $("#sh-note").textContent = upload ? SH_NOTE_UPLOAD : SH_NOTE_VIEW;
  if (upload) {
    $("#sh-name").value = "";
    const sel = $("#sh-folder"); sel.innerHTML = "";
    sel.append(new Option("– kein Ordner –", ""));
    for (const tg of [...(state.tags || [])].sort((a, b) => a.name.localeCompare(b.name, "de"))) sel.append(new Option(tg.name, String(tg.id)));
  }
  const files = t.files && t.files.length && canShareFiles();
  $("#sh-title").textContent = upload ? "Briefkasten-Link" : t.files ? "Teilen" : "Freigabe-Link";
  $("#sh-files").classList.toggle("hidden", !files);
  if (files) {
    const btn = $("#sh-files-btn"), note = $("#sh-files-note");
    if (t.files.length > SHARE_FILES_MAX) {
      btn.disabled = true; note.textContent = `Direkt teilen geht mit höchstens ${SHARE_FILES_MAX} Dateien – nimm „Herunterladen“ (ZIP) oder einen Link.`;
    } else {
      btn.disabled = true; btn.textContent = "PDFs werden geladen …";
      note.textContent = "Öffnet das Teilen-Menü des Geräts (Nachrichten, Mail, AirDrop …). Die Dateien gehen direkt an den Empfänger, ohne Link.";
      prepareShareFiles(t.files, t)
        .then(() => { if (shareTarget === t) { btn.disabled = false; btn.textContent = t.files.length === 1 ? "Als PDF teilen …" : `${t.files.length} PDFs teilen …`; } })
        .catch((e) => { if (shareTarget === t) { btn.textContent = "Als PDFs teilen …"; note.textContent = "Fehler: " + e.message; } });
    }
  }
  // Links gibt es nur für eigene Dokumente
  const linkOk = t.own !== false;
  $("#sh-setup").classList.toggle("hidden", !linkOk); $("#sh-create").classList.toggle("hidden", !linkOk);
  if (!linkOk) $("#sh-what").textContent = t.label + " – ein Link geht nur mit eigenen Dokumenten; in der Auswahl sind auch fremde.";
  showDialog($("#share-dialog"));
}
$("#sh-files-btn").onclick = async () => {
  if (!shareFiles) return;
  try {
    await navigator.share({ files: shareFiles, title: shareTarget.label });
    $("#share-dialog").close();
  } catch (e) { if (e.name !== "AbortError") toast("Teilen ging nicht: " + e.message, 4000); }
};
$("#share-dialog").addEventListener("close", () => { shareFiles = null; });
function isPrivateUrl(u) {
  try {
    const h = new URL(u).hostname;
    return h === "localhost" || h.endsWith(".local") || !h.includes(".") || /^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(h);
  } catch { return false; }
}
$("#sh-create").onclick = async () => {
  try {
    const upload = shareTarget.kind === "upload";
    const link = await api("/links", json("POST", { kind: shareTarget.kind,
      target: upload ? (Number($("#sh-folder").value) || null) : shareTarget.target,
      name: upload ? ($("#sh-name").value.trim() || null) : undefined,
      expires_days: Number($("#sh-expiry").value), password: $("#sh-password").value || null }));
    if (upload && !$("#settings").classList.contains("hidden")) renderLinks();
    $("#sh-url").value = link.url;
    $("#sh-setup").classList.add("hidden"); $("#sh-create").classList.add("hidden"); $("#sh-result").classList.remove("hidden");
    const warn = $("#sh-warn");
    warn.textContent = isPrivateUrl(link.url) ? "Diese Adresse funktioniert nur in deinem Heimnetz. Für Leute draußen unter Einstellungen → Freigabe-Links die öffentliche Adresse eintragen." : "";
    warn.classList.toggle("hidden", !warn.textContent);
    $("#sh-url").select();
  } catch (e) { toast("Fehler: " + e.message); }
};
// Kopieren: Clipboard-API nur in sicherem Kontext (HTTPS/localhost); sonst über ein markiertes Textfeld.
// Das Feld muss im offenen Dialog liegen – außerhalb ist bei showModal() alles gesperrt (inert).
function copyText(text, btn) {
  const done = (ok) => {
    if (btn) { clearTimeout(btn._t); btn._label ??= btn.textContent; btn.textContent = ok ? "Kopiert ✓" : "⌘C drücken"; btn._t = setTimeout(() => btn.textContent = btn._label, 2000); }
    toast(ok ? "Link kopiert" : "Kopieren ging nicht – der Link ist markiert, mit ⌘C bzw. Strg+C kopieren", ok ? 2500 : 5000);
  };
  const fallback = () => {
    const host = document.querySelector("dialog[open]") || document.body;
    const t = document.createElement("textarea");
    t.value = text; t.setAttribute("readonly", ""); t.style.cssText = "position:fixed;top:0;left:0;opacity:0;pointer-events:none";
    host.appendChild(t); t.focus(); t.select(); t.setSelectionRange(0, text.length);
    let ok = false; try { ok = document.execCommand("copy"); } catch {}
    t.remove();
    if (!ok && host.contains($("#sh-url")) && $("#sh-url").value === text) $("#sh-url").select();
    return ok;
  };
  if (navigator.clipboard && window.isSecureContext)
    navigator.clipboard.writeText(text).then(() => done(true), () => done(fallback()));
  else done(fallback());
}
$("#sh-copy").onclick = (e) => copyText($("#sh-url").value, e.currentTarget);
$("#share-btn").onclick = () => {
  const d = state.current; if (!d) return;
  openShareDialog({ kind: "doc", target: d.id, label: `Dokument „${d.title}“`, files: [d], own: d.is_own !== false });
};
$("#s-upload-new").onclick = () => openShareDialog({ kind: "upload", label: "Leute ohne Konto können dir hierüber Dokumente schicken." });
async function renderLinks() {
  const box = $("#s-links");
  const links = await api("/links").catch(() => []);
  if (!links.length) { box.innerHTML = `<p class="muted small">Noch keine Links.</p>`; return; }
  const kind = { doc: "Dokument", docs: "Auswahl", tag: "Ordner", view: "Suche", upload: "Briefkasten" };
  box.innerHTML = "";
  for (const l of links) {
    const row = document.createElement("div"); row.className = "link-row";
    const until = l.expires_at ? (l.expired ? `<span class="expired">abgelaufen</span>` : `bis ${fmtDate(l.expires_at.slice(0, 10))}`) : "unbefristet";
    row.innerHTML = `<div style="flex:1;min-width:0"><div class="nm">${esc(l.name)}</div>` +
      `<div class="lmeta">${kind[l.kind] || esc(l.kind)} · ${l.kind === "upload" ? `${l.documents} eingegangen` : `${l.documents} Dok.`} · ${until}${l.has_password ? " · mit Passwort" : ""} · ${l.access_count}× geöffnet</div></div>` +
      `<button class="btn ghost">Kopieren</button><button class="btn ghost danger">Zurückziehen</button>`;
    const [copy, revoke] = row.querySelectorAll("button");
    copy.onclick = () => copyText(l.url, copy);
    revoke.onclick = async () => { if (!confirm(`Link „${l.name}“ zurückziehen? Er funktioniert danach nicht mehr.`)) return; await api(`/links/${l.id}`, { method: "DELETE" }).then(renderLinks, (e) => toast("Fehler: " + e.message)); };
    box.appendChild(row);
  }
}
// ---------- Doppelte Dokumente ----------
$("#s-dups-run").onclick = async () => {
  const btn = $("#s-dups-run"), st = $("#s-dups-state"), box = $("#s-dups-list");
  btn.disabled = true; st.textContent = "Suche läuft …"; box.innerHTML = "";
  try {
    const pairs = await api("/duplicates");
    st.textContent = pairs.length ? `${pairs.length} ${pairs.length === 1 ? "mögliches Paar" : "mögliche Paare"}` : "Keine doppelten Dokumente gefunden.";
    for (const p of pairs) box.appendChild(dupPair(p));
  } catch (e) { st.textContent = "Fehler: " + e.message; }
  finally { btn.disabled = false; }
};
function dupMeta(d) {
  const amount = d.amount != null ? `${Number(d.amount).toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${d.currency || ""}`.trim() : "";
  return [d.document_date ? fmtDate(d.document_date) : "Ohne Datum", d.sender, amount, d.page_count ? `${d.page_count} S.` : "",
          d.created_at ? `hinzugefügt ${fmtDate(d.created_at)}` : "", d.is_own ? "" : `von ${d.owner_name}`]
    .filter(Boolean).join(" · ");
}
function dupPair(p) {
  const el = document.createElement("div"); el.className = "dup-pair"; el.dataset.ids = `${p.a.id},${p.b.id}`;
  const side = (d) => `<div class="dup-doc"><a href="${d.file_url}" target="_blank" rel="noopener" title="PDF öffnen"><img loading="lazy" src="${d.thumbnail_url}" alt=""></a>` +
    `<div class="t">${esc(d.title)}</div><div class="m">${esc(dupMeta(d))}</div>` +
    `<button class="btn" data-keep="${d.id}">Dieses behalten</button></div>`;
  el.innerHTML = `<div class="dup-why">${p.reasons.map(esc).join(" · ")}</div><div class="dup-sides">${side(p.a)}${side(p.b)}</div>` +
    `<div class="row" style="justify-content:flex-end"><button class="btn ghost" data-ignore>Beide behalten</button></div>`;
  for (const b of el.querySelectorAll("[data-keep]")) b.onclick = async () => {
    const keep = Number(b.dataset.keep), drop = keep === p.a.id ? p.b.id : p.a.id;
    try {
      await api("/duplicates/resolve", json("POST", { keep, drop }));
      // Paare mit dem weggelegten Dokument sind damit erledigt
      for (const x of [...$("#s-dups-list").children]) if (x.dataset.ids.split(",").map(Number).includes(drop)) x.remove();
      toast("Behalten – das andere liegt im Papierkorb");
    } catch (e) { toast("Fehler: " + e.message); }
  };
  el.querySelector("[data-ignore]").onclick = async () => {
    try { await api("/duplicates/ignore", json("POST", { a: p.a.id, b: p.b.id })); el.remove(); }
    catch (e) { toast("Fehler: " + e.message); }
  };
  return el;
}

// ---------- Familie: Personen, Notfallmappe, Aufräumen ----------
// Alles lädt erst beim Aufklappen des jeweiligen Abschnitts.
async function renderPersons() {
  const box = $("#s-persons");
  const list = await api("/persons").catch(() => []);
  box.innerHTML = list.length ? "" : `<p class="muted small">Noch keine Personen – zum Beispiel deine Kinder.</p>`;
  for (const p of list) {
    const row = document.createElement("div"); row.className = "list-row";
    row.innerHTML = `<span class="person-pill" style="--tag:${tagColor(p.name, p.color)}">${esc(p.name)}</span>` +
      `<span class="muted small">${p.count} Dok.${p.auto ? "" : " · Name zu kurz zum automatischen Zuordnen – Dokumente von Hand taggen"}</span><span class="spacer"></span>` +
      (p.can_remove ? `<button class="btn ghost danger">Entfernen</button>` : "");
    const b = row.querySelector("button");
    if (b) b.onclick = async () => {
      if (!confirm(`„${p.name}“ als Person entfernen?\n\nOrdner und Dokumente bleiben – neue Dokumente werden nur nicht mehr automatisch zugeordnet.`)) return;
      try { await api(`/persons/${p.id}`, { method: "DELETE" }); renderPersons(); } catch (e) { toast("Fehler: " + e.message); }
    };
    box.appendChild(row);
  }
}
$("#s-persons-sect").addEventListener("toggle", (e) => { if (e.target.open) renderPersons(); });
$("#s-person-add").onclick = async () => {
  const name = $("#s-person-name").value.trim();
  if (!name) return;
  try {
    const p = await api("/persons", json("POST", { name }));
    $("#s-person-name").value = "";
    toast(p.tagged ? `${p.name} ist dabei – ${p.tagged} Dokument${p.tagged === 1 ? "" : "e"} zugeordnet` : `${p.name} ist dabei`);
    renderPersons(); loadTags();
  } catch (e) { toast("Fehler: " + e.message, 4000); }
};
$("#s-person-name").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#s-person-add").click(); } });

let emergency = null;
function docLine(d, extra = []) {
  return [fmtDate(d.document_date), d.sender, d.is_own === false && d.owner_name ? `von ${d.owner_name}` : "", ...extra].filter(Boolean).join(" · ");
}
async function renderEmergency() {
  const box = $("#s-emergency");
  try { emergency = await api("/emergency"); }
  catch (e) { box.innerHTML = `<p class="muted small">Fehler: ${esc(e.message)}</p>`; return; }
  box.innerHTML = emergency.documents.length ? "" : `<p class="muted small">Noch leer. Such unten nach Vollmacht, Patientenverfügung oder Versicherung und leg die Dokumente hinein.</p>`;
  for (const d of emergency.documents) {
    const row = document.createElement("div"); row.className = "list-row";
    row.innerHTML = `<a class="cl-title" href="${d.file_url}" target="_blank" rel="noopener">${esc(d.title)}</a>` +
      `<span class="muted small">${esc(docLine(d))}</span><span class="spacer"></span>` +
      (d.can_edit ? `<button class="btn ghost">Herausnehmen</button>` : "");
    const b = row.querySelector("button");
    if (b) b.onclick = async () => {
      try { await api(`/emergency/documents/${d.id}`, { method: "DELETE" }); renderEmergency(); } catch (e) { toast("Fehler: " + e.message); }
    };
    box.appendChild(row);
  }
  const own = emergency.own_count, all = emergency.documents.length;
  const active = (emergency.links || []).filter((l) => !l.expired).length;
  $("#s-emergency-share").disabled = !own;
  $("#s-emergency-note").textContent = !own ? "Teilen geht, sobald eigene Dokumente in der Mappe liegen."
    : `In einen Link kommen deine ${own} eigenen Dokument${own === 1 ? "" : "e"}` + (all > own ? " – Dokumente anderer teilen diese selbst." : ".") +
      (active ? ` Schon ${active} aktive${active === 1 ? "r" : ""} Link${active === 1 ? "" : "s"} – zu finden unter Freigabe-Links.` : "");
}
$("#s-emergency-sect").addEventListener("toggle", (e) => { if (e.target.open) renderEmergency(); });
$("#s-emergency-search").onclick = async () => {
  const q = $("#s-emergency-q").value.trim(), box = $("#s-emergency-results");
  box.innerHTML = "";
  if (!q) return;
  const docs = await api(`/documents?scope=all&limit=20&q=${encodeURIComponent(q)}`).catch(() => []);
  const inside = new Set((emergency?.documents || []).map((d) => d.id));
  const hits = docs.filter((d) => d.can_edit && !inside.has(d.id));
  if (!hits.length) { box.innerHTML = `<p class="muted small">Nichts gefunden.</p>`; return; }
  for (const d of hits) {
    const row = document.createElement("div"); row.className = "list-row";
    row.innerHTML = `<span class="cl-title">${esc(d.title)}</span><span class="muted small">${esc(docLine(d))}</span><span class="spacer"></span><button class="btn">Hinzufügen</button>`;
    row.querySelector("button").onclick = async () => {
      try { await api("/emergency/documents", json("POST", { document_id: d.id })); row.remove(); renderEmergency(); }
      catch (e) { toast("Fehler: " + e.message); }
    };
    box.appendChild(row);
  }
};
$("#s-emergency-q").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#s-emergency-search").click(); } });
$("#s-emergency-share").onclick = () => {
  if (!emergency || !emergency.tag_id) return;
  openShareDialog({ kind: "tag", target: emergency.tag_id,
    label: "Notfallmappe für deine Angehörigen – der Link zeigt deine eigenen Dokumente darin, später hinzugefügte kommen von selbst dazu." });
  $("#sh-expiry").value = "365";
};

async function renderCleanup() {
  const st = $("#s-cleanup-state"), box = $("#s-cleanup-list"), btn = $("#s-cleanup-run");
  btn.disabled = true; st.textContent = "Wird geprüft …"; box.innerHTML = "";
  try {
    const r = await api("/cleanup");
    st.textContent = r.count ? `${r.count} Vorschl${r.count === 1 ? "ag" : "äge"}` : "Nichts zum Aufräumen – alles liegt noch in seiner Frist.";
    for (const g of r.groups) box.appendChild(cleanupGroup(g));
  } catch (e) { st.textContent = "Fehler: " + e.message; }
  finally { btn.disabled = false; }
}
function cleanupGroup(g) {
  const el = document.createElement("div"); el.className = "cleanup-group";
  el.innerHTML = `<div class="cleanup-head"><div><b>${esc(g.label)}</b> <span class="muted small">· ${g.years} Jahr${g.years === 1 ? "" : "e"} nach Jahresende</span>` +
    `<div class="muted small">${esc(g.basis)}</div></div><button class="btn danger" data-all>Alle in den Papierkorb</button></div>`;
  const gone = () => { if (!el.querySelector(".list-row")) el.remove(); };
  for (const d of g.documents) {
    const row = document.createElement("div"); row.className = "list-row"; row.dataset.id = d.id;
    row.innerHTML = `<a class="cl-title" href="${d.file_url}" target="_blank" rel="noopener">${esc(d.title)}</a>` +
      `<span class="muted small">${esc(docLine(d, [`aufzubewahren bis ${fmtDate(d.keep_until)}`]))}</span><span class="spacer"></span><button class="btn ghost">In den Papierkorb</button>`;
    row.querySelector("button").onclick = async () => {
      try { await api(`/documents/${d.id}`, { method: "DELETE" }); row.remove(); gone(); } catch (e) { toast("Fehler: " + e.message); }
    };
    el.appendChild(row);
  }
  el.querySelector("[data-all]").onclick = async () => {
    const rows = [...el.querySelectorAll(".list-row")];
    if (!confirm(`${rows.length} Dokument${rows.length === 1 ? "" : "e"} aus „${g.label}“ in den Papierkorb legen? Von dort kannst du sie zurückholen.`)) return;
    for (const row of rows) {
      try { await api(`/documents/${row.dataset.id}`, { method: "DELETE" }); row.remove(); } catch (e) { toast("Fehler: " + e.message); break; }
    }
    gone(); toast("In den Papierkorb gelegt");
  };
  return el;
}
$("#s-cleanup-run").onclick = renderCleanup;
async function renderRetention() {
  const isAdmin = !!(state.me && state.me.is_admin);
  $("#s-cleanup-admin").classList.toggle("hidden", !isAdmin);
  if (!isAdmin) return;
  const r = await api("/cleanup/rules").catch(() => null);
  if (!r) return;
  const box = $("#s-cleanup-rules");
  box.innerHTML = r.defaults.map((d) => `<div class="muted small">Vorgabe: <b>${esc(d.label)}</b> – ${d.years == null ? "nie" : `${d.years} Jahre`}</div>`).join("");
  for (const c of r.custom) {
    const row = document.createElement("div"); row.className = "list-row";
    row.innerHTML = `<span>${esc(c.tag)}</span><span class="muted small">${c.years == null ? "nie vorschlagen" : `${c.years} Jahr${c.years === 1 ? "" : "e"}`}</span><span class="spacer"></span><button class="btn ghost">Zurück zur Vorgabe</button>`;
    row.querySelector("button").onclick = async () => { await api(`/cleanup/rules/${c.tag_id}`, { method: "DELETE" }).catch((e) => toast("Fehler: " + e.message)); renderRetention(); };
    box.appendChild(row);
  }
  $("#s-ret-tags").innerHTML = (state.tags || []).map((t) => `<option value="${esc(t.name)}"></option>`).join("");
}
$("#s-cleanup-sect").addEventListener("toggle", (e) => { if (e.target.open) renderRetention(); });
$("#s-ret-save").onclick = async () => {
  const tag = $("#s-ret-tag").value.trim(), v = $("#s-ret-years").value;
  if (!tag) return;
  try {
    await api("/cleanup/rules", json("PUT", { tag, years: v === "never" ? null : Number(v) }));
    $("#s-ret-tag").value = ""; toast("Gespeichert"); renderRetention();
  } catch (e) { toast("Fehler: " + e.message); }
};

$("#s-public-save").onclick = async () => {
  try { await api("/settings", json("PATCH", { public_url: $("#s-public-url").value.trim() })); toast("Gespeichert"); renderLinks(); }
  catch (e) { toast("Fehler: " + e.message); }
};

// Export: wahlweise als Ordnerstruktur
$("#s-export-folders").onchange = (e) => {
  const layout = e.target.checked ? "&layout=folders" : "";
  $("#s-export-mine").href = "/export.zip?scope=mine" + layout;
  $("#s-export-all").href = "/export.zip?scope=all" + layout;
};

// Theme
function applyTheme() {
  const t = localStorage.getItem("bl.theme") || "system";
  document.documentElement.dataset.theme = t;
  for (const b of document.querySelectorAll("#theme-seg button")) b.classList.toggle("active", b.dataset.theme === t);
}
for (const b of document.querySelectorAll("#theme-seg button"))
  b.onclick = () => { localStorage.setItem("bl.theme", b.dataset.theme); applyTheme(); };
applyTheme();
// Farbthema (Hintergrund + Akzent) wie in der iOS-App; pro Browser, Standard „Wiese“. Der Kopf von index.html
// setzt es schon vor dem ersten Bild; hier nur Auswahl und Wechsel.
const COLOR_THEMES = ["wiese", "sonne", "ozean", "lavendel", "bonbon", "schlicht"];
function colorTheme() {
  let c = null;
  try { c = localStorage.getItem("bl.color"); } catch {}
  return COLOR_THEMES.includes(c) ? c : "wiese";
}
function applyColor() {
  const c = colorTheme();
  document.documentElement.dataset.color = c;
  for (const b of document.querySelectorAll("#color-picker .sw")) {
    b.classList.toggle("active", b.dataset.color === c);
    b.setAttribute("aria-checked", b.dataset.color === c ? "true" : "false");
  }
}
for (const b of document.querySelectorAll("#color-picker .sw"))
  b.onclick = () => { try { localStorage.setItem("bl.color", b.dataset.color); } catch {} applyColor(); };
applyColor();
function applyStartSeg() {
  const s = localStorage.getItem("bl.start") || "last";
  for (const b of document.querySelectorAll("#start-seg button")) b.classList.toggle("active", b.dataset.start === s);
}
for (const b of document.querySelectorAll("#start-seg button"))
  b.onclick = () => { localStorage.setItem("bl.start", b.dataset.start); applyStartSeg(); toast("Gespeichert"); };
applyStartSeg();

$("#s-show-partner").onchange = async (e) => {
  await api("/auth/me", json("PATCH", { show_partner_in_overview: e.target.checked }));
  toast("Gespeichert");
};
$("#s-share-add").onclick = async () => {
  const id = Number($("#s-share-user").value); if (!id) return;
  await api("/shares", json("PUT", { user_id: id, can_edit: $("#s-share-edit").checked }));
  toast("Freigegeben"); openSettings();
};
$("#s-save").onclick = async () => {
  const body = { display_name: $("#s-name").value.trim() || undefined };
  if ($("#s-pass").value) { body.password = $("#s-pass").value; body.current_password = $("#s-pass-old").value; }
  try {
    await api("/auth/me", json("PATCH", body));
    toast(body.password ? "Gespeichert – andere Geräte sind abgemeldet" : "Gespeichert"); openSettings();
  } catch (e) { toast("Fehler: " + e.message, 4000); }
};
$("#s-delete-me").onclick = async () => {
  const password = $("#s-delete-pass").value;
  if (!password) { toast("Bitte dein Passwort eingeben"); return; }
  if (!confirm("Dein Konto und alle deine Dokumente endgültig löschen? Das lässt sich nicht rückgängig machen.")) return;
  try {
    await api("/auth/me", json("DELETE", { password }));
    toast("Dein Konto ist gelöscht", 4000);
    showLogin();
  } catch (e) { toast("Fehler: " + e.message, 4000); }
};
$("#s-new-add").onclick = async () => {
  try {
    await api("/users", json("POST", { username: $("#s-new-user").value, display_name: $("#s-new-name").value, password: $("#s-new-pass").value }));
    $("#s-new-user").value = $("#s-new-name").value = $("#s-new-pass").value = "";
    toast("Nutzer angelegt"); openSettings();
  } catch (e) { toast("Fehler: " + e.message, 4000); }
};

// ---------- Einladungen (Admin) ----------
const INVITE_SHARE = { none: "ohne Freigabe", read: "sieht deine Dokumente", edit: "darf deine Dokumente bearbeiten" };
async function renderInvites() {
  const box = $("#s-invites");
  const list = await api("/invites").catch(() => []);
  box.innerHTML = list.length ? `<div class="k" style="margin-top:4px">Offene Einladungen</div>` : "";
  for (const i of list) {
    const row = document.createElement("div"); row.className = "list-row";
    const until = i.expired ? `<span class="expired">abgelaufen</span>` : `bis ${fmtDate(i.expires_at.slice(0, 10))}`;
    row.innerHTML = `<span>${esc(i.display_name)}</span><span class="muted small">${INVITE_SHARE[i.share_with_me] || ""}${i.is_admin ? " · Admin" : ""} · ${until} · von ${esc(i.invited_by || "")}</span>` +
      `<span class="spacer"></span><button class="btn ghost danger">Zurückziehen</button>`;
    row.querySelector("button").onclick = async () => {
      if (!confirm(`Einladung für ${i.display_name} zurückziehen? Der Link funktioniert danach nicht mehr.`)) return;
      await api(`/invites/${i.id}`, { method: "DELETE" }); renderInvites();
    };
    box.appendChild(row);
  }
}
$("#s-inv-add").onclick = async () => {
  const name = $("#s-inv-name").value.trim();
  if (!name) { $("#s-inv-name").focus(); toast("Name fehlt"); return; }
  try {
    const inv = await api("/invites", json("POST", { display_name: name, share_with_me: $("#s-inv-share").value, is_admin: $("#s-inv-admin").checked }));
    $("#s-inv-name").value = ""; $("#s-inv-admin").checked = false; $("#s-inv-share").value = "none";
    showInviteDialog(inv);
    renderInvites();
  } catch (e) { toast("Fehler: " + e.message, 4000); }
};
$("#s-inv-name").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#s-inv-add").click(); } });
function showInviteDialog(inv) {
  $("#iv-title").textContent = `Einladung für ${inv.display_name}`;
  $("#iv-url").value = inv.url;
  try { $("#iv-qr").innerHTML = QR.svg(inv.url); } catch { $("#iv-qr").innerHTML = ""; }
  const warn = $("#iv-warn");
  warn.textContent = isPrivateUrl(inv.url) ? "Diese Adresse funktioniert nur in deinem Heimnetz (oder per VPN)." : "";
  warn.classList.toggle("hidden", !warn.textContent);
  showDialog($("#invite-dialog"));
}
$("#iv-copy").onclick = (e) => copyText($("#iv-url").value, e.currentTarget);
$("#invite-dialog").addEventListener("close", () => { $("#iv-url").value = ""; $("#iv-qr").innerHTML = ""; });

// ---------- Tag-Farben ----------
const TAG_HUES = [215, 140, 35, 285, 10, 190, 330, 60, 250, 100];
function tagColor(name, override) {
  if (override) return override;
  let h = 0; for (const c of name.toLowerCase()) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return `hsl(${TAG_HUES[h % TAG_HUES.length]} 85% 68%)`;
}
function pill(t) { return `<span class="pill" style="--tag:${esc(tagColor(t.name, t.color))}">${esc(t.name)}</span>`; }

// ---------- Helpers ----------
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function enc(s) { return encodeURIComponent(s || ""); }
function fmtDate(s) {
  if (!s) return "";
  const d = new Date(s.includes("T") || s.length === 10 ? s : s.replace(" ", "T") + "Z");
  if (isNaN(d)) return s;
  return isoToDmy(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`);
}

// ---------- Start ----------
(async () => {
  const invite = inviteFromHash();
  if (invite) { showInvite(invite); return; }
  try {
    const h = await fetch("/ping").then((r) => r.json());
    if (h.needs_setup) { show("#setup"); return; }
    await api("/auth/me"); enterMain();
  } catch { showLogin(); }
})();
