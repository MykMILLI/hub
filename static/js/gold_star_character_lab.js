import * as THREE from "/static/vendor/three/three.module.js";

(() => {
  const host = document.getElementById("characterLabCanvasHost");
  const fallback = document.getElementById("webglFallback");
  if (!host || !fallback) {
    return;
  }

  const modeButtons = Array.from(document.querySelectorAll("[data-mode]"));
  const expressionButtons = Array.from(document.querySelectorAll("[data-expression]"));
  const performanceButtons = Array.from(document.querySelectorAll("[data-performance]"));
  const cameraButtons = Array.from(document.querySelectorAll("[data-camera]"));
  const pauseMotionBtn = document.getElementById("pauseMotionBtn");
  const motionToggle = document.getElementById("motionToggle");
  const slowMoRange = document.getElementById("slowMoRange");
  const slowMoBadge = document.getElementById("slowMoBadge");
  const reducedMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");

  const canUseWebGL = (() => {
    const canvas = document.createElement("canvas");
    return !!(canvas.getContext("webgl2") || canvas.getContext("webgl"));
  })();
  if (!canUseWebGL) {
    fallback.classList.add("active");
    return;
  }

  const viewState = {
    mode: "material",
    camera: "front",
    expression: "inviting",
    paused: false,
    slowMotion: 1,
    reducedMotionOverride: false,
  };

  let renderer;
  let scene;
  let camera;
  let running = false;
  let frameHandle = 0;
  let lastTs = 0;
  let pausedByVisibility = false;
  let activePerformance = null;
  let currentPose = { y: 0, rotY: 0, rotZ: 0, squashX: 1, squashY: 1 };
  let currentExpression = { smile: 0.6, mouthOpen: 0.5, eyelidTop: 0.2, lowerLid: 0.14, cheek: 0.45, dimple: 0.4, brow: 0.16 };

  const lights = [];
  const cleanupCallbacks = [];
  const materials = {};
  const refs = {};

  initScene();
  bindControls();
  applyViewState();
  startLoop();

  function initScene() {
    scene = new THREE.Scene();
    scene.background = new THREE.Color(0xf8f4ff);

    const width = Math.max(host.clientWidth, 320);
    const height = Math.max(host.clientHeight, 320);
    camera = new THREE.PerspectiveCamera(27, width / height, 0.1, 100);

    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: "high-performance" });
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.8));
    renderer.setSize(width, height, true);
    host.appendChild(renderer.domElement);

    materials.star = new THREE.MeshPhongMaterial({
      color: 0xffd86c,
      specular: 0x5f4c22,
      shininess: 14,
      emissive: 0x2e2208,
      emissiveIntensity: 0.04,
    });
    materials.silhouette = new THREE.MeshBasicMaterial({ color: 0x000000 });
    materials.eyeWhite = new THREE.MeshStandardMaterial({ color: 0xfffcf6, roughness: 0.33, metalness: 0.01 });
    materials.pupil = new THREE.MeshStandardMaterial({ color: 0x2d2140, roughness: 0.25, metalness: 0.05 });
    materials.eyelid = new THREE.MeshStandardMaterial({ color: 0xe0b14f, roughness: 0.55, metalness: 0.02 });
    materials.mouthLip = new THREE.MeshStandardMaterial({ color: 0x8c4050, roughness: 0.46, metalness: 0.02, side: THREE.DoubleSide });
    materials.mouthInner = new THREE.MeshStandardMaterial({ color: 0x4f1f2a, roughness: 0.62, metalness: 0.01, side: THREE.DoubleSide });
    materials.tongue = new THREE.MeshStandardMaterial({ color: 0xe18390, roughness: 0.58, metalness: 0.02, side: THREE.DoubleSide });
    materials.cheek = new THREE.MeshStandardMaterial({
      color: 0xff7d9a,
      roughness: 0.74,
      metalness: 0.01,
      transparent: true,
      opacity: 0.72,
      side: THREE.DoubleSide,
    });
    materials.swoosh = new THREE.MeshBasicMaterial({ color: 0xeed8ff, transparent: true, opacity: 0, side: THREE.DoubleSide });

    refs.starRoot = new THREE.Group();
    refs.starRoot.position.set(0, 0.1, 0);
    scene.add(refs.starRoot);

    const starGeometry = createStarBodyGeometry();
    refs.star = new THREE.Mesh(starGeometry, materials.star);
    refs.star.rotation.z = Math.PI;
    refs.starRoot.add(refs.star);

    buildFace();
    buildSwoosh();

    refs.shadow = new THREE.Mesh(
      new THREE.CircleGeometry(1.95, 64),
      new THREE.MeshBasicMaterial({ color: 0x3d2f64, transparent: true, opacity: 0.16 })
    );
    refs.shadow.rotation.x = -Math.PI / 2;
    refs.shadow.position.set(0, -2.2, 0.65);
    scene.add(refs.shadow);

    const ambient = new THREE.AmbientLight(0xfff6de, 0.34);
    lights.push(ambient);
    scene.add(ambient);

    const hemi = new THREE.HemisphereLight(0xfff7dc, 0xebe3ff, 0.78);
    hemi.position.set(0, 4.9, 0);
    lights.push(hemi);
    scene.add(hemi);

    const key = new THREE.DirectionalLight(0xfff4ce, 0.84);
    key.position.set(2.35, 3.05, 4.85);
    lights.push(key);
    scene.add(key);

    const fill = new THREE.DirectionalLight(0xf7eeff, 0.34);
    fill.position.set(-3.2, 2.1, 3.15);
    lights.push(fill);
    scene.add(fill);

    const rim = new THREE.DirectionalLight(0xfff2d8, 0.2);
    rim.position.set(0.2, 2.2, -3.1);
    lights.push(rim);
    scene.add(rim);

    const onResize = () => {
      const nextWidth = Math.max(host.clientWidth, 320);
      const nextHeight = Math.max(host.clientHeight, 320);
      camera.aspect = nextWidth / nextHeight;
      camera.updateProjectionMatrix();
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.8));
      renderer.setSize(nextWidth, nextHeight, true);
      render();
    };
    window.addEventListener("resize", onResize);
    cleanupCallbacks.push(() => window.removeEventListener("resize", onResize));

    const onVisibility = () => {
      if (document.hidden) {
        pausedByVisibility = true;
      } else {
        pausedByVisibility = false;
        lastTs = 0;
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    cleanupCallbacks.push(() => document.removeEventListener("visibilitychange", onVisibility));

    const onUnload = () => teardown();
    window.addEventListener("beforeunload", onUnload);
    cleanupCallbacks.push(() => window.removeEventListener("beforeunload", onUnload));
  }

  function createStarBodyGeometry() {
    const outerRadius = 1.55;
    const innerRadius = outerRadius * 0.5;
    const bodyDepth = 0.52;

    const alternatingPoints = [];
    for (let index = 0; index < 10; index += 1) {
      const angle = -Math.PI / 2 + (index * Math.PI) / 5;
      const radius = index % 2 === 0 ? outerRadius : innerRadius;
      alternatingPoints.push(new THREE.Vector3(Math.cos(angle) * radius, Math.sin(angle) * radius, 0));
    }

    const contourCurve = new THREE.CatmullRomCurve3(alternatingPoints, true, "catmullrom", 0.3);
    const contour = contourCurve.getPoints(320).map((point) => new THREE.Vector2(point.x, point.y));
    const shape = new THREE.Shape(contour);
    let geometry = new THREE.ExtrudeGeometry(shape, {
      depth: bodyDepth,
      steps: 1,
      bevelEnabled: true,
      bevelThickness: 0.12,
      bevelSize: 0.085,
      bevelSegments: 14,
      curveSegments: 120,
    });

    geometry.translate(0, 0, -bodyDepth / 2);
    const position = geometry.attributes.position;
    const maxRadius = outerRadius * 1.08;
    const domeLift = 0.14;

    for (let i = 0; i < position.count; i += 1) {
      const x = position.getX(i);
      const y = position.getY(i);
      let z = position.getZ(i);
      const radial = Math.sqrt(x * x + y * y);
      const radialFactor = Math.max(0, 1 - Math.pow(Math.min(1, radial / maxRadius), 1.34));
      if (z > 0) {
        z += radialFactor * domeLift;
      } else if (z < 0) {
        z -= radialFactor * domeLift;
      }
      position.setZ(i, z);
    }

    geometry.computeVertexNormals();
    geometry.center();
    return geometry;
  }

  function buildFace() {
    refs.face = new THREE.Group();
    refs.face.rotation.z = Math.PI;
    refs.face.position.z = 0.03;
    refs.star.add(refs.face);

    refs.leftEye = createEye();
    refs.rightEye = createEye();
    refs.leftEye.root.position.set(-0.38, 0.2, 0.37);
    refs.rightEye.root.position.set(0.38, 0.2, 0.37);
    refs.face.add(refs.leftEye.root, refs.rightEye.root);

    refs.leftBrow = createBrow();
    refs.rightBrow = createBrow();
    refs.leftBrow.position.set(-0.4, 0.42, 0.39);
    refs.rightBrow.position.set(0.4, 0.42, 0.39);
    refs.face.add(refs.leftBrow, refs.rightBrow);

    refs.mouth = new THREE.Group();
    refs.mouth.position.set(0, -0.12, 0.49);

    refs.mouthLip = new THREE.Mesh(new THREE.RingGeometry(0.08, 0.106, 36, 1, Math.PI * 0.11, Math.PI * 0.78), materials.mouthLip);
    refs.mouthLip.rotation.z = Math.PI;
    refs.mouthInner = new THREE.Mesh(new THREE.CircleGeometry(0.096, 28), materials.mouthInner);
    refs.mouthInner.position.set(0, -0.012, -0.008);
    refs.mouthInner.scale.set(1, 0.62, 1);
    refs.mouthTongue = new THREE.Mesh(new THREE.CircleGeometry(0.048, 24), materials.tongue);
    refs.mouthTongue.position.set(0, -0.046, -0.004);
    refs.mouthTongue.scale.set(1.05, 0.64, 1);
    refs.mouth.add(refs.mouthInner, refs.mouthTongue, refs.mouthLip);
    refs.face.add(refs.mouth);

    refs.leftCheek = new THREE.Mesh(new THREE.CircleGeometry(0.11, 32), materials.cheek);
    refs.rightCheek = new THREE.Mesh(new THREE.CircleGeometry(0.11, 32), materials.cheek);
    refs.leftCheek.position.set(-0.56, 0.03, 0.46);
    refs.rightCheek.position.set(0.56, 0.03, 0.46);
    refs.leftCheek.scale.set(1, 0.58, 1);
    refs.rightCheek.scale.set(1, 0.58, 1);
    refs.face.add(refs.leftCheek, refs.rightCheek);

    refs.leftDimple = createDimple();
    refs.rightDimple = createDimple();
    refs.leftDimple.position.set(-0.21, -0.13, 0.44);
    refs.rightDimple.position.set(0.21, -0.13, 0.44);
    refs.face.add(refs.leftDimple, refs.rightDimple);
  }

  function createEye() {
    const root = new THREE.Group();
    const eye = new THREE.Mesh(new THREE.SphereGeometry(0.16, 28, 24), materials.eyeWhite);
    eye.scale.set(1, 0.82, 0.44);
    const pupil = new THREE.Mesh(new THREE.SphereGeometry(0.08, 20, 16), materials.pupil);
    pupil.scale.set(1, 1.02, 0.48);
    pupil.position.set(0, -0.01, 0.07);
    const highlight = new THREE.Mesh(new THREE.SphereGeometry(0.017, 10, 8), new THREE.MeshBasicMaterial({ color: 0xffffff }));
    highlight.position.set(-0.02, 0.022, 0.08);

    const upperLid = new THREE.Mesh(new THREE.SphereGeometry(0.168, 24, 18, 0, Math.PI * 2, 0, Math.PI * 0.45), materials.eyelid);
    upperLid.scale.set(1, 0.82, 0.44);
    upperLid.position.set(0, 0.092, 0.006);

    const lowerLid = new THREE.Mesh(new THREE.TorusGeometry(0.105, 0.013, 10, 24, Math.PI * 0.86), materials.eyelid);
    lowerLid.rotation.z = Math.PI;
    lowerLid.rotation.x = 0.08;
    lowerLid.position.set(0, -0.09, 0.045);

    root.add(eye, pupil, highlight, upperLid, lowerLid);
    return { root, pupil, upperLid, lowerLid };
  }

  function createBrow() {
    const brow = new THREE.Mesh(new THREE.TorusGeometry(0.072, 0.012, 10, 22, Math.PI * 0.82), materials.eyelid);
    brow.rotation.z = Math.PI;
    brow.rotation.x = 0.12;
    return brow;
  }

  function createDimple() {
    const dimple = new THREE.Mesh(new THREE.TorusGeometry(0.03, 0.006, 8, 16, Math.PI * 0.9), materials.mouthLip);
    dimple.rotation.z = Math.PI;
    dimple.rotation.x = 0.2;
    return dimple;
  }

  function buildSwoosh() {
    refs.swooshGroup = new THREE.Group();
    refs.swooshGroup.visible = false;
    refs.starRoot.add(refs.swooshGroup);
    refs.swooshMeshes = [];

    const curves = [
      new THREE.CatmullRomCurve3([
        new THREE.Vector3(-1.7, 0.82, 0.25),
        new THREE.Vector3(-2.18, 0.36, -0.2),
        new THREE.Vector3(-1.7, -0.2, -0.55),
      ]),
      new THREE.CatmullRomCurve3([
        new THREE.Vector3(1.62, 0.95, 0.18),
        new THREE.Vector3(2.26, 0.46, -0.16),
        new THREE.Vector3(1.9, -0.26, -0.52),
      ]),
      new THREE.CatmullRomCurve3([
        new THREE.Vector3(0.26, 1.72, -0.2),
        new THREE.Vector3(0.9, 1.44, -0.62),
        new THREE.Vector3(1.4, 0.88, -0.84),
      ]),
    ];

    curves.forEach((curve, index) => {
      const mesh = new THREE.Mesh(new THREE.TubeGeometry(curve, 32, 0.024 - index * 0.004, 9, false), materials.swoosh.clone());
      mesh.visible = false;
      refs.swooshGroup.add(mesh);
      refs.swooshMeshes.push(mesh);
    });
  }

  function bindControls() {
    modeButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const nextMode = button.dataset.mode;
        if (!["silhouette", "material"].includes(nextMode)) {
          return;
        }
        viewState.mode = nextMode;
        if (nextMode === "silhouette") {
          viewState.camera = "front";
        }
        applyViewState();
      });
    });

    expressionButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const nextExpression = button.dataset.expression;
        if (!["friendlyIdle", "delighted", "inviting", "softSmile"].includes(nextExpression)) {
          return;
        }
        viewState.expression = nextExpression;
        activePerformance = null;
        hideSwoosh();
        applyViewState();
      });
    });

    performanceButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const action = button.dataset.performance;
        if (action === "pause") {
          viewState.paused = !viewState.paused;
          if (pauseMotionBtn) {
            pauseMotionBtn.classList.toggle("active", viewState.paused);
            pauseMotionBtn.setAttribute("aria-pressed", viewState.paused ? "true" : "false");
          }
          return;
        }
        if (action === "hello") {
          startPerformance("hello");
        } else if (action === "turn") {
          startPerformance("turn");
        }
      });
    });

    cameraButtons.forEach((button) => {
      button.addEventListener("click", () => {
        if (viewState.mode === "silhouette") {
          return;
        }
        const nextCamera = button.dataset.camera;
        if (!["front", "threeQuarter", "side"].includes(nextCamera)) {
          return;
        }
        viewState.camera = nextCamera;
        applyViewState();
      });
    });

    if (motionToggle) {
      motionToggle.checked = false;
      motionToggle.addEventListener("change", () => {
        viewState.reducedMotionOverride = !!motionToggle.checked;
      });
    }

    if (slowMoRange && slowMoBadge) {
      slowMoRange.addEventListener("input", () => {
        viewState.slowMotion = Math.max(0.2, Math.min(1, Number(slowMoRange.value) || 1));
        slowMoBadge.textContent = `${viewState.slowMotion.toFixed(2)}×`;
      });
      slowMoBadge.textContent = `${viewState.slowMotion.toFixed(2)}×`;
    }
  }

  function startPerformance(type) {
    if (viewState.mode === "silhouette") {
      return;
    }
    hideSwoosh();
    if (isReducedMotion()) {
      activePerformance = null;
      viewState.expression = type === "turn" ? "delighted" : "inviting";
      return;
    }
    activePerformance = {
      type,
      time: 0,
      duration: type === "turn" ? 1.38 : 1.62,
    };
  }

  function isReducedMotion() {
    return !!viewState.reducedMotionOverride || !!reducedMotionQuery.matches;
  }

  function getExpressionPreset(name) {
    if (name === "friendlyIdle") {
      return { smile: 0.6, mouthOpen: 0.4, eyelidTop: 0.2, lowerLid: 0.14, cheek: 0.44, dimple: 0.38, brow: 0.16 };
    }
    if (name === "delighted") {
      return { smile: 0.94, mouthOpen: 0.72, eyelidTop: 0.14, lowerLid: 0.26, cheek: 0.9, dimple: 0.8, brow: 0.22 };
    }
    if (name === "softSmile") {
      return { smile: 0.45, mouthOpen: 0.22, eyelidTop: 0.24, lowerLid: 0.1, cheek: 0.3, dimple: 0.2, brow: 0.1 };
    }
    return { smile: 0.82, mouthOpen: 0.58, eyelidTop: 0.18, lowerLid: 0.2, cheek: 0.72, dimple: 0.62, brow: 0.2 };
  }

  function startLoop() {
    if (running) {
      return;
    }
    running = true;
    lastTs = 0;
    const tick = (ts) => {
      if (!running) {
        return;
      }
      frameHandle = window.requestAnimationFrame(tick);
      if (!lastTs) {
        lastTs = ts;
      }
      const rawDelta = Math.min(0.05, (ts - lastTs) / 1000);
      lastTs = ts;
      if (pausedByVisibility || viewState.paused) {
        render();
        return;
      }
      update(rawDelta * viewState.slowMotion);
      render();
    };
    frameHandle = window.requestAnimationFrame(tick);
  }

  function update(delta) {
    const baseTarget = getExpressionPreset(viewState.expression);
    const poseTarget = { y: 0, rotY: 0, rotZ: 0, squashX: 1, squashY: 1 };
    let expressionTarget = { ...baseTarget };

    if (activePerformance) {
      activePerformance.time += delta;
      const t = Math.min(1, activePerformance.time / activePerformance.duration);
      if (activePerformance.type === "hello") {
        applyHelloPerformance(t, poseTarget, expressionTarget);
      } else if (activePerformance.type === "turn") {
        applyTurnPerformance(t, poseTarget, expressionTarget);
      }
      if (t >= 1) {
        if (activePerformance.type === "hello") {
          viewState.expression = "friendlyIdle";
        } else if (activePerformance.type === "turn") {
          viewState.expression = "delighted";
        }
        activePerformance = null;
        hideSwoosh();
      }
    } else {
      hideSwoosh();
    }

    const blend = Math.min(1, delta * 8.2);
    for (const key of Object.keys(currentExpression)) {
      currentExpression[key] += (expressionTarget[key] - currentExpression[key]) * blend;
    }
    for (const key of Object.keys(currentPose)) {
      currentPose[key] += (poseTarget[key] - currentPose[key]) * blend;
    }
    applyPose();
    applyExpression();
  }

  function applyHelloPerformance(t, poseTarget, expressionTarget) {
    if (t < 0.2) {
      const p = easeOut(t / 0.2);
      poseTarget.rotZ = -0.08 * p;
      poseTarget.y = -0.03 * p;
      blendExpression(expressionTarget, getExpressionPreset("friendlyIdle"), getExpressionPreset("inviting"), p);
      return;
    }
    if (t < 0.44) {
      const p = easeInOut((t - 0.2) / 0.24);
      poseTarget.y = -0.2 * p;
      poseTarget.squashX = 1 + 0.07 * p;
      poseTarget.squashY = 1 - 0.11 * p;
      blendExpression(expressionTarget, getExpressionPreset("inviting"), getExpressionPreset("delighted"), p * 0.7);
      return;
    }
    if (t < 0.82) {
      const p = (t - 0.44) / 0.38;
      const arc = Math.sin(p * Math.PI);
      poseTarget.y = arc * 0.34;
      poseTarget.rotZ = Math.sin(p * Math.PI) * 0.06;
      poseTarget.squashX = 1 - arc * 0.06;
      poseTarget.squashY = 1 + arc * 0.08;
      blendExpression(expressionTarget, getExpressionPreset("inviting"), getExpressionPreset("delighted"), p);
      return;
    }
    const p = easeInOut((t - 0.82) / 0.18);
    poseTarget.y = (1 - p) * 0.08;
    poseTarget.rotZ = (1 - p) * 0.02;
    blendExpression(expressionTarget, getExpressionPreset("delighted"), getExpressionPreset("friendlyIdle"), p);
  }

  function applyTurnPerformance(t, poseTarget, expressionTarget) {
    if (t < 0.22) {
      const p = easeInOut(t / 0.22);
      poseTarget.rotY = -0.34 * p;
      poseTarget.squashX = 1 + 0.06 * p;
      poseTarget.squashY = 1 - 0.08 * p;
      blendExpression(expressionTarget, getExpressionPreset("inviting"), getExpressionPreset("delighted"), p * 0.65);
      return;
    }
    if (t < 0.84) {
      const p = (t - 0.22) / 0.62;
      const eased = easeOut(p);
      poseTarget.rotY = -0.2 + eased * (Math.PI * 2.05);
      poseTarget.y = Math.sin(p * Math.PI) * 0.09;
      poseTarget.squashX = 1 - Math.sin(p * Math.PI) * 0.04;
      poseTarget.squashY = 1 + Math.sin(p * Math.PI) * 0.04;
      updateSwoosh(1 - Math.abs(0.5 - p) * 2);
      blendExpression(expressionTarget, getExpressionPreset("delighted"), getExpressionPreset("delighted"), 1);
      return;
    }
    const p = easeInOut((t - 0.84) / 0.16);
    poseTarget.rotY = (1 - p) * 0.18;
    poseTarget.y = (1 - p) * 0.03;
    updateSwoosh(1 - p);
    blendExpression(expressionTarget, getExpressionPreset("delighted"), getExpressionPreset("delighted"), 1);
  }

  function blendExpression(out, a, b, t) {
    const keys = Object.keys(out);
    keys.forEach((key) => {
      out[key] = a[key] + (b[key] - a[key]) * t;
    });
  }

  function updateSwoosh(strength) {
    if (!refs.swooshMeshes) {
      return;
    }
    refs.swooshGroup.visible = true;
    refs.swooshMeshes.forEach((mesh, index) => {
      const pulse = Math.max(0, Math.min(1, strength - index * 0.22));
      mesh.visible = pulse > 0.01;
      mesh.material.opacity = pulse * (0.5 - index * 0.12);
    });
  }

  function hideSwoosh() {
    if (!refs.swooshMeshes) {
      return;
    }
    refs.swooshGroup.visible = false;
    refs.swooshMeshes.forEach((mesh) => {
      mesh.visible = false;
      mesh.material.opacity = 0;
    });
  }

  function applyPose() {
    refs.starRoot.position.y = 0.1 + currentPose.y;
    refs.starRoot.rotation.y = currentPose.rotY;
    refs.starRoot.rotation.z = currentPose.rotZ;
    refs.starRoot.scale.set(currentPose.squashX, currentPose.squashY, 1);

    refs.shadow.scale.set(1 + currentPose.y * 0.22, 1 + currentPose.y * 0.22, 1);
    refs.shadow.material.opacity = 0.16 - Math.max(0, currentPose.y) * 0.1;
  }

  function applyExpression() {
    const expression = currentExpression;
    const eyeBaseScale = 1;
    const eyeScaleY = eyeBaseScale - expression.eyelidTop * 0.24;
    refs.leftEye.root.scale.set(1, eyeScaleY, 1);
    refs.rightEye.root.scale.set(1, eyeScaleY, 1);

    refs.leftEye.upperLid.position.y = 0.096 - expression.eyelidTop * 0.05;
    refs.rightEye.upperLid.position.y = 0.096 - expression.eyelidTop * 0.05;
    refs.leftEye.lowerLid.position.y = -0.09 + expression.lowerLid * 0.045;
    refs.rightEye.lowerLid.position.y = -0.09 + expression.lowerLid * 0.045;

    refs.leftBrow.position.y = 0.42 + expression.brow * 0.08;
    refs.rightBrow.position.y = 0.42 + expression.brow * 0.08;
    refs.leftBrow.rotation.x = 0.12 + expression.brow * 0.2;
    refs.rightBrow.rotation.x = 0.12 + expression.brow * 0.2;

    refs.leftCheek.material.opacity = 0.4 + expression.cheek * 0.38;
    refs.rightCheek.material.opacity = 0.4 + expression.cheek * 0.38;
    refs.leftCheek.position.y = 0.03 + expression.cheek * 0.04;
    refs.rightCheek.position.y = 0.03 + expression.cheek * 0.04;

    refs.leftDimple.material.opacity = expression.dimple * 0.85;
    refs.rightDimple.material.opacity = expression.dimple * 0.85;
    refs.leftDimple.position.y = -0.13 + expression.dimple * 0.045;
    refs.rightDimple.position.y = -0.13 + expression.dimple * 0.045;

    refs.mouth.scale.set(0.86 + expression.smile * 0.24, 0.84 + expression.smile * 0.2, 1);
    refs.mouth.position.y = -0.12 + expression.smile * 0.035;
    refs.mouthLip.scale.set(1, 0.76 + expression.smile * 0.26, 1);
    refs.mouthInner.scale.y = 0.42 + expression.mouthOpen * 0.74;
    refs.mouthTongue.scale.y = 0.36 + expression.mouthOpen * 0.56;
    refs.mouthTongue.position.y = -0.053 + expression.mouthOpen * 0.016;
    refs.mouthInner.visible = expression.mouthOpen > 0.08;
    refs.mouthTongue.visible = expression.mouthOpen > 0.22;
  }

  function applyViewState() {
    if (viewState.mode === "silhouette") {
      scene.background = new THREE.Color(0xffffff);
      refs.star.material = materials.silhouette;
      refs.face.visible = false;
      refs.shadow.visible = false;
      lights.forEach((light) => {
        light.visible = false;
      });
      setCameraPose("front");
      activePerformance = null;
      hideSwoosh();
    } else {
      scene.background = new THREE.Color(0xf8f4ff);
      refs.star.material = materials.star;
      refs.face.visible = true;
      refs.shadow.visible = true;
      lights.forEach((light) => {
        light.visible = true;
      });
      setCameraPose(viewState.camera);
    }

    modeButtons.forEach((button) => {
      const active = button.dataset.mode === viewState.mode;
      button.classList.toggle("active", active);
      button.classList.toggle("primary", active);
    });

    expressionButtons.forEach((button) => {
      const active = button.dataset.expression === viewState.expression;
      button.classList.toggle("active", active);
      button.classList.toggle("primary", active);
      button.disabled = viewState.mode === "silhouette";
    });

    performanceButtons.forEach((button) => {
      const isPause = button.dataset.performance === "pause";
      button.disabled = viewState.mode === "silhouette" && !isPause;
      if (isPause) {
        button.classList.toggle("active", viewState.paused);
      }
    });

    cameraButtons.forEach((button) => {
      const active = viewState.mode === "material" && button.dataset.camera === viewState.camera;
      button.classList.toggle("active", active);
      button.disabled = viewState.mode === "silhouette";
    });

    render();
  }

  function setCameraPose(preset) {
    if (preset === "side") {
      camera.position.set(10.0, 0.12, 0.02);
    } else if (preset === "threeQuarter") {
      camera.position.set(2.55, 0.28, 9.6);
    } else {
      camera.position.set(0, 0.3, 10.0);
    }
    camera.lookAt(0, 0.15, 0);
  }

  function easeInOut(x) {
    const clamped = Math.max(0, Math.min(1, x));
    return clamped * clamped * (3 - 2 * clamped);
  }

  function easeOut(x) {
    const clamped = Math.max(0, Math.min(1, x));
    return 1 - (1 - clamped) * (1 - clamped);
  }

  function render() {
    renderer.render(scene, camera);
  }

  function teardown() {
    running = false;
    if (frameHandle) {
      window.cancelAnimationFrame(frameHandle);
      frameHandle = 0;
    }
    cleanupCallbacks.splice(0).forEach((cleanup) => cleanup());

    scene.traverse((node) => {
      if (!(node instanceof THREE.Mesh)) {
        return;
      }
      if (node.geometry) {
        node.geometry.dispose();
      }
      if (Array.isArray(node.material)) {
        node.material.forEach((material) => material.dispose());
      } else if (node.material) {
        node.material.dispose();
      }
    });

    renderer.dispose();
    if (renderer.domElement && renderer.domElement.parentElement === host) {
      host.removeChild(renderer.domElement);
    }
  }
})();
