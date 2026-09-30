(async function () {
  try {
    const cfg = await RD.api("/api/config");
    RD.applyConfig(cfg);
  } catch (_) {
    // The page remains usable with its static fallback branding.
  }
})();
