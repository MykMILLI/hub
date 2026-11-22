import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

const baseUrl = process.env.HOMEHUB_BASE_URL || "http://127.0.0.1:5000";
const outputDir = path.resolve("docs", "gold-stars-preview");

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1366, height: 900 } });
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

const screenshotPaths = {
  friendly: path.join(outputDir, "phase6-fix-friendly-settled.png"),
  welcoming: path.join(outputDir, "phase6-fix-welcoming-settled.png"),
  delighted: path.join(outputDir, "phase6-fix-delighted-settled.png"),
};

async function clickControl(label) {
  await page.getByRole("button", { name: label }).click();
}

async function settle(ms = 700) {
  await page.waitForTimeout(ms);
}

async function readDebugState() {
  return page.evaluate(() => window.__goldStarLabDebug.getState());
}

await fs.mkdir(outputDir, { recursive: true });
await clickControl("Material");
await clickControl("Front view");
await settle();

await clickControl("Friendly idle");
await settle(820);
const friendly = await readDebugState();
await page.screenshot({ path: screenshotPaths.friendly });

await clickControl("Welcoming open smile");
await settle(820);
const welcoming = await readDebugState();
await page.screenshot({ path: screenshotPaths.welcoming });

await clickControl("Delighted / dimples");
await settle(820);
const delighted = await readDebugState();
await page.screenshot({ path: screenshotPaths.delighted });

await page.getByLabel("Reduced motion override").check();
await settle(250);
await clickControl("Friendly idle");
await settle(420);
const reducedFriendly = await readDebugState();
await clickControl("Welcoming open smile");
await settle(420);
const reducedWelcoming = await readDebugState();
await page.getByLabel("Reduced motion override").uncheck();
await settle(250);

await clickControl("Friendly idle");
await settle(500);
await clickControl("Say hello");
const helloSamples = [];
for (const waitMs of [140, 260, 360, 380, 360, 380]) {
  await settle(waitMs);
  helloSamples.push(await readDebugState());
}

const roundedMouth = helloSamples.map((sample) => Number(sample.current.mouthOpen.toFixed(2)));
const uniqueMouth = new Set(roundedMouth);
const helloRange = Math.max(...roundedMouth) - Math.min(...roundedMouth);
const hasMidPerformance = helloSamples.some((sample) => sample.activePerformance === "hello");

const assertions = [];
assertions.push({
  name: "friendly_has_closed_smile",
  pass:
    friendly.current.mouthOpen <= 0.08 &&
    friendly.applied.closedSmileVisible &&
    !friendly.applied.openMouthVisible,
});
assertions.push({
  name: "welcoming_is_open_and_lifted",
  pass:
    welcoming.current.mouthOpen >= 0.5 &&
    welcoming.current.smile > friendly.current.smile &&
    welcoming.applied.openMouthVisible &&
    welcoming.applied.tongueVisible,
});
assertions.push({
  name: "delighted_is_stronger_than_welcoming",
  pass:
    delighted.current.mouthOpen > welcoming.current.mouthOpen &&
    delighted.current.cheekLift > welcoming.current.cheekLift &&
    delighted.current.eyeSquint > welcoming.current.eyeSquint,
});
assertions.push({
  name: "reduced_motion_keeps_static_expression_changes",
  pass:
    reducedFriendly.current.mouthOpen < 0.1 &&
    reducedWelcoming.current.mouthOpen > 0.5 &&
    reducedWelcoming.current.smile > reducedFriendly.current.smile,
});
assertions.push({
  name: "say_hello_has_multiple_facial_states",
  pass: uniqueMouth.size >= 4 && helloRange > 0.35 && hasMidPerformance,
});
assertions.push({
  name: "no_console_errors",
  pass: consoleErrors.length === 0,
});

const failed = assertions.filter((entry) => !entry.pass);
const report = {
  buildId: friendly.buildId,
  url: `${baseUrl}/gold-stars/preview/character-lab`,
  friendly,
  welcoming,
  delighted,
  reducedFriendly,
  reducedWelcoming,
  helloSamples: helloSamples.map((sample) => ({
    selectedExpression: sample.selectedExpression,
    activePerformance: sample.activePerformance,
    current: sample.current,
  })),
  roundedMouth,
  assertions,
  consoleErrors,
  screenshots: screenshotPaths,
};

const reportPath = path.join(outputDir, "phase6-expression-regression.json");
await fs.writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");

await browser.close();

if (failed.length) {
  throw new Error(`Expression regression check failed: ${failed.map((entry) => entry.name).join(", ")}`);
}

console.log(`Expression regression check passed. Report: ${reportPath}`);
