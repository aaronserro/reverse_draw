// Demo market layer for the trading dashboard.
//
// This is the ONLY source of market data on the page, and it is entirely
// client-side: nothing here is persisted and no other holder really sees it.
// The dashboard reads it through the RDMarkets interface below, so replacing
// this file with real `/api/trading/*` calls is the whole backend migration.
//
// What IS real on the dashboard comes from /api/trading/session: the signed-in
// holder's name, their ticket numbers and each ticket's in/out state, plus the
// draw's stage and still-in counts. Everything in this file is invented.

const RDMarkets = {
  isDemo: true,
};

// Other holders' standing markets. Names are fictional on purpose: the real
// draw never exposes one holder's identity to another, so a live order book
// would need a deliberate product decision about what to reveal.
RDMarkets.MARKETS = [
  { id: "bob", name: "Bob Smith", tickets: [57, 203], bid: 30, ask: 55 },
  { id: "priya", name: "Priya Patel", tickets: [140], bid: 40, ask: 70 },
  { id: "tom", name: "Tom Reyes", tickets: [9, 318, 902], out: [902], bid: 25, ask: 58 },
  { id: "ana", name: "Ana Silva", tickets: [777], bid: 50, ask: 90 },
];

// Settled trades, newest first.
RDMarkets.TRADES = [
  { b: "Marcus Chen", s: "Lena Ortiz", t: 664, p: 48 },
  { b: "Priya Patel", s: "Chris Wong", t: 505, p: 42 },
  { b: "Dee Park", s: "Omar Aziz", t: 121, p: 38 },
  { b: "Omar Aziz", s: "Tom Reyes", t: 311, p: 35 },
];

// The pending inbound offer. Price is fixed; the ticket is chosen from the
// holder's own still-in tickets so the approve flow works on real numbers.
RDMarkets.INCOMING = { from: "Sam Lee", price: 60 };

RDMarkets.first = (name) => String(name).split(" ")[0];

/**
 * Build the demo market snapshot for a signed-in holder.
 *
 * @param {{name: string, tickets: Array<{ticket: number, active: boolean}>,
 *          draw: {rounds_done: number}}} session
 * @returns {{markets: Array, trades: Array, incoming: object|null}}
 */
RDMarkets.load = function (session) {
  const mine = new Set((session.tickets || []).map((t) => t.ticket));
  const stillIn = (session.tickets || []).filter((t) => t.active).map((t) => t.ticket);
  const roundsDone = (session.draw && session.draw.rounds_done) || 0;
  const roundName = roundsDone > 0 ? `Round ${roundsDone}` : "an earlier round";

  // Never list a ticket the signed-in holder already owns: the dashboard would
  // otherwise offer to sell them their own ticket.
  const markets = RDMarkets.MARKETS.map((m) => {
    const tickets = m.tickets.filter((n) => !mine.has(n));
    const out = (m.out || []).filter((n) => tickets.includes(n));
    const warnTicket = out[0];
    return {
      ...m,
      tickets,
      out,
      warn: warnTicket
        ? `#${warnTicket} was knocked out in ${roundName}. ` +
          `${RDMarkets.first(m.name)}'s market stays open, but that ticket can't be bought.`
        : "",
      warnShort: warnTicket ? `#${warnTicket} out in ${roundName}` : "",
    };
  }).filter((m) => m.tickets.length);

  // Prefer the holder's second still-in ticket so the board still shows an
  // untouched one next to the sold one, matching the reference layout.
  const wanted = stillIn.length > 1 ? stillIn[1] : stillIn[0];

  return {
    markets,
    trades: RDMarkets.TRADES.slice(),
    incoming: wanted === undefined
      ? null
      : { ...RDMarkets.INCOMING, ticket: wanted },
  };
};
