/* Twitch Recorder — dashboard. Nessuna dipendenza; mpegts.js viene caricato solo all'apertura del player. */
(() => {
  "use strict";

  // ── Utilità ──
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const store = {
    get(key, fallback) { try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* storage non disponibile */ } },
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const csrf = () => $('meta[name="csrf-token"]')?.content || "";

  const FILTERS = [
    ["all", "Tutti"], ["live", "In onda"], ["rec", "In REC"], ["rerun", "Rerun"],
    ["active", "Registrazione attiva"], ["paused", "In pausa"], ["sleeping", "Dormono"], ["issues", "Da verificare"],
  ];
  const S = {
    channels: new Map(), order: [], recordings: [], recVersion: -1, activeSizes: {},
    stats: {}, config: {}, disk: {}, quality: [], platforms: [], offset: 0,
    filter: store.get("tr-filter", "all"), sort: store.get("tr-sort", "default"),
    selecting: false, selected: new Set(), drawer: null, view: "channels",
    uiMode: store.get("tr-ui", "auto"),   // auto (segue MINIMAL_UI) | full | minimal
  };
  const isMinimal = () => S.uiMode === "minimal" || (S.uiMode === "auto" && !!S.config.minimal_ui);
  function applyUiMode() { document.body.classList.toggle("minimal", isMinimal()); }
  if (!FILTERS.some(([k]) => k === S.filter)) S.filter = "all";

  const serverNow = () => Date.now() / 1000 - S.offset;
  function tsSeconds(value) {
    if (value === null || value === undefined || value === "") return null;
    const n = Number(value);
    if (Number.isFinite(n)) return n;
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed / 1000 : null;
  }
  function duration(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
    if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
    return `${s}s`;
  }
  function ago(value) {
    const t = tsSeconds(value);
    if (t === null) return "mai";
    const sec = Math.max(0, serverNow() - t);
    if (sec < 45) return "adesso";
    if (sec < 3600) return `${Math.round(sec / 60)} min fa`;
    if (sec < 86400) return `${Math.round(sec / 3600)} h fa`;
    const days = Math.round(sec / 86400);
    return days === 1 ? "ieri" : `${days} giorni fa`;
  }
  const fullDate = (value) => { const t = tsSeconds(value); return t === null ? "" : new Date(t * 1000).toLocaleString("it-IT"); };
  const compact = (n) => n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1).replace(".", ",")}k` : String(n);
  function humanSize(n) {
    const units = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i ? 1 : 0).replace(".", ",")} ${units[i]}`;
  }

  function toast(msg, kind = "ok") {
    const el = document.createElement("div");
    el.className = `toast ${kind === "ok" ? "" : kind}`;
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), kind === "err" ? 6000 : 3500);
  }
  function goToLogin() { location.href = `/login?next=${encodeURIComponent(location.pathname + location.search)}`; }

  async function api(url, body) {
    try {
      const r = await fetch(url, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
        body: JSON.stringify(body || {}),
      });
      if (r.status === 401) { goToLogin(); return { ok: false }; }
      const data = await r.json().catch(() => ({}));
      if (data.ok === undefined) data.ok = r.ok;
      return data;
    } catch {
      toast("Il server non risponde. Controlla la connessione.", "err");
      return { ok: false };
    }
  }
  const room = (action, name, extra = {}) => {
    const ch = S.channels.get(name);
    return api("/api/room", { action, channel: name, platform: ch?.platform || "twitch", ...extra });
  };
  async function act(promise, okFallback) {
    const r = await promise;
    if (r.ok) { if (r.msg || okFallback) toast(r.msg || okFallback); }
    else toast(r.msg || r.error || "Operazione non riuscita.", "warn");
    return r;
  }

  function confirmDialog(text, okLabel = "Conferma") {
    const dlg = $("#confirm");
    $("#confirmText").textContent = text;
    $("#confirmOk").textContent = okLabel;
    return new Promise((resolve) => {
      dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
      dlg.returnValue = "";
      dlg.showModal();
    });
  }

  // ── Classificazione dei canali ──
  const rerunLevel = (ch) => (ch.rerun && ch.rerun.level && ch.rerun.level !== "none") ? ch.rerun.level : null;
  const isOnAir = (ch) => !ch.sleeping && (ch.actually_recording || ch.online || ch.last_check_result === "rerun");
  const isIssue = (ch) => !ch.sleeping && (["verifying_offline", "error", "checking", "private"].includes(ch.last_check_result) || ch.last_status === "unknown");

  function matchesFilter(ch, filter = S.filter) {
    switch (filter) {
      case "live": return isOnAir(ch);
      case "rec": return !!ch.actually_recording;
      case "rerun": return !!rerunLevel(ch) || ch.last_check_result === "rerun";
      case "active": return ch.is_recording && !ch.sleeping;
      case "paused": return !ch.is_recording && !ch.sleeping;
      case "sleeping": return !!ch.sleeping;
      case "issues": return isIssue(ch);
      default: return true;
    }
  }
  function matchesSearch(ch, term) {
    if (!term) return true;
    return `${ch.name} ${ch.display_name || ""} ${ch.platform_label || ""} ${ch.title || ""} ${ch.game || ""}`.toLocaleLowerCase().includes(term);
  }
  // Ordine stabile: i dati che cambiano a ogni controllo (ultima live, errori) non spostano le schede.
  const byName = (a, b) => a.name.localeCompare(b.name, "it", { sensitivity: "base" });
  const lastLive = (c) => tsSeconds(c.last_live) || 0;
  const wallRank = (c) => (c.actually_recording ? 0 : c.online ? 1 : 2);
  const listRank = (c) => (c.sleeping ? 2 : c.is_recording ? 0 : 1);
  const startedAt = (c) => tsSeconds(c.stream_started_at) || c.rec_started_at || 0;
  function sorterFor(section) {
    return (a, b) => {
      switch (S.sort) {
        case "name": return byName(a, b);
        case "last_live": return section === "wall" ? byName(a, b) : lastLive(b) - lastLive(a) || byName(a, b);
        case "viewers": return (b.viewers || 0) - (a.viewers || 0) || byName(a, b);
        case "last_checked": return (b.last_checked_at || 0) - (a.last_checked_at || 0) || byName(a, b);
        default:
          return section === "wall"
            ? wallRank(a) - wallRank(b) || startedAt(a) - startedAt(b) || byName(a, b)
            : listRank(a) - listRank(b) || lastLive(b) - lastLive(a) || byName(a, b);
      }
    };
  }

  function statusOf(ch) {
    if (ch.sleeping) return { dot: "sleeping", html: "Dorme: controlli sospesi" };
    switch (ch.last_check_result) {
      case "verifying_offline": return { dot: "verifying", html: `<span class="warn">Verifico la fine della live ${ch.offline_confirmation_count}/${ch.offline_confirmations_required}</span>` };
      case "error": return { dot: "error", html: `<span class="err">Controllo non riuscito</span> · ultimo stato ${ch.last_status === "online" ? "online" : "offline"}` };
      case "rerun": return { dot: "rerun", html: `<span class="warn">Rerun in corso, non registrata</span>` };
      case "private": return { dot: "private", html: "Live privata o solo abbonati" };
      case "checking": return { dot: "checking", html: "In verifica…" };
    }
    if (ch.online) return { dot: "online", html: "In onda" };
    if (ch.last_status === "unknown") return { dot: "checking", html: "In verifica…" };
    const last = ch.last_live ? `ultima live <span data-rel="${esc(ch.last_live)}" title="${esc(fullDate(ch.last_live))}">${esc(ago(ch.last_live))}</span>` : "nessuna live rilevata";
    const mode = ch.single_live_mode
      ? (ch.single_live_offline_remaining != null ? ` · Una volta: pausa tra ${duration(ch.single_live_offline_remaining)}` : " · Una volta attiva")
      : (ch.is_recording ? "" : " · in pausa");
    return { dot: "offline", html: `Offline · ${last}${mode}` };
  }

  // ── Stato ricevuto dal server ──
  function applyFull(d) {
    S.offset = Date.now() / 1000 - d.server_time;
    S.channels = new Map(d.channels.map((c) => [c.name, c]));
    S.order = d.channels.map((c) => c.name);
    S.recordings = d.recordings || [];
    S.recVersion = d.recordings_version;
    S.activeSizes = d.active_sizes || {};
    S.stats = d.stats || {}; S.config = d.config || {}; S.disk = d.disk || {};
    S.quality = d.quality_options || []; S.platforms = d.platforms || [];
    initSelects();
    applyUiMode();
    scheduleRender({ recordings: true });
  }
  function applyDelta(d) {
    S.offset = Date.now() / 1000 - d.server_time;
    for (const ch of d.channels_changed || []) S.channels.set(ch.name, ch);
    for (const name of d.channels_removed || []) { S.channels.delete(name); S.selected.delete(name); }
    if (d.order) S.order = d.order;
    if (d.stats) S.stats = d.stats;
    if (d.disk) S.disk = d.disk;
    if (d.active_sizes) S.activeSizes = d.active_sizes;
    const recChanged = !!d.recordings;
    if (recChanged) { S.recordings = d.recordings; S.recVersion = d.recordings_version; }
    scheduleRender({ recordings: recChanged, changed: (d.channels_changed || []).map((c) => c.name) });
  }

  let renderPending = null;
  function scheduleRender(opts = {}) {
    if (!renderPending) {
      renderPending = { recordings: false, changed: new Set() };
      requestAnimationFrame(() => { const p = renderPending; renderPending = null; render(p); });
    }
    if (opts.recordings) renderPending.recordings = true;
    (opts.changed || []).forEach((n) => renderPending.changed.add(n));
  }

  function render(p) {
    renderHeader();
    renderChips();
    renderChannels();
    if (p.recordings) renderRecordings(); else updateActiveSizes();
    if (S.drawer?.type === "channel" && (p.changed.has(S.drawer.name) || !S.channels.has(S.drawer.name))) renderDrawer();
  }

  // ── Intestazione ──
  function renderHeader() {
    const st = S.stats;
    $("#sOnline").textContent = st.online_channels ?? 0;
    $("#sRec").textContent = st.active_recordings ?? 0;
    $("#sRerun").textContent = st.suspected_reruns ?? 0;
    $("#sRerunWrap").hidden = !st.suspected_reruns;
    const recCount = st.active_recordings || 0;
    document.body.classList.toggle("has-rec", recCount > 0);
    const health = $("#health");
    const ms = st.monitor_state || "ok";
    health.className = `health ${ms === "ok" ? "ok" : ms === "stopped" ? "bad" : ""}`;
    $("#healthText").textContent = ms === "ok" ? "Monitor attivo" : ms === "stopped" ? "Monitor fermo" : "Monitor lento";
    health.title = `Ultimo controllo ${st.monitor_last_cycle == null ? "in attesa" : duration(st.monitor_last_cycle) + " fa"} · richieste in corso ${st.monitor_inflight ?? 0}`;
    const disk = S.disk;
    if (disk.total) {
      $("#diskFill").style.width = `${disk.used_pct}%`;
      $("#diskText").textContent = st.low_disk ? `${disk.free_human} liberi: nuove REC sospese` : `${disk.free_human} liberi`;
      $("#disk").className = `disk ${st.low_disk || disk.used_pct > 90 ? "crit" : disk.used_pct > 75 ? "warn" : ""}`;
      $("#disk").title = `Usati ${disk.used_human} di ${disk.total_human} (${disk.used_pct}%) · registrazioni ${st.total_size_human || "—"}`;
    }
    $("#tabChCount").textContent = S.channels.size || "";
    $("#tabRecCount").textContent = S.recordings.length || "";
  }

  function renderChips() {
    const box = $("#chips");
    const all = [...S.channels.values()];
    if (!box.childElementCount) {
      box.innerHTML = FILTERS.map(([k, label]) => `<button type="button" class="chip" role="radio" data-filter="${k}">${label} <b></b></button>`).join("");
    }
    for (const btn of box.children) {
      const k = btn.dataset.filter;
      const count = all.filter((c) => matchesFilter(c, k)).length;
      btn.querySelector("b").textContent = count;
      btn.dataset.zero = String(count === 0);
      btn.setAttribute("aria-checked", String(k === S.filter));
    }
  }

  // ── Canali ──
  const tiles = new Map();
  const rows = new Map();
  let lastInteraction = 0;

  function thumbSrc(ch) {
    if (!ch.thumb_url || isMinimal()) return "";
    const bucket = Math.floor(Date.now() / (ch.thumb_url.startsWith("/") ? 90000 : 120000));
    return `${ch.thumb_url}${ch.thumb_url.includes("?") ? "&" : "?"}t=${bucket}`;
  }
  // Fotogramma dal flusso in registrazione (per il file in corso nella lista registrazioni).
  function frameSrc(ch) {
    if (!ch.frame_url || isMinimal()) return "";
    return `${ch.frame_url}?t=${Math.floor(Date.now() / 90000)}`;
  }
  const coversOff = () => document.documentElement.classList.contains("covers-off");
  // Con il pulsante occhio attivo i titoli delle live non vengono mostrati (neanche nei suggerimenti).
  const T = (text) => (text && coversOff() ? "Titolo nascosto" : text);
  function avatarHtml(ch) {
    const letter = esc((ch.display_name || ch.name).slice(0, 1).toUpperCase());
    // Icona del canale; la lettera compare se l'icona manca, non si carica o le copertine sono nascoste.
    return ch.avatar && !isMinimal() && !coversOff()
      ? `<img src="${esc(ch.avatar)}" alt="" loading="lazy" onerror="this.replaceWith(document.createTextNode('${letter}'))">`
      : letter;
  }
  const platTag = (ch) => `<span class="plat plat-${esc(ch.platform)}" title="${esc(ch.platform_label)}">${esc(ch.platform_short)}</span>`;


  // Comandi rapidi sempre visibili (schede e righe).
  function quickTools(el, ch, { compact = false } = {}) {
    const box = el.querySelector(".tools");
    const sig = [ch.is_recording, ch.actually_recording, ch.sleeping, ch.single_live_mode, compact].join("|");
    if (box._sig === sig) return;
    box._sig = sig;
    // Un solo pulsante compatto che mostra lo stato e lo cambia al clic.
    const state = ch.sleeping ? ["st-sleep", "moon", "Dorme: nessun controllo e nessuna REC. Clic per risvegliarlo", "sleep"]
      : ch.actually_recording ? ["st-rec", "rec", "Sta registrando. Clic per fermare e mettere in pausa", "rec-toggle"]
        : ch.is_recording ? ["st-armed", "rec", "In attesa: registrerà appena va in onda. Clic per mettere in pausa", "rec-toggle"]
          : ["st-paused", "pause", "In pausa: controllato ma non registrato. Clic per registrare", "rec-toggle"];
    const rec = `<button class="icon-btn st ${state[0]}" type="button" data-act="${state[3]}" title="${state[2]}" aria-label="${state[2]}">${icon(state[1])}</button>`;
    box.innerHTML = `${rec}
      <button class="icon-btn ${ch.single_live_mode ? "is-once on" : ""}" type="button" data-act="once" aria-pressed="${!!ch.single_live_mode}" title="${ch.single_live_mode ? "Una volta attiva: dopo questa live il canale va in pausa. Clic per disattivare" : "Registra solo la prossima live (o quella in corso), poi pausa"}" aria-label="Una volta" ${ch.sleeping ? "disabled" : ""}>${icon("clock")}</button>
      ${ch.sleeping ? "" : `<button class="icon-btn" type="button" data-act="sleep" title="Metti a dormire: niente controlli e niente REC" aria-label="Metti a dormire">${icon("moon")}</button>`}
      <button class="icon-btn hide-compact" type="button" data-act="check" title="Controlla ora" aria-label="Controlla ora" ${ch.sleeping ? "disabled" : ""}>${icon("refresh")}</button>
      <button class="icon-btn" type="button" data-act="details" title="Dettagli, qualità e rerun" aria-label="Dettagli">${icon("dots")}</button>`;
  }

  function makeTile() {
    const el = document.createElement("article");
    el.className = "mon";
    el.innerHTML = `
      <button class="mon-screen" type="button" data-act="watch">
        <span class="mon-empty"></span>
        <img alt="" decoding="async">
        <span class="tally"></span>
        <span class="viewers" hidden>${icon("eye")}<span></span></span>
        <span class="mon-play">${icon("play")}</span>
      </button>
      <input class="mon-check" type="checkbox" aria-label="Seleziona">
      <div class="mon-label">
        <span class="avatar"></span>
        <div class="mon-text">
          <div class="mon-name"><span class="mini-tally"></span><a target="_blank" rel="noopener noreferrer"></a><span class="plat-slot"></span></div>
          <div class="mon-title"></div>
          <div class="mon-sub"></div>
        </div>
      </div>
      <div class="cue-flag" hidden></div>
      <div class="tools mon-tools"></div>`;
    const img = el.querySelector("img");
    img.addEventListener("load", () => img.classList.add("loaded"));
    img.addEventListener("error", () => img.classList.remove("loaded"));
    return el;
  }

  function setHtml(el, html) { if (el._html !== html) { el.innerHTML = html; el._html = html; } }
  function setText(el, text) { if (el.textContent !== text) el.textContent = text; }

  function updateTile(el, ch) {
    el.dataset.name = ch.name;
    const level = rerunLevel(ch) || (ch.last_check_result === "rerun" ? "confirmed" : null);
    el.classList.toggle("is-rec", !!ch.actually_recording);
    el.classList.toggle("is-cue", !ch.actually_recording && !!level);
    el.classList.toggle("selected", S.selected.has(ch.name));
    const img = el.querySelector("img");
    const src = thumbSrc(ch);
    if (src && img.getAttribute("src") !== src) img.src = src;
    if (!src) { img.removeAttribute("src"); img.classList.remove("loaded"); }
    setHtml(el.querySelector(".mon-empty"), `<span class="avatar">${avatarHtml(ch)}</span><span>Anteprima non disponibile</span>`);
    const tally = el.querySelector(".tally");
    if (ch.actually_recording) {
      tally.innerHTML = `REC <span data-since="${ch.rec_started_at || ""}">${duration(serverNow() - (ch.rec_started_at || serverNow()))}</span>`;
    } else setText(tally, ch.last_check_result === "rerun" ? "RERUN" : "LIVE");
    setHtml(el.querySelector(".mini-tally"), tally.innerHTML);
    const v = el.querySelector(".viewers");
    v.hidden = ch.viewers == null;
    if (ch.viewers != null) setText(v.querySelector("span"), compact(ch.viewers));
    el.querySelector(".mon-screen").setAttribute("aria-label", `Guarda ${ch.display_name || ch.name}`);
    el.querySelector(".mon-check").checked = S.selected.has(ch.name);
    setHtml(el.querySelector(".mon-label .avatar"), avatarHtml(ch));
    const a = el.querySelector(".mon-name a");
    a.href = ch.home_url; setText(a, ch.display_name || ch.name);
    setHtml(el.querySelector(".plat-slot"), platTag(ch));
    const title = el.querySelector(".mon-title");
    setText(title, T(ch.title) || (ch.actually_recording ? "Registrazione in corso" : "In onda"));
    title.title = T(ch.title) || "";
    title.classList.toggle("is-hidden-title", !!ch.title && coversOff());
    const sub = [];
    if (ch.game) sub.push(esc(ch.game));
    if (ch.resolved_quality && ch.actually_recording) sub.push(esc(ch.resolved_quality === ch.quality ? ch.resolved_quality : `${ch.resolved_quality} (richiesta ${ch.quality})`));
    else sub.push(esc(ch.quality));
    if (ch.preview_active) sub.push(`${ch.preview_connections} in visione`);
    if (ch.reconnect_attempts) sub.push(`<span class="warn">riconnessione ${ch.reconnect_attempts}/${S.config.reconnect_tries ?? 3}</span>`);
    setHtml(el.querySelector(".mon-sub"), sub.map((s) => `<span>${s}</span>`).join(""));
    quickTools(el, ch);
    const flag = el.querySelector(".cue-flag");
    flag.hidden = !level;
    if (level) {
      const label = { confirmed: "Rerun", probable: "Rerun probabile", suspect: "Rerun?" }[level];
      const tail = ch.last_check_result === "rerun" ? ", non registrata" : ch.actually_recording ? ", registrata e segnata" : "";
      const manual = ch.rerun?.override;
      const why = (ch.rerun?.reasons || []).map((r) => r.text).join("\n");
      setHtml(flag, `${icon("repeat")}<button class="cue-text" type="button" data-act="details" title="${esc(`${label}${tail}\n${why}\nClic per i dettagli`)}">${label}${ch.rerun?.score != null && level !== "confirmed" ? ` ${ch.rerun.score}/100` : ""}</button>
        ${manual ? "" : `<button class="cue-btn" type="button" data-act="verdict" data-value="rerun" title="Conferma: è una rerun">È rerun</button>
        <button class="cue-btn" type="button" data-act="verdict" data-value="original" title="Non è una rerun: il titolo verrà ignorato in futuro">Non lo è</button>`}`);
    }
  }

  function makeRow() {
    const el = document.createElement("div");
    el.className = "row";
    el.innerHTML = `
      <input class="row-check" type="checkbox" aria-label="Seleziona">
      <span class="dot"></span>
      <div class="row-name"><span class="avatar"></span><a target="_blank" rel="noopener noreferrer"></a><span class="plat-slot"></span></div>
      <div class="row-status"></div>
      <span class="row-quality"></span>
      <div class="tools row-actions"></div>`;
    return el;
  }
  function updateRow(el, ch) {
    el.dataset.name = ch.name;
    const st = statusOf(ch);
    el.querySelector(".dot").className = `dot ${st.dot}`;
    el.classList.toggle("is-paused", !ch.is_recording && !ch.sleeping);
    el.classList.toggle("is-sleeping", !!ch.sleeping);
    el.classList.toggle("selected", S.selected.has(ch.name));
    el.querySelector(".row-check").checked = S.selected.has(ch.name);
    setHtml(el.querySelector(".avatar"), avatarHtml(ch));
    const a = el.querySelector(".row-name a");
    a.href = ch.home_url; setText(a, ch.display_name || ch.name);
    setHtml(el.querySelector(".plat-slot"), platTag(ch));
    if (isMinimal()) {
      updateRowMinimal(el, ch, st);
    } else if (isOnAir(ch)) {
      const level = rerunLevel(ch) || (ch.last_check_result === "rerun" ? "confirmed" : null);
      const tag = ch.actually_recording
        ? `<span class="live-tag rec">REC <span data-since="${ch.rec_started_at || ""}">${duration(serverNow() - (ch.rec_started_at || serverNow()))}</span></span>`
        : `<span class="live-tag">${ch.last_check_result === "rerun" ? "RERUN" : "LIVE"}</span>`;
      const parts = [ch.title ? `<span class="live-title" title="${esc(T(ch.title))}">${esc(T(ch.title))}</span>` : "",
        ch.game ? `<span class="live-game">${esc(ch.game)}</span>` : "", ch.viewers != null ? `<span>${compact(ch.viewers)}</span>` : "",
        level ? `<span class="warn">${level === "confirmed" ? "rerun" : "rerun?"}</span>` : ""].join("");
      setHtml(el.querySelector(".row-status"), `${tag}${parts}`);
      el.querySelector(".dot").className = `dot ${ch.actually_recording ? "error" : "online"}`;
    } else setHtml(el.querySelector(".row-status"), st.html);
    el.classList.toggle("is-live", isOnAir(ch));
    el.querySelector(".row-status").classList.toggle("live", isOnAir(ch));
    setText(el.querySelector(".row-quality"), ch.quality);
    quickTools(el, ch, { compact: true });
  }

  // Minimal: due colonne fisse (stato breve + informazione), niente etichette piene.
  function updateRowMinimal(el, ch, st) {
    const level = rerunLevel(ch) || (ch.last_check_result === "rerun" ? "confirmed" : null);
    let state, cls, info = "";
    if (ch.actually_recording) {
      state = `REC <span data-since="${ch.rec_started_at || ""}">${duration(serverNow() - (ch.rec_started_at || serverNow()))}</span>`; cls = "m-rec";
    } else if (ch.last_check_result === "rerun") { state = "rerun"; cls = "m-cue"; }
    else if (ch.online) { state = "live"; cls = "m-air"; }
    else if (ch.sleeping) { state = "dorme"; cls = "m-off"; }
    else if (ch.last_check_result === "verifying_offline") { state = `verifica ${ch.offline_confirmation_count}/${ch.offline_confirmations_required}`; cls = "m-cue"; }
    else if (ch.last_check_result === "error") { state = "errore"; cls = "m-err"; }
    else if (ch.last_check_result === "private") { state = "privata"; cls = "m-off"; }
    else if (ch.last_status === "unknown" || ch.last_check_result === "checking") { state = "verifica"; cls = "m-off"; }
    else { state = ch.is_recording ? "offline" : "in pausa"; cls = "m-off"; }
    if (isOnAir(ch)) {
      info = [ch.title ? `<span class="m-title" title="${esc(T(ch.title))}">${esc(T(ch.title))}</span>` : "",
        ch.game ? `<span class="m-meta">${esc(ch.game)}</span>` : "",
        ch.viewers != null ? `<span class="m-meta">${compact(ch.viewers)}</span>` : "",
        level ? `<span class="m-cue">${level === "confirmed" ? "rerun" : "rerun?"}</span>` : ""].join("");
    } else if (ch.last_check_result === "error" && ch.last_error) {
      info = `<span class="m-meta" title="${esc(ch.last_error)}">${esc(ch.last_error)}</span>`;
    } else if (!ch.sleeping) {
      info = ch.last_live
        ? `<span class="m-meta" title="${esc(fullDate(ch.last_live))}">ultima live <span data-rel="${esc(ch.last_live)}">${esc(ago(ch.last_live))}</span>${ch.single_live_mode ? ", Una volta attiva" : ""}</span>`
        : `<span class="m-meta">nessuna live rilevata</span>`;
    }
    setHtml(el.querySelector(".row-status"), `<span class="m-state ${cls}">${state}</span>${info}`);
    el.querySelector(".dot").className = `dot ${ch.actually_recording ? "error" : st.dot}`;
  }

  function syncContainer(container, map, list, make, update) {
    const wanted = new Set(list.map((c) => c.name));
    for (const [name, el] of map) if (!wanted.has(name)) { el.remove(); map.delete(name); }
    const interacting = Date.now() - lastInteraction < 1500 || container.contains(document.activeElement);
    let index = 0;
    for (const ch of list) {
      let el = map.get(ch.name);
      if (!el) { el = make(); map.set(ch.name, el); }
      update(el, ch);
      const current = container.children[index];
      if (current !== el && (!interacting || !el.isConnected)) container.insertBefore(el, current || null);
      index++;
    }
    if (interacting && !syncContainer.retry) {
      syncContainer.retry = setTimeout(() => { syncContainer.retry = null; scheduleRender(); }, 1600);
    }
  }

  function renderChannels() {
    const term = ($("#search").value || "").trim().toLocaleLowerCase();
    const visible = S.order.map((n) => S.channels.get(n)).filter(Boolean)
      .filter((c) => matchesFilter(c) && matchesSearch(c, term));
    let onAir = visible.filter(isOnAir).sort(sorterFor("wall"));
    let others = visible.filter((c) => !isOnAir(c)).sort(sorterFor("list"));
    if (isMinimal()) {
      // Minimal: un'unica lista densa, con le dirette in cima.
      others = [...onAir, ...others];
      onAir = [];
    }
    syncContainer($("#wall"), tiles, onAir, makeTile, updateTile);
    syncContainer($("#rows"), rows, others, makeRow, updateRow);
    $("#wallSection").hidden = !onAir.length;
    $("#listSection").hidden = !others.length;
    $("#listTitle").textContent = onAir.length ? "Altri canali" : "Canali";
    const empty = $("#emptyChannels");
    empty.hidden = visible.length > 0;
    if (!visible.length) {
      empty.textContent = S.channels.size
        ? "Nessun canale corrisponde alla ricerca o al filtro."
        : "Nessun canale. Scrivi un nome o incolla un link qui sopra per iniziare a monitorarlo.";
    }
  }

  // ── Registrazioni ──
  function dayLabel(ts) {
    const d = new Date(ts * 1000), today = new Date();
    const start = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const diff = Math.round((start(today) - start(d)) / 86400000);
    if (diff === 0) return "Oggi";
    if (diff === 1) return "Ieri";
    return d.toLocaleDateString("it-IT", { weekday: "long", day: "numeric", month: "long", year: d.getFullYear() === today.getFullYear() ? undefined : "numeric" });
  }
  // Rendering a chiavi: le righe esistenti non vengono ricreate, quindi miniature e scroll restano fermi.
  const recNodes = new Map();
  const dayNodes = new Map();
  function recThumbHtml(r, ch) {
    if (isMinimal()) return "";
    if (r.in_use) return ch?.frame_url ? `<img src="${esc(frameSrc(ch))}" alt="" decoding="async" onerror="this.style.visibility='hidden'" onload="this.style.visibility=''">` : "";
    return `<img src="/thumb/rec/${encodeURIComponent(r.name)}.jpg" alt="" loading="lazy" decoding="async" onerror="this.remove()">`;
  }
  function updateRecNode(el, r) {
    const ch = S.channels.get(r.channel);
    const sig = JSON.stringify([r.in_use, r.title, r.game, r.rerun?.level, r.in_use ? 0 : r.duration, ch?.display_name, ch?.avatar, isMinimal(), coversOff()]);
    if (el._sig !== sig) {
      el._sig = sig;
      const started = new Date(r.order_ts * 1000).toLocaleTimeString("it-IT", { hour: "2-digit", minute: "2-digit" });
      const cue = r.rerun ? `<span class="cue-badge" title="${esc((r.rerun.reasons || []).join("\n"))}">${r.rerun.level === "confirmed" ? "rerun" : "rerun?"}</span>` : "";
      const dur = r.in_use
        ? `<span class="dur" data-since="${r.started_ts || ""}">${r.started_ts ? duration(serverNow() - r.started_ts) : ""}</span>`
        : (r.duration ? `<span class="dur">${duration(r.duration)}</span>` : "");
      el.innerHTML = `
        <button class="rec-thumb ${r.in_use ? "live" : ""}" type="button" data-act="play-file" aria-label="Riproduci ${esc(r.name)}">${recThumbHtml(r, ch)}${dur}</button>
        <div class="rec-info">
          <div class="rec-channel">${r.in_use ? '<span class="rec-live">REC</span>' : ""}${esc(ch?.display_name || r.channel)}${cue}</div>
          <div class="rec-title" title="${esc(T(r.title) || r.name)}">${esc(T(r.title) || r.name)}</div>
          <div class="rec-meta"><span>${started}</span><span data-size></span>${r.game ? `<span>${esc(r.game)}</span>` : ""}</div>
        </div>
        <div class="rec-actions">
          <a class="icon-btn" href="/recordings/${encodeURIComponent(r.name)}?dl=1" title="Scarica" aria-label="Scarica ${esc(r.name)}">${icon("download")}</a>
          <button class="icon-btn" type="button" data-act="delete-file" ${r.in_use ? "disabled title='In registrazione'" : "title='Elimina'"} aria-label="Elimina ${esc(r.name)}">${icon("trash")}</button>
        </div>`;
    } else if (r.in_use && !isMinimal()) {
      // Aggiorna solo la miniatura live quando cambia il suo intervallo di aggiornamento.
      const img = el.querySelector(".rec-thumb img");
      const src = ch ? frameSrc(ch) : "";
      if (img && src && img.getAttribute("src") !== src) img.src = src;
    }
    setText(el.querySelector("[data-size]"), humanSize(S.activeSizes[r.name] ?? r.size));
  }
  function renderRecordings() {
    const box = $("#recs");
    const term = ($("#recSearch").value || "").trim().toLocaleLowerCase();
    const list = S.recordings.filter((r) => !term || `${r.name} ${r.title || ""} ${r.game || ""}`.toLocaleLowerCase().includes(term));
    const total = S.recordings.reduce((sum, r) => sum + (S.activeSizes[r.name] ?? r.size), 0);
    setText($("#recTotal"), S.recordings.length ? `${S.recordings.length} file, ${humanSize(total)}` : "");
    let empty = box.querySelector(":scope > .empty");
    if (!list.length) {
      for (const n of [...recNodes.values(), ...dayNodes.values()]) n.remove();
      recNodes.clear(); dayNodes.clear();
      if (!empty) { empty = document.createElement("div"); empty.className = "empty"; box.appendChild(empty); }
      empty.textContent = S.recordings.length ? "Nessun file corrisponde al filtro." : "Le registrazioni compariranno qui appena un canale in REC va in onda.";
      return;
    }
    empty?.remove();
    const wanted = [];
    const seenFiles = new Set(), seenDays = new Set();
    let lastDay = "";
    for (const r of list) {
      const day = dayLabel(r.order_ts);
      if (day !== lastDay) {
        let h = dayNodes.get(day);
        if (!h) { h = document.createElement("div"); h.className = "rec-day"; h.textContent = day; dayNodes.set(day, h); }
        wanted.push(h); seenDays.add(day); lastDay = day;
      }
      let el = recNodes.get(r.name);
      if (!el) { el = document.createElement("div"); el.className = "rec"; el.dataset.file = r.name; recNodes.set(r.name, el); }
      updateRecNode(el, r);
      wanted.push(el); seenFiles.add(r.name);
    }
    for (const [k, n] of recNodes) if (!seenFiles.has(k)) { n.remove(); recNodes.delete(k); }
    for (const [k, n] of dayNodes) if (!seenDays.has(k)) { n.remove(); dayNodes.delete(k); }
    wanted.forEach((node, i) => { if (box.children[i] !== node) box.insertBefore(node, box.children[i] || null); });
  }
  function updateActiveSizes() {
    for (const [name, size] of Object.entries(S.activeSizes)) {
      const el = recNodes.get(name)?.querySelector("[data-size]");
      if (el) setText(el, humanSize(size));
    }
  }

  // ── Pannello dettagli canale ──
  function openDrawer(spec) {
    S.drawer = spec;
    renderDrawer();
    const d = $("#drawer");
    d.classList.add("open");
    d.setAttribute("aria-hidden", "false");
    setTimeout(() => $(".drawer-head .icon-btn").focus(), 50);
  }
  function closeDrawer() {
    S.drawer = null;
    $("#drawer").classList.remove("open");
    $("#drawer").setAttribute("aria-hidden", "true");
  }
  function qualityOptions(selected) {
    return S.quality.map((q) => `<option value="${esc(q)}" ${q === selected ? "selected" : ""}>${esc(q)}</option>`).join("");
  }

  function renderDrawer() {
    if (!S.drawer) return;
    const body = $("#drawerBody");
    if (body.contains(document.activeElement) && document.activeElement.tagName === "SELECT") return;
    if (S.drawer.type === "settings") { renderSettings(body); return; }
    const ch = S.channels.get(S.drawer.name);
    if (!ch) { closeDrawer(); return; }
    $("#drawerTitle").innerHTML = `<span class="avatar">${avatarHtml(ch)}</span><span>${esc(ch.display_name || ch.name)}</span>${platTag(ch)}`;
    const st = statusOf(ch);
    const level = rerunLevel(ch);
    const policyLabel = { mark: "Registra e segna", skip: "Salta le rerun", record: "Ignora i sospetti" };
    const r = ch.rerun;
    const rerunBlock = (r && (isOnAir(ch))) ? `
        <p>${level ? `<b>${{ confirmed: "Rerun", probable: "Probabile rerun", suspect: "Possibile rerun" }[level]}</b>` : "Nessun indizio di rerun"}${level !== "confirmed" ? ` · punteggio ${r.score}/100` : ""}${r.override ? " · valutazione manuale" : ""}</p>
        <div class="meter" aria-hidden="true"><div style="width:${Math.min(100, r.score || 0)}%"></div></div>
        ${r.reasons?.length ? `<ul class="reasons">${r.reasons.map((x) => `<li>${esc(x.text)} <b>+${x.points}</b></li>`).join("")}</ul>` : ""}
        <div class="btn-row" style="margin-top:12px">
          <button class="btn btn-ghost" type="button" data-act="verdict" data-value="rerun">È una rerun</button>
          <button class="btn btn-ghost" type="button" data-act="verdict" data-value="original">Non è una rerun</button>
          ${r.override ? `<button class="btn btn-ghost" type="button" data-act="verdict" data-value="">Torna ad automatico</button>` : ""}
        </div>` : `<p class="muted">La valutazione compare quando il canale è in onda. Gli indizi usati: tag e titolo, titoli già trasmessi, spettatori e orario rispetto allo storico del canale.</p>`;
    body.innerHTML = `
      ${ch.thumb_url ? `<div class="drawer-hero"><img src="${esc(thumbSrc(ch))}" alt=""></div>` : ""}
      <div class="group">
        <dl class="kv">
          <dt>Stato</dt><dd>${st.html}</dd>
          ${ch.title ? `<dt>Titolo</dt><dd>${esc(T(ch.title))}</dd>` : ""}
          ${ch.game ? `<dt>Categoria</dt><dd>${esc(ch.game)}</dd>` : ""}
          ${ch.viewers != null ? `<dt>Spettatori</dt><dd>${ch.viewers}</dd>` : ""}
          ${ch.actually_recording ? `<dt>In REC da</dt><dd><span data-since="${ch.rec_started_at}">${duration(serverNow() - ch.rec_started_at)}</span></dd><dt>File</dt><dd>${esc(ch.current_file || "")}</dd>` : ""}
          ${ch.resolved_quality ? `<dt>Qualità aperta</dt><dd>${esc(ch.resolved_quality)}</dd>` : ""}
          <dt>Ultima live</dt><dd title="${esc(fullDate(ch.last_live))}">${esc(ago(ch.last_live))}</dd>
          <dt>Ultimo controllo</dt><dd>${ch.sleeping ? "sospeso" : ch.last_checked_at ? `<span data-rel="${ch.last_checked_at}">${esc(ago(ch.last_checked_at))}</span>` : "mai"}</dd>
          ${ch.preview_active ? `<dt>Preview</dt><dd>${ch.preview_mode === "shared" ? "dal flusso della REC" : "flusso temporaneo"}, ${ch.preview_connections} in visione</dd>` : ""}
          ${ch.check_failures ? `<dt>Errori di controllo</dt><dd>${ch.check_failures} di fila</dd>` : ""}
        </dl>
        ${ch.last_error ? `<p class="error-box">${esc(ch.last_error)}</p>` : ""}
      </div>
      <div class="group">
        <h3>Registrazione</h3>
        <div class="btn-row">
          <button class="btn ${ch.is_recording ? "btn-ghost" : "btn-primary"}" type="button" data-act="rec-toggle" ${ch.sleeping ? "disabled" : ""}>${icon(ch.is_recording ? "pause" : "rec")}${ch.is_recording ? "Metti in pausa" : "Registra"}</button>
          ${isOnAir(ch) || ch.last_check_result === "rerun" ? `<button class="btn btn-ghost" type="button" data-act="watch">${icon("play")}Guarda</button>` : ""}
          <button class="btn btn-ghost" type="button" data-act="once" aria-pressed="${ch.single_live_mode}" title="Registra solo la prossima live (o quella in corso), poi metti in pausa">${icon("clock")}${ch.single_live_mode ? "Una volta: attiva" : "Solo una volta"}</button>
          <button class="btn btn-ghost" type="button" data-act="sleep" aria-pressed="${ch.sleeping}" title="A dormire: nessun controllo e nessuna registrazione">${icon(ch.sleeping ? "sun" : "moon")}${ch.sleeping ? "Risveglia" : "Metti a dormire"}</button>
          <button class="btn btn-ghost" type="button" data-act="check" ${ch.sleeping ? "disabled" : ""}>${icon("refresh")}Controlla ora</button>
        </div>
        <div class="field" style="margin-top:14px">
          <label for="dQuality">Qualità</label>
          <select id="dQuality" data-act="quality">${qualityOptions(ch.quality)}</select>
          <small>Se la qualità non esiste, viene scelta la più vicina. Cambiarla durante la REC chiude il file e ne apre uno nuovo.</small>
        </div>
      </div>
      <div class="group">
        <h3>Rerun</h3>
        ${rerunBlock}
        <div class="field" style="margin-top:14px">
          <label for="dPolicy">Quando una live sembra una rerun</label>
          <select id="dPolicy" data-act="policy">
            <option value="" ${!ch.rerun_policy ? "selected" : ""}>Predefinito (${esc(policyLabel[S.config.rerun_default_policy] || "Registra e segna")})</option>
            <option value="mark" ${ch.rerun_policy === "mark" ? "selected" : ""}>Registra e segna</option>
            <option value="skip" ${ch.rerun_policy === "skip" ? "selected" : ""}>Salta (non registrare le rerun probabili)</option>
            <option value="record" ${ch.rerun_policy === "record" ? "selected" : ""}>Ignora i sospetti</option>
          </select>
          ${S.config.helix_available ? "" : "<small>Con TWITCH_CLIENT_ID e TWITCH_CLIENT_SECRET il rilevamento usa anche tag ufficiali, VOD e spettatori.</small>"}
        </div>
      </div>
      <div class="group">
        <div class="btn-row">
          <a class="btn btn-ghost" href="${esc(ch.home_url)}" target="_blank" rel="noopener noreferrer">${icon("external")}Apri su ${esc(ch.platform_label)}</a>
          <button class="btn btn-danger" type="button" data-act="remove">${icon("trash")}Rimuovi canale</button>
        </div>
      </div>`;
  }

  function renderSettings(body) {
    const c = S.config;
    $("#drawerTitle").textContent = "Impostazioni e strumenti";
    const yes = (v) => (v ? "sì" : "no");
    body.innerHTML = `
      <div class="group">
        <h3>Tutti i canali</h3>
        <div class="btn-row">
          <button class="btn btn-ghost" type="button" data-cmd="resume-all">${icon("rec")}Registra tutti</button>
          <button class="btn btn-ghost" type="button" data-cmd="pause-all">${icon("pause")}Metti tutti in pausa</button>
          <button class="btn btn-ghost" type="button" data-cmd="check-all">${icon("refresh")}Controlla tutti</button>
        </div>
      </div>
      <div class="group">
        <h3>Importa ed esporta</h3>
        <div class="btn-row">
          <a class="btn btn-ghost" href="/api/export?format=json">${icon("download")}Esporta JSON</a>
          <a class="btn btn-ghost" href="/api/export?format=csv">${icon("download")}Esporta CSV</a>
          <button class="btn btn-ghost" type="button" data-cmd="import">${icon("upload")}Importa file</button>
          <input class="import-file" id="importFile" type="file" accept=".json,.csv,application/json,text/csv">
        </div>
        <label style="display:flex;gap:8px;margin-top:10px;font-size:13px"><input type="checkbox" id="importUpdateQuality"> Aggiorna la qualità dei canali già presenti</label>
        <p class="muted" id="importResult" style="font-size:13px"></p>
      </div>
      <div class="group">
        <h3>Configurazione attiva</h3>
        <dl class="kv">
          <dt>Controlli Twitch</dt><dd>${c.helix_checks ? "API Helix, una richiesta ogni 100 canali" : "Streamlink, un processo per canale"}</dd>
          <dt>Spazio minimo</dt><dd>${c.min_free_gb ? `${c.min_free_gb} GB liberi, sotto non partono nuove REC` : "nessun limite"}</dd>
          <dt>Intervalli</dt><dd>online ${c.online_check_interval}s, offline ${c.offline_check_interval}s, errori ${c.error_check_interval}s</dd>
          <dt>Conferme offline</dt><dd>${c.offline_confirmations} a ${c.offline_confirm_interval}s</dd>
          <dt>Riconnessioni</dt><dd>${c.reconnect_tries} tentativi ogni ${c.reconnect_wait}s</dd>
          <dt>Rerun</dt><dd>${{ mark: "registra e segna", skip: "salta", record: "ignora i sospetti" }[c.rerun_default_policy]}; sospetta da ${c.rerun_suspect_score}, probabile da ${c.rerun_probable_score}${c.skip_reruns ? "; le rerun dichiarate vengono sempre saltate" : ""}</dd>
          <dt>Notifiche</dt><dd>${esc(c.notify_type)}</dd>
          <dt>Conversione MP4</dt><dd>${yes(c.auto_convert)}${c.auto_convert && c.delete_after_convert ? ", il TS viene eliminato" : ""}</dd>
          <dt>Deduplicazione</dt><dd>${yes(c.dedup_enabled)}</dd>
          <dt>Worker</dt><dd>${c.monitor_workers}</dd>
          <dt>Versione</dt><dd>${esc(c.version || "dev")}</dd>
        </dl>
        <p class="muted" style="font-size:12.5px">Si modificano nel file .env e si applicano al riavvio del container.</p>
      </div>
      <div class="group">
        <h3>Aspetto</h3>
        <div class="field">
          <label for="uiMode">Interfaccia</label>
          <select id="uiMode">
            <option value="auto" ${S.uiMode === "auto" ? "selected" : ""}>Predefinita del server (${S.config.minimal_ui ? "minimal" : "completa"})</option>
            <option value="full" ${S.uiMode === "full" ? "selected" : ""}>Completa: copertine e avatar</option>
            <option value="minimal" ${S.uiMode === "minimal" ? "selected" : ""}>Minimal: solo testo, nessuna immagine</option>
          </select>
          <small>Vale solo per questo browser. La predefinita si imposta con MINIMAL_UI nel file .env.</small>
        </div>
      </div>
      <div class="group">
        <h3>Scorciatoie</h3>
        <dl class="kv"><dt><kbd>/</kbd></dt><dd>Cerca</dd><dt><kbd>Esc</kbd></dt><dd>Chiudi pannello o player</dd><dt><kbd>s</kbd></dt><dd>Selezione multipla</dd><dt><kbd>h</kbd></dt><dd>Nascondi o mostra copertine e video</dd></dl>
      </div>`;
  }

  // ── Player ──
  let mpegtsLoading = null;
  // Prima la copia inclusa nell'immagine Docker, poi il CDN solo se manca (per esempio in sviluppo).
  const MPEGTS_SOURCES = ["/static/vendor/mpegts.js", "https://cdn.jsdelivr.net/npm/mpegts.js@1.7.3/dist/mpegts.js"];
  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = src; s.onload = resolve;
      s.onerror = () => { s.remove(); reject(new Error(src)); };
      document.head.appendChild(s);
    });
  }
  function loadMpegts() {
    if (window.mpegts) return Promise.resolve();
    if (!mpegtsLoading) {
      mpegtsLoading = MPEGTS_SOURCES.reduce((chain, src) => chain.catch(() => loadScript(src)), Promise.reject())
        .catch((err) => { mpegtsLoading = null; throw err; });
    }
    return mpegtsLoading;
  }
  const clock = (sec) => {
    if (!Number.isFinite(sec) || sec < 0) return "0:00";
    sec = Math.floor(sec);
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), x = String(sec % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${x}` : `${m}:${x}`;
  };

  const P = {
    mts: null, src: null, timers: {}, tries: 0, lastTime: -1, lastMove: 0, idleSince: 0,
    el: () => $("#player"), v: () => $("#video"),
  };
  const MAX_TRIES = 6;

  function pStatus(text, { error = false, retry = false } = {}) {
    const box = $("#playerStatus");
    box.hidden = !text;
    $("#pcBig").hidden = !!text;
    box.classList.toggle("error", error);
    $("#playerStatusText").textContent = text || "";
    $("#playerRetry").hidden = !retry;
  }
  function pClearTimers() { Object.values(P.timers).forEach((t) => { clearTimeout(t); clearInterval(t); }); P.timers = {}; }
  function pDetach() {
    const v = P.v();
    if (P.mts) { const m = P.mts; P.mts = null; try { m.pause(); m.unload(); m.detachMediaElement(); m.destroy(); } catch { /* già chiuso */ } }
    v.onplaying = v.onwaiting = v.onerror = v.onended = v.ontimeupdate = v.onloadedmetadata = v.onvolumechange = v.onpause = v.onplay = null;
    try { v.pause(); v.removeAttribute("src"); v.load(); } catch { /* nessun media */ }
  }

  function pAttach() {
    const src = P.src;
    if (!src) return;
    pDetach();
    const v = P.v();
    v.muted = store.get("tr-muted", "0") === "1";
    v.volume = Math.min(1, Math.max(0, Number(store.get("tr-volume", "0.8")) || 0.8));
    v.onplaying = () => {
      pStatus("");
      clearTimeout(P.timers.stable);
      P.timers.stable = setTimeout(() => { P.tries = 0; }, 60000);
      pSync();
    };
    v.onwaiting = () => { if (src.live) pStatus("In attesa del flusso…"); };
    v.onpause = v.onplay = v.ontimeupdate = v.onvolumechange = pSync;
    v.onended = () => { if (src.live) pReconnect("il flusso si è interrotto"); else pSync(); };
    v.onerror = () => src.live ? pReconnect("errore di riproduzione") : pStatus("Il browser non riesce a riprodurre questo file. Puoi scaricarlo.", { error: true });

    const needsMpegts = src.live || src.ts;
    if (needsMpegts) {
      if (!window.mpegts || !mpegts.isSupported()) {
        const ios = /iPhone|iPod/.test(navigator.userAgent);
        pStatus(ios
          ? "Su iPhone serve iOS 17.1 o successivo per guardare la diretta qui. Puoi aprirla nell'app della piattaforma con il pulsante in basso."
          : "Questo browser non supporta la riproduzione MPEG-TS (serve Media Source Extensions).", { error: true });
        return;
      }
      const m = mpegts.createPlayer(
        { type: "mpegts", isLive: !!src.live, url: new URL(src.url, location.href).href, duration: src.live ? undefined : (src.duration ? src.duration * 1000 : undefined) },
        src.live ? {
          enableWorker: false, lazyLoad: false, enableStashBuffer: true, stashInitialSize: 256 * 1024,
          liveBufferLatencyChasing: true, liveBufferLatencyMaxLatency: 10, liveBufferLatencyMinRemain: 3,
          liveSync: true, liveSyncMaxLatency: 10, liveSyncTargetLatency: 4, liveSyncPlaybackRate: 1.1,
          fixAudioTimestampGap: true, autoCleanupSourceBuffer: true,
          autoCleanupMaxBackwardDuration: 20, autoCleanupMinBackwardDuration: 10,
        } : { enableWorker: false, lazyLoad: true, seekType: "range", fixAudioTimestampGap: true, autoCleanupSourceBuffer: true },
      );
      P.mts = m;
      m.attachMediaElement(v);
      m.on(mpegts.Events.ERROR, (type, detail, info) => {
        if (P.mts !== m) return;
        console.warn("[player]", type, detail, info);
        const reason = type === mpegts.ErrorTypes?.MEDIA_ERROR ? "errore di decodifica"
          : /HttpStatusCodeInvalid/i.test(detail) ? `il server ha risposto ${info?.code || "con un errore"}`
            : /Timeout/i.test(detail) ? "nessuna risposta dal server" : "connessione interrotta";
        if (src.live) pReconnect(reason);
        else pStatus("Il file non si può riprodurre nel browser. Puoi scaricarlo.", { error: true });
      });
      m.load();
    } else {
      v.src = src.url;
    }
    pStatus("Caricamento…");
    P.lastTime = -1; P.idleSince = Date.now();
    pAutoplay(v);
    clearInterval(P.timers.watch);
    if (src.live) P.timers.watch = setInterval(pWatchdog, 2000);
  }

  function pAutoplay(v) {
    v.play().catch(() => {
      // Telefoni e alcuni browser bloccano l'avvio con audio: si riparte muti con l'invito a toccare.
      if (v.muted) { pStatus(""); pSync(); return; }
      v.muted = true;
      v.play().then(() => { $("#pcUnmute").hidden = false; }).catch(() => { pStatus(""); pSync(); });
    });
  }

  // Se il video resta fermo (nessun avanzamento) per 15 s mentre dovrebbe suonare, si riconnette.
  function pWatchdog() {
    const v = P.v();
    if (!P.src?.live || v.paused) { P.idleSince = Date.now(); return; }
    if (v.currentTime !== P.lastTime) { P.lastTime = v.currentTime; P.idleSince = Date.now(); }
    else if (Date.now() - P.idleSince > 15000) { pReconnect("flusso fermo"); return; }
    // Troppo indietro rispetto alla diretta (dopo una pausa o un rallentamento): salta in avanti.
    const edge = v.buffered.length ? v.buffered.end(v.buffered.length - 1) : 0;
    if (edge - v.currentTime > 25) v.currentTime = edge - 3;
    pSync();
  }

  function pReconnect(reason) {
    if (!P.src?.live || P.timers.retry) return;
    clearInterval(P.timers.watch);
    if (P.tries >= MAX_TRIES) {
      pDetach();
      pStatus(`La diretta non risponde (${reason}).`, { error: true, retry: true });
      return;
    }
    P.tries++;
    const delay = Math.min(10000, 1000 * 2 ** (P.tries - 1));
    pStatus(`Riconnessione: ${reason}. Tentativo ${P.tries} di ${MAX_TRIES}…`);
    P.timers.retry = setTimeout(() => { P.timers.retry = null; pAttach(); }, delay);
  }

  function pSync() {
    const v = P.v(), src = P.src;
    if (!src) return;
    P.el().classList.toggle("paused", v.paused);
    $("#pcBig").innerHTML = icon(v.paused ? "play" : "pause");
    $("#pcBig").setAttribute("aria-label", v.paused ? "Riproduci" : "Pausa");
    if (!v.muted) $("#pcUnmute").hidden = true;
    $("#pcPlay").innerHTML = icon(v.paused ? "play" : "pause");
    $("#pcPlay").setAttribute("aria-label", v.paused ? "Riproduci" : "Pausa");
    const muted = v.muted || v.volume === 0;
    $("#pcMute").innerHTML = icon(muted ? "volume-x" : "volume");
    $("#pcMute").setAttribute("aria-label", muted ? "Riattiva l'audio" : "Disattiva l'audio");
    if (document.activeElement !== $("#pcVol")) $("#pcVol").value = muted ? 0 : v.volume;
    if (src.live) {
      const edge = v.buffered.length ? v.buffered.end(v.buffered.length - 1) : 0;
      const behind = edge && edge - v.currentTime > 8;
      const live = $("#pcLive");
      live.classList.toggle("behind", !!behind);
      live.title = behind ? `${Math.round(edge - v.currentTime)} s dietro la diretta: clic per tornare in diretta` : "Sei in diretta";
      $("#pcTime").textContent = src.since ? duration(serverNow() - src.since) : "";
    } else {
      const total = Number.isFinite(v.duration) && v.duration > 0 ? v.duration : (src.duration || 0);
      $("#pcTime").textContent = `${clock(v.currentTime)} / ${clock(total)}`;
      if (total && document.activeElement !== $("#pcSeek")) $("#pcSeek").value = Math.round((v.currentTime / total) * 1000);
    }
  }

  function pTitle() {
    const src = P.src;
    if (!src) return;
    $("#playerTitle").textContent = src.realTitle && coversOff() ? "Titolo nascosto" : src.title;
    $("#playerAvatar").innerHTML = src.channel ? (isMinimal() ? "" : avatarHtml(src.channel)) : "";
    $("#playerAvatar").hidden = !src.channel || isMinimal();
  }

  function pShowControls() {
    const el = P.el();
    el.classList.remove("idle");
    clearTimeout(P.timers.idle);
    P.timers.idle = setTimeout(() => { if (!P.v().paused && !el.matches(":focus-within .pc *")) el.classList.add("idle"); }, lastPointer === "mouse" ? 2500 : 3500);
  }

  async function openPlayer(src) {
    closePlayer(true);
    P.src = src; P.tries = 0;
    const el = P.el();
    el.classList.toggle("is-live", !!src.live);
    el.classList.toggle("touch", matchMedia("(pointer: coarse)").matches);
    $("#playerBox").style.transform = "";
    const tag = $("#ptTag");
    tag.textContent = src.live ? (src.rec ? "REC" : "LIVE") : "FILE";
    tag.className = `pt-tag ${src.live ? (src.rec ? "pt-rec" : "pt-air") : "pt-file"}`;
    $("#playerDetails").hidden = !src.channel || !src.live;
    $("#ptName").textContent = src.channel?.display_name || src.channel?.name || "";
    $("#playerSub").innerHTML = src.sub || "";
    pTitle();
    const ext = $("#playerExternal");
    ext.hidden = !src.external; if (src.external) ext.href = src.external;
    $("#playerExternalLabel").textContent = src.channel ? `Apri su ${src.channel.platform_label}` : "Apri";
    const dl = $("#playerDownload");
    dl.hidden = !src.download; if (src.download) dl.href = src.download;
    $("#pcSeek").value = 0;
    el.classList.add("open");
    el.setAttribute("aria-hidden", "false");
    document.body.classList.add("player-open");
    $("#pcUnmute").hidden = true;
    pShowControls();
    pStatus("Caricamento…");
    if (src.live || src.ts) {
      try { await loadMpegts(); } catch {
        pStatus("Impossibile caricare il lettore (mpegts.js). Controlla la connessione a internet.", { error: true });
        return;
      }
    }
    if (P.src !== src) return;
    pAttach();
    if (!el.classList.contains("touch")) setTimeout(() => $("#pcPlay").focus(), 50);
  }

  function closePlayer(silent = false) {
    pClearTimers();
    pDetach();
    P.src = null;
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    if (document.pictureInPictureElement) document.exitPictureInPicture().catch(() => {});
    if (!silent) {
      P.el().classList.remove("open");
      P.el().setAttribute("aria-hidden", "true");
      document.body.classList.remove("player-open");
    }
  }

  function watch(name) {
    const ch = S.channels.get(name);
    if (!ch) return;
    const meta = [ch.game, ch.viewers != null ? `${compact(ch.viewers)} spettatori` : "",
      ch.actually_recording ? "dal flusso in registrazione" : "flusso temporaneo, nessun file"].filter(Boolean);
    openPlayer({
      live: true,
      url: `/live/${encodeURIComponent(ch.platform)}/${encodeURIComponent(ch.name)}`,
      title: ch.title || ch.display_name || ch.name,
      realTitle: ch.title,
      sub: `<b>${esc(ch.display_name || ch.name)}</b>${meta.map((m) => `<span>${esc(m)}</span>`).join("")}`,
      channel: ch,
      rec: !!ch.actually_recording,
      external: ch.home_url,
      since: tsSeconds(ch.stream_started_at) || ch.rec_started_at || null,
    });
  }
  function playFile(file) {
    const r = S.recordings.find((x) => x.name === file);
    const ch = r && S.channels.get(r.channel);
    const when = r ? new Date(r.order_ts * 1000).toLocaleString("it-IT", { dateStyle: "medium", timeStyle: "short" }) : "";
    openPlayer({
      live: false, ts: file.toLowerCase().endsWith(".ts"),
      url: `/recordings/${encodeURIComponent(file)}`,
      download: `/recordings/${encodeURIComponent(file)}?dl=1`,
      title: r?.title || file,
      realTitle: r?.title,
      sub: `<b>${esc(ch?.display_name || r?.channel || "")}</b><span>${esc(when)}</span>${r?.game ? `<span>${esc(r.game)}</span>` : ""}`,
      channel: ch || null,
      duration: r?.duration || 0,
    });
  }

  // Controlli
  const pv = () => P.v();
  $("#pcPlay").addEventListener("click", () => { const v = pv(); if (v.paused) v.play().catch(() => {}); else v.pause(); });
  let lastPointer = "mouse";
  const togglePlay = () => { const v = pv(); if (v.paused) v.play().catch(() => {}); else v.pause(); };
  $("#playerStage").addEventListener("pointerdown", (e) => { lastPointer = e.pointerType || "mouse"; }, true);

  // Tocco sul video: un tocco mostra o nasconde i controlli. Sui file, un doppio tocco
  // sul terzo sinistro o destro va indietro o avanti di 10 secondi (come nelle app video).
  let tapTimer = null, lastTap = { t: 0, x: 0 };
  function seekBy(delta, side) {
    const v = pv();
    v.currentTime = Math.max(0, Math.min((Number.isFinite(v.duration) ? v.duration : Infinity), v.currentTime + delta));
    const fx = side === "left" ? $("#fxBack") : $("#fxFwd");
    fx.classList.remove("show"); void fx.offsetWidth; fx.classList.add("show");
  }
  $("#playerStage").addEventListener("click", (e) => {
    if (e.target.closest(".pc, .player-status, .pc-big, .pc-unmute")) return;
    if (lastPointer === "drag") return;
    if (lastPointer === "mouse") { togglePlay(); return; }
    const el = P.el();
    const rect = e.currentTarget.getBoundingClientRect();
    const x = (e.clientX - rect.left) / rect.width;
    const now = Date.now();
    if (!P.src?.live && now - lastTap.t < 300 && (x < 0.35 || x > 0.65)) {
      clearTimeout(tapTimer); tapTimer = null;
      seekBy(x < 0.35 ? -10 : 10, x < 0.35 ? "left" : "right");
      lastTap = { t: 0, x: 0 };
      return;
    }
    lastTap = { t: now, x };
    clearTimeout(tapTimer);
    tapTimer = setTimeout(() => {
      if (el.classList.contains("idle")) pShowControls(); else { clearTimeout(P.timers.idle); el.classList.add("idle"); }
    }, P.src?.live ? 0 : 220);
  });
  $("#playerStage").addEventListener("dblclick", (e) => { if (lastPointer === "mouse" && !e.target.closest(".pc")) toggleFullscreen(); });
  $("#pcBig").addEventListener("click", () => { togglePlay(); pShowControls(); });

  // Trascinare verso il basso (dalla barra in alto o dal video) chiude il player.
  (() => {
    const box = $("#playerBox");
    let drag = null;
    const start = (e) => {
      if (e.pointerType === "mouse" || !P.el().classList.contains("touch") || e.target.closest(".pc, input, a, .player-actions")) return;
      if (document.fullscreenElement) return;
      drag = { y: e.clientY, x: e.clientX, dy: 0, t: Date.now(), active: false };
    };
    const move = (e) => {
      if (!drag) return;
      const dy = e.clientY - drag.y, dx = Math.abs(e.clientX - drag.x);
      if (!drag.active) {
        if (dy > 12 && dy > dx * 1.5) { drag.active = true; box.classList.add("dragging"); }
        else if (dx > 12 || dy < -12) { drag = null; return; }
        else return;
      }
      drag.dy = Math.max(0, dy);
      box.style.transform = `translateY(${drag.dy}px)`;
      box.style.opacity = String(Math.max(0.4, 1 - drag.dy / 600));
      e.preventDefault();
    };
    const end = () => {
      if (!drag) return;
      const { dy, t, active } = drag;
      drag = null;
      if (!active) return;
      box.classList.remove("dragging");
      lastPointer = "drag";   // il tocco che termina il trascinamento non apre i controlli
      setTimeout(() => { lastPointer = "touch"; }, 50);
      const fast = dy / Math.max(1, Date.now() - t) > 0.6;
      if (dy > 140 || (fast && dy > 50)) {
        box.style.transform = "translateY(100%)"; box.style.opacity = "0";
        setTimeout(() => { closePlayer(); box.style.transform = ""; box.style.opacity = ""; }, 180);
      } else { box.style.transform = ""; box.style.opacity = ""; }
    };
    for (const target of [$("#playerTop"), $("#playerStage")]) target.addEventListener("pointerdown", start);
    window.addEventListener("pointermove", move, { passive: false });
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
  })();
  $("#pcUnmute").addEventListener("click", () => {
    const v = pv(); v.muted = false; if (v.volume === 0) v.volume = 0.8;
    store.set("tr-muted", "0"); $("#pcUnmute").hidden = true;
  });
  $("#pcMute").addEventListener("click", () => { const v = pv(); v.muted = !v.muted; if (!v.muted && v.volume === 0) v.volume = 0.5; store.set("tr-muted", v.muted ? "1" : "0"); });
  $("#pcVol").addEventListener("input", (e) => { const v = pv(); v.volume = Number(e.target.value); v.muted = v.volume === 0; store.set("tr-volume", String(v.volume)); store.set("tr-muted", v.muted ? "1" : "0"); });
  $("#pcSeek").addEventListener("input", (e) => {
    const v = pv(), total = Number.isFinite(v.duration) && v.duration > 0 ? v.duration : (P.src?.duration || 0);
    if (total) v.currentTime = (Number(e.target.value) / 1000) * total;
  });
  $("#pcLive").addEventListener("click", () => {
    const v = pv(); if (v.buffered.length) v.currentTime = v.buffered.end(v.buffered.length - 1) - 2; v.play().catch(() => {});
  });
  $("#playerRetry").addEventListener("click", () => { P.tries = 0; pAttach(); });
  $("#playerDetails").addEventListener("click", () => {
    const name = P.src?.channel?.name;
    closePlayer();
    if (name) openDrawer({ type: "channel", name });
  });
  function toggleFullscreen() {
    const stage = $("#playerStage"), v = pv();
    if (document.fullscreenElement || document.webkitFullscreenElement) {
      (document.exitFullscreen || document.webkitExitFullscreen).call(document)?.catch?.(() => {});
      return;
    }
    const enter = stage.requestFullscreen || stage.webkitRequestFullscreen;
    if (enter) {
      Promise.resolve(enter.call(stage)).then(() => {
        // Sul telefono la diretta si guarda meglio in orizzontale (dove il browser lo consente).
        screen.orientation?.lock?.("landscape").catch(() => {});
      }).catch(() => v.webkitEnterFullscreen?.());
    } else if (v.webkitEnterFullscreen) {
      v.webkitEnterFullscreen();   // iPhone: schermo intero nativo del video
    }
  }
  $("#pcFull").addEventListener("click", toggleFullscreen);
  document.addEventListener("fullscreenchange", () => {
    $("#pcFull").innerHTML = icon(document.fullscreenElement ? "minimize" : "maximize");
    if (!document.fullscreenElement) screen.orientation?.unlock?.();
  });
  $("#pcPip").hidden = !document.pictureInPictureEnabled;
  $("#pcPip").addEventListener("click", () => {
    if (document.pictureInPictureElement) document.exitPictureInPicture().catch(() => {});
    else pv().requestPictureInPicture?.().catch(() => {});
  });
  // Mouse: i controlli compaiono al movimento. Tocco: li gestisce il tap sul video (vedi sopra);
  // toccare un controllo li tiene visibili.
  P.el().addEventListener("pointermove", (e) => { if (e.pointerType === "mouse") pShowControls(); });
  P.el().addEventListener("pointerdown", (e) => { if (e.pointerType === "mouse" || e.target.closest(".pc")) pShowControls(); });
  P.el().addEventListener("keydown", pShowControls);
  P.el().addEventListener("keydown", (e) => {
    if (!P.src || e.target.matches("input[type=range]") && ["ArrowLeft", "ArrowRight"].includes(e.key)) return;
    const v = pv();
    if (e.key === " " || e.key === "k") { e.preventDefault(); if (v.paused) v.play().catch(() => {}); else v.pause(); }
    else if (e.key === "m") $("#pcMute").click();
    else if (e.key === "h" && e.target.matches("input[type=range]")) toggleCovers();
    else if (e.key === "f") toggleFullscreen();
    else if (!P.src.live && e.key === "ArrowRight") v.currentTime += 10;
    else if (!P.src.live && e.key === "ArrowLeft") v.currentTime = Math.max(0, v.currentTime - 10);
  });

  // ── Nascondi copertine (pulsante rapido, tasto h) ──
  // Solo estetico: copertine, miniature, avatar e video vengono coperti; audio, REC e dati non cambiano.
  function setCovers(hidden) {
    document.documentElement.classList.toggle("covers-off", hidden);
    // La finestra picture-in-picture sta fuori dalla pagina: va chiusa per nasconderla davvero.
    if (hidden && document.pictureInPictureElement) document.exitPictureInPicture().catch(() => {});
    store.set("tr-covers-off", hidden ? "1" : "0");
    const btn = $('[data-cmd="panic"]');
    btn.setAttribute("aria-pressed", String(hidden));
    btn.classList.toggle("on", hidden);
    btn.innerHTML = icon(hidden ? "eye-off" : "eye");
    btn.title = hidden ? "Mostra copertine e video (h)" : "Nascondi copertine e video (h)";
    btn.setAttribute("aria-label", btn.title);
  }
  function redrawAll() {
    for (const m of [tiles, rows]) for (const el of m.values()) {
      el.querySelectorAll(".mon-empty, .avatar, .tools, .plat-slot, .row-status, .mon-sub").forEach((n) => { n._html = null; n._sig = null; });
    }
    recNodes.forEach((n) => { n._sig = null; });
    pTitle();
    if (S.drawer) renderDrawer();
    scheduleRender({ recordings: true });
  }
  const toggleCovers = () => { setCovers(!coversOff()); redrawAll(); };
  setCovers(coversOff());

  // ── Selezione multipla ──
  function setSelecting(on) {
    S.selecting = on;
    if (!on) S.selected.clear();
    document.body.classList.toggle("selecting", on);
    $$('[data-cmd="select-mode"]').forEach((b) => b.setAttribute("aria-pressed", String(on)));
    updateBulkbar();
    scheduleRender();
  }
  function toggleSelected(name, on) {
    if (on) S.selected.add(name); else S.selected.delete(name);
    updateBulkbar();
    scheduleRender();
  }
  function updateBulkbar() {
    $("#bulkbar").hidden = !S.selecting;
    $("#bulkCount").textContent = S.selected.size === 1 ? "1 selezionato" : `${S.selected.size} selezionati`;
    $$("#bulkbar [data-bulk], #bulkbar select").forEach((b) => { b.disabled = !S.selected.size; });
  }
  async function runBulk(op, value) {
    const names = [...S.selected];
    if (!names.length) return;
    if (op === "remove" && !(await confirmDialog(`Rimuovere ${names.length} canali? Le registrazioni già salvate restano sul disco.`, "Rimuovi"))) return;
    const r = await api("/api/room", { action: "bulk", channels: names, op, value });
    if (r.failed?.length) toast(`${r.done} fatti, ${r.failed.length} non riusciti: ${r.failed[0].msg}`, "warn");
    else if (r.ok) toast(`Fatto su ${r.done} canali.`);
    if (op === "remove") setSelecting(false);
  }

  // ── Eventi ──
  function initSelects() {
    const plat = $("#addPlatform");
    if (!plat.options.length) plat.innerHTML = S.platforms.map((p) => `<option value="${p.key}">${esc(p.label)}</option>`).join("");
    const q = $("#addQuality");
    if (!q.options.length && S.quality.length) { q.innerHTML = qualityOptions(S.config.default_quality || "best"); }
    const bq = $("#bulkQuality");
    if (bq.options.length < 2) bq.insertAdjacentHTML("beforeend", S.quality.map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join(""));
  }

  async function channelAction(kind, name, target) {
    const ch = S.channels.get(name);
    if (!ch) return;
    const run = (action, extra) => act(room(action, name, extra));
    switch (kind) {
      case "watch": return watch(name);
      case "details": return openDrawer({ type: "channel", name });
      case "rec-toggle": return run(ch.is_recording ? "pause" : "resume");
      case "once": return run("toggle_single_live");
      case "sleep": return run("toggle_sleep");
      case "check": await api("/api/check", { channel: name }); return toast("Controllo in coda.");
      case "remove":
        if (await confirmDialog(`Rimuovere ${ch.display_name || name}? Le registrazioni già salvate restano sul disco.`, "Rimuovi")) {
          closeDrawer(); await run("remove");
        }
        return;
      case "verdict": return run("rerun_override", { verdict: target.dataset.value || null });
    }
  }

  document.addEventListener("click", async (e) => {
    const cmd = e.target.closest("[data-cmd]");
    if (cmd) {
      switch (cmd.dataset.cmd) {
        case "theme": {
          const order = ["dark", "light"];
          const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
          const next = order[(order.indexOf(cur) + 1) % order.length];
          document.documentElement.dataset.theme = next; store.set("tr-theme", next); return;
        }
        case "panic": return toggleCovers();
        case "settings": return openDrawer({ type: "settings" });
        case "close-drawer": return closeDrawer();
        case "close-player": return closePlayer();
        case "select-mode": return setSelecting(!S.selecting);
        case "check-all": { const r = await api("/api/check", {}); return toast(`Controlli in coda: ${r.queued ?? 0}.`); }
        case "pause-all": return act(api("/api/room", { action: "pause_all" }));
        case "resume-all": return act(api("/api/room", { action: "resume_all" }));
        case "import": return $("#importFile")?.click();
        case "toggle-add": {
          const open = !document.body.classList.contains("adding");
          document.body.classList.toggle("adding", open);
          cmd.setAttribute("aria-expanded", String(open));
          if (open) $("#addChannel").focus();
          return;
        }
      }
    }
    const chip = e.target.closest("[data-filter]");
    if (chip) { S.filter = chip.dataset.filter; store.set("tr-filter", S.filter); scheduleRender(); return; }
    const bulk = e.target.closest("[data-bulk]");
    if (bulk) return runBulk(bulk.dataset.bulk);
    const actEl = e.target.closest("[data-act]");
    if (S.selecting && !e.target.closest("a, input, select")) {
      const item = e.target.closest(".row, .mon");
      if (item && (!actEl || actEl.classList.contains("mon-screen"))) { toggleSelected(item.dataset.name, !S.selected.has(item.dataset.name)); return; }
    }
    if (!actEl) return;
    const host = actEl.closest("[data-name]");
    if (actEl.dataset.act === "play-file" || actEl.dataset.act === "delete-file") {
      const file = actEl.closest("[data-file]").dataset.file;
      const rec = S.recordings.find((r) => r.name === file);
      if (actEl.dataset.act === "play-file") {
        if (rec?.in_use) { const ch = S.channels.get(rec.channel); if (ch) return watch(ch.name); }
        return playFile(file);
      }
      if (await confirmDialog(`Eliminare definitivamente ${file}?`, "Elimina")) {
        const r = await api("/api/delete_recording", { filename: file });
        if (!r.ok) toast(r.msg || "Eliminazione non riuscita.", "err"); else toast("File eliminato.");
      }
      return;
    }
    const name = host?.dataset.name || (S.drawer?.type === "channel" ? S.drawer.name : null);
    if (!name) return;
    lastInteraction = Date.now();
    await channelAction(actEl.dataset.act, name, actEl);
  });

  document.addEventListener("change", async (e) => {
    const t = e.target;
    if (t.matches(".row-check, .mon-check")) { toggleSelected(t.closest("[data-name]").dataset.name, t.checked); return; }
    if (t.id === "sort") { S.sort = t.value; store.set("tr-sort", S.sort); scheduleRender(); return; }
    if (t.id === "bulkQuality" && t.value) { await runBulk("quality", t.value); t.value = ""; return; }
    if (t.id === "bulkRerun" && t.value) { await runBulk("rerun_policy", t.value === "default" ? "" : t.value); t.value = ""; return; }
    if (t.id === "importFile" && t.files[0]) { await importFile(t.files[0]); t.value = ""; return; }
    if (t.id === "uiMode") {
      S.uiMode = t.value; store.set("tr-ui", S.uiMode); applyUiMode();
      for (const m of [tiles, rows]) for (const el of m.values()) { el.querySelectorAll(".mon-empty, .avatar, .tools, .plat-slot").forEach((n) => { n._html = null; n._sig = null; }); }
      recNodes.forEach((n) => { n._sig = null; });
      scheduleRender({ recordings: true }); return;
    }
    if (S.drawer?.type === "channel") {
      const name = S.drawer.name;
      if (t.dataset.act === "quality") { await act(room("set_quality", name, { quality: t.value })); t.blur(); }
      if (t.dataset.act === "policy") { await act(room("set_rerun_policy", name, { policy: t.value || null })); t.blur(); }
    }
  });

  ["pointerdown", "focusin"].forEach((type) => document.addEventListener(type, (e) => {
    if (e.target.closest?.("#wall, #rows")) lastInteraction = Date.now();
  }, true));

  $("#search").addEventListener("input", () => scheduleRender());
  $("#recSearch").addEventListener("input", () => renderRecordings());
  $("#sort").value = ["default", "name", "last_live", "viewers", "last_checked"].includes(S.sort) ? S.sort : "default";

  $("#addForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("#addChannel");
    const value = input.value.trim();
    if (!value) { input.focus(); return; }
    const r = await act(api("/api/room", { action: "add", channel: value, platform: $("#addPlatform").value, quality: $("#addQuality").value }));
    if (r.ok) input.value = "";
  });

  async function importFile(file) {
    const out = $("#importResult");
    const fd = new FormData();
    fd.append("file", file);
    fd.append("update_existing_quality", $("#importUpdateQuality")?.checked ? "true" : "false");
    try {
      const r = await fetch("/api/import", { method: "POST", headers: { "X-CSRF-Token": csrf() }, body: fd });
      if (r.status === 401) return goToLogin();
      const d = await r.json();
      const msg = r.ok ? `Importati ${d.imported} canali, ${d.updated || 0} qualità aggiornate, ${d.skipped} saltati.` : (d.error || "Importazione non riuscita.");
      if (out) out.textContent = msg;
      toast(msg, r.ok ? "ok" : "err");
    } catch (err) { toast(`Importazione non riuscita: ${err.message}`, "err"); }
  }

  $$(".tab").forEach((tab) => tab.addEventListener("click", () => {
    S.view = tab.dataset.tab;
    $(".layout").dataset.view = S.view;
    $$(".tab").forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
  }));

  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName);
    if (e.key === "Escape") {
      if ($("#player").classList.contains("open")) closePlayer();
      else if (S.drawer) closeDrawer();
      else if (S.selecting) setSelecting(false);
      return;
    }
    if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "/") { e.preventDefault(); $("#search").focus(); }
    if (e.key === "h") toggleCovers();
    if (e.key === "s") setSelecting(!S.selecting);
  });
  $("#player").addEventListener("click", (e) => { if (e.target.id === "player") closePlayer(); });


  // Contatori che scorrono senza attendere il server.
  let tick = 0;
  setInterval(() => {
    tick++;
    const now = serverNow();
    $$("[data-since]").forEach((el) => { const t = Number(el.dataset.since); if (t) el.textContent = duration(now - t); });
    if (tick % 20 === 0) $$("[data-rel]").forEach((el) => { el.textContent = ago(el.dataset.rel); });
    if (tick % 60 === 0) {
      $$(".mon").forEach((el) => { const ch = S.channels.get(el.dataset.name); if (ch) updateTile(el, ch); });
      renderRecordings();
    }
  }, 1000);

  // ── Collegamento in tempo reale ──
  let es = null, retryTimer = null;
  async function probeSession() {
    try { const r = await fetch("/api/status", { credentials: "same-origin", cache: "no-store" }); if (r.status === 401) goToLogin(); else if (r.ok) applyFull(await r.json()); } catch { /* rete assente */ }
  }
  function connect() {
    if (es) es.close();
    es = new EventSource("/api/stream");
    es.addEventListener("status", (ev) => { try { applyFull(JSON.parse(ev.data)); } catch (err) { console.error(err); } });
    es.addEventListener("delta", (ev) => { try { applyDelta(JSON.parse(ev.data)); } catch (err) { console.error(err); } });
    es.onerror = () => {
      es.close();
      $("#health").className = "health bad";
      $("#healthText").textContent = "Disconnesso";
      clearTimeout(retryTimer);
      retryTimer = setTimeout(() => { probeSession(); connect(); }, 3000);
    };
  }
  if ("EventSource" in window) connect();
  else { probeSession(); setInterval(probeSession, 8000); }
})();
