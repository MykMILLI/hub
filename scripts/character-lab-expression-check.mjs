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

await fs.mkdir(outputDir, { recursive: true });
await page.getByRole("button", { name: "Material" }).click();
await page.getByRole("button", { name: "Front view" }).click();
await page.waitForTimeout(250);

const fallbackScreenshot = path.join(outputDir, "phase8-fallback-static-face.png");
await page.locator("#characterLabCanvasHost").screenshot({ path: fallbackScreenshot });

const actionNames = ["idle", "noticeNewStar", "tapReaction", "collecting", "celebrate", "rewardApproved"];
for (const actionName of actionNames) {
  await page.getByRole("button", { name: actionName }).click();
  await page.waitForTimeout(140);
}

await page.getByLabel("Reduced motion override").check();
await page.waitForTimeout(120);
await page.getByRole("button", { name: "celebrate" }).click();
await page.waitForTimeout(120);
await page.getByLabel("Reduced motion override").uncheck();
await page.waitForTimeout(120);

const state = await page.evaluate(() => window.__goldStarLabDebug.getState());
const adapterMethods = await page.evaluate(() => {
  const adapter = window.__goldStarCharacterAdapter;
  if (!adapter) {
    return [];
  }
  return [
    "idle",
    "noticeNewStar",
    "tapReaction",
    "collecting",
    "celebrate",
    "rewardApproved",
    "setReducedMotion",
    "dispose",
    "getState",
  ].filter((name) => typeof adapter[name] === "function");
});

const assertions = [];
assertions.push({
  name: "adapter_contract_methods_present",
  pass: adapterMethods.length === 9,
});
assertions.push({
  name: "static_face_fallback_flags",
  pass:
    state.face.hasOpenMouth === false &&
    state.face.hasAnimatedEyelids === false &&
    state.face.hasDimples === false &&
    state.face.hasSmileCreases === false &&
    state.face.mouthContours === 1,
});
assertions.push({
  name: "no_console_errors",
  pass: consoleErrors.length === 0,
});

const report = {
  buildId: state.buildId,
  url: `${baseUrl}/gold-stars/preview/character-lab`,
  adapterMethods,
  state,
  assertions,
  consoleErrors,
  screenshot: fallbackScreenshot,
};

const reportPath = path.join(outputDir, "phase8-fallback-regression.json");
await fs.writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");

await context.close();
await browser.close();

const failed = assertions.filter((entry) => !entry.pass);
if (failed.length) {
  throw new Error(`Fallback regression check failed: ${failed.map((entry) => entry.name).join(", ")}`);
}

console.log(`Fallback regression check passed. Report: ${reportPath}`);
