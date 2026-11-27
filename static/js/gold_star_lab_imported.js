import { createStarMascot, webglAvailable } from "/static/js/gold_star_mascot.js";

(async () => {
  const host = document.getElementById("glbCanvasHost");
  const fallback = document.getElementById("glbFallback");
  const status = document.getElementById("glbStatus");
  if (!host) return;
  const $ = (id) => document.getElementById(id);
  const setStatus = (text) => { if (status) status.textContent = text; };

  if (!webglAvailable()) {
    fallback.classList.add("active");
    setStatus("WebGL unavailable");
    return;
  }

  let mascot;
  try {
    mascot = await createStarMascot(host, {
      background: 0xf8f4ff,
      reducedMotion: window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches,
      onStatus: (s) => setStatus(s === "loading" ? "Loading model…" : s === "ready" ? "Model loaded" : s),
    });
  } catch (err) {
    fallback.classList.add("active");
    setStatus("Load failed");
    console.error("[gold-star lab] GLB load failed", err);
    return;
  }
  window.__goldStarImportedAdapter = mascot;
  $("glbReducedMotion").checked = mascot.getState().reducedMotion;

  // Access the mixer speed through playGreet's timeScale; pause by toggling
  // reduced-motion-free "frozen" speed.
  let speed = 1;
  let paused = false;
  document.querySelectorAll("[data-glb]").forEach((btn) => btn.addEventListener("click", () => {
    const what = btn.dataset.glb;
    if (what === "greet") { paused = false; mascot.setSpeed(1); mascot.playGreet(speed); }
    if (what === "slow") { speed = 0.25; mascot.playGreet(speed); }
    if (what === "normal") { speed = 1; mascot.playGreet(speed); }
    if (what === "pause") {
      paused = !paused;
      mascot.setSpeed(paused ? 0 : 1);
    }
  }));
  document.querySelectorAll("[data-glb-action]").forEach((btn) => btn.addEventListener("click", () => {
    const name = btn.dataset.glbAction;
    if (typeof mascot[name] === "function") mascot[name]();
  }));
  document.querySelectorAll("[data-glb-camera]").forEach((btn) => btn.addEventListener("click", () => {
    mascot.setCamera(btn.dataset.glbCamera);
  }));
  $("glbReducedMotion").addEventListener("change", (e) => mascot.setReducedMotion(e.target.checked));

  function refresh() {
    const s = mascot.getState();
    $("glbLoad").textContent = `${s.loadMs} ms`;
    $("glbClips").textContent = s.clips.length ? `${s.clips.join(", ")} (${s.clipDuration}s)` : "none";
    $("glbFps").textContent = s.reducedMotion && !s.playing ? "idle (on demand)" : `${s.fps} fps`;
    $("glbTris").textContent = Math.round(s.triangles).toLocaleString();
    $("glbGpu").textContent = String(s.renderer || "unknown").slice(0, 48);
    $("glbAction").textContent = s.action + (s.playing ? " ▶" : "");
  }
  refresh();
  setInterval(refresh, 1000);
  window.addEventListener("beforeunload", () => mascot.dispose());
})();
