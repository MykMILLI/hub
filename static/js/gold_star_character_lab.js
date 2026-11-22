import * as THREE from "/static/vendor/three/three.module.js";

(() => {
  const host = document.getElementById("characterLabCanvasHost");
  const fallback = document.getElementById("webglFallback");
  if (!host || !fallback) {
    return;
  }

  const modeButtons = Array.from(document.querySelectorAll("[data-mode]"));
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
  let starMesh;
  let silhouetteMaterial;
  let goldMaterial;
  const lights = [];
  const cleanupCallbacks = [];

  const viewState = {
    mode: "silhouette",
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
    host.appendChild(renderer.domElement);

    goldMaterial = new THREE.MeshStandardMaterial({
      color: 0xffe07a,
      roughness: 0.38,
      metalness: 0.04,
      emissive: 0x5f4200,
      emissiveIntensity: 0.08,
    });
    silhouetteMaterial = new THREE.MeshBasicMaterial({ color: 0x000000 });

    const starGeometry = createStarBodyGeometry();
    starMesh = new THREE.Mesh(starGeometry, silhouetteMaterial);
    starMesh.rotation.z = Math.PI;
    scene.add(starMesh);

    const hemi = new THREE.HemisphereLight(0xfff8de, 0xece6ff, 0.9);
    hemi.position.set(0, 4, 0);
    lights.push(hemi);
    scene.add(hemi);

    const key = new THREE.DirectionalLight(0xfff4cb, 1.18);
    key.position.set(2.8, 3.2, 4.2);
    lights.push(key);
    scene.add(key);

    const fill = new THREE.DirectionalLight(0xf4e9ff, 0.52);
    fill.position.set(-3.4, 2.1, 3.6);
    lights.push(fill);
    scene.add(fill);

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

  function applyViewState() {
    if (viewState.mode === "silhouette") {
      scene.background = new THREE.Color(0xffffff);
      starMesh.material = silhouetteMaterial;
      lights.forEach((light) => {
        light.visible = false;
      });
      setCameraPose("front");
    } else {
      scene.background = new THREE.Color(0xf8f5ff);
      starMesh.material = goldMaterial;
      lights.forEach((light) => {
        light.visible = true;
      });
      setCameraPose(viewState.camera);
    }

    modeButtons.forEach((button) => {
      button.classList.toggle("active", button.dataset.mode === viewState.mode);
      button.classList.toggle("primary", button.dataset.mode === viewState.mode);
    });
    cameraButtons.forEach((button) => {
      const isActive = viewState.mode === "material" && button.dataset.camera === viewState.camera;
      button.classList.toggle("active", isActive);
      button.disabled = viewState.mode === "silhouette";
    });
  }

  function setCameraPose(preset) {
    if (preset === "side") {
      camera.position.set(5.9, 0.0, 0.02);
    } else if (preset === "threeQuarter") {
      camera.position.set(4.2, 0.35, 4.45);
    } else {
      camera.position.set(0, 0.0, 7.1);
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
