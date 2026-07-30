/* DramaKorean runtime patch channel — fetched fresh on every load, cache-busted.
 *
 * Why this file exists. Roughly half of every paid landing runs a document the TikTok
 * in-app webview froze hours ago: 20 of 41 landings on 07-29 evening were still the
 * l13 snapshot, all with ttfb 0 and kb 0 (a document that crossed no network), and the
 * share was NOT decaying with time. Loop 17 fixed the dead line-tap, loop 19 fixed the
 * blind poster field — neither reached the frozen half, because a redeploy cannot reach
 * a document that is never re-fetched. The only escape anyone found was changing the ad's
 * landing URL, which costs a re-audit and kills delivery during prime.
 *
 * But a document can be frozen; a request it makes at run time cannot be. The frozen l13
 * snapshots still POST to dk_events over the live network — that is how we can see them
 * at all. So the page asks for this file at run time, and from here on any loop can repair
 * a snapshot it can no longer redeploy.
 *
 * Rules for anything added here, without exception:
 *   1. Additive only. The page must be completely correct with this file missing, 404ing,
 *      or failing to parse. Never move a fix OUT of index.html and into here.
 *   2. Idempotent. A current build already carries every fix below, so each patch must
 *      check before it binds — two handlers on one tap is worse than none.
 *   3. Silent. Every branch swallows its own errors; nothing here may reach the visitor.
 */
(function () {
  var V = 1;
  var fixed = [];

  function host() {
    try { return (typeof DK_BUILD !== "undefined" && DK_BUILD) || "?"; } catch (e) { return "?"; }
  }
  function say(event, meta) {
    try { if (typeof track === "function") track(event, meta); } catch (e) {}
  }

  /* Repair 1 — the poster is the play surface (loop 17).
     Visitors tap the art, not the small button: on 07-29 four of four taps landed on the
     cover or a script line, never the button. Builds before l17 leave #cover inert, so a
     tap on the one thing people actually aim at does nothing. Bind only when nothing is
     bound — a current build has the inline onclick and must not get a second handler. */
  try {
    var c = document.getElementById("cover");
    if (c && !c.getAttribute("onclick") && !c.__dkbound && typeof startPlay === "function") {
      c.__dkbound = 1;
      c.addEventListener("click", function () { try { startPlay(); } catch (e) {} });
      fixed.push("cover");
    }
  } catch (e) {}

  /* Reports on every load, patched or not: which builds are still being served, and
     whether a frozen document can in fact pull this file. `fix` empty on a current
     build is the expected, healthy reading. */
  say("boot", { v: V, b: host(), fix: fixed.join(",") });
})();
