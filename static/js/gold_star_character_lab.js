import * as THREE from "/static/vendor/three/three.module.js";

(() => {
  const host = document.getElementById("characterLabCanvasHost");
  const fallback = document.getElementById("webglFallback");
  if (!host || !fallback) {
    return;
  }

  const LAB_BUILD_ID = "gold-star-lab-2026-11-22-static-fallback-v1";
  const modeButtons = Array.from(document.querySelectorAll("[data-mode]"));
  const cameraButtons = Array.from(document.querySelectorAll("[data-camera]"));
  const actionButtons = Array.from(document.querySelectorAll("[data-action]"));
  const motionToggle = document.getElementById("motionToggle");

  const diagBuildId = document.getElementById("diagBuildId");
  const diagExpression = document.getElementById("diagExpression");
  const diagPerformance = document.getElementById("diagPerformance");
  const diagMotion = document.getElementById("diagMotion");
  const diagMouthOpen = document.getElementById("diagMouthOpen");
  const diagSmile = document.getElementById("diagSmile");
  const diagCheekLift = document.getElementById("diagCheekLift");
  const diagEyeSquint = document.getElementById("diagEyeSquint");

  const canUseWebGL = (() => {
    const canvas = document.createElement("canvas");
    return !!(canvas.getContext("webgl2") || canvas.getContext("webgl"));
  })();
  if (!canUseWebGL) {
    fallback.classList.add("active");
    return;
  }

  const state = {
    mode: "material",
    camera: "front",
    reducedMotion: false,
    action: "idle",
  };

  let renderer;
  let scene;
  let camera;
  const refs = {};
  const lights = [];
  const materials = {};

  initScene();
  bindControls();
  applyViewState();
  applyActionPose("idle");
  render();

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
    materials.eyeWhite = new THREE.MeshStandardMaterial({ color: 0xfffcf6, roughness: 0.34, metalness: 0.01 });
    materials.pupil = new THREE.MeshStandardMaterial({ color: 0x1d1730, roughness: 0.26, metalness: 0.03 });
    materials.smile = new THREE.MeshStandardMaterial({ color: 0x8a4151, roughness: 0.46, metalness: 0.02 });

    refs.starRoot = new THREE.Group();
    refs.starRoot.position.set(0, 0.1, 0);
    scene.add(refs.starRoot);

    refs.star = new THREE.Mesh(createStarBodyGeometry(), materials.star);
    refs.star.rotation.z = Math.PI;
    refs.starRoot.add(refs.star);

    refs.face = new THREE.Group();
    refs.face.rotation.z = Math.PI;
    refs.face.position.z = 0.03;
    refs.star.add(refs.face);

    refs.leftEye = createEye();
    refs.rightEye = createEye();
    refs.leftEye.position.set(-0.44, 0.24, 0.39);
    refs.rightEye.position.set(0.44, 0.24, 0.39);
    refs.face.add(refs.leftEye, refs.rightEye);

    refs.smile = new THREE.Mesh(new THREE.TorusGeometry(0.165, 0.015, 12, 28, Math.PI * 0.7), materials.smile);
    refs.smile.rotation.z = Math.PI;
    refs.smile.rotation.x = 0.18;
    refs.smile.position.set(0, -0.16, 0.47);
    refs.face.add(refs.smile);

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

    window.addEventListener("resize", onResize);
    window.addEventListener("beforeunload", teardown);
  }

  function createEye() {
    const eye = new THREE.Group();
    const white = new THREE.Mesh(new THREE.SphereGeometry(0.18, 28, 24), materials.eyeWhite);
    white.scale.set(1, 0.84, 0.44);
    const pupil = new THREE.Mesh(new THREE.SphereGeometry(0.058, 18, 14), materials.pupil);
    pupil.position.set(0, -0.01, 0.09);
    pupil.scale.set(1, 1.1, 0.56);
    const highlight = new THREE.Mesh(new THREE.SphereGeometry(0.016, 10, 8), new THREE.MeshBasicMaterial({ color: 0xffffff }));
    highlight.position.set(-0.02, 0.032, 0.106);
    eye.add(white, pupil, highlight);
    return eye;
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
    const geometry = new THREE.ExtrudeGeometry(shape, {
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

  function bindControls() {
    modeButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const nextMode = button.dataset.mode;
        if (!["silhouette", "material"].includes(nextMode)) {
          return;
        }
        state.mode = nextMode;
        if (nextMode === "silhouette") {
          state.camera = "front";
        }
        applyViewState();
      });
    });

    cameraButtons.forEach((button) => {
      button.addEventListener("click", () => {
        if (state.mode === "silhouette") {
          return;
        }
        const nextCamera = button.dataset.camera;
        if (!["front", "threeQuarter", "side"].includes(nextCamera)) {
          return;
        }
        state.camera = nextCamera;
        applyViewState();
      });
    });

    actionButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const nextAction = button.dataset.action;
        if (!nextAction || typeof rendererAdapter[nextAction] !== "function") {
          return;
        }
        rendererAdapter[nextAction]();
      });
    });

    if (motionToggle) {
      motionToggle.checked = false;
      motionToggle.addEventListener("change", () => {
        rendererAdapter.setReducedMotion(!!motionToggle.checked);
      });
    }
  }

  function applyActionPose(actionName) {
    state.action = actionName;
    const reduced = state.reducedMotion;
    const poses = {
      idle: { y: 0, rotY: 0, rotZ: 0, scaleX: 1, scaleY: 1 },
      noticeNewStar: reduced ? { y: 0, rotY: 0, rotZ: 0, scaleX: 1, scaleY: 1 } : { y: 0.05, rotY: -0.06, rotZ: 0.01, scaleX: 1, scaleY: 1 },
      tapReaction: reduced ? { y: 0, rotY: 0, rotZ: 0, scaleX: 1, scaleY: 1 } : { y: 0.02, rotY: 0.04, rotZ: -0.02, scaleX: 1.02, scaleY: 0.98 },
      collecting: reduced ? { y: 0, rotY: 0, rotZ: 0, scaleX: 1, scaleY: 1 } : { y: 0.04, rotY: 0.05, rotZ: 0.03, scaleX: 0.99, scaleY: 1.01 },
      celebrate: reduced ? { y: 0, rotY: 0, rotZ: 0, scaleX: 1, scaleY: 1 } : { y: 0.08, rotY: 0, rotZ: 0.05, scaleX: 0.98, scaleY: 1.02 },
      rewardApproved: reduced ? { y: 0, rotY: 0, rotZ: 0, scaleX: 1, scaleY: 1 } : { y: 0.06, rotY: -0.03, rotZ: 0.02, scaleX: 1, scaleY: 1 },
    };
    const pose = poses[actionName] || poses.idle;

    refs.starRoot.position.y = 0.1 + pose.y;
    refs.starRoot.rotation.y = pose.rotY;
    refs.starRoot.rotation.z = pose.rotZ;
    refs.starRoot.scale.set(pose.scaleX, pose.scaleY, 1);
    refs.shadow.scale.set(1 + pose.y * 0.2, 1 + pose.y * 0.2, 1);
    refs.shadow.material.opacity = 0.16 - Math.max(0, pose.y) * 0.1;

    actionButtons.forEach((button) => {
      const active = button.dataset.action === actionName;
      button.classList.toggle("active", active);
      button.classList.toggle("primary", active);
    });
    syncDiagnostics();
    render();
  }

  function applyViewState() {
    if (state.mode === "silhouette") {
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
      setCameraPose(state.camera);
    }

    modeButtons.forEach((button) => {
      const active = button.dataset.mode === state.mode;
      button.classList.toggle("active", active);
      button.classList.toggle("primary", active);
    });
    cameraButtons.forEach((button) => {
      const active = state.mode === "material" && button.dataset.camera === state.camera;
      button.classList.toggle("active", active);
      button.disabled = state.mode === "silhouette";
    });
    actionButtons.forEach((button) => {
      button.disabled = state.mode === "silhouette";
    });
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

  function syncDiagnostics() {
    if (diagBuildId) {
      diagBuildId.textContent = LAB_BUILD_ID;
    }
    if (diagExpression) {
      diagExpression.textContent = state.action;
    }
    if (diagPerformance) {
      diagPerformance.textContent = "none";
    }
    if (diagMotion) {
      diagMotion.textContent = state.reducedMotion ? "reduced-motion" : "static";
    }
    if (diagMouthOpen) {
      diagMouthOpen.textContent = "0 / 0";
    }
    if (diagSmile) {
      diagSmile.textContent = "1 / 1";
    }
    if (diagCheekLift) {
      diagCheekLift.textContent = "0";
    }
    if (diagEyeSquint) {
      diagEyeSquint.textContent = "0";
    }
  }

  function onResize() {
    const nextWidth = Math.max(host.clientWidth, 320);
    const nextHeight = Math.max(host.clientHeight, 320);
    camera.aspect = nextWidth / nextHeight;
    camera.updateProjectionMatrix();
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.8));
    renderer.setSize(nextWidth, nextHeight, true);
    render();
  }

  function render() {
    renderer.render(scene, camera);
  }

  function teardown() {
    window.removeEventListener("resize", onResize);
    window.removeEventListener("beforeunload", teardown);
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

  const rendererAdapter = {
    idle() {
      applyActionPose("idle");
    },
    noticeNewStar() {
      applyActionPose("noticeNewStar");
    },
    tapReaction() {
      applyActionPose("tapReaction");
    },
    collecting() {
      applyActionPose("collecting");
    },
    celebrate() {
      applyActionPose("celebrate");
    },
    rewardApproved() {
      applyActionPose("rewardApproved");
    },
    setReducedMotion(enabled) {
      state.reducedMotion = !!enabled;
      applyActionPose(state.action);
    },
    dispose() {
      teardown();
    },
    getState() {
      return {
        buildId: LAB_BUILD_ID,
        mode: state.mode,
        camera: state.camera,
        action: state.action,
        reducedMotion: state.reducedMotion,
      };
    },
  };

  window.__goldStarCharacterAdapter = rendererAdapter;
  window.__goldStarLabDebug = {
    getState() {
      return {
        ...rendererAdapter.getState(),
        face: {
          hasOpenMouth: false,
          hasAnimatedEyelids: false,
          hasDimples: false,
          hasSmileCreases: false,
          mouthContours: 1,
        },
      };
    },
  };
})();
