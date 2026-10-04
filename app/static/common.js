// Shared helpers for the public and admin pages.

const RD = {};

// ---------------------------------------------------------------- network
RD.api = async function (url, opts = {}) {
  const res = await fetch(url, {
    credentials: "same-origin",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    const err = new Error((data && data.detail) || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
};

// ---------------------------------------------------------------- formatting
RD.esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
RD.fmt = (n) => Number(n).toLocaleString();
RD.fmtTime = (iso) => iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" }) : "";
RD.fmtClock = (d) => d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });

RD.applyConfig = function (cfg) {
  const r = document.documentElement.style;
  r.setProperty("--c-active", cfg.colors.active);
  r.setProperty("--c-out", cfg.colors.eliminated);
  r.setProperty("--c-last", cfg.colors.last_round);
  r.setProperty("--c-win", cfg.colors.winner);
  r.setProperty("--c-prize", cfg.colors.prize);
  r.setProperty("--c-hl", cfg.colors.highlight);
  document.querySelectorAll("[data-org-name]").forEach((el) => (el.textContent = cfg.org_name));
  document.querySelectorAll("[data-org-initials]").forEach((el) => (el.textContent = cfg.org_initials));
  document.querySelectorAll("[data-posted-in]").forEach((el) => (el.textContent = `Posted in ${cfg.posted_in}`));
};

// ---------------------------------------------------------------- draw state helpers
RD.remaining = (state) => state.status.filter((r) => r === 0).length;

RD.ticketStatus = function (state, t) {
  const r = state.status[t - 1];
  if (r) {
    const round = state.rounds[r - 1];
    if (round?.kind === "prize") {
      return { cls: "prize", text: `${round.prize || round.label} winner` };
    }
    return { cls: "out", text: `Eliminated in ${state.schedule.labels[r - 1]}` };
  }
  if (state.finished) return { cls: "win", text: state.schedule.completion_label };
  return { cls: "in", text: "Still in" };
};

RD.search = function (query, state, holders) {
  const q = query.trim().toLowerCase();
  const hits = new Set();
  if (!q) return hits;
  if (/^#?\d+$/.test(q)) {
    const t = parseInt(q.replace("#", ""), 10);
    if (t >= 1 && t <= state.schedule.total) hits.add(t);
    return hits;
  }
  if (holders) {
    for (const [t, name] of Object.entries(holders)) {
      if (name.toLowerCase().includes(q)) hits.add(Number(t));
    }
  }
  return hits;
};

// ---------------------------------------------------------------- board
RD.renderBoard = function (el, cfg, state, { highlight = null, holders = null, fresh = null } = {}) {
  el.style.gridTemplateColumns = `repeat(${cfg.grid_columns}, minmax(0, 1fr))`;
  const winners = new Set(state.winners.map((w) => w.ticket));
  const last = state.rounds_done;
  const parts = [];
  for (let t = 1; t <= state.schedule.total; t++) {
    const r = state.status[t - 1];
    const prize = r && state.rounds[r - 1]?.kind === "prize";
    let cls = winners.has(t) ? "win" : prize ? "prize" : !r ? "active" : cfg.highlight_last_round && r === last ? "last" : "out";
    let style = "";
    if (fresh && fresh.has(t)) {
      cls += " fresh";
      style = ` style="animation-delay:${Math.floor(Math.random() * 900)}ms"`;
    }
    if (highlight && highlight.has(t)) cls += " hl";
    const who = holders && holders[t] ? ` · ${holders[t]}` : "";
    const tip = `#${t}${who} · ${RD.ticketStatus(state, t).text}`;
    parts.push(`<div class="cell ${cls}"${style} title="${RD.esc(tip)}">${t}</div>`);
  }
  el.innerHTML = parts.join("");
  el.classList.toggle("searching", !!(highlight && highlight.size));
};

// `matchLabel` names the highlight swatch; omit it to leave that entry out.
RD.legendHTML = (matchLabel = "", includePrize = false, resultLabel = "Finalist") =>
  `<span><i style="background:var(--c-active)"></i>Still in</span>` +
  `<span><i style="background:var(--c-last)"></i>Out this round</span>` +
  `<span><i style="background:var(--c-out)"></i>Out earlier</span>` +
  (includePrize ? `<span><i style="background:var(--c-prize)"></i>Gift-card winner</span>` : "") +
  `<span><i style="background:var(--c-win)"></i>${RD.esc(resultLabel)}</span>` +
  (matchLabel ? `<span><i style="background:var(--c-hl)"></i>${RD.esc(matchLabel)}</span>` : "");

// ---------------------------------------------------------------- announcement
RD.announcement = function (cfg, state) {
  const s = state.schedule;
  const total = RD.fmt(s.total);
  const nRounds = s.survivors.length;
  if (!state.started) {
    return {
      eyebrow: "Starting soon",
      headline: "Are you in it?",
      text: `The ${cfg.org_name} starts with ${total} tickets. Every green square is in the running for ${cfg.prize_text}. Good luck!`,
    };
  }
  if (state.finished) {
    const who = state.winners.map((w) => `#${w.ticket}${w.holder ? ` (${w.holder})` : ""}`);
    const one = who.length === 1;
    const result = one
      ? s.completion_label.toLowerCase()
      : s.completion_label_plural.toLowerCase();
    return {
      eyebrow: `Draw complete · ${s.labels[nRounds - 1]}`,
      headline: `We have ${one ? "our" : RD.fmt(who.length)} ${result}!`,
      text: `${one ? "Ticket" : "Tickets"} ${who.join(", ")} ${one ? "is" : "are"} ` +
        `the ${result} remaining. ${cfg.closing_note}`,
    };
  }
  const last = state.rounds[state.rounds.length - 1];
  if (last.kind === "prize") {
    const selected = last.selected_tickets.map((ticket) => `#${ticket}`).join(", ");
    return {
      eyebrow: `${last.label} complete · ${state.rounds_done} of ${nRounds}`,
      headline: `${last.prize || "Gift card"} winner drawn!`,
      text: `Congratulations to ticket ${selected}. It wins the gift card and leaves the grand-prize pool. ` +
        `${RD.fmt(last.survivors)} tickets remain in the reverse draw.`,
    };
  }
  return {
    eyebrow: `${last.label} complete · ${state.rounds_done} of ${nRounds}`,
    headline: "Are you still in it?",
    text: `The ${cfg.org_name} started with ${total} tickets. In ${last.label}, ${RD.fmt(last.eliminated_count)} tickets were pulled out. ` +
      `If your ticket number is green on the board, you're still in it, and still have a chance to win up to ${cfg.prize_text}! Good luck!`,
  };
};

// Fills the navy announcement band on the public board, and the same content
// inside a plain card on the admin page (`.card.hero` restyles it).
RD.renderHero = function (el, cfg, state) {
  const a = RD.announcement(cfg, state);
  el.classList.toggle("win", state.finished);
  const last = state.rounds[state.rounds.length - 1];
  let resultCards = "";
  if (state.finished && state.winners.length) {
    const label = state.winners.length === 1
      ? state.schedule.completion_label
      : state.schedule.completion_label_plural;
    const numbers = state.winners.map((w) =>
      `#${w.ticket}${w.holder ? ` · ${RD.esc(w.holder)}` : ""}`
    ).join("<br>");
    resultCards = `<div class="winner-cards"><div class="winner-card">` +
      `<span class="k">${RD.esc(label)}</span><span class="winner-list">${numbers}</span>` +
      `<span class="prize">${RD.esc(cfg.closing_note)}</span></div></div>`;
  } else if (last?.kind === "prize") {
    const numbers = last.selected_tickets.map((ticket) => `#${ticket}`).join(", ");
    resultCards = `<div class="winner-cards"><div class="winner-card prize-winner">` +
      `<span class="k">${RD.esc(last.prize || "Gift card winner")}</span>` +
      `<span class="n">${RD.esc(numbers)}</span></div></div>`;
  }
  const meta = state.finished ? ""
    : `<div class="hero-meta"><span class="prize-chip">🏆 ${RD.esc(cfg.prize_text)}</span>` +
      `<span class="muted small">${RD.esc(cfg.closing_note)}</span></div>`;
  el.innerHTML =
    `<div class="hero-deco" aria-hidden="true"><i></i><i></i></div>` +
    `<div class="hero-inner"><div class="hero-copy">` +
      `<div class="eyebrow"><i></i>${RD.esc(a.eyebrow)}</div>` +
      `<h1>${RD.esc(a.headline)}</h1>` +
      `<p class="lead">${RD.esc(a.text)}</p>` + meta +
    `</div>` + resultCards + `</div>`;
};

// ---------------------------------------------------------------- toast & modal
RD.toast = function (msg, isErr = false) {
  let box = document.querySelector(".toasts");
  if (!box) { box = document.createElement("div"); box.className = "toasts"; document.body.appendChild(box); }
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), isErr ? 5000 : 3000);
};

// RD.confirm({title, body, confirmText, danger, requireText}) -> Promise<boolean>
RD.confirm = function ({ title, body = "", confirmText = "Confirm", danger = false, requireText = "" }) {
  return new Promise((resolve) => {
    const d = document.createElement("dialog");
    d.className = "modal";
    d.innerHTML =
      `<form method="dialog"><div class="modal-body"><h3>${RD.esc(title)}</h3><p>${RD.esc(body)}</p>` +
      (requireText ? `<input class="input" autocomplete="off" placeholder="Type ${RD.esc(requireText)} to confirm">` : "") +
      `</div><div class="modal-actions"><button class="btn ghost" value="cancel">Cancel</button>` +
      `<button class="btn ${danger ? "danger" : "primary"}" value="ok">${RD.esc(confirmText)}</button></div></form>`;
    document.body.appendChild(d);
    const ok = d.querySelector('[value="ok"]');
    const input = d.querySelector("input");
    if (input) {
      ok.disabled = true;
      input.addEventListener("input", () => (ok.disabled = input.value.trim() !== requireText));
    }
    d.addEventListener("close", () => { resolve(d.returnValue === "ok"); d.remove(); });
    d.showModal();
    (input || ok).focus();
  });
};

// ---------------------------------------------------------------- tiny storage wrapper
RD.store = {
  get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (_) {} },
};

// ---------------------------------------------------------------- mobile navigation
(function initMobileNavigation() {
  const menus = Array.from(document.querySelectorAll(".nav-menu-toggle")).map((toggle) => {
    const shelf = document.getElementById(toggle.getAttribute("aria-controls"));
    const header = toggle.closest(".nav");
    if (!shelf || !header) return null;

    const setOpen = (open) => {
      toggle.setAttribute("aria-expanded", String(open));
      toggle.setAttribute("aria-label", open ? "Close navigation menu" : "Open navigation menu");
      shelf.hidden = !open;
      if (open) shelf.querySelector("a")?.focus();
    };
    const isOpen = () => toggle.getAttribute("aria-expanded") === "true";

    toggle.addEventListener("click", () => setOpen(!isOpen()));
    shelf.querySelectorAll("a").forEach((link) => link.addEventListener("click", () => setOpen(false)));
    return { toggle, header, setOpen, isOpen };
  }).filter(Boolean);

  if (!menus.length) return;
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const menu = menus.find(({ isOpen }) => isOpen());
    if (!menu) return;
    menu.setOpen(false);
    menu.toggle.focus();
  });
  document.addEventListener("click", (event) => {
    menus.forEach((menu) => {
      if (menu.isOpen() && !menu.header.contains(event.target)) menu.setOpen(false);
    });
  });
  window.addEventListener("resize", () => {
    if (window.innerWidth > 760) menus.forEach((menu) => menu.setOpen(false));
  });
})();
