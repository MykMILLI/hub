import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const moduleSource = fs.readFileSync(path.join(__dirname, '../static/js/camera_focus_controls.js'), 'utf8');
const controlsModule = await import(`data:text/javascript,${encodeURIComponent(moduleSource)}`);
const { createDigitalZoomController, createPtzHoldController, mapJoystickVectorToAction } = controlsModule;

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test('digital zoom bounds and reset', () => {
	const updates = [];
	const controller = createDigitalZoomController({
		minZoom: 1,
		maxZoom: 4,
		onUpdate: (value) => updates.push(value),
	});
	controller.setZoom(5.3);
	assert.equal(controller.getState().zoom, 4);
	controller.setZoom(0.2);
	assert.equal(controller.getState().zoom, 1);
	controller.setZoom(2.5);
	controller.beginDrag(1, 100, 100);
	controller.moveDrag(1, 320, 260);
	assert.notEqual(controller.getState().offsetX, 0);
	controller.reset();
	assert.equal(controller.getState().zoom, 1);
	assert.equal(controller.getState().offsetX, 0);
	assert.equal(controller.getState().offsetY, 0);
	assert.ok(updates.length >= 4);
});

test('digital zoom drag remains bounded', () => {
	const controller = createDigitalZoomController({ minZoom: 1, maxZoom: 4 });
	controller.setZoom(4);
	controller.beginDrag(3, 0, 0);
	controller.moveDrag(3, 5000, -5000);
	const state = controller.getState();
	assert.ok(state.offsetX <= 40 && state.offsetX >= -40);
	assert.ok(state.offsetY <= 40 && state.offsetY >= -40);
});

test('digital zoom operations do not trigger PTZ API calls', async () => {
	let ptzCalls = 0;
	const zoom = createDigitalZoomController();
	const ptz = createPtzHoldController({
		startMove: async () => {
			ptzCalls += 1;
			return { leaseId: 'lease-z', leaseSeconds: 1.4 };
		},
		stopMove: async () => ({ ok: true }),
		renewMove: async () => ({ ok: true }),
	});
	zoom.setZoom(3.2);
	zoom.beginDrag(1, 40, 40);
	zoom.moveDrag(1, 140, 120);
	zoom.endDrag(1);
	zoom.reset();
	assert.equal(ptzCalls, 0);
	await ptz.forceStop('no-op');
});

test('joystick maps vectors to all 8 directions with deadzone', () => {
	assert.equal(mapJoystickVectorToAction(0, 0), '');
	assert.equal(mapJoystickVectorToAction(0.05, 0.05), '');
	assert.equal(mapJoystickVectorToAction(1, 0), 'right');
	assert.equal(mapJoystickVectorToAction(0.8, 0.8), 'right_down');
	assert.equal(mapJoystickVectorToAction(0, 1), 'down');
	assert.equal(mapJoystickVectorToAction(-0.8, 0.8), 'left_down');
	assert.equal(mapJoystickVectorToAction(-1, 0), 'left');
	assert.equal(mapJoystickVectorToAction(-0.8, -0.8), 'left_up');
	assert.equal(mapJoystickVectorToAction(0, -1), 'up');
	assert.equal(mapJoystickVectorToAction(0.8, -0.8), 'right_up');
	assert.equal(mapJoystickVectorToAction(0.8, -0.8, { allowDiagonal: false }), 'up');
});

test('ptz release before start response resolves triggers queued stop', async () => {
	const events = [];
	let resolveStart;
	const startPromise = new Promise((resolve) => {
		resolveStart = resolve;
	});
	const controller = createPtzHoldController({
		startMove: async () => {
			events.push('start-called');
			return startPromise;
		},
		stopMove: async () => {
			events.push('stop-called');
			return { ok: true };
		},
		renewMove: async () => {
			events.push('renew-called');
			return { ok: true };
		},
	});
	const down = controller.pointerDown({ pointerId: 10, cameraId: 'front', action: 'up' });
	const earlyRelease = controller.pointerRelease({ pointerId: 10, reason: 'release' });
	await earlyRelease;
	resolveStart({ leaseId: 'lease-a', leaseSeconds: 1.4 });
	await down;
	await wait(260);
	assert.ok(events.includes('stop-called'));
});

test('ptz stale start response is stopped and does not replace current movement', async () => {
	let firstResolve;
	const firstStart = new Promise((resolve) => {
		firstResolve = resolve;
	});
	const secondStart = Promise.resolve({ leaseId: 'lease-2', leaseSeconds: 1.4 });
	const stopEvents = [];
	let startCount = 0;
	const controller = createPtzHoldController({
		startMove: async () => {
			startCount += 1;
			return startCount === 1 ? firstStart : secondStart;
		},
		stopMove: async ({ leaseId, reason }) => {
			stopEvents.push({ leaseId, reason });
			return { ok: true };
		},
		renewMove: async () => ({ ok: true }),
	});
	const p1 = controller.pointerDown({ pointerId: 1, cameraId: 'front', action: 'left' });
	await controller.pointerDown({ pointerId: 2, cameraId: 'front', action: 'right' });
	firstResolve({ leaseId: 'lease-1', leaseSeconds: 1.4 });
	await p1;
	await wait(50);
	assert.ok(stopEvents.some((item) => item.leaseId === 'lease-1' && item.reason === 'stale-start'));
	assert.equal(controller.getState().leaseId, 'lease-2');
	await controller.forceStop('cleanup');
});

test('ptz force stop works on blur/visibility style interruption', async () => {
	let stopCount = 0;
	const controller = createPtzHoldController({
		startMove: async () => ({ leaseId: 'lease-force', leaseSeconds: 1.4 }),
		stopMove: async () => {
			stopCount += 1;
			return { ok: true };
		},
		renewMove: async () => ({ ok: true }),
	});
	await controller.pointerDown({ pointerId: 5, cameraId: 'front', action: 'down' });
	await controller.forceStop('emergency');
	assert.equal(stopCount, 1);
	assert.equal(controller.getState().leaseId, '');
});
