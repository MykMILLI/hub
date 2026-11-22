import * as THREE from "/static/vendor/three/three.module.js";

(() => {
  const host = document.getElementById("characterLabCanvasHost");
  const fallback = document.getElementById("webglFallback");
  if (!host || !fallback) {
    return;
  }

  const modeButtons = Array.from(document.querySelectorAll("[data-mode]"));
  const faceButtons = Array.from(document.querySelectorAll("[data-face]"));
  const cameraButtons = Array.from(document.querySelectorAll("[data-camera]"));

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
  let starRoot;
  let starMesh;
  let groundShadow;
  let silhouetteMaterial;
  let goldMaterial;
  let eyeWhiteMaterial;
  let pupilMaterial;
  let eyelidMaterial;
  let mouthMaterial;
  let cheekMaterial;
  let faceGroup;
  let leftEyeGroup;
  let rightEyeGroup;
  let leftPupil;
  let rightPupil;
  let leftEyelid;
  let rightEyelid;
  let mouth;
  let leftCheek;
  let rightCheek;
  const lights = [];
  const cleanupCallbacks = [];

  const viewState = {
    mode: "silhouette",
    face: "A",
    camera: "front",
  };

  initScene();
  bindControls();
  applyViewState();
  render();

  function initScene() {
    scene = new THREE.Scene();
    scene.background = new THREE.Color(0xffffff);

    const width = Math.max(host.clientWidth, 320);
    const height = Math.max(host.clientHeight, 320);
    camera = new THREE.PerspectiveCamera(30, width / height, 0.1, 100);

    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: "high-performance" });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.8));
    renderer.setSize(width, height, true);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    host.appendChild(renderer.domElement);

    goldMaterial = new THREE.MeshStandardMaterial({
      color: 0xffd86a,
      roughness: 0.44,
      metalness: 0.02,
      emissive: 0x3e2a00,
      emissiveIntensity: 0.05,
    });
    silhouetteMaterial = new THREE.MeshBasicMaterial({ color: 0x000000 });
    eyeWhiteMaterial = new THREE.MeshStandardMaterial({ color: 0xfffdf8, roughness: 0.34, metalness: 0.01 });
    pupilMaterial = new THREE.MeshStandardMaterial({ color: 0x32213f, roughness: 0.3, metalness: 0.04 });
    eyelidMaterial = new THREE.MeshStandardMaterial({ color: 0xe5b44e, roughness: 0.52, metalness: 0.02 });
    mouthMaterial = new THREE.MeshStandardMaterial({ color: 0x8d3c4d, roughness: 0.45, metalness: 0.02, side: THREE.DoubleSide });
    cheekMaterial = new THREE.MeshStandardMaterial({
      color: 0xff7e98,
      roughness: 0.75,
      metalness: 0.01,
      transparent: true,
      opacity: 0.72,
      side: THREE.DoubleSide,
    });

    starRoot = new THREE.Group();

    const starGeometry = createStarBodyGeometry();
    starMesh = new THREE.Mesh(starGeometry, goldMaterial);
    starMesh.rotation.z = Math.PI;
    starRoot.add(starMesh);
    buildFace();
    scene.add(starRoot);

    groundShadow = new THREE.Mesh(
      new THREE.CircleGeometry(2.05, 64),
      new THREE.MeshBasicMaterial({ color: 0x3b2f63, transparent: true, opacity: 0.16 })
    );
    groundShadow.rotation.x = -Math.PI / 2;
    groundShadow.position.set(0, -2.1, 0.6);
    scene.add(groundShadow);

    const hemi = new THREE.HemisphereLight(0xfff8de, 0xece6ff, 0.76);
    hemi.position.set(0, 5, 0);
    lights.push(hemi);
    scene.add(hemi);

    const key = new THREE.DirectionalLight(0xfff2cc, 1.08);
    key.position.set(2.2, 2.7, 4.8);
    lights.push(key);
    scene.add(key);

    const fill = new THREE.DirectionalLight(0xf7edff, 0.38);
    fill.position.set(-2.8, 1.7, 3.3);
    lights.push(fill);
    scene.add(fill);

    const rim = new THREE.DirectionalLight(0xfff4d9, 0.26);
    rim.position.set(0.2, 2.1, -3.2);
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

    const onBeforeUnload = () => teardown();
    window.addEventListener("beforeunload", onBeforeUnload);
    cleanupCallbacks.push(() => window.removeEventListener("beforeunload", onBeforeUnload));
  }

  function createStarBodyGeometry() {
    const outerRadius = 1.55;
    const innerRadius = outerRadius * 0.5;
    const bodyDepth = 0.58;

    const alternatingPoints = [];
    for (let index = 0; index < 10; index += 1) {
      const angle = -Math.PI / 2 + (index * Math.PI) / 5;
      const radius = index % 2 === 0 ? outerRadius : innerRadius;
      alternatingPoints.push(new THREE.Vector3(Math.cos(angle) * radius, Math.sin(angle) * radius, 0));
    }

    const contourCurve = new THREE.CatmullRomCurve3(alternatingPoints, true, "catmullrom", 0.3);
    const contour3d = contourCurve.getPoints(260);
    const contour = contour3d.map((point) => new THREE.Vector2(point.x, point.y));

    const shape = new THREE.Shape(contour);
    const geometry = new THREE.ExtrudeGeometry(shape, {
      depth: bodyDepth,
      steps: 1,
      bevelEnabled: true,
      bevelThickness: 0.12,
      bevelSize: 0.09,
      bevelSegments: 8,
      curveSegments: 64,
    });

    geometry.translate(0, 0, -bodyDepth / 2);
    const position = geometry.attributes.position;
    const maxRadius = outerRadius * 1.08;
    const frontEdge = bodyDepth / 2;
    const backEdge = -bodyDepth / 2;
    const domeLift = 0.12;

    for (let i = 0; i < position.count; i += 1) {
      const x = position.getX(i);
      const y = position.getY(i);
      let z = position.getZ(i);
      const radial = Math.sqrt(x * x + y * y);
      const radialFactor = Math.max(0, 1 - Math.pow(Math.min(1, radial / maxRadius), 1.32));
      if (z > 0) {
        z += radialFactor * domeLift;
      } else if (z < 0) {
        z -= radialFactor * domeLift;
      }
      if (z > frontEdge + domeLift) {
        z = frontEdge + domeLift;
      }
      if (z < backEdge - domeLift) {
        z = backEdge - domeLift;
      }
      position.setZ(i, z);
    }
    position.needsUpdate = true;
    geometry.computeVertexNormals();
    geometry.center();
    return geometry;
  }

  function buildFace() {
    faceGroup = new THREE.Group();
    faceGroup.rotation.z = Math.PI;
    starMesh.add(faceGroup);

    leftEyeGroup = new THREE.Group();
    rightEyeGroup = new THREE.Group();
    leftEyeGroup.position.set(-0.38, 0.2, 0.36);
    rightEyeGroup.position.set(0.38, 0.2, 0.36);

    const eyeGeometry = new THREE.SphereGeometry(0.16, 28, 24);
    const leftEye = new THREE.Mesh(eyeGeometry, eyeWhiteMaterial);
    const rightEye = new THREE.Mesh(eyeGeometry, eyeWhiteMaterial);
    leftEye.scale.set(1, 0.82, 0.44);
    rightEye.scale.set(1, 0.82, 0.44);
    leftEyeGroup.add(leftEye);
    rightEyeGroup.add(rightEye);

    const pupilGeometry = new THREE.SphereGeometry(0.078, 22, 18);
    leftPupil = new THREE.Mesh(pupilGeometry, pupilMaterial);
    rightPupil = new THREE.Mesh(pupilGeometry, pupilMaterial);
    leftPupil.scale.set(1, 1.05, 0.5);
    rightPupil.scale.set(1, 1.05, 0.5);
    leftPupil.position.set(0, -0.01, 0.07);
    rightPupil.position.set(0, -0.01, 0.07);
    leftEyeGroup.add(leftPupil);
    rightEyeGroup.add(rightPupil);

    const highlightGeometry = new THREE.SphereGeometry(0.019, 10, 8);
    const leftHighlight = new THREE.Mesh(highlightGeometry, new THREE.MeshBasicMaterial({ color: 0xffffff }));
    const rightHighlight = leftHighlight.clone();
    leftHighlight.position.set(-0.022, 0.026, 0.084);
    rightHighlight.position.set(-0.022, 0.026, 0.084);
    leftEyeGroup.add(leftHighlight);
    rightEyeGroup.add(rightHighlight);

    const eyelidGeometry = new THREE.SphereGeometry(0.165, 24, 16, 0, Math.PI * 2, 0, Math.PI * 0.43);
    leftEyelid = new THREE.Mesh(eyelidGeometry, eyelidMaterial);
    rightEyelid = new THREE.Mesh(eyelidGeometry, eyelidMaterial);
    leftEyelid.scale.set(1, 0.82, 0.46);
    rightEyelid.scale.set(1, 0.82, 0.46);
    leftEyelid.position.set(0, 0.092, 0.006);
    rightEyelid.position.set(0, 0.092, 0.006);
    leftEyeGroup.add(leftEyelid);
    rightEyeGroup.add(rightEyelid);

    const mouthGeometry = new THREE.RingGeometry(0.08, 0.105, 34, 1, Math.PI * 0.1, Math.PI * 0.8);
    mouth = new THREE.Mesh(mouthGeometry, mouthMaterial);
    mouth.position.set(0, -0.1, 0.48);
    mouth.rotation.z = Math.PI;

    const cheekGeometry = new THREE.CircleGeometry(0.11, 28);
    leftCheek = new THREE.Mesh(cheekGeometry, cheekMaterial);
    rightCheek = new THREE.Mesh(cheekGeometry, cheekMaterial);
    leftCheek.position.set(-0.55, 0.04, 0.45);
    rightCheek.position.set(0.55, 0.04, 0.45);
    leftCheek.scale.set(1, 0.56, 1);
    rightCheek.scale.set(1, 0.56, 1);

    faceGroup.add(leftEyeGroup, rightEyeGroup, mouth, leftCheek, rightCheek);
    applyFacePreset();
  }

  function bindControls() {
    modeButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const nextMode = button.dataset.mode;
        if (nextMode !== "silhouette" && nextMode !== "material") {
          return;
        }
        viewState.mode = nextMode;
        if (nextMode === "silhouette") {
          viewState.camera = "front";
        }
        applyViewState();
        render();
      });
    });

    faceButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const nextFace = button.dataset.face;
        if (!["A", "B"].includes(nextFace)) {
          return;
        }
        viewState.face = nextFace;
        applyFacePreset();
        applyViewState();
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
        render();
      });
    });
  }

  function applyFacePreset() {
    if (!faceGroup) {
      return;
    }
    const isOptionA = viewState.face === "A";

    const eyeScale = isOptionA ? 1 : 0.88;
    const eyeY = isOptionA ? 0.2 : 0.18;
    const eyeSpacing = isOptionA ? 0.38 : 0.35;
    leftEyeGroup.position.set(-eyeSpacing, eyeY, 0.36);
    rightEyeGroup.position.set(eyeSpacing, eyeY, 0.36);
    leftEyeGroup.scale.set(eyeScale, eyeScale, eyeScale);
    rightEyeGroup.scale.set(eyeScale, eyeScale, eyeScale);

    leftPupil.position.y = isOptionA ? -0.012 : -0.022;
    rightPupil.position.y = isOptionA ? -0.012 : -0.022;

    leftEyelid.position.y = isOptionA ? 0.096 : 0.086;
    rightEyelid.position.y = isOptionA ? 0.096 : 0.086;

    mouth.scale.set(isOptionA ? 0.82 : 1, isOptionA ? 0.82 : 1.06, 1);
    mouth.position.y = isOptionA ? -0.1 : -0.08;

    leftCheek.scale.set(isOptionA ? 1 : 0.88, 0.56, 1);
    rightCheek.scale.set(isOptionA ? 1 : 0.88, 0.56, 1);
    leftCheek.position.set(-(isOptionA ? 0.55 : 0.5), 0.04, 0.45);
    rightCheek.position.set(isOptionA ? 0.55 : 0.5, 0.04, 0.45);
  }

  function applyViewState() {
    if (viewState.mode === "silhouette") {
      scene.background = new THREE.Color(0xffffff);
      starMesh.material = silhouetteMaterial;
      if (faceGroup) {
        faceGroup.visible = false;
      }
      if (groundShadow) {
        groundShadow.visible = false;
      }
      lights.forEach((light) => {
        light.visible = false;
      });
      setCameraPose("front");
    } else {
      scene.background = new THREE.Color(0xf8f4ff);
      starMesh.material = goldMaterial;
      if (faceGroup) {
        faceGroup.visible = true;
      }
      if (groundShadow) {
        groundShadow.visible = true;
      }
      lights.forEach((light) => {
        light.visible = true;
      });
      setCameraPose(viewState.camera);
    }

    modeButtons.forEach((button) => {
      button.classList.toggle("active", button.dataset.mode === viewState.mode);
      button.classList.toggle("primary", button.dataset.mode === viewState.mode);
    });
    faceButtons.forEach((button) => {
      const isActive = button.dataset.face === viewState.face;
      button.classList.toggle("active", isActive);
      button.classList.toggle("primary", isActive);
      button.disabled = viewState.mode === "silhouette";
    });
    cameraButtons.forEach((button) => {
      const isActive = viewState.mode === "material" && button.dataset.camera === viewState.camera;
      button.classList.toggle("active", isActive);
      button.disabled = viewState.mode === "silhouette";
    });
  }

  function setCameraPose(preset) {
    if (preset === "side") {
      camera.position.set(5.3, 0.05, 0.02);
    } else if (preset === "threeQuarter") {
      camera.position.set(3.75, 0.2, 3.85);
    } else {
      camera.position.set(0, 0.05, 6.45);
    }
    camera.lookAt(0, 0, 0);
  }

  function render() {
    renderer.render(scene, camera);
  }

  function teardown() {
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
