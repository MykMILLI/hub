import * as THREE from "/static/vendor/three/three.module.js";

(() => {
  const host = document.getElementById("characterLabCanvasHost");
  const fallback = document.getElementById("webglFallback");
  if (!host || !fallback) {
    return;
  }

  const slowMoRange = document.getElementById("slowMoRange");
  const slowMoBadge = document.getElementById("slowMoBadge");
  const pauseMotionBtn = document.getElementById("pauseMotionBtn");
  const reducedMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");

  const canUseWebGL = (() => {
    const canvas = document.createElement("canvas");
    return !!(canvas.getContext("webgl2") || canvas.getContext("webgl"));
  })();

  if (!canUseWebGL) {
    fallback.classList.add("active");
    return;
  }

  let renderer;
  let scene;
  let camera;
  let running = false;
  let frameToken = 0;
  let lastFrameTs = 0;
  let accumulated = 0;
  let paused = false;
  let slowMotion = 1;
  let activeAction = "idle";
  let actionTime = 0;
  let nextBlinkAt = 2 + Math.random() * 2.4;
  let blinkProgress = 0;
  let blinkActive = false;
  let gazeTarget = { x: 0, y: 0 };
  let currentGaze = { x: 0, y: 0 };
  let targetCameraYaw = 0.58;
  let cameraYaw = 0.58;
  let targetCameraDistance = 6.2;
  const cleanupCallbacks = [];
  const animPose = {
    bob: 0,
    bodyTilt: 0,
    bodyYaw: 0,
    bodyScaleY: 1,
    bodyScaleX: 1,
    leftShoulder: 0.08,
    rightShoulder: -0.08,
    leftElbow: -0.12,
    rightElbow: 0.12,
    mouthSmile: 0.2,
    eyelidClose: 0,
    eyePitch: 0,
    eyeYaw: 0,
  };

  const character = createCharacter();
  initScene(character.root);
  bindControls();
  resumeLoop();

  function initScene(characterRoot) {
    scene = new THREE.Scene();
    scene.background = new THREE.Color(0xf4ecff);
    scene.fog = new THREE.Fog(0xf4ecff, 8, 22);

    const width = host.clientWidth || 980;
    const height = host.clientHeight || 520;

    camera = new THREE.PerspectiveCamera(34, width / height, 0.1, 100);
    camera.position.set(Math.sin(cameraYaw) * targetCameraDistance, 1.1, Math.cos(cameraYaw) * targetCameraDistance);
    camera.lookAt(0, 0.7, 0);

    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: "high-performance" });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.8));
    renderer.setSize(width, height, false);
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFShadowMap;
    host.appendChild(renderer.domElement);

    const hemi = new THREE.HemisphereLight(0xfff6d9, 0xd9cdff, 0.72);
    hemi.position.set(0, 7, 0);
    scene.add(hemi);

    const keyLight = new THREE.DirectionalLight(0xfff8e5, 1.1);
    keyLight.position.set(3.2, 5.4, 4.1);
    keyLight.castShadow = true;
    keyLight.shadow.mapSize.set(1024, 1024);
    keyLight.shadow.camera.near = 0.5;
    keyLight.shadow.camera.far = 20;
    keyLight.shadow.camera.left = -5;
    keyLight.shadow.camera.right = 5;
    keyLight.shadow.camera.top = 5;
    keyLight.shadow.camera.bottom = -5;
    scene.add(keyLight);

    const fill = new THREE.PointLight(0xf1dcff, 0.48, 18);
    fill.position.set(-4.3, 2.5, 3.5);
    scene.add(fill);

    const ground = new THREE.Mesh(
      new THREE.CircleGeometry(5.4, 48),
      new THREE.MeshStandardMaterial({ color: 0xe8dcff, roughness: 0.94, metalness: 0.02 })
    );
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = -1.02;
    ground.receiveShadow = true;
    scene.add(ground);

    const shadowDisc = new THREE.Mesh(
      new THREE.CircleGeometry(1.28, 64),
      new THREE.MeshBasicMaterial({ color: 0x3f2c69, transparent: true, opacity: 0.16 })
    );
    shadowDisc.rotation.x = -Math.PI / 2;
    shadowDisc.position.set(0, -0.995, 0.35);
    scene.add(shadowDisc);

    scene.add(characterRoot);

    const onResize = () => {
      const nextWidth = Math.max(host.clientWidth, 240);
      const nextHeight = Math.max(host.clientHeight, 320);
      camera.aspect = nextWidth / nextHeight;
      camera.updateProjectionMatrix();
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.8));
      renderer.setSize(nextWidth, nextHeight, false);
    };
    window.addEventListener("resize", onResize);
    cleanupCallbacks.push(() => window.removeEventListener("resize", onResize));

    const onVisibility = () => {
      if (document.hidden) {
        stopLoop();
      } else {
        resumeLoop();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    cleanupCallbacks.push(() => document.removeEventListener("visibilitychange", onVisibility));

    if (reducedMotionQuery.addEventListener) {
      const onReducedMotion = () => {
        if (reducedMotionQuery.matches) {
          triggerAction("neutral");
        }
      };
      reducedMotionQuery.addEventListener("change", onReducedMotion);
      cleanupCallbacks.push(() => reducedMotionQuery.removeEventListener("change", onReducedMotion));
    }
  }

  function createCharacter() {
    const root = new THREE.Group();
    root.position.set(0, 0.02, 0);
    root.rotation.y = -0.08;

    const bodyMaterial = new THREE.MeshStandardMaterial({
      color: 0xffcf55,
      roughness: 0.52,
      metalness: 0.08,
    });
    const softGoldMaterial = new THREE.MeshStandardMaterial({
      color: 0xffdc73,
      roughness: 0.56,
      metalness: 0.05,
    });

    const body = new THREE.Group();
    root.add(body);

    const centerCore = makeSpherePart(1.05, 1.02, 0.8, 0, 0.03, 0.06, bodyMaterial);
    body.add(centerCore);
    const frontBelly = makeSpherePart(0.84, 0.92, 0.68, 0, -0.02, 0.42, softGoldMaterial);
    body.add(frontBelly);
    const backVolume = makeSpherePart(0.86, 0.84, 0.72, 0, 0.02, -0.36, bodyMaterial);
    body.add(backVolume);

    const tipPositions = [
      [0, 1.23],
      [1.18, 0.36],
      [0.72, -1.07],
      [-0.72, -1.07],
      [-1.18, 0.36],
    ];
    for (const [x, y] of tipPositions) {
      const tip = makeSpherePart(0.54, 0.66, 0.46, x, y, 0.16, bodyMaterial);
      body.add(tip);
      const backTip = makeSpherePart(0.42, 0.5, 0.36, x * 0.97, y * 0.97, -0.18, bodyMaterial);
      body.add(backTip);
    }

    const cheekMaterial = new THREE.MeshStandardMaterial({
      color: 0xff8cab,
      roughness: 0.78,
      metalness: 0.01,
      transparent: true,
      opacity: 0.48,
    });
    const leftCheek = makeSpherePart(0.13, 0.1, 0.08, -0.38, -0.08, 0.82, cheekMaterial);
    const rightCheek = makeSpherePart(0.13, 0.1, 0.08, 0.38, -0.08, 0.82, cheekMaterial);
    body.add(leftCheek);
    body.add(rightCheek);

    const eyeWhiteMaterial = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.33, metalness: 0.03 });
    const pupilMaterial = new THREE.MeshStandardMaterial({ color: 0x2f214a, roughness: 0.45, metalness: 0.04 });
    const eyeHighlightMaterial = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.2, metalness: 0.1 });
    const eyelidMaterial = bodyMaterial.clone();
    eyelidMaterial.roughness = 0.63;

    const leftEyeAnchor = new THREE.Group();
    const rightEyeAnchor = new THREE.Group();
    leftEyeAnchor.position.set(-0.34, 0.2, 0.92);
    rightEyeAnchor.position.set(0.34, 0.2, 0.92);
    body.add(leftEyeAnchor, rightEyeAnchor);

    const leftEye = new THREE.Mesh(new THREE.SphereGeometry(0.145, 26, 22), eyeWhiteMaterial);
    const rightEye = new THREE.Mesh(new THREE.SphereGeometry(0.145, 26, 22), eyeWhiteMaterial);
    leftEye.castShadow = true;
    rightEye.castShadow = true;

    const leftPupil = new THREE.Mesh(new THREE.SphereGeometry(0.066, 18, 16), pupilMaterial);
    const rightPupil = new THREE.Mesh(new THREE.SphereGeometry(0.066, 18, 16), pupilMaterial);
    leftPupil.position.set(0, -0.01, 0.11);
    rightPupil.position.set(0, -0.01, 0.11);
    leftEye.add(leftPupil);
    rightEye.add(rightPupil);

    const leftHighlight = new THREE.Mesh(new THREE.SphereGeometry(0.018, 12, 10), eyeHighlightMaterial);
    const rightHighlight = leftHighlight.clone();
    leftHighlight.position.set(-0.03, 0.04, 0.11);
    rightHighlight.position.set(-0.03, 0.04, 0.11);
    leftEye.add(leftHighlight);
    rightEye.add(rightHighlight);

    leftEyeAnchor.add(leftEye);
    rightEyeAnchor.add(rightEye);

    const eyelidGeo = new THREE.SphereGeometry(0.15, 24, 20, 0, Math.PI * 2, 0, Math.PI * 0.52);
    const leftEyelid = new THREE.Mesh(eyelidGeo, eyelidMaterial);
    const rightEyelid = new THREE.Mesh(eyelidGeo, eyelidMaterial);
    leftEyelid.position.set(0, 0.08, 0.03);
    rightEyelid.position.set(0, 0.08, 0.03);
    leftEyeAnchor.add(leftEyelid);
    rightEyeAnchor.add(rightEyelid);

    const mouthPivot = new THREE.Group();
    mouthPivot.position.set(0, -0.12, 0.88);
    const smile = new THREE.Mesh(
      new THREE.TorusGeometry(0.18, 0.021, 16, 46, Math.PI * 0.92),
      new THREE.MeshStandardMaterial({ color: 0x7a2e2f, roughness: 0.38, metalness: 0.02 })
    );
    smile.rotation.z = Math.PI;
    smile.rotation.x = 0.22;
    mouthPivot.add(smile);
    body.add(mouthPivot);

    const armMaterial = new THREE.MeshStandardMaterial({ color: 0xffcc67, roughness: 0.54, metalness: 0.04 });
    const handMaterial = new THREE.MeshStandardMaterial({ color: 0xffd98d, roughness: 0.56, metalness: 0.03 });

    const leftArm = createArm(true, armMaterial, handMaterial);
    const rightArm = createArm(false, armMaterial, handMaterial);
    body.add(leftArm.root, rightArm.root);

    root.traverse((node) => {
      if (!(node instanceof THREE.Mesh)) {
        return;
      }
      node.castShadow = true;
      node.receiveShadow = false;
    });

    return {
      root,
      body,
      leftArm,
      rightArm,
      leftEyeAnchor,
      rightEyeAnchor,
      leftEyelid,
      rightEyelid,
      mouthPivot,
      smile,
    };
  }

  function makeSpherePart(scaleX, scaleY, scaleZ, x, y, z, material) {
    const mesh = new THREE.Mesh(new THREE.SphereGeometry(0.5, 28, 22), material);
    mesh.scale.set(scaleX, scaleY, scaleZ);
    mesh.position.set(x, y, z);
    return mesh;
  }

  function createArm(leftSide, armMaterial, handMaterial) {
    const direction = leftSide ? -1 : 1;
    const root = new THREE.Group();
    root.position.set(direction * 0.92, 0.36, 0.16);
    root.rotation.z = direction * (leftSide ? 0.34 : -0.34);

    const upper = new THREE.Mesh(new THREE.CapsuleGeometry(0.07, 0.35, 8, 12), armMaterial);
    upper.rotation.z = direction * 0.15;
    upper.position.y = -0.25;
    root.add(upper);

    const elbow = new THREE.Group();
    elbow.position.set(direction * 0.1, -0.47, 0.04);
    root.add(elbow);

    const fore = new THREE.Mesh(new THREE.CapsuleGeometry(0.06, 0.24, 7, 10), armMaterial);
    fore.rotation.z = direction * 0.1;
    fore.position.y = -0.16;
    elbow.add(fore);

    const hand = new THREE.Mesh(new THREE.SphereGeometry(0.12, 16, 14), handMaterial);
    hand.position.set(direction * 0.05, -0.35, 0.06);
    elbow.add(hand);

    return { root, elbow };
  }

  function bindControls() {
    const actionButtons = Array.from(document.querySelectorAll("[data-action]"));
    actionButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const action = button.dataset.action || "";
        triggerAction(action);
      });
    });

    if (slowMoRange && slowMoBadge) {
      slowMoRange.addEventListener("input", () => {
        const value = Math.max(0.2, Math.min(1, Number(slowMoRange.value) || 1));
        slowMotion = value;
        slowMoBadge.textContent = `${value.toFixed(2)}×`;
      });
    }

    const cameraButtons = Array.from(document.querySelectorAll("[data-camera]"));
    cameraButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const mode = button.dataset.camera || "";
        if (mode === "front") {
          targetCameraYaw = 0;
          targetCameraDistance = 6.1;
        } else if (mode === "side") {
          targetCameraYaw = 1.46;
          targetCameraDistance = 6.2;
        } else {
          targetCameraYaw = 0.62;
          targetCameraDistance = 6.2;
        }
      });
    });

    const onUnload = () => teardown();
    window.addEventListener("beforeunload", onUnload);
    cleanupCallbacks.push(() => window.removeEventListener("beforeunload", onUnload));
  }

  function triggerAction(action) {
    if (action === "pause") {
      paused = !paused;
      if (pauseMotionBtn) {
        pauseMotionBtn.classList.toggle("active", paused);
        pauseMotionBtn.setAttribute("aria-pressed", paused ? "true" : "false");
        pauseMotionBtn.textContent = paused ? "Resume motion" : "Pause motion";
      }
      if (!paused && !running && !document.hidden) {
        resumeLoop();
      }
      return;
    }
    activeAction = action || "idle";
    actionTime = 0;
  }

  function resumeLoop() {
    if (running || paused) {
      return;
    }
    running = true;
    lastFrameTs = 0;
    const loop = (timestamp) => {
      if (!running || paused) {
        return;
      }
      frameToken = window.requestAnimationFrame(loop);
      if (!lastFrameTs) {
        lastFrameTs = timestamp;
        return;
      }
      const rawDelta = Math.min(0.05, (timestamp - lastFrameTs) / 1000);
      lastFrameTs = timestamp;
      const delta = rawDelta * slowMotion * (reducedMotionQuery.matches ? 0.45 : 1);
      update(delta);
      renderer.render(scene, camera);
    };
    frameToken = window.requestAnimationFrame(loop);
  }

  function stopLoop() {
    if (!running) {
      return;
    }
    running = false;
    if (frameToken) {
      window.cancelAnimationFrame(frameToken);
      frameToken = 0;
    }
  }

  function update(delta) {
    accumulated += delta;
    actionTime += delta;
    updateBlink(delta);
    updateGaze(delta);
    updateCamera(delta);
    applyAction(delta);
    applyPose();
  }

  function updateCamera(delta) {
    const ease = Math.min(1, delta * 3.6);
    cameraYaw += (targetCameraYaw - cameraYaw) * ease;
    const radius = targetCameraDistance;
    camera.position.x += (Math.sin(cameraYaw) * radius - camera.position.x) * ease;
    camera.position.z += (Math.cos(cameraYaw) * radius - camera.position.z) * ease;
    camera.position.y += (1.1 - camera.position.y) * ease;
    camera.lookAt(0, 0.7, 0);
  }

  function updateBlink(delta) {
    if (!blinkActive) {
      nextBlinkAt -= delta;
      if (nextBlinkAt <= 0) {
        blinkActive = true;
        blinkProgress = 0;
      }
      return;
    }
    blinkProgress += delta * 6.3;
    if (blinkProgress >= 1) {
      blinkActive = false;
      blinkProgress = 0;
      nextBlinkAt = 1.8 + Math.random() * 2.6;
    }
  }

  function updateGaze(delta) {
    if (activeAction === "idle" && !reducedMotionQuery.matches && Math.random() < 0.006) {
      gazeTarget = {
        x: (Math.random() - 0.5) * 0.35,
        y: (Math.random() - 0.5) * 0.25,
      };
    }
    const gazeEase = Math.min(1, delta * 5.6);
    currentGaze.x += (gazeTarget.x - currentGaze.x) * gazeEase;
    currentGaze.y += (gazeTarget.y - currentGaze.y) * gazeEase;
  }

  function applyAction(delta) {
    const t = actionTime;
    const smooth = (v) => v * v * (3 - 2 * v);
    const damp = Math.min(1, delta * 7);

    const target = {
      bob: 0,
      bodyTilt: 0,
      bodyYaw: 0,
      bodyScaleY: 1,
      bodyScaleX: 1,
      leftShoulder: 0.08,
      rightShoulder: -0.08,
      leftElbow: -0.12,
      rightElbow: 0.12,
      mouthSmile: 0.2,
      eyelidClose: 0,
      eyePitch: currentGaze.y,
      eyeYaw: currentGaze.x,
    };

    if (activeAction === "idle") {
      const drift = Math.sin(accumulated * 1.6);
      target.bob = reducedMotionQuery.matches ? 0.01 : drift * 0.065;
      target.bodyTilt = reducedMotionQuery.matches ? 0 : Math.sin(accumulated * 0.8) * 0.06;
      target.bodyYaw = Math.sin(accumulated * 0.52) * 0.08;
      target.leftShoulder = 0.09 + Math.sin(accumulated * 1.1) * 0.03;
      target.rightShoulder = -0.09 - Math.sin(accumulated * 1.04) * 0.03;
      target.mouthSmile = 0.22 + Math.sin(accumulated * 0.6) * 0.03;
    } else if (activeAction === "notice") {
      const cycle = Math.min(1, t / 1.6);
      const p = smooth(cycle);
      target.bob = Math.sin(p * Math.PI) * 0.08;
      target.bodyTilt = -0.12 + p * 0.18;
      target.bodyYaw = 0.24 - p * 0.18;
      target.rightShoulder = -0.95 + Math.sin(t * 9.4) * 0.38;
      target.rightElbow = 0.78 + Math.sin(t * 9.4 + 0.6) * 0.3;
      target.leftShoulder = 0.14;
      target.eyeYaw = 0.24;
      target.eyePitch = -0.05;
      target.mouthSmile = 0.44;
      if (cycle >= 1) {
        activeAction = "idle";
        actionTime = 0;
      }
    } else if (activeAction === "tap") {
      const cycle = Math.min(1, t / 0.62);
      target.bob = Math.sin(cycle * Math.PI) * 0.16;
      target.bodyTilt = Math.sin(cycle * Math.PI * 2) * 0.12;
      target.bodyYaw = Math.sin(cycle * Math.PI * 3) * 0.1;
      target.leftShoulder = 0.2 + Math.sin(cycle * Math.PI) * 0.2;
      target.rightShoulder = -0.2 - Math.sin(cycle * Math.PI) * 0.2;
      target.leftElbow = -0.28;
      target.rightElbow = 0.28;
      target.mouthSmile = 0.34;
      if (cycle >= 1) {
        activeAction = "idle";
        actionTime = 0;
      }
    } else if (activeAction === "collect") {
      const cycle = Math.min(1, t / 1.4);
      const launch = Math.sin(cycle * Math.PI);
      target.bob = launch * 0.24;
      target.bodyScaleY = 1 - launch * 0.17;
      target.bodyScaleX = 1 + launch * 0.14;
      target.bodyTilt = Math.sin(cycle * Math.PI * 2) * 0.12;
      target.bodyYaw = Math.sin(cycle * Math.PI * 3) * 0.16;
      target.leftShoulder = 0.64 - launch * 0.2;
      target.rightShoulder = -0.64 + launch * 0.2;
      target.leftElbow = -0.62;
      target.rightElbow = 0.62;
      target.eyePitch = -0.1;
      target.mouthSmile = 0.52;
      if (cycle >= 1) {
        activeAction = "idle";
        actionTime = 0;
      }
    } else if (activeAction === "neutral") {
      target.mouthSmile = 0.2;
      target.eyePitch = 0;
      target.eyeYaw = 0;
      if (t >= 0.45) {
        activeAction = "idle";
        actionTime = 0;
      }
    }

    if (blinkActive) {
      const blinkArc = Math.sin(blinkProgress * Math.PI);
      target.eyelidClose = Math.min(1, blinkArc * 1.2);
    }

    for (const key of Object.keys(animPose)) {
      animPose[key] += (target[key] - animPose[key]) * damp;
    }
  }

  function applyPose() {
    character.root.position.y = animPose.bob;
    character.root.rotation.z = animPose.bodyTilt;
    character.root.rotation.y = -0.08 + animPose.bodyYaw;
    character.body.scale.set(animPose.bodyScaleX, animPose.bodyScaleY, 1);

    character.leftArm.root.rotation.z = -0.3 + animPose.leftShoulder;
    character.rightArm.root.rotation.z = 0.3 + animPose.rightShoulder;
    character.leftArm.elbow.rotation.z = animPose.leftElbow;
    character.rightArm.elbow.rotation.z = animPose.rightElbow;

    character.leftEyeAnchor.rotation.y = animPose.eyeYaw;
    character.rightEyeAnchor.rotation.y = animPose.eyeYaw;
    character.leftEyeAnchor.rotation.x = animPose.eyePitch;
    character.rightEyeAnchor.rotation.x = animPose.eyePitch;

    const lidOffset = animPose.eyelidClose * 0.22;
    const lidScale = Math.max(0.12, 1 - animPose.eyelidClose * 0.9);
    character.leftEyelid.position.y = 0.08 - lidOffset;
    character.rightEyelid.position.y = 0.08 - lidOffset;
    character.leftEyelid.scale.y = lidScale;
    character.rightEyelid.scale.y = lidScale;

    character.smile.scale.y = Math.max(0.45, animPose.mouthSmile * 2.2);
    character.smile.position.y = -0.02 + animPose.mouthSmile * 0.04;
  }

  function teardown() {
    stopLoop();
    cleanupCallbacks.splice(0).forEach((cleanup) => cleanup());
    if (!scene || !renderer) {
      return;
    }
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
