const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

export function mapJoystickVectorToAction(dx, dy, { deadzone = 0.28, allowDiagonal = true } = {}) {
	const magnitude = Math.hypot(dx, dy);
	if (!Number.isFinite(magnitude) || magnitude < deadzone) return '';
	const angle = Math.atan2(dy, dx);
	const sector = Math.round(angle / (Math.PI / 4));
	if (!allowDiagonal) {
		if (Math.abs(dx) > Math.abs(dy)) {
			return dx < 0 ? 'left' : 'right';
		}
		return dy < 0 ? 'up' : 'down';
	}
	const normalized = ((sector % 8) + 8) % 8;
	const directions = ['right', 'right_down', 'down', 'left_down', 'left', 'left_up', 'up', 'right_up'];
	return directions[normalized] || '';
}

export function createDigitalZoomController(options = {}) {
	const {
		minZoom = 1,
		maxZoom = 4,
		step = 0.1,
		onUpdate = () => {},
	} = options;
	const state = {
		zoom: 1,
		offsetX: 0,
		offsetY: 0,
		dragging: false,
		pointerId: null,
		lastX: 0,
		lastY: 0,
	};

	const publish = () => {
		onUpdate({
			zoom: state.zoom,
			offsetX: state.offsetX,
			offsetY: state.offsetY,
			dragging: state.dragging,
		});
	};

	const clampOffsets = () => {
		const maxOffset = ((state.zoom - 1) / state.zoom) * 50;
		state.offsetX = clamp(state.offsetX, -maxOffset, maxOffset);
		state.offsetY = clamp(state.offsetY, -maxOffset, maxOffset);
	};

	const setZoom = (requested) => {
		state.zoom = clamp(Math.round(requested * 10) / 10, minZoom, maxZoom);
		if (state.zoom <= 1) {
			state.offsetX = 0;
			state.offsetY = 0;
		} else {
			clampOffsets();
		}
		publish();
		return state.zoom;
	};

	const nudgeZoom = (delta) => setZoom(state.zoom + delta);

	const reset = () => {
		state.zoom = 1;
		state.offsetX = 0;
		state.offsetY = 0;
		state.dragging = false;
		state.pointerId = null;
		publish();
	};

	const beginDrag = (pointerId, clientX, clientY) => {
		if (state.zoom <= 1) return false;
		state.dragging = true;
		state.pointerId = pointerId;
		state.lastX = clientX;
		state.lastY = clientY;
		publish();
		return true;
	};

	const moveDrag = (pointerId, clientX, clientY) => {
		if (!state.dragging || state.pointerId !== pointerId || state.zoom <= 1) return false;
		const deltaX = clientX - state.lastX;
		const deltaY = clientY - state.lastY;
		const movementScale = (state.zoom - 1) / state.zoom;
		state.offsetX += deltaX * movementScale * 0.12;
		state.offsetY += deltaY * movementScale * 0.12;
		state.lastX = clientX;
		state.lastY = clientY;
		clampOffsets();
		publish();
		return true;
	};

	const endDrag = (pointerId) => {
		if (!state.dragging) return false;
		if (state.pointerId !== pointerId) return false;
		state.dragging = false;
		state.pointerId = null;
		publish();
		return true;
	};

	publish();

	return {
		getState: () => ({ ...state }),
		minZoom,
		maxZoom,
		step,
		setZoom,
		nudgeZoom,
		reset,
		beginDrag,
		moveDrag,
		endDrag,
	};
}

export function createPtzHoldController(options = {}) {
	const {
		startMove,
		stopMove,
		renewMove,
		now = () => Date.now(),
		setIntervalImpl = (fn, ms) => setInterval(fn, ms),
		clearIntervalImpl = (id) => clearInterval(id),
		minTapMoveMs = 220,
	} = options;

	if (typeof startMove !== 'function' || typeof stopMove !== 'function' || typeof renewMove !== 'function') {
		throw new Error('createPtzHoldController requires startMove, stopMove, and renewMove functions');
	}

	const state = {
		activePointerId: null,
		activeAction: '',
		startedAt: 0,
		releaseRequested: false,
		leaseId: '',
		leaseToken: 0,
		renewTimer: null,
		cameraId: '',
	};

	const clearRenew = () => {
		if (state.renewTimer) {
			clearIntervalImpl(state.renewTimer);
			state.renewTimer = null;
		}
	};

	const scheduleRenew = (cameraId, action, leaseId, leaseSeconds, leaseToken) => {
		clearRenew();
		const delay = Math.max(300, Math.floor((Number(leaseSeconds || 1.4) * 1000) / 2));
		state.renewTimer = setIntervalImpl(async () => {
			if (state.leaseToken !== leaseToken || state.leaseId !== leaseId || state.cameraId !== cameraId) return;
			try {
				await renewMove({ cameraId, action, leaseId });
			} catch (error) {
				clearRenew();
			}
		}, delay);
	};

	const completeStop = async (reason = 'release') => {
		const currentLease = state.leaseId;
		const currentCamera = state.cameraId;
		if (!currentCamera) return { stopped: false };
		clearRenew();
		state.leaseId = '';
		state.activeAction = '';
		state.activePointerId = null;
		state.releaseRequested = false;
		state.startedAt = 0;
		return stopMove({ cameraId: currentCamera, leaseId: currentLease, reason });
	};

	const pointerDown = async ({ pointerId, cameraId, action }) => {
		state.leaseToken += 1;
		const leaseToken = state.leaseToken;
		if (state.cameraId && state.cameraId !== cameraId) {
			await completeStop('camera-switch');
		}
		state.cameraId = cameraId;
		state.activePointerId = pointerId;
		state.activeAction = action;
		state.startedAt = now();
		state.releaseRequested = false;
		const started = await startMove({ cameraId, action });
		if (!started || !started.leaseId) return { started: false };
		if (state.leaseToken !== leaseToken || state.activePointerId !== pointerId || state.cameraId !== cameraId) {
			await stopMove({ cameraId, leaseId: started.leaseId, reason: 'stale-start' });
			return { started: false, stale: true };
		}
		state.leaseId = started.leaseId;
		scheduleRenew(cameraId, action, started.leaseId, started.leaseSeconds, leaseToken);
		if (state.releaseRequested) {
			await pointerRelease({ pointerId, reason: 'queued-release' });
		}
		return { started: true, leaseId: started.leaseId };
	};

	const pointerRelease = async ({ pointerId, reason = 'release' }) => {
		if (state.activePointerId !== pointerId) return { stopped: false, ignored: true };
		state.releaseRequested = true;
		if (!state.leaseId) return { stopped: false, pendingStart: true };
		const elapsed = Math.max(0, now() - state.startedAt);
		const remaining = Math.max(0, minTapMoveMs - elapsed);
		if (remaining > 0) {
			await new Promise((resolve) => setTimeout(resolve, remaining));
		}
		return completeStop(reason);
	};

	const forceStop = async (reason = 'force-stop') => {
		state.releaseRequested = true;
		return completeStop(reason);
	};

	return {
		pointerDown,
		pointerRelease,
		forceStop,
		getState: () => ({ ...state }),
	};
}
