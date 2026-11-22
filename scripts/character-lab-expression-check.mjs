import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

const baseUrl = process.env.HOMEHUB_BASE_URL || "http://127.0.0.1:5000";
const outputDir = path.resolve("docs", "gold-stars-preview");

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  viewport: { width: 1366, height: 900 },
  recordVideo: { dir: outputDir, size: { width: 1280, height: 720 } },
});
const page = await context.newPage();

const consoleErrors = [];
page.on("pageerror", (error) => {
  consoleErrors.push(String(error));
});
page.on("console", (message) => {
  if (message.type() === "error") {
    consoleErrors.push(message.text());
  }
});

const loginResponse = await page.request.post(`${baseUrl}/api/member-login`, {
  data: { name: "Rachel", pin: "" },
});
if (!loginResponse.ok()) {
  const payload = await loginResponse.text();
  throw new Error(`Unable to establish member session: ${loginResponse.status()} ${payload}`);
}

await page.goto(`${baseUrl}/gold-stars/preview/character-lab`, { waitUntil: "networkidle" });
await page.waitForFunction(() => typeof window.__goldStarLabDebug?.getState === "function");

await fs.mkdir(outputDir, { recursive: true });
await page.getByRole("button", { name: "Material" }).click();
await page.getByRole("button", { name: "Front view" }).click();
await page.waitForTimeout(450);

const timelineStops = [
  { key: "relaxed", progress: 0, label: "Relaxed" },
  { key: "recognition", progress: 20, label: "Recognition" },
  { key: "anticipation", progress: 44, label: "Anticipation" },
  { key: "delight", progress: 70, label: "Delight" },
  { key: "settle", progress: 88, label: "Settle" },
];

const screenshotPaths = {};
const samples = {};
for (const stop of timelineStops) {
  await page.locator("#greetingProgressRange").fill(String(stop.progress));
  await page.waitForTimeout(280);
  const screenshotPath = path.join(outputDir, `phase7-greeting-${stop.key}.png`);
  await page.locator("#characterLabCanvasHost").screenshot({ path: screenshotPath });
  screenshotPaths[stop.key] = screenshotPath;
  samples[stop.key] = await page.evaluate(() => window.__goldStarLabDebug.getState());
}

await page.getByLabel("Reduced motion override").check();
await page.waitForTimeout(220);
await page.locator("#greetingProgressRange").fill("0");
await page.waitForTimeout(240);
const reducedRelaxed = await page.evaluate(() => window.__goldStarLabDebug.getState());
await page.locator("#greetingProgressRange").fill("70");
await page.waitForTimeout(240);
const reducedDelight = await page.evaluate(() => window.__goldStarLabDebug.getState());
await page.getByLabel("Reduced motion override").uncheck();
await page.waitForTimeout(200);

await page.getByRole("button", { name: "Restart" }).click();
await page.waitForTimeout(250);
await page.getByRole("button", { name: "Play greeting" }).click();

const playbackSamples = [];
for (const waitMs of [220, 280, 320, 340, 360, 380, 430]) {
  await page.waitForTimeout(waitMs);
  playbackSamples.push(await page.evaluate(() => window.__goldStarLabDebug.getState()));
}

const smileValues = playbackSamples.map((sample) => Number(sample.current.smile.toFixed(2)));
const mouthValues = playbackSamples.map((sample) => Number(sample.current.mouthOpen.toFixed(2)));
const cheekValues = playbackSamples.map((sample) => Number(sample.current.cheekLift.toFixed(2)));
const eyeValues = playbackSamples.map((sample) => Number(sample.current.eyeSquint.toFixed(2)));
const bodyYValues = playbackSamples.map((sample) => Number(sample.applied.starY.toFixed(2)));

const assertions = [];
assertions.push({
  name: "timeline_relaxed_vs_delight_visibly_different",
  pass:
    samples.delight.current.mouthOpen - samples.relaxed.current.mouthOpen > 0.55 &&
    samples.delight.current.smile - samples.relaxed.current.smile > 0.45 &&
    samples.delight.current.cheekLift - samples.relaxed.current.cheekLift > 0.5 &&
    samples.delight.current.eyeSquint - samples.relaxed.current.eyeSquint > 0.4,
});
assertions.push({
  name: "anticipation_body_dip_exists",
  pass: samples.anticipation.applied.starY < samples.relaxed.applied.starY - 0.08,
});
assertions.push({
  name: "delight_body_rise_exists",
  pass: samples.delight.applied.starY > samples.relaxed.applied.starY + 0.14,
});
assertions.push({
  name: "reduced_motion_keeps_face_change_without_hop",
  pass:
    Math.abs(reducedRelaxed.applied.starY - reducedDelight.applied.starY) < 0.02 &&
    reducedDelight.current.mouthOpen - reducedRelaxed.current.mouthOpen > 0.35 &&
    reducedDelight.current.cheekLift - reducedRelaxed.current.cheekLift > 0.35,
});
assertions.push({
  name: "playback_shows_multi_step_face_and_body_changes",
  pass:
    new Set(mouthValues).size >= 5 &&
    Math.max(...mouthValues) - Math.min(...mouthValues) > 0.55 &&
    Math.max(...smileValues) - Math.min(...smileValues) > 0.5 &&
    Math.max(...cheekValues) - Math.min(...cheekValues) > 0.45 &&
    Math.max(...eyeValues) - Math.min(...eyeValues) > 0.35 &&
    Math.max(...bodyYValues) - Math.min(...bodyYValues) > 0.22,
});
assertions.push({
  name: "no_console_errors",
  pass: consoleErrors.length === 0,
});

const report = {
  buildId: samples.relaxed.buildId,
  url: `${baseUrl}/gold-stars/preview/character-lab`,
  timelineSamples: samples,
  reducedMotionSamples: { reducedRelaxed, reducedDelight },
  playbackSamples: playbackSamples.map((sample) => ({
    progress: sample.progress,
    stage: sample.stage,
    current: sample.current,
    applied: sample.applied,
  })),
  series: { smileValues, mouthValues, cheekValues, eyeValues, bodyYValues },
  assertions,
  consoleErrors,
  screenshots: screenshotPaths,
};

const reportPath = path.join(outputDir, "phase7-greeting-regression.json");
await fs.writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");

await context.close();
await browser.close();

const failed = assertions.filter((entry) => !entry.pass);
if (failed.length) {
  throw new Error(`Greeting regression check failed: ${failed.map((entry) => entry.name).join(", ")}`);
}

console.log(`Greeting regression check passed. Report: ${reportPath}`);
