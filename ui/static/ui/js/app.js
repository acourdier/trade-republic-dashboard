/* Sync flow: queue a sync, then poll its status. When Trade Republic needs a
   fresh login, the dialog asks the user to approve it in the TR app and
   counts down TR's 120 s window. */
(function () {
  "use strict";
  const dialog = document.getElementById("sync-dialog");
  if (!dialog) return;
  const $ = (sel) => dialog.querySelector(sel);
  const csrf = () => (document.cookie.match(/(?:^|; )csrftoken=([^;]+)/) || [])[1] || "";
  const APPROVAL_SEC = 120;
  let timer = null;
  let approvalStarted = null;

  function show(state, { title, text, stage } = {}) {
    dialog.dataset.state = state;
    $(".s-title").textContent = title || "";
    $(".s-text").textContent = text || "";
    $(".s-stage").textContent = stage || "";
    $(".countdown").hidden = state !== "approve";
    $(".progress").hidden = state !== "approve";
    $(".s-retry").hidden = state !== "failed";
    $(".s-close").textContent = state === "running" || state === "approve" ? "Hide" : "Close";
    if (!dialog.open) dialog.showModal();
  }

  function tickCountdown() {
    const left = Math.max(0, APPROVAL_SEC - Math.floor((Date.now() - approvalStarted) / 1000));
    $(".countdown").textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
    $(".progress > i").style.width = `${((APPROVAL_SEC - left) / APPROVAL_SEC) * 100}%`;
  }

  function render(run) {
    if (run.status === "awaiting_approval") {
      approvalStarted = approvalStarted || Date.now();
      tickCountdown();
      show("approve", {
        title: "Approve in your Trade Republic app",
        text: "Trade Republic sent a login request to your phone. Open the app and tap approve.",
      });
      return false;
    }
    approvalStarted = null;
    if (run.status === "done") {
      show("done", { title: "Up to date", text: summary(run.counts) });
      setTimeout(() => window.location.reload(), 900);
      return true;
    }
    if (run.status === "failed") {
      show("failed", { title: "Sync didn't finish", text: run.message });
      return true;
    }
    show("running", { title: "Syncing", text: "Reading your account from Trade Republic.", stage: run.stage });
    return false;
  }

  function summary(c) {
    if (!c) return "";
    const parts = [];
    if (c.positions != null) parts.push(`${c.positions} positions`);
    if (c.stored != null) parts.push(`${c.stored} events`);
    if (c.indexed != null) parts.push(`${c.indexed} documents`);
    return parts.join(" · ");
  }

  async function poll(id) {
    clearTimeout(timer);
    try {
      const r = await fetch(`/sync/${id}/`, { headers: { Accept: "application/json" } });
      const run = await r.json();
      if (r.status === 401) { show("failed", { title: "Signed out", text: run.message }); return; }
      if (!render(run)) timer = setTimeout(() => poll(id), 1000);
    } catch (e) {
      timer = setTimeout(() => poll(id), 3000);
    }
  }

  async function start(mode) {
    show("running", { title: "Syncing", text: "Starting…" });
    const body = new URLSearchParams({ mode });
    const r = await fetch("/sync/", {
      method: "POST",
      headers: { "X-CSRFToken": csrf(), Accept: "application/json" },
      body,
    });
    const run = await r.json();
    if (!r.ok) { show("failed", { title: "Sync didn't start", text: run.message }); return; }
    dialog.dataset.run = run.id;
    poll(run.id);
  }

  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-sync]");
    if (b) { e.preventDefault(); start(b.dataset.sync || "incremental"); }
  });
  $(".s-retry").addEventListener("click", () => start("incremental"));
  $(".s-close").addEventListener("click", () => dialog.close());
  setInterval(() => { if (dialog.dataset.state === "approve") tickCountdown(); }, 1000);

  // Resume an in-flight run after a page load.
  if (dialog.dataset.active) poll(dialog.dataset.active);
})();
