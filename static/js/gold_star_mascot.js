// Gold Star mascot renderer: loads the approved animated GLB (star-hop-blink.glb,
// clip "greet") and exposes the Phase-2 character adapter:
//   idle, noticeNewStar, tapReaction, collecting, celebrate, rewardApproved,
//   setReducedMotion, setCamera, dispose, getState
// Accounting never depends on this module: callers keep the SVG fallback if
// WebGL or the model load fails, and animation failures are swallowed.
import * as THREE from "/static/vendor/three/three.module.js";
import { GLTFLoader } from "/static/vendor/three/addons/loaders/GLTFLoader.js";

export const MASCOT_BUILD_ID = "gold-star-mascot-2026-11-27-glb-v1";
const DEFAULT_URL = "/static/assets/gold-star/star-hop-blink.glb";

export function webglAvailable() {
  try {
    const canvas = document.createElement("canvas");
    return !!(canvas.getContext("webgl2") || canvas.getContext("webgl"));
  } catch (_) {
    return false;
  }
}

let cachedGltfPromise = null;
function loadGltf(url) {
  if (!cachedGltfPromise) {
    const loader = new GLTFLoader();
    cachedGltfPromise = new Promise((resolve, reject) => loader.load(url, resolve, undefined, reject));
    cachedGltfPromise.catch(() => { cachedGltfPromise = null; });
  }
  return cachedGltfPromise;
}

/**
 * @param {HTMLElement} host  element the canvas is appended to (sized by CSS)
 * @param {object} options    { url, reducedMotion, background, onStatus(status, detail) }
 * @returns {Promise<object>} adapter (resolves after the model is shown)
 */
export async function createStarMascot(host, options = {}) {
  const url = options.url || DEFAULT_URL;
  const onStatus = typeof options.onStatus === "function" ? options.onStatus : () => {};
  if (!host) throw new Error("No host element");
  if (!webglAvailable()) throw new Error("WebGL is not available on this device");

  const state = {
    reducedMotion: !!options.reducedMotion,
    action: "idle",
    camera: "front",
    playing: false,
    disposed: false,
    loadMs: 0,
    clipNames: [],
    clipDuration: 0,
    fps: 0,
    triangles: 0,
  };

  const scene = new THREE.Scene();
  if (options.background !== undefined && options.background !== null) {
    scene.background = new THREE.Color(options.background);
  }
  const camera = new THREE.PerspectiveCamera(28, 1, 0.05, 100);
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: options.background == null, powerPreference: "default" });
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.NoToneMapping;
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
  renderer.domElement.style.display = "block";
  renderer.domElement.style.width = "100%";
  renderer.domElement.style.height = "100%";
  renderer.domElement.setAttribute("aria-hidden", "true");
  host.appendChild(renderer.domElement);

  // Soft, even lighting: the face colour is baked into the texture, so we
  // want it readable from the front without harsh shading.
  scene.add(new THREE.HemisphereLight(0xfffaf0, 0xf1e8ff, 1.55));
  const key = new THREE.DirectionalLight(0xffffff, 1.35);
  key.position.set(1.8, 2.6, 4.2);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xfff3dd, 0.55);
  fill.position.set(-2.6, 1.2, 2.8);
  scene.add(fill);
  const rim = new THREE.DirectionalLight(0xffffff, 0.35);
  rim.position.set(0, 2, -4);
  scene.add(rim);

  // Ground shadow
  const shadow = new THREE.Mesh(
    new THREE.CircleGeometry(0.8, 48),
    new THREE.MeshBasicMaterial({ color: 0x3d2f64, transparent: true, opacity: 0.14, depthWrite: false })
  );
  shadow.rotation.x = -Math.PI / 2;
  scene.add(shadow);

  // rig: float (code-driven idle) -> pose (code-driven accents) -> model (GLB, clip-driven)
  const floatRig = new THREE.Group();
  const poseRig = new THREE.Group();
  floatRig.add(poseRig);
  scene.add(floatRig);

  onStatus("loading");
  const started = performance.now();
  let gltf;
  try {
    gltf = await loadGltf(url);
  } catch (err) {
    renderer.dispose();
    renderer.domElement.remove();
    onStatus("error", err);
    throw err;
  }
  state.loadMs = Math.round(performance.now() - started);

  const model = gltf.scene.clone(true);
  poseRig.add(model);
  model.traverse((node) => {
    if (node.isMesh) {
      node.frustumCulled = false; // morph/anim bounds would otherwise clip during the hop
      const idx = node.geometry.index;
      state.triangles += idx ? idx.count / 3 : node.geometry.attributes.position.count / 3;
      const mats = Array.isArray(node.material) ? node.material : [node.material];
      mats.forEach((m) => { if (m && m.map) m.map.anisotropy = Math.min(4, renderer.capabilities.getMaxAnisotropy()); });
    }
  });

  // Normalise size/placement from the rest pose: ~2 units tall, base on y=0.
  model.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(model);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const scale = 2 / Math.max(size.y, size.x, 0.0001);
  model.scale.multiplyScalar(scale);
  model.position.sub(center.multiplyScalar(scale));
  model.position.y += (size.y * scale) / 2;
  const modelHeight = size.y * scale;
  shadow.position.y = 0.001;

  const mixer = new THREE.AnimationMixer(model);
  const clips = gltf.animations || [];
  state.clipNames = clips.map((c) => c.name);
  const greetClip = clips.find((c) => c.name === "greet") || clips[0] || null;
  state.clipDuration = greetClip ? Number(greetClip.duration.toFixed(2)) : 0;
  const greetAction = greetClip ? mixer.clipAction(greetClip) : null;
  if (greetAction) {
    greetAction.setLoop(THREE.LoopOnce, 1);
    greetAction.clampWhenFinished = false;
  }
  mixer.addEventListener("finished", () => {
    state.playing = false;
    if (state.action !== "idle") state.action = "idle";
  });

  // ----------------------------------------------------------- camera
  function setCamera(preset) {
    state.camera = ["front", "threeQuarter", "side", "back"].includes(preset) ? preset : "front";
    const target = new THREE.Vector3(0, modelHeight * 0.52, 0);
    const dist = 5.4;
    const dirs = {
      front: new THREE.Vector3(0, 0.08, 1),
      threeQuarter: new THREE.Vector3(0.62, 0.1, 0.78),
      side: new THREE.Vector3(1, 0.06, 0.001),
      back: new THREE.Vector3(0, 0.08, -1),
    };
    const dir = dirs[state.camera].clone().normalize();
    camera.position.copy(target).addScaledVector(dir, dist);
    camera.lookAt(target);
    requestFrame();
  }

  // ----------------------------------------------------------- sizing
  function resize() {
    const w = Math.max(host.clientWidth, 120);
    const h = Math.max(host.clientHeight, 120);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h, false);
    requestFrame();
  }
  const resizeObserver = typeof ResizeObserver !== "undefined" ? new ResizeObserver(resize) : null;
  if (resizeObserver) resizeObserver.observe(host);
  else window.addEventListener("resize", resize);

  // ----------------------------------------------------------- accents
  // Small code-driven variations layered on top of the one authored clip.
  let accent = null; // { start, duration, fn(t) }
  function startAccent(duration, fn) {
    accent = { start: performance.now(), duration, fn };
    requestFrame();
  }
  function clearAccent() {
    accent = null;
    poseRig.rotation.set(0, 0, 0);
    poseRig.scale.set(1, 1, 1);
    poseRig.position.set(0, 0, 0);
  }

  function playGreet(timeScale = 1) {
    if (!greetAction || state.reducedMotion) return false;
    if (state.playing && greetAction.time < greetClip.duration * 0.6) {
      return false; // ignore rapid re-triggers instead of stacking animations
    }
    greetAction.reset();
    greetAction.timeScale = timeScale;
    greetAction.play();
    state.playing = true;
    requestFrame();
    return true;
  }

  // ----------------------------------------------------------- loop
  let lastTick = performance.now();
  let rafId = 0;
  let frames = 0;
  let fpsWindowStart = performance.now();
  let nextIdleBlinkAt = performance.now() + 6000 + Math.random() * 5000;

  function needsContinuousFrames() {
    return !state.reducedMotion || state.playing || !!accent;
  }

  function tick() {
    rafId = 0;
    if (state.disposed) return;
    const now = performance.now();
    const dt = Math.min((now - lastTick) / 1000, 0.1);
    lastTick = now;
    try {
      mixer.update(dt);
      if (!state.reducedMotion) {
        const t = now / 1000;
        floatRig.position.y = Math.sin(t * 1.6) * 0.045;
        floatRig.rotation.z = Math.sin(t * 0.9) * 0.025;
        shadow.scale.setScalar(1 - floatRig.position.y * 0.8);
        if (state.action === "idle" && !state.playing && now > nextIdleBlinkAt) {
          // occasional friendly hop-and-blink while idle
          nextIdleBlinkAt = now + 9000 + Math.random() * 7000;
          playGreet(1);
        }
      } else {
        floatRig.position.y = 0;
        floatRig.rotation.z = 0;
      }
      if (accent) {
        const p = Math.min(1, (now - accent.start) / accent.duration);
        accent.fn(p);
        if (p >= 1) clearAccent();
      }
      renderer.render(scene, camera);
    } catch (err) {
      // never let rendering break the page
      onStatus("render-error", err);
      return;
    }
    frames += 1;
    if (now - fpsWindowStart >= 1000) {
      state.fps = Math.round((frames * 1000) / (now - fpsWindowStart));
      frames = 0;
      fpsWindowStart = now;
    }
    if (needsContinuousFrames() && !document.hidden) requestFrame();
  }

  function requestFrame() {
    if (!rafId && !state.disposed) rafId = requestAnimationFrame(tick);
  }

  function onVisibility() {
    if (document.hidden) {
      if (rafId) cancelAnimationFrame(rafId);
      rafId = 0;
      clearAccent();
    } else {
      lastTick = performance.now();
      requestFrame();
    }
  }
  document.addEventListener("visibilitychange", onVisibility);

  // ----------------------------------------------------------- actions
  const ease = (p) => Math.sin(p * Math.PI);
  const actions = {
    idle() {
      state.action = "idle";
      clearAccent();
      requestFrame();
    },
    noticeNewStar() {
      state.action = "noticeNewStar";
      if (state.reducedMotion) return requestFrame();
      startAccent(900, (p) => { poseRig.rotation.y = -0.35 * ease(p); poseRig.rotation.z = 0.06 * ease(p); });
      playGreet(1);
    },
    tapReaction() {
      state.action = "tapReaction";
      if (state.reducedMotion) return requestFrame();
      startAccent(420, (p) => {
        const s = ease(p);
        poseRig.scale.set(1 + 0.07 * s, 1 - 0.08 * s, 1 + 0.07 * s);
      });
    },
    collecting() {
      state.action = "collecting";
      if (state.reducedMotion) return requestFrame();
      playGreet(1.25);
    },
    celebrate() {
      state.action = "celebrate";
      if (state.reducedMotion) return requestFrame();
      startAccent(1100, (p) => { poseRig.rotation.y = Math.PI * 2 * (1 - Math.pow(1 - p, 3)); });
      playGreet(1);
    },
    rewardApproved() {
      state.action = "rewardApproved";
      if (state.reducedMotion) return requestFrame();
      startAccent(1200, (p) => { poseRig.rotation.z = Math.sin(p * Math.PI * 4) * 0.12 * (1 - p); });
      playGreet(0.9);
    },
  };

  const adapter = {
    ...Object.fromEntries(Object.entries(actions).map(([name, fn]) => [name, () => {
      try { fn(); } catch (_) { /* animation must never block */ }
    }])),
    playGreet(timeScale = 1) {
      return playGreet(timeScale);
    },
    setReducedMotion(enabled) {
      state.reducedMotion = !!enabled;
      if (state.reducedMotion) {
        if (greetAction) greetAction.stop();
        state.playing = false;
        clearAccent();
      }
      requestFrame();
    },
    setSpeed(value) {
      mixer.timeScale = Math.max(0, Number(value) || 0);
      requestFrame();
    },
    setCamera,
    resize,
    get canvas() {
      return renderer.domElement;
    },
    dispose() {
      if (state.disposed) return;
      state.disposed = true;
      if (rafId) cancelAnimationFrame(rafId);
      document.removeEventListener("visibilitychange", onVisibility);
      if (resizeObserver) resizeObserver.disconnect();
      else window.removeEventListener("resize", resize);
      mixer.stopAllAction();
      mixer.uncacheRoot(model);
      scene.traverse((node) => {
        if (!node.isMesh) return;
        // geometry/textures are shared with the cached GLTF; only dispose ours
        if (node === shadow) {
          node.geometry.dispose();
          node.material.dispose();
        }
      });
      renderer.dispose();
      renderer.domElement.remove();
    },
    getState() {
      return {
        buildId: MASCOT_BUILD_ID,
        action: state.action,
        camera: state.camera,
        reducedMotion: state.reducedMotion,
        playing: state.playing,
        loadMs: state.loadMs,
        clips: state.clipNames.slice(),
        clipDuration: state.clipDuration,
        fps: state.fps,
        triangles: state.triangles,
        renderer: (() => {
          try {
            const gl = renderer.getContext();
            const ext = gl.getExtension("WEBGL_debug_renderer_info");
            return ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
          } catch (_) {
            return "unknown";
          }
        })(),
        memory: renderer.info ? { geometries: renderer.info.memory.geometries, textures: renderer.info.memory.textures } : null,
      };
    },
  };

  setCamera("front");
  resize();
  onStatus("ready", adapter.getState());
  if (!state.reducedMotion) playGreet(1);
  requestFrame();
  return adapter;
}
