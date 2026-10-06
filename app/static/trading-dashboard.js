// Live marketplace dashboard for authenticated ticket holders.

(async function () {
  const $ = (id) => document.getElementById(id);
  const esc = RD.esc;
  const money = new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
  });

  let config = {
    trading_enabled: true,
    trading_poll_seconds: 15,
    trading_poll_jitter_percent: 20,
    trading_poll_max_backoff_seconds: 120,
    trading_min_price_cents: 100,
    trading_max_price_cents: 10000000,
  };
  let snapshot = null;
  let refreshPromise = null;
  let mutationInFlight = false;
  let connectionError = "";
  let pollTimer = null;
  let pollFailures = 0;
  let retryAfterSeconds = 0;
  let lastRefreshAt = 0;
  let marketEtag = "";
  const tabId = idempotencyKey();
  const leaderKey = "rd-trading-poll-leader";
  const leaderLeaseMilliseconds = 30000;
  const marketChannel = "BroadcastChannel" in globalThis
    ? new BroadcastChannel("reverse-draw-market")
    : null;

  function formatMoney(cents) {
    return cents !== null && cents !== undefined && Number.isSafeInteger(Number(cents))
      ? money.format(Number(cents) / 100)
      : "--";
  }

  function parseMoney(value) {
    const text = String(value || "").trim();
    if (!/^\d+(?:\.\d{1,2})?$/.test(text)) return null;
    const [whole, fraction = ""] = text.split(".");
    const cents = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
    return Number.isSafeInteger(cents) ? cents : null;
  }

  function activeOwnListings() {
    return (snapshot?.own_listings || []).filter((listing) =>
      listing.status === "open" || listing.status === "reserved"
    );
  }

  function listingForTicket(ticket) {
    return activeOwnListings().find((listing) => listing.ticket === Number(ticket));
  }

  function pendingOutgoingByListing() {
    return new Map(
      (snapshot?.outgoing_requests || [])
        .filter((request) => request.status === "pending")
        .map((request) => [request.listing_id, request])
    );
  }

  function marketIsOpen() {
    return Boolean(config.trading_enabled && snapshot?.draw_status === "active");
  }

  function renderHead() {
    const draw = snapshot.draw;
    const stage = draw.finished
      ? draw.rounds_total
      : Math.min(draw.rounds_done + 1, draw.rounds_total);
    $("tdStage").innerHTML =
      `STAGE ${stage} / ${draw.rounds_total}` +
      (draw.rounds_done
        ? `<span class="td-only-d"> · ROUND ${draw.rounds_done} DONE</span>`
        : "");
    $("tdWho").textContent = snapshot.name;
    const dots = Math.min(draw.rounds_total, 10);
    $("tdDots").innerHTML = Array.from(
      { length: dots },
      (_, i) => `<i class="${i < draw.rounds_done ? "on" : ""}"></i>`
    ).join("");
  }

  function renderStats() {
    const listedTickets = new Set(activeOwnListings().map((listing) => listing.ticket));
    $("tdStubs").innerHTML = (snapshot.tickets || []).map((ticket) => {
      const kind = ticket.active ? (listedTickets.has(ticket.ticket) ? "sold" : "in") : "out";
      const marker = listedTickets.has(ticket.ticket) ? "LISTED" : (ticket.active ? "IN" : "OUT");
      return `<div class="td-stub ${kind}" title="${esc(ticket.status)}">` +
        `<b>${ticket.ticket}</b><span>${marker}</span></div>`;
    }).join("") || `<span class="td-empty">No tickets assigned</span>`;

    $("tdStillIn").textContent = RD.fmt(snapshot.draw.still_in);
    $("tdStillInOf").textContent = `of ${RD.fmt(snapshot.draw.total)}`;
    $("tdBestBid").textContent = formatMoney(snapshot.best_bid_cents);
    $("tdLowAsk").textContent = formatMoney(snapshot.low_ask_cents);
    $("tdLastTrade").textContent = formatMoney(snapshot.last_trade?.price_cents);
  }

  function renderMarketState() {
    const state = $("tdMarketState");
    state.classList.toggle("error", Boolean(connectionError));
    if (!config.trading_enabled) {
      state.textContent = "Marketplace unavailable · Trading is disabled.";
    } else if (connectionError) {
      state.textContent = `Connection interrupted · ${connectionError}`;
    } else if (!marketIsOpen()) {
      state.textContent = "Marketplace closed · Trading requires an active draw.";
    } else {
      state.textContent = `Live marketplace · Updated ${RD.fmtTime(snapshot.server_time)}`;
    }
  }

  function syncListingForm() {
    const select = $("tdListTicket");
    const previous = select.value;
    const tickets = (snapshot.tickets || []).filter((ticket) => ticket.active);
    select.innerHTML = tickets.map((ticket) => {
      const listing = listingForTicket(ticket.ticket);
      const suffix = listing ? ` · Listed ${formatMoney(listing.price_cents)}` : "";
      return `<option value="${ticket.ticket}">#${ticket.ticket}${esc(suffix)}</option>`;
    }).join("");
    if (tickets.some((ticket) => String(ticket.ticket) === previous)) {
      select.value = previous;
    }
    if (select.dataset.selected !== select.value && select.value) {
      const listing = listingForTicket(select.value);
      $("tdListPrice").value = listing ? (listing.price_cents / 100).toFixed(2) : "";
    }
    select.dataset.selected = select.value;
    updateListingFormState();
  }

  function updateListingFormState() {
    const ticket = $("tdListTicket").value;
    const listing = listingForTicket(ticket);
    const disabled = mutationInFlight || !marketIsOpen() || !ticket;
    $("tdListTicket").disabled = mutationInFlight || !marketIsOpen() || !$("tdListTicket").options.length;
    $("tdListPrice").disabled = disabled;
    $("tdListSubmit").disabled = disabled;
    $("tdListSubmit").textContent = listing ? "UPDATE ASK" : "LIST TICKET";
    if (!config.trading_enabled) {
      $("tdListHint").textContent = "Trading is currently disabled by the organizer.";
    } else if (!marketIsOpen()) {
      $("tdListHint").textContent = "Listings can be changed only while the draw is active.";
    } else if (!ticket) {
      $("tdListHint").textContent = "You have no active tickets available to list.";
    } else {
      $("tdListHint").textContent = listing
        ? `Updating ticket #${ticket}; its current ask is ${formatMoney(listing.price_cents)}.`
        : `List ticket #${ticket} for a price within the configured limits.`;
    }
  }

  function renderBook() {
    const outgoing = pendingOutgoingByListing();
    const listings = snapshot.open_listings || [];
    if (!listings.length) {
      $("tdBook").innerHTML = `<div class="td-row"><span class="td-empty">No active listings.</span></div>`;
      return;
    }
    $("tdBook").innerHTML = listings.map((listing) => {
      const own = listing.seller_id === snapshot.participant_id;
      const request = outgoing.get(listing.id);
      let actions;
      if (own) {
        actions =
          `<button class="td-manage" type="button" data-action="edit" data-ticket="${listing.ticket}" ${mutationInFlight || !marketIsOpen() ? "disabled" : ""}>EDIT ${formatMoney(listing.price_cents)}</button>` +
          `<button class="td-cancel" type="button" data-action="cancel" data-id="${esc(listing.id)}" ${mutationInFlight ? "disabled" : ""}>CANCEL</button>`;
      } else if (request) {
        actions =
          `<span class="td-sent">REQUEST PENDING</span>` +
          `<button class="td-cancel" type="button" data-action="withdraw" data-id="${esc(request.id)}" ${mutationInFlight ? "disabled" : ""}>WITHDRAW</button>`;
      } else {
        actions =
          `<span class="td-sent">TICKET #${listing.ticket}</span>` +
          `<button class="td-buy${listing.price_cents === snapshot.low_ask_cents ? " best" : ""}" type="button" data-action="request" data-id="${esc(listing.id)}" ${mutationInFlight || !marketIsOpen() ? "disabled" : ""}>BUY ${formatMoney(listing.price_cents)}</button>`;
      }
      return `<div class="td-row"><div class="td-row-main"><div class="td-row-top">` +
        `<span class="td-name">${esc(own ? "Your listing" : listing.seller_name)}</span>` +
        `<span class="td-chips"><span class="td-chip in">#${listing.ticket}</span>` +
        `<span class="td-chip">${formatMoney(listing.price_cents)}</span></span>` +
        `</div></div><div class="td-act">${actions}</div></div>`;
    }).join("");
  }

  function renderFeed() {
    const cards = [];
    (snapshot.incoming_requests || [])
      .filter((request) => request.status === "pending")
      .forEach((request) => cards.push(
        `<article class="td-f req"><div class="td-f-text">${esc(request.buyer_name)} wants ticket #${request.ticket} for ${formatMoney(request.offered_price_cents)}<small>Expires ${esc(RD.fmtTime(request.expires_at))}</small></div>` +
        `<div class="td-request-actions"><button class="td-review" type="button" data-action="approve" data-id="${esc(request.id)}" ${mutationInFlight || !marketIsOpen() ? "disabled" : ""}>APPROVE</button>` +
        `<button class="td-decline" type="button" data-action="decline" data-id="${esc(request.id)}" ${mutationInFlight ? "disabled" : ""}>DECLINE</button></div></article>`
      ));
    (snapshot.outgoing_requests || [])
      .filter((request) => request.status === "pending")
      .forEach((request) => cards.push(
        `<article class="td-f sent">Request sent to ${esc(request.seller_name)} for ticket #${request.ticket} at ${formatMoney(request.offered_price_cents)}.` +
        `<button class="td-withdraw" type="button" data-action="withdraw" data-id="${esc(request.id)}" ${mutationInFlight ? "disabled" : ""}>Withdraw request</button></article>`
      ));
    (snapshot.feed || []).forEach((trade) => cards.push(
      `<article class="td-f trade">Ticket #${trade.ticket} transferred from ${esc(trade.seller_name)} to ${esc(trade.buyer_name)} for ${formatMoney(trade.price_cents)}.` +
      `<small>${esc(RD.fmtTime(trade.executed_at))}</small></article>`
    ));
    $("tdFeed").innerHTML = cards.join("") || `<span class="td-empty">No requests or settled trades yet.</span>`;
  }

  function renderAll() {
    if (!snapshot) return;
    renderHead();
    renderStats();
    renderMarketState();
    syncListingForm();
    renderBook();
    renderFeed();
  }

  function redirectToLogin() {
    window.location.href = "/trading/login";
  }

  async function refreshMarket({ quiet = false } = {}) {
    if (!config.trading_enabled) return snapshot;
    if (refreshPromise) return refreshPromise;
    const headers = marketEtag ? { "If-None-Match": marketEtag } : {};
    refreshPromise = RD.api("/api/trading/market", { headers })
      .then((data) => {
        if (!data.__not_modified__) {
          snapshot = data;
          marketEtag = data.__etag || "";
          marketChannel?.postMessage({ type: "snapshot", snapshot, etag: marketEtag });
        }
        connectionError = "";
        pollFailures = 0;
        retryAfterSeconds = 0;
        lastRefreshAt = Date.now();
        renderAll();
        return snapshot;
      })
      .catch((error) => {
        if (error.status === 401) {
          redirectToLogin();
          return null;
        }
        pollFailures += 1;
        retryAfterSeconds = error.retryAfter || 0;
        connectionError = error.message || "Unable to refresh live data.";
        renderMarketState();
        if (!quiet) RD.toast(connectionError, true);
        throw error;
      })
      .finally(() => { refreshPromise = null; });
    return refreshPromise;
  }

  async function mutate(action, successMessage) {
    if (mutationInFlight) return false;
    mutationInFlight = true;
    renderAll();
    try {
      await action();
      await refreshMarket({ quiet: true });
      RD.toast(successMessage);
      marketChannel?.postMessage({ type: "invalidate" });
      return true;
    } catch (error) {
      if (error.status === 401) {
        redirectToLogin();
        return false;
      }
      if (error.status === 409) {
        await refreshMarket({ quiet: true }).catch(() => {});
        RD.toast(`${error.message} Live data has been refreshed.`, true);
      } else if (error.status === 503) {
        RD.toast(`Marketplace unavailable: ${error.message}`, true);
      } else {
        RD.toast(error.message || "The marketplace action failed.", true);
      }
      return false;
    } finally {
      mutationInFlight = false;
      renderAll();
    }
  }

  function idempotencyKey() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }

  function purchaseKey(listingId) {
    const key = `rd-purchase-${listingId}`;
    let value = null;
    try { value = sessionStorage.getItem(key); } catch (_) {}
    if (!value) {
      value = idempotencyKey();
      try { sessionStorage.setItem(key, value); } catch (_) {}
    }
    return { storageKey: key, value };
  }

  function clearPurchaseKey(storageKey) {
    try { sessionStorage.removeItem(storageKey); } catch (_) {}
  }

  $("tdListingForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const ticket = Number($("tdListTicket").value);
    const priceCents = parseMoney($("tdListPrice").value);
    if (!ticket || priceCents === null) {
      RD.toast("Enter a valid dollar amount with no more than two decimal places.", true);
      return;
    }
    if (priceCents < config.trading_min_price_cents || priceCents > config.trading_max_price_cents) {
      RD.toast(`Price must be between ${formatMoney(config.trading_min_price_cents)} and ${formatMoney(config.trading_max_price_cents)}.`, true);
      return;
    }
    const listing = listingForTicket(ticket);
    await mutate(
      () => RD.api("/api/trading/listings", {
        method: "POST",
        body: {
          ticket,
          price_cents: priceCents,
          expected_listing_version: listing?.version ?? null,
        },
      }),
      listing ? `Ticket #${ticket} ask updated.` : `Ticket #${ticket} listed.`
    );
  });

  $("tdListTicket").addEventListener("change", () => {
    const listing = listingForTicket($("tdListTicket").value);
    $("tdListPrice").value = listing ? (listing.price_cents / 100).toFixed(2) : "";
    $("tdListTicket").dataset.selected = $("tdListTicket").value;
    updateListingFormState();
  });

  async function handleAction(button) {
    const action = button.dataset.action;
    const id = button.dataset.id;
    if (action === "edit") {
      $("tdListTicket").value = button.dataset.ticket;
      $("tdListTicket").dispatchEvent(new Event("change"));
      $("tdListPrice").focus();
      $("tdListingForm").scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    if (action === "request") {
      const listing = (snapshot.open_listings || []).find((row) => row.id === id);
      if (!listing) return refreshMarket();
      const confirmed = await RD.confirm({
        title: `Request ticket #${listing.ticket}?`,
        body: `Send a purchase request for ${formatMoney(listing.price_cents)}. The seller must approve before the ticket transfers.`,
        confirmText: "Send request",
      });
      if (confirmed) {
        const requestKey = purchaseKey(id);
        const succeeded = await mutate(
          () => RD.api(`/api/trading/listings/${id}/requests`, {
          method: "POST",
            body: { idempotency_key: requestKey.value },
          }),
          "Purchase request sent."
        );
        if (succeeded) clearPurchaseKey(requestKey.storageKey);
      }
      return;
    }
    if (action === "cancel") {
      const listing = activeOwnListings().find((row) => row.id === id);
      if (!listing) return refreshMarket();
      const confirmed = await RD.confirm({
        title: `Cancel listing for ticket #${listing.ticket}?`,
        body: "Pending purchase requests for this listing will be closed.",
        confirmText: "Cancel listing",
        danger: true,
      });
      if (confirmed) await mutate(
        () => RD.api(`/api/trading/listings/${id}`, {
          method: "DELETE",
          body: {
            expected_listing_version: listing.version,
          },
        }),
        `Listing for ticket #${listing.ticket} canceled.`
      );
      return;
    }
    const request = [...(snapshot.incoming_requests || []), ...(snapshot.outgoing_requests || [])]
      .find((row) => row.id === id);
    if (!request) return refreshMarket();
    if (action === "approve") {
      const confirmed = await RD.confirm({
        title: `Transfer ticket #${request.ticket}?`,
        body: `Approving settles the ${formatMoney(request.offered_price_cents)} trade and transfers ownership to ${request.buyer_name}.`,
        confirmText: "Approve transfer",
        danger: true,
      });
      if (confirmed) await mutate(
        () => RD.api(`/api/trading/requests/${id}/approve`, {
          method: "POST",
          body: {},
        }),
        `Ticket #${request.ticket} transferred to ${request.buyer_name}.`
      );
    } else if (action === "decline") {
      const confirmed = await RD.confirm({
        title: "Decline purchase request?",
        body: `Decline ${request.buyer_name}'s request for ticket #${request.ticket}.`,
        confirmText: "Decline",
        danger: true,
      });
      if (confirmed) await mutate(
        () => RD.api(`/api/trading/requests/${id}/decline`, { method: "POST" }),
        "Purchase request declined."
      );
    } else if (action === "withdraw") {
      await mutate(
        () => RD.api(`/api/trading/requests/${id}/withdraw`, { method: "POST" }),
        "Purchase request withdrawn."
      );
    }
  }

  [$("tdBook"), $("tdFeed")].forEach((container) => {
    container.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-action]");
      if (button && !button.disabled) handleAction(button).catch((error) => {
        RD.toast(error.message || "The marketplace action failed.", true);
      });
    });
  });

  $("tdSignout").addEventListener("click", async () => {
    if (pollTimer) clearTimeout(pollTimer);
    marketChannel?.close();
    await RD.api("/api/trading/logout", { method: "POST" }).catch(() => {});
    redirectToLogin();
  });

  function failBoot(message) {
    $("tdBootMsg").textContent = message;
    $("tdBootLink").hidden = false;
  }

  try {
    config = { ...config, ...(await RD.api("/api/config")) };
    RD.applyConfig(config);
  } catch (_) {
    // Static branding and conservative marketplace defaults remain usable.
  }

  try {
    snapshot = await RD.api("/api/trading/session");
  } catch (error) {
    failBoot(error.status === 401
      ? "Sign in as a ticket holder to open the trading dashboard."
      : "We could not check your session. Please sign in again.");
    return;
  }

  if (!snapshot.authenticated) {
    failBoot("Sign in as a ticket holder to open the trading dashboard.");
    return;
  }

  $("tdBoot").hidden = true;
  $("tdApp").hidden = false;
  renderAll();

  if (config.trading_enabled) {
    await refreshMarket().catch(() => {});

    function basePollMilliseconds() {
      return Math.max(1, Number(config.trading_poll_seconds) || 15) * 1000;
    }

    function jitter(milliseconds) {
      const percent = Math.max(0, Math.min(50, Number(config.trading_poll_jitter_percent) || 0));
      return milliseconds * (1 + ((Math.random() * 2) - 1) * percent / 100);
    }

    function ownsPollLease() {
      const now = Date.now();
      let lease = null;
      try { lease = JSON.parse(localStorage.getItem(leaderKey) || "null"); } catch (_) {}
      if (!lease || lease.expiresAt <= now || lease.tabId === tabId) {
        try {
          localStorage.setItem(leaderKey, JSON.stringify({
            tabId,
            expiresAt: now + leaderLeaseMilliseconds,
          }));
          return true;
        } catch (_) {
          return true;
        }
      }
      return false;
    }

    function nextPollDelay() {
      const base = basePollMilliseconds();
      const maximum = Math.max(base, Number(config.trading_poll_max_backoff_seconds || 120) * 1000);
      const backoff = pollFailures ? Math.min(maximum, base * (2 ** pollFailures)) : base;
      return jitter(Math.max(backoff, retryAfterSeconds * 1000));
    }

    function schedulePoll(delay = nextPollDelay()) {
      if (pollTimer) clearTimeout(pollTimer);
      pollTimer = setTimeout(async () => {
        if (ownsPollLease() && !document.hidden && navigator.onLine && !mutationInFlight) {
          await refreshMarket({ quiet: true }).catch(() => {});
        }
        schedulePoll();
      }, Math.max(1000, delay));
    }

    function refreshIfStale() {
      if (!ownsPollLease() || Date.now() - lastRefreshAt < 2000) return;
      refreshMarket({ quiet: true }).catch(() => {});
    }

    marketChannel?.addEventListener("message", (event) => {
      if (event.data?.type === "snapshot" && !ownsPollLease()) {
        snapshot = event.data.snapshot;
        marketEtag = event.data.etag || "";
        connectionError = "";
        lastRefreshAt = Date.now();
        renderAll();
      } else if (event.data?.type === "invalidate" && ownsPollLease()) {
        refreshIfStale();
      }
    });

    schedulePoll();
    window.addEventListener("focus", refreshIfStale);
    window.addEventListener("online", refreshIfStale);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshIfStale();
    });
  }
})();