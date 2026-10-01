// Trading dashboard — port of "Trading Dashboard v3".
//
// Real data: the signed-in holder, their tickets and in/out state, and the
// draw's stage and still-in counts, all from /api/trading/session.
// Demo data: every market, price and trade, from trading-markets.js. Requests
// and approvals move state in this tab only — nothing is sent anywhere.

(async function () {
  const $ = (id) => document.getElementById(id);
  const esc = RD.esc;
  const first = RDMarkets.first;

  let session = null;
  let demo = { markets: [], trades: [], incoming: null };

  const state = {
    mid: 48,
    k: 3,
    open: true,
    offered: null,     // null = "not chosen yet", filled from still-in tickets
    modal: null,       // null | "buy" | "sell" | "approve"
    targetId: null,
    pick: null,
    sent: {},          // market id -> {short, full}
    req: "none",       // "none" | "pending" | "approved" | "declined"
  };

  let toastTimer = null;

  // ---------------------------------------------------------------- derived
  const soldTickets = () =>
    state.req === "approved" && demo.incoming ? [demo.incoming.ticket] : [];

  // Still-in tickets the holder can actually trade: in the draw and not sold.
  function myStillIn() {
    const sold = new Set(soldTickets());
    return (session.tickets || [])
      .filter((t) => t.active && !sold.has(t.ticket))
      .map((t) => t.ticket);
  }

  function offeredTickets() {
    const live = myStillIn();
    if (state.offered === null) return live;
    const kept = state.offered.filter((n) => live.includes(n));
    // A market with nothing on offer is meaningless, so fall back to all.
    return kept.length ? kept : live;
  }

  const prices = () => ({ bid: state.mid - state.k, ask: state.mid + state.k });

  function trades() {
    const sold = soldTickets();
    const mine = sold.length && demo.incoming
      ? [{ b: demo.incoming.from, s: "You", t: demo.incoming.ticket, p: demo.incoming.price }]
      : [];
    return mine.concat(demo.trades);
  }

  // ---------------------------------------------------------------- toast
  function showToast(msg) {
    const el = $("tdToast");
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 3000);
  }

  // ---------------------------------------------------------------- header
  function renderHead() {
    const d = session.draw;
    const stage = d.finished ? d.rounds_total : Math.min(d.rounds_done + 1, d.rounds_total);
    $("tdStage").innerHTML =
      `STAGE ${stage} / ${d.rounds_total}` +
      (d.rounds_done
        ? `<span class="td-only-d"> · ROUND ${d.rounds_done} DONE</span>`
        : "");
    $("tdWho").textContent = session.name;
    const dots = Math.min(d.rounds_total, 10);
    $("tdDots").innerHTML = Array.from(
      { length: dots },
      (_, i) => `<i class="${i < d.rounds_done ? "on" : ""}"></i>`
    ).join("");
  }

  // ---------------------------------------------------------------- stats
  function renderStats() {
    const sold = new Set(soldTickets());
    $("tdStubs").innerHTML = (session.tickets || []).map((t) => {
      const kind = sold.has(t.ticket) ? "sold" : t.active ? "in" : "out";
      const tag = kind === "sold" ? "SOLD" : kind === "in" ? "IN" : "OUT";
      return `<div class="td-stub ${kind}" title="${esc(t.status)}">` +
        `<b>${t.ticket}</b><span>${tag}</span></div>`;
    }).join("") || `<span class="td-sub">No tickets assigned</span>`;

    const bids = demo.markets.map((m) => m.bid);
    const asks = demo.markets.map((m) => m.ask);
    const all = trades();
    $("tdStillIn").textContent = RD.fmt(session.draw.still_in);
    $("tdStillInOf").textContent = `of ${RD.fmt(session.draw.total)}`;
    $("tdBestBid").textContent = bids.length ? `$${Math.max(...bids)}` : "--";
    $("tdLowAsk").textContent = asks.length ? `$${Math.min(...asks)}` : "--";
    $("tdLastTrade").textContent = all.length ? `$${all[0].p}` : "--";
  }

  // ---------------------------------------------------------------- order book
  function renderBook() {
    const bids = demo.markets.map((m) => m.bid);
    const asks = demo.markets.map((m) => m.ask);
    const bestBid = bids.length ? Math.max(...bids) : null;
    const lowAsk = asks.length ? Math.min(...asks) : null;
    const canSell = myStillIn().length > 0;

    $("tdBook").innerHTML = demo.markets.map((m) => {
      const chips = m.tickets.map((n) => {
        const out = (m.out || []).includes(n);
        return `<span class="td-chip ${out ? "out" : "in"}">` +
          `<span class="td-only-d">#</span>${n}</span>`;
      }).join("");

      const warn = m.warn
        ? `<div class="td-warn"><span class="td-only-m">${esc(m.warnShort)}</span>` +
          `<span class="td-only-d">${esc(m.warn)}</span></div>`
        : "";

      let act;
      if (state.sent[m.id]) {
        act = `<div class="td-sent"><span class="td-only-m">SENT · WAITING</span>` +
          `<span class="td-only-d">REQUEST SENT · WAITING</span></div>`;
      } else {
        // Selling needs a still-in ticket of your own to hand over.
        const sell = canSell
          ? `<button type="button" class="td-sell${m.bid === bestBid ? " best" : ""}" ` +
            `data-act="sell" data-id="${esc(m.id)}" ` +
            `title="Sell one of your tickets to ${esc(m.name)} at $${m.bid}">S ${m.bid}</button>`
          : `<button type="button" class="td-sell" disabled ` +
            `title="You need a still-in ticket to sell">S ${m.bid}</button>`;
        act = `<div class="td-act">${sell}` +
          `<button type="button" class="td-buy${m.ask === lowAsk ? " best" : ""}" ` +
          `data-act="buy" data-id="${esc(m.id)}" ` +
          `title="Buy a ticket from ${esc(m.name)} at $${m.ask}">B ${m.ask}</button></div>`;
      }

      return `<div class="td-row"><div class="td-row-main"><div class="td-row-top">` +
        `<span class="td-name"><span class="td-only-m">${esc(first(m.name))}</span>` +
        `<span class="td-only-d">${esc(m.name)}</span></span>` +
        `<div class="td-chips">${chips}</div></div>${warn}</div>${act}</div>`;
    }).join("") || `<div class="td-row"><span class="td-sub">No markets are open.</span></div>`;
  }

  // ---------------------------------------------------------------- feed
  function renderFeed() {
    const items = [];

    if (state.req === "pending" && demo.incoming) {
      const inc = demo.incoming;
      items.push(
        `<div class="td-f req"><div class="td-f-text">` +
        `<span class="td-only-m">${esc(first(inc.from))} wants #${inc.ticket} from You at ${inc.price}</span>` +
        `<span class="td-only-d">${esc(inc.from)} wants to BUY #${inc.ticket} from You at $${inc.price}</span>` +
        `</div><button type="button" class="td-review" data-act="review">REVIEW</button></div>`
      );
    }

    Object.values(state.sent).forEach((v) => {
      items.push(
        `<div class="td-f sent"><span class="td-only-m">${esc(v.short)}</span>` +
        `<span class="td-only-d">${esc(v.full)}</span></div>`
      );
    });

    trades().forEach((x, i) => {
      const seller = x.s === "You" ? "You" : first(x.s);
      items.push(
        `<div class="td-f trade" style="opacity:${i < 3 ? 1 : 0.55}">` +
        `<span class="td-only-m">${esc(first(x.b))} BOUGHT #${x.t} from ${esc(seller)} at ${x.p}</span>` +
        `<span class="td-only-d">${esc(x.b)} BOUGHT #${x.t} from ${esc(x.s)} at $${x.p}</span></div>`
      );
    });

    $("tdFeed").innerHTML = items.join("");
  }

  // ---------------------------------------------------------------- your market
  function renderMarket() {
    const live = myStillIn();
    const empty = live.length === 0;
    const { bid, ask } = prices();
    const open = state.open && !empty;
    const show = (v) => (open ? v : "--");

    const offered = offeredTickets();
    const chips = live.map((n) => {
      const on = offered.includes(n);
      return `<button type="button" class="td-offer${on ? " on" : ""}" ` +
        `data-act="offer" data-n="${n}" aria-pressed="${on}">` +
        `${on ? "✓ #" : "#"}${n}</button>`;
    }).join("");

    const fill = empty
      ? ""
      : `<div class="td-fill" style="left:${bid}%;right:${100 - ask}%"></div>` +
        `<div class="td-knob lo" style="left:${bid}%"></div>` +
        `<div class="td-knob hi" style="right:${100 - ask}%"></div>`;

    const el = $("tdMarket");
    el.classList.toggle("empty", empty);
    el.innerHTML =
      `<div class="td-m-title"><span class="td-m-k">YOUR MARKET</span>` +
      `<button type="button" class="td-open${open ? " on" : ""}" data-act="toggle-open" ` +
      `aria-pressed="${open}">${open ? "● OPEN" : "○ CLOSED"}</button></div>` +

      `<div class="td-m-price">` +
        `<div class="td-bidask"><div class="td-k">BID</div>` +
          `<div class="td-bid">${show(bid)}</div></div>` +
        `<div class="td-slider"><div class="td-track">${fill}</div>` +
          `<div class="td-spread">SPREAD: ${show(ask - bid)}</div></div>` +
        `<div class="td-bidask"><div class="td-k">ASK</div>` +
          `<div class="td-ask">${show(ask)}</div></div>` +
      `</div>` +

      `<div class="td-m-ctl">` +
        `<div class="td-step">` +
          `<button type="button" data-act="dec" aria-label="Lower your mid price">−</button>` +
          `<span class="td-mid">${state.mid}</span>` +
          `<button type="button" data-act="inc" aria-label="Raise your mid price">+</button>` +
        `</div>` +
        `<div class="td-spreads">` +
          [1, 2, 3].map((k) =>
            `<button type="button" class="${state.k === k ? "on" : ""}" ` +
            `data-act="spread" data-k="${k}" aria-pressed="${state.k === k}">±${k}</button>`
          ).join("") +
        `</div>` +
      `</div>` +

      `<div class="td-m-offer"><span class="td-offer-k">OFFERING</span>${chips}</div>` +

      `<div class="td-note"><b>You need a still-in ticket to make a market.</b>` +
      `<span>${(session.tickets || []).length === 1 ? "Your only ticket is" : "All your tickets are"} ` +
      `out of the draw. You can still watch the order book and the feed.</span></div>`;
  }

  // ---------------------------------------------------------------- modal
  function modalView() {
    const m = demo.markets.find((x) => x.id === state.targetId);

    if ((state.modal === "buy" || state.modal === "sell") && m) {
      const buy = state.modal === "buy";
      const f = first(m.name);
      const price = buy ? m.ask : m.bid;
      const pool = buy ? m.tickets.filter((n) => !(m.out || []).includes(n)) : myStillIn();
      return {
        kind: buy ? "buy" : "sell",
        kicker: buy ? `BUY FROM ${f.toUpperCase()} · THEIR ASK` : `SELL TO ${f.toUpperCase()} · THEIR BID`,
        title: buy ? `Buy a ticket from ${m.name}` : `Sell a ticket to ${m.name}`,
        sub: buy
          ? `Pick which of ${f}'s tickets you want.`
          : `${f} buys any still-in ticket. Pick one of yours.`,
        pickLabel: buy ? "THEIR TICKETS" : "YOUR STILL-IN TICKETS",
        choices: pool,
        priceLabel: buy ? "You pay" : "You get",
        price,
        fine: `${f} has to approve before it's final. Payment is settled in person.`,
        secondary: "Cancel",
        primary: state.pick ? `SEND REQUEST · $${price}` : "PICK A TICKET",
        ready: state.pick !== null,
        onPrimary() {
          const v = buy
            ? {
                short: `You → ${f}: buy #${state.pick} at ${price}. Waiting…`,
                full: `You asked ${m.name} to sell #${state.pick} at $${price}. Waiting for approval…`,
              }
            : {
                short: `You → ${f}: sell #${state.pick} at ${price}. Waiting…`,
                full: `You offered ${m.name} #${state.pick} at $${price}. Waiting for approval…`,
              };
          state.sent[m.id] = v;
          state.modal = null;
          render();
          showToast(`Request sent. ${f} needs to approve.`);
        },
      };
    }

    if (state.modal === "approve" && demo.incoming) {
      const inc = demo.incoming;
      const f = first(inc.from);
      return {
        kind: "approve",
        kicker: "NEEDS YOUR OK",
        title: `Approve ${inc.from}?`,
        sub: `${f} wants to buy one of your tickets at your ask.`,
        pickLabel: "TICKET",
        choices: [inc.ticket],
        fixed: true,
        priceLabel: "You get",
        price: inc.price,
        fine: `Once approved, #${inc.ticket} moves to ${f} and the trade is posted to ` +
          `the feed. Collect the $${inc.price} in person.`,
        secondary: "Decline",
        primary: `APPROVE SALE · $${inc.price}`,
        ready: true,
        onSecondary() {
          state.req = "declined";
          state.modal = null;
          render();
          showToast(`Declined. ${f} has been told.`);
        },
        onPrimary() {
          state.req = "approved";
          state.modal = null;
          render();
          showToast(`Sold #${inc.ticket} to ${inc.from} for $${inc.price}.`);
        },
      };
    }
    return null;
  }

  let currentModal = null;
  let modalWasOpen = false;

  function renderModal() {
    const scrim = $("tdScrim");
    currentModal = modalView();
    if (!currentModal) {
      scrim.hidden = true;
      scrim.innerHTML = "";
      modalWasOpen = false;
      return;
    }
    const v = currentModal;

    // Identify the focused control so it can be found again after the rebuild.
    const active = modalWasOpen ? document.activeElement : null;
    const keep = active && scrim.contains(active) && active.dataset.act
      ? `[data-act="${active.dataset.act}"]` +
        (active.dataset.n ? `[data-n="${active.dataset.n}"]` : "")
      : null;

    const choices = v.choices.map((n) => {
      if (v.fixed) return `<span class="td-choice fixed gold">#${n}</span>`;
      const on = state.pick === n;
      return `<button type="button" class="td-choice${on ? " on" : ""}" ` +
        `data-act="choice" data-n="${n}" aria-pressed="${on}">#${n}</button>`;
    }).join("") || `<span class="td-sub">Nothing available.</span>`;

    scrim.hidden = false;
    scrim.innerHTML =
      `<div class="td-sheet" role="dialog" aria-modal="true" tabindex="-1" aria-label="${esc(v.title)}">` +
      `<div class="td-grab"></div>` +
      `<div class="td-m-kicker td-accent-${v.kind}">${esc(v.kicker)}</div>` +
      `<div class="td-m-h">${esc(v.title)}</div>` +
      `<div class="td-m-sub">${esc(v.sub)}</div>` +
      `<div class="td-m-pick">${esc(v.pickLabel)}</div>` +
      `<div class="td-choices">${choices}</div>` +
      `<div class="td-amount-row"><span class="td-amount-lbl">${esc(v.priceLabel)}</span>` +
      `<span class="td-amount td-accent-${v.kind}">$${v.price}</span></div>` +
      `<div class="td-m-fine">${esc(v.fine)}</div>` +
      `<div class="td-m-acts">` +
      `<button type="button" class="td-m-cancel" data-act="modal-secondary">${esc(v.secondary)}</button>` +
      `<button type="button" class="td-m-go ${v.ready ? "td-go-" + v.kind : "off"}" ` +
      `data-act="modal-primary"${v.ready ? "" : " disabled"}>${esc(v.primary)}</button>` +
      `</div></div>`;

    // Rebuilding the sheet destroys whatever had focus, so put it back on the
    // same control. On the way open there is nothing to restore: focus the
    // sheet itself rather than the first ticket, because a ring on a choice
    // reads as "already picked" while the primary button is still disabled.
    const again = keep && scrim.querySelector(keep);
    (again || scrim.querySelector(".td-sheet")).focus();
    modalWasOpen = true;
  }

  // ---------------------------------------------------------------- render
  function render() {
    renderHead();
    renderStats();
    renderBook();
    renderFeed();
    renderMarket();
    renderModal();
  }

  // ---------------------------------------------------------------- actions
  const actions = {
    "toggle-open": () => { state.open = !state.open; },
    dec: () => { state.mid = Math.max(state.k + 1, state.mid - 1); },
    inc: () => { state.mid = Math.min(100 - state.k, state.mid + 1); },
    spread: (el) => {
      state.k = Number(el.dataset.k);
      // Keep the quote inside 0–100 after a wider spread.
      state.mid = Math.min(100 - state.k, Math.max(state.k + 1, state.mid));
    },
    offer: (el) => {
      const n = Number(el.dataset.n);
      const current = offeredTickets();
      const next = current.includes(n) ? current.filter((x) => x !== n) : current.concat(n);
      // Refuse to empty the list: an open market must offer something.
      if (!next.length) {
        showToast("Keep at least one ticket on offer, or close your market.");
        return;
      }
      state.offered = next;
    },
    sell: (el) => { state.modal = "sell"; state.targetId = el.dataset.id; state.pick = null; },
    buy: (el) => { state.modal = "buy"; state.targetId = el.dataset.id; state.pick = null; },
    review: () => { state.modal = "approve"; },
    choice: (el) => { state.pick = Number(el.dataset.n); },
    "modal-primary": () => {
      if (currentModal && currentModal.ready && currentModal.onPrimary) {
        currentModal.onPrimary();
        return "handled";
      }
    },
    "modal-secondary": () => {
      if (currentModal && currentModal.onSecondary) {
        currentModal.onSecondary();
        return "handled";
      }
      state.modal = null;
    },
  };

  $("tdApp").addEventListener("click", (event) => {
    const el = event.target.closest("[data-act]");
    if (el && !el.disabled) {
      const fn = actions[el.dataset.act];
      if (fn) {
        // "handled" means the action already re-rendered after async-ish work.
        if (fn(el) !== "handled") render();
        return;
      }
    }
    // A click on the backdrop itself dismisses the sheet.
    if (event.target === $("tdScrim")) {
      state.modal = null;
      render();
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.modal) {
      state.modal = null;
      render();
    }
  });

  $("tdSignout").addEventListener("click", async () => {
    await RD.api("/api/trading/logout", { method: "POST" }).catch(() => {});
    window.location.href = "/trading/login";
  });

  // ---------------------------------------------------------------- boot
  function failBoot(message) {
    $("tdBootMsg").textContent = message;
    $("tdBootLink").hidden = false;
  }

  try {
    const cfg = await RD.api("/api/config");
    RD.applyConfig(cfg);
  } catch (_) {
    // Static branding in the markup is a fine fallback.
  }

  try {
    session = await RD.api("/api/trading/session");
  } catch (_) {
    failBoot("We could not check your session. Please sign in again.");
    return;
  }

  if (!session.authenticated) {
    failBoot("Sign in as a ticket holder to open the trading dashboard.");
    return;
  }

  demo = RDMarkets.load(session);
  state.req = demo.incoming ? "pending" : "none";

  // Open mid-market near the demo book so the first view is sensible.
  const asks = demo.markets.map((m) => m.ask);
  if (asks.length) {
    state.mid = Math.min(100 - state.k, Math.max(state.k + 1, Math.round(Math.min(...asks))));
  }

  $("tdBoot").hidden = true;
  $("tdApp").hidden = false;
  render();
})();
