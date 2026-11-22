import * as THREE from "/static/vendor/three/three.module.js";

(() => {
  const host = document.getElementById("characterLabCanvasHost");
  const fallback = document.getElementById("webglFallback");
  if (!host || !fallback) {
    return;
  }

  const modeButtons = Array.from(document.querySelectorAll("[data-mode]"));
  const performanceButtons = Array.from(document.querySelectorAll("[data-performance]"));
  const cameraButtons = Array.from(document.querySelectorAll("[data-camera]"));
  const pauseMotionBtn = document.getElementById("pauseMotionBtn");
  const motionToggle = document.getElementById("motionToggle");
  const slowMoRange = document.getElementById("slowMoRange");
  const slowMoBadge = document.getElementById("slowMoBadge");
  const greetingProgressRange = document.getElementById("greetingProgressRange");
  const greetingProgressBadge = document.getElementById("greetingProgressBadge");
  const reducedMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");

  const diagBuildId = document.getElementById("diagBuildId");
  const diagExpression = document.getElementById("diagExpression");
  const diagPerformance = document.getElementById("diagPerformance");
  const diagMotion = document.getElementById("diagMotion");
  const diagMouthOpen = document.getElementById("diagMouthOpen");
  const diagSmile = document.getElementById("diagSmile");
  const diagCheekLift = document.getElementById("diagCheekLift");
  const diagEyeSquint = document.getElementById("diagEyeSquint");
  const LAB_BUILD_ID = "gold-star-lab-2026-11-22-greeting-v1";

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
    paused: false,
    slowMotion: 1,
    reducedMotionOverride: false,
  };

  const performanceState = {
    progress: 0,
    playing: false,
    durationSeconds: 2.65,
    activeName: null,
  };

  const faceState = {
    current: { smile: 0.28, mouthOpen: 0.01, cheekLift: 0.2, eyeSquint: 0.08, eyeOpen: 0.74, browLift: 0.08, gazeY: 0.02 },
    target: { smile: 0.28, mouthOpen: 0.01, cheekLift: 0.2, eyeSquint: 0.08, eyeOpen: 0.74, browLift: 0.08, gazeY: 0.02 },
  };

  const poseState = {
    current: { y: 0, rotY: 0, rotZ: 0, squashX: 1, squashY: 1 },
    target: { y: 0, rotY: 0, rotZ: 0, squashX: 1, squashY: 1 },
  };

  let renderer;
  let scene;
  let camera;
  let running = false;
  let frameHandle = 0;
  let lastTs = 0;
  let pausedByVisibility = false;

  const refs = {};
  const lights = [];
  const cleanupCallbacks = [];
  const materials = {};

  initScene();
  bindControls();
  applyTimelinePose(0, true);
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
    materials.eyeWhite = new THREE.MeshStandardMaterial({ color: 0xfffcf6, roughness: 0.36, metalness: 0.01 });
    materials.iris = new THREE.MeshStandardMaterial({ color: 0x3b2c53, roughness: 0.24, metalness: 0.04 });
    materials.pupil = new THREE.MeshStandardMaterial({ color: 0x181422, roughness: 0.22, metalness: 0.04 });
    materials.eyelid = new THREE.MeshStandardMaterial({ color: 0xe4b351, roughness: 0.55, metalness: 0.02 });
    materials.brow = new THREE.MeshStandardMaterial({ color: 0xdca646, roughness: 0.52, metalness: 0.02 });
    materials.mouthLip = new THREE.MeshStandardMaterial({ color: 0x8f3f4f, roughness: 0.46, metalness: 0.02, side: THREE.DoubleSide });
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
    materials.crease = new THREE.MeshStandardMaterial({ color: 0xa04a58, roughness: 0.58, metalness: 0.02, transparent: true, opacity: 0.45 });

    refs.starRoot = new THREE.Group();
    refs.starRoot.position.set(0, 0.1, 0);
    scene.add(refs.starRoot);

    const starGeometry = createStarBodyGeometry();
    refs.star = new THREE.Mesh(starGeometry, materials.star);
    refs.star.rotation.z = Math.PI;
    refs.starRoot.add(refs.star);

    buildFace();

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
    refs.leftEye.root.position.set(-0.48, 0.28, 0.4);
    refs.rightEye.root.position.set(0.48, 0.28, 0.4);
    refs.face.add(refs.leftEye.root, refs.rightEye.root);

    refs.leftBrow = createBrow();
    refs.rightBrow = createBrow();
    refs.leftBrow.position.set(-0.5, 0.53, 0.41);
    refs.rightBrow.position.set(0.5, 0.53, 0.41);
    refs.face.add(refs.leftBrow, refs.rightBrow);

    refs.mouth = new THREE.Group();
    refs.mouth.position.set(0, -0.19, 0.5);

    refs.upperLip = new THREE.Mesh(new THREE.TorusGeometry(0.198, 0.018, 12, 32, Math.PI * 0.72), materials.mouthLip);
    refs.upperLip.rotation.z = Math.PI;
    refs.upperLip.rotation.x = 0.2;
    refs.upperLip.position.set(0, 0.034, 0.014);

    refs.lowerLip = new THREE.Mesh(new THREE.TorusGeometry(0.212, 0.017, 12, 32, Math.PI * 0.86), materials.mouthLip);
    refs.lowerLip.rotation.z = Math.PI;
    refs.lowerLip.rotation.x = -0.08;
    refs.lowerLip.position.set(0, -0.042, 0.012);

    refs.mouthInner = new THREE.Mesh(new THREE.CircleGeometry(0.16, 36), materials.mouthInner);
    refs.mouthInner.position.set(0, -0.01, -0.01);
    refs.mouthInner.scale.set(1.5, 0.52, 1);

    refs.mouthTongue = new THREE.Mesh(new THREE.CircleGeometry(0.068, 30), materials.tongue);
    refs.mouthTongue.position.set(0, -0.11, -0.004);
    refs.mouthTongue.scale.set(1.34, 0.66, 1);

    refs.leftMouthCorner = new THREE.Mesh(new THREE.SphereGeometry(0.02, 12, 10), materials.mouthLip);
    refs.rightMouthCorner = new THREE.Mesh(new THREE.SphereGeometry(0.02, 12, 10), materials.mouthLip);
    refs.leftMouthCorner.position.set(-0.2, -0.012, 0.01);
    refs.rightMouthCorner.position.set(0.2, -0.012, 0.01);

    refs.leftCheek = new THREE.Mesh(new THREE.CircleGeometry(0.14, 36), materials.cheek);
    refs.rightCheek = new THREE.Mesh(new THREE.CircleGeometry(0.14, 36), materials.cheek);
    refs.leftCheek.position.set(-0.62, 0.03, 0.46);
    refs.rightCheek.position.set(0.62, 0.03, 0.46);
    refs.leftCheek.scale.set(1.18, 0.64, 1);
    refs.rightCheek.scale.set(1.18, 0.64, 1);

    refs.leftSmileCrease = new THREE.Mesh(new THREE.TorusGeometry(0.043, 0.008, 8, 16, Math.PI * 0.72), materials.crease);
    refs.rightSmileCrease = new THREE.Mesh(new THREE.TorusGeometry(0.043, 0.008, 8, 16, Math.PI * 0.72), materials.crease);
    refs.leftSmileCrease.rotation.z = Math.PI * 1.08;
    refs.rightSmileCrease.rotation.z = Math.PI * 1.92;
    refs.leftSmileCrease.position.set(-0.31, -0.13, 0.44);
    refs.rightSmileCrease.position.set(0.31, -0.13, 0.44);

    refs.mouth.add(
      refs.mouthInner,
      refs.mouthTongue,
      refs.upperLip,
      refs.lowerLip,
      refs.leftMouthCorner,
      refs.rightMouthCorner
    );
    refs.face.add(refs.mouth, refs.leftCheek, refs.rightCheek, refs.leftSmileCrease, refs.rightSmileCrease);
  }

  function createEye() {
    const root = new THREE.Group();
    const white = new THREE.Mesh(new THREE.SphereGeometry(0.21, 32, 26), materials.eyeWhite);
    white.scale.set(1, 0.82, 0.46);

    const iris = new THREE.Mesh(new THREE.SphereGeometry(0.086, 22, 16), materials.iris);
    iris.position.set(0, -0.008, 0.084);
    iris.scale.set(1, 1.1, 0.56);

    const pupil = new THREE.Mesh(new THREE.SphereGeometry(0.044, 18, 14), materials.pupil);
    pupil.position.set(0, -0.009, 0.112);
    pupil.scale.set(1, 1.2, 0.58);

    const highlight = new THREE.Mesh(new THREE.SphereGeometry(0.02, 10, 8), new THREE.MeshBasicMaterial({ color: 0xffffff }));
    highlight.position.set(-0.024, 0.038, 0.126);

    const upperLid = new THREE.Mesh(
      new THREE.SphereGeometry(0.22, 28, 20, 0, Math.PI * 2, 0, Math.PI * 0.53),
      materials.eyelid
    );
    upperLid.scale.set(1.03, 0.84, 0.48);
    upperLid.position.set(0, 0.114, 0.007);

    const lowerLid = new THREE.Mesh(new THREE.TorusGeometry(0.141, 0.015, 10, 26, Math.PI * 0.92), materials.eyelid);
    lowerLid.rotation.z = Math.PI;
    lowerLid.rotation.x = 0.08;
    lowerLid.position.set(0, -0.12, 0.046);

    root.add(white, iris, pupil, highlight, upperLid, lowerLid);
    return { root, iris, pupil, upperLid, lowerLid };
  }

  function createBrow() {
    const brow = new THREE.Mesh(new THREE.TorusGeometry(0.09, 0.014, 10, 24, Math.PI * 0.85), materials.brow);
    brow.rotation.z = Math.PI;
    brow.rotation.x = 0.16;
    return brow;
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
          performanceState.playing = false;
          performanceState.activeName = null;
        }
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
          syncDiagnostics();
          render();
          return;
        }
        if (viewState.mode === "silhouette") {
          return;
        }
        if (action === "play") {
          if (performanceState.progress >= 0.999) {
            performanceState.progress = 0;
          }
          performanceState.playing = true;
          performanceState.activeName = "greeting";
        } else if (action === "restart") {
          performanceState.progress = 0;
          performanceState.playing = false;
          performanceState.activeName = null;
          applyTimelinePose(0, true);
        }
        syncDiagnostics();
        render();
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
        syncDiagnostics();
        render();
      });
    }

    if (slowMoRange && slowMoBadge) {
      slowMoRange.addEventListener("input", () => {
        viewState.slowMotion = Math.max(0.2, Math.min(1, Number(slowMoRange.value) || 1));
        slowMoBadge.textContent = `${viewState.slowMotion.toFixed(2)}×`;
        syncDiagnostics();
      });
      slowMoBadge.textContent = `${viewState.slowMotion.toFixed(2)}×`;
    }

    if (greetingProgressRange && greetingProgressBadge) {
      greetingProgressRange.addEventListener("input", () => {
        performanceState.progress = Math.max(0, Math.min(1, (Number(greetingProgressRange.value) || 0) / 100));
        performanceState.playing = false;
        performanceState.activeName = null;
        applyTimelinePose(performanceState.progress, true);
      });
      greetingProgressBadge.textContent = "0%";
    }
  }

  function isReducedMotion() {
    return !!viewState.reducedMotionOverride || !!reducedMotionQuery.matches;
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

      if (!pausedByVisibility && !viewState.paused && viewState.mode === "material" && performanceState.playing) {
        performanceState.progress += (rawDelta * viewState.slowMotion) / performanceState.durationSeconds;
        if (performanceState.progress >= 1) {
          performanceState.progress = 1;
          performanceState.playing = false;
          performanceState.activeName = null;
        } else {
          performanceState.activeName = "greeting";
        }
      }

      applyTimelinePose(performanceState.progress, false);
      render();
    };
    frameHandle = window.requestAnimationFrame(tick);
  }

  function evaluateGreeting(progress) {
    const keyframes = [
      {
        t: 0,
        stage: "Relaxed",
        face: { smile: 0.26, mouthOpen: 0.01, cheekLift: 0.2, eyeSquint: 0.08, eyeOpen: 0.74, browLift: 0.08, gazeY: 0.02 },
        pose: { y: 0, rotY: 0, rotZ: 0, squashX: 1, squashY: 1 },
      },
      {
        t: 0.22,
        stage: "Recognition",
        face: { smile: 0.35, mouthOpen: 0.03, cheekLift: 0.28, eyeSquint: 0.06, eyeOpen: 0.88, browLift: 0.28, gazeY: 0.08 },
        pose: { y: 0.01, rotY: 0.02, rotZ: 0, squashX: 1, squashY: 1 },
      },
      {
        t: 0.44,
        stage: "Anticipation",
        face: { smile: 0.54, mouthOpen: 0.18, cheekLift: 0.44, eyeSquint: 0.18, eyeOpen: 0.78, browLift: 0.14, gazeY: 0.04 },
        pose: { y: -0.18, rotY: 0, rotZ: -0.03, squashX: 1.08, squashY: 0.9 },
      },
      {
        t: 0.7,
        stage: "Delight",
        face: { smile: 0.94, mouthOpen: 0.86, cheekLift: 0.96, eyeSquint: 0.62, eyeOpen: 0.58, browLift: 0.12, gazeY: 0.06 },
        pose: { y: 0.28, rotY: 0, rotZ: 0.055, squashX: 0.95, squashY: 1.1 },
      },
      {
        t: 0.84,
        stage: "Settle",
        face: { smile: 0.78, mouthOpen: 0.48, cheekLift: 0.72, eyeSquint: 0.42, eyeOpen: 0.62, browLift: 0.1, gazeY: 0.04 },
        pose: { y: 0.08, rotY: 0, rotZ: 0.01, squashX: 0.98, squashY: 1.03 },
      },
      {
        t: 1,
        stage: "Relaxed",
        face: { smile: 0.38, mouthOpen: 0.06, cheekLift: 0.3, eyeSquint: 0.14, eyeOpen: 0.72, browLift: 0.08, gazeY: 0.02 },
        pose: { y: 0, rotY: 0, rotZ: 0, squashX: 1, squashY: 1 },
      },
    ];

    const clamped = Math.max(0, Math.min(1, progress));
    let from = keyframes[0];
    let to = keyframes[keyframes.length - 1];
    for (let i = 0; i < keyframes.length - 1; i += 1) {
      if (clamped >= keyframes[i].t && clamped <= keyframes[i + 1].t) {
        from = keyframes[i];
        to = keyframes[i + 1];
        break;
      }
    }
    const section = Math.max(0.0001, to.t - from.t);
    const localT = smoothStep((clamped - from.t) / section);
    const face = lerpObject(from.face, to.face, localT);
    const pose = lerpObject(from.pose, to.pose, localT);

    let stage = from.stage;
    if (clamped >= 0.2 && clamped < 0.36) {
      stage = "Recognition";
    } else if (clamped >= 0.36 && clamped < 0.58) {
      stage = "Anticipation";
    } else if (clamped >= 0.58 && clamped < 0.8) {
      stage = "Delight";
    } else if (clamped >= 0.8 && clamped < 0.95) {
      stage = "Settle";
    } else {
      stage = "Relaxed";
    }

    if (isReducedMotion()) {
      pose.y = 0;
      pose.rotY = 0;
      pose.rotZ = 0;
      pose.squashX = 1;
      pose.squashY = 1;
      if (stage === "Anticipation" || stage === "Delight") {
        face.mouthOpen = Math.max(face.mouthOpen, 0.4);
        face.cheekLift = Math.max(face.cheekLift, 0.66);
        face.eyeSquint = Math.max(face.eyeSquint, 0.34);
      }
    }

    return { stage, face, pose };
  }

  function applyTimelinePose(progress, immediate) {
    const { stage, face, pose } = evaluateGreeting(progress);
    faceState.target = face;
    poseState.target = pose;

    if (immediate) {
      faceState.current = { ...face };
      poseState.current = { ...pose };
    } else {
      const blend = Math.max(0.18, Math.min(1, (viewState.slowMotion > 0.6 ? 0.28 : 0.2)));
      for (const key of Object.keys(faceState.current)) {
        faceState.current[key] += (face[key] - faceState.current[key]) * blend;
      }
      for (const key of Object.keys(poseState.current)) {
        poseState.current[key] += (pose[key] - poseState.current[key]) * blend;
      }
    }

    applyPose();
    applyFace();
    syncProgressUi(progress);
    syncDiagnostics(stage);
  }

  function applyPose() {
    refs.starRoot.position.y = 0.1 + poseState.current.y;
    refs.starRoot.rotation.y = poseState.current.rotY;
    refs.starRoot.rotation.z = poseState.current.rotZ;
    refs.starRoot.scale.set(poseState.current.squashX, poseState.current.squashY, 1);

    refs.shadow.scale.set(1 + poseState.current.y * 0.2, 1 + poseState.current.y * 0.2, 1);
    refs.shadow.material.opacity = 0.16 - Math.max(0, poseState.current.y) * 0.1;
  }

  function applyFace() {
    const face = faceState.current;
    const eyeScaleY = Math.max(0.58, face.eyeOpen - face.eyeSquint * 0.22);

    refs.leftEye.root.scale.set(1, eyeScaleY, 1);
    refs.rightEye.root.scale.set(1, eyeScaleY, 1);
    refs.leftEye.upperLid.position.y = 0.116 - face.eyeOpen * 0.07 - face.eyeSquint * 0.03;
    refs.rightEye.upperLid.position.y = 0.116 - face.eyeOpen * 0.07 - face.eyeSquint * 0.03;
    refs.leftEye.lowerLid.position.y = -0.12 + face.eyeSquint * 0.07;
    refs.rightEye.lowerLid.position.y = -0.12 + face.eyeSquint * 0.07;

    refs.leftEye.iris.position.y = -0.008 + face.gazeY * 0.02;
    refs.rightEye.iris.position.y = -0.008 + face.gazeY * 0.02;
    refs.leftEye.pupil.position.y = -0.009 + face.gazeY * 0.026;
    refs.rightEye.pupil.position.y = -0.009 + face.gazeY * 0.026;

    refs.leftBrow.position.y = 0.53 + face.browLift * 0.12;
    refs.rightBrow.position.y = 0.53 + face.browLift * 0.12;
    refs.leftBrow.rotation.x = 0.16 + face.browLift * 0.24;
    refs.rightBrow.rotation.x = 0.16 + face.browLift * 0.24;

    refs.leftCheek.material.opacity = 0.28 + face.cheekLift * 0.48;
    refs.rightCheek.material.opacity = 0.28 + face.cheekLift * 0.48;
    refs.leftCheek.position.y = 0.03 + face.cheekLift * 0.07;
    refs.rightCheek.position.y = 0.03 + face.cheekLift * 0.07;
    refs.leftCheek.scale.y = 0.58 + face.cheekLift * 0.18;
    refs.rightCheek.scale.y = 0.58 + face.cheekLift * 0.18;

    refs.leftSmileCrease.material.opacity = 0.18 + face.cheekLift * 0.38;
    refs.rightSmileCrease.material.opacity = 0.18 + face.cheekLift * 0.38;
    refs.leftSmileCrease.position.y = -0.13 + face.cheekLift * 0.05;
    refs.rightSmileCrease.position.y = -0.13 + face.cheekLift * 0.05;

    const mouthWidth = 0.9 + face.smile * 0.62;
    const mouthHeight = 0.5 + face.smile * 0.36;
    const mouthOpen = Math.max(0, Math.min(1, face.mouthOpen));
    const cornerLift = face.smile * 0.08 + mouthOpen * 0.03;

    refs.mouth.scale.set(mouthWidth, mouthHeight, 1);
    refs.mouth.position.y = -0.2 + face.smile * 0.045;

    refs.upperLip.position.y = 0.036 + cornerLift;
    refs.upperLip.scale.set(1, 0.88 + face.smile * 0.28, 1);

    const jawDrop = mouthOpen * 0.16;
    refs.lowerLip.position.y = -0.036 - jawDrop;
    refs.lowerLip.scale.set(1.02, 0.86 + mouthOpen * 0.5, 1);

    refs.leftMouthCorner.position.set(-0.206, -0.012 + cornerLift * 0.9, 0.01);
    refs.rightMouthCorner.position.set(0.206, -0.012 + cornerLift * 0.9, 0.01);

    refs.mouthInner.position.y = -0.016 - mouthOpen * 0.05;
    refs.mouthInner.scale.set(1.48, 0.34 + mouthOpen * 1.02, 1);
    refs.mouthInner.visible = mouthOpen > 0.03;

    refs.mouthTongue.position.y = -0.118 - mouthOpen * 0.016;
    refs.mouthTongue.scale.set(1.34, 0.54 + mouthOpen * 0.5, 1);
    refs.mouthTongue.visible = mouthOpen > 0.22;
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

    performanceButtons.forEach((button) => {
      const isPause = button.dataset.performance === "pause";
      button.disabled = viewState.mode === "silhouette" && !isPause;
      if (isPause) {
        button.classList.toggle("active", viewState.paused);
      }
      if (button.dataset.performance === "play") {
        button.classList.toggle("active", performanceState.playing);
      }
    });

    cameraButtons.forEach((button) => {
      const active = viewState.mode === "material" && button.dataset.camera === viewState.camera;
      button.classList.toggle("active", active);
      button.disabled = viewState.mode === "silhouette";
    });

    if (greetingProgressRange) {
      greetingProgressRange.disabled = viewState.mode === "silhouette";
    }

    syncDiagnostics();
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

  function syncProgressUi(progress) {
    const clamped = Math.max(0, Math.min(1, progress));
    if (greetingProgressRange) {
      greetingProgressRange.value = String(Math.round(clamped * 100));
    }
    if (greetingProgressBadge) {
      greetingProgressBadge.textContent = `${Math.round(clamped * 100)}%`;
    }
  }

  function syncDiagnostics(stageLabel = null) {
    const stage = stageLabel || evaluateGreeting(performanceState.progress).stage;
    const motionState = viewState.paused
      ? "paused"
      : pausedByVisibility
        ? "hidden-tab"
        : isReducedMotion()
          ? "reduced-motion"
          : "running";
    if (diagBuildId) {
      diagBuildId.textContent = LAB_BUILD_ID;
    }
    if (diagExpression) {
      diagExpression.textContent = stage;
    }
    if (diagPerformance) {
      diagPerformance.textContent = performanceState.activeName || "none";
    }
    if (diagMotion) {
      diagMotion.textContent = motionState;
    }
    if (diagMouthOpen) {
      diagMouthOpen.textContent = `${faceState.current.mouthOpen.toFixed(2)} / ${faceState.target.mouthOpen.toFixed(2)}`;
    }
    if (diagSmile) {
      diagSmile.textContent = `${faceState.current.smile.toFixed(2)} / ${faceState.target.smile.toFixed(2)}`;
    }
    if (diagCheekLift) {
      diagCheekLift.textContent = faceState.current.cheekLift.toFixed(2);
    }
    if (diagEyeSquint) {
      diagEyeSquint.textContent = faceState.current.eyeSquint.toFixed(2);
    }
  }

  function smoothStep(x) {
    const clamped = Math.max(0, Math.min(1, x));
    return clamped * clamped * (3 - 2 * clamped);
  }

  function lerpObject(a, b, t) {
    const out = {};
    Object.keys(a).forEach((key) => {
      out[key] = a[key] + (b[key] - a[key]) * t;
    });
    return out;
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

  window.__goldStarLabDebug = {
    getState() {
      const stage = evaluateGreeting(performanceState.progress).stage;
      return {
        buildId: LAB_BUILD_ID,
        stage,
        progress: performanceState.progress,
        viewState: { ...viewState },
        activePerformance: performanceState.activeName,
        current: { ...faceState.current },
        target: { ...faceState.target },
        applied: {
          mouthInnerVisible: !!refs.mouthInner?.visible,
          tongueVisible: !!refs.mouthTongue?.visible,
          mouthInnerScaleY: Number(refs.mouthInner?.scale?.y ?? 0),
          mouthScaleY: Number(refs.mouth?.scale?.y ?? 0),
          lowerLipY: Number(refs.lowerLip?.position?.y ?? 0),
          leftCheekY: Number(refs.leftCheek?.position?.y ?? 0),
          leftEyeScaleY: Number(refs.leftEye?.root?.scale?.y ?? 0),
          starY: Number(refs.starRoot?.position?.y ?? 0),
        },
      };
    },
  };
})();
