// Browser plugin not available: reproducible Playwright smoke against isolated fixtures.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import readline from "node:readline";
import { chromium } from "playwright";

const output = path.resolve(
  process.env.CUEFLOW_QA_OUTPUT || "/tmp/cueflow-browser-qa",
);
await mkdir(output, { recursive: true });
const child = spawn(
  process.env.CUEFLOW_TEST_PYTHON || "python3",
  ["-m", "scripts.browser_fixture"],
  { stdio: ["pipe", "pipe", "pipe"] },
);
let stderr = "";
child.stderr.on("data", (data) => {
  stderr += data.toString();
});
const lines = readline.createInterface({ input: child.stdout });
let timer;
const fixture = await new Promise((resolve, reject) => {
  timer = setTimeout(
    () => reject(new Error(`Fixture did not start: ${stderr}`)),
    30000,
  );
  lines.on("line", (line) => {
    try {
      const value = JSON.parse(line);
      if (value.url) resolve(value);
    } catch {}
  });
  child.on("exit", (code) =>
    reject(new Error(`Fixture exited ${code}: ${stderr}`)),
  );
}).finally(() => clearTimeout(timer));
let browser;
const errors = [];
const checks = [];
try {
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
  });
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (["error", "warning"].includes(message.type()))
      errors.push(message.text());
  });
  page.on("response", (response) => {
    if (response.status() >= 400)
      errors.push(`${response.status()} ${response.url()}`);
  });
  await page.goto(fixture.url);
  assert.equal(await page.title(), "CueFlow 本地字幕工坊");
  assert.equal(new URL(page.url()).hostname, "127.0.0.1");
  await page
    .locator('[data-job-id="retry"]')
    .getByText(/已中断/)
    .waitFor();
  assert.ok((await page.locator("body").innerText()).includes("从声音"));
  assert.equal(
    await page.locator("vite-error-overlay, nextjs-portal").count(),
    0,
  );
  checks.push(
    "identity, meaningful content, no error overlay, interrupted task restored",
  );

  await page.locator("#settingsPanel summary").click();
  await page.locator("#setting-model_path").fill(fixture.model);
  await page.locator("#settingsSave").click();
  await page.locator("#settingsSave").waitFor({ state: "visible" });
  await page.waitForFunction(
    () =>
      !document.getElementById("settingsSave").disabled &&
      document.getElementById("setting-model_path").value.length > 0,
  );
  assert.equal(
    await page.locator("#setting-model_path").inputValue(),
    fixture.model,
  );
  const config = await (
    await page.request.get(`${fixture.url}/api/config`)
  ).json();
  assert.equal(config.resources.model_path, fixture.model);
  assert.equal(config.effective.model_path.source, "saved");
  checks.push(
    "settings save through CSRF-protected route and effective shared resource state",
  );

  await page
    .locator('[data-job-id="retry"]')
    .getByRole("button", { name: "恢复" })
    .click();
  await page
    .locator('[data-job-id="retry"]')
    .getByRole("link", { name: "下载结果" })
    .waitFor();
  const download = await page.request.get(
    fixture.url +
      (await page.locator('[data-job-id="retry"] a').getAttribute("href")),
  );
  assert.ok((await download.text()).includes("浏览器验证"));
  checks.push(
    "manual resume -> complete -> downloadable synthetic translation",
  );
  await page
    .locator('[data-job-id="cancel"]')
    .getByRole("button", { name: "恢复" })
    .click();
  await page
    .locator('[data-job-id="cancel"]')
    .getByRole("button", { name: "取消" })
    .waitFor();
  await page
    .locator('[data-job-id="cancel"]')
    .getByRole("button", { name: "取消" })
    .click();
  await page
    .locator('[data-job-id="cancel"]')
    .getByText(/已取消/)
    .waitFor();
  checks.push("running task -> cancellation -> recoverable cancelled state");
  await page.reload();
  await page.locator('[data-job-id="retry"] a').waitFor();
  await page
    .locator('[data-job-id="cancel"]')
    .getByText(/已取消/)
    .waitFor();
  checks.push("reload preserves task history and saved configuration");

  await page.locator("#reviewSourceInput").setInputFiles({
    name: "browser.srt",
    mimeType: "text/plain",
    buffer: Buffer.from("1\n00:00:00,000 --> 00:00:01,000\nOriginal text\n"),
  });
  await page.locator("#reviewLoadButton").click();
  await page.locator("#reviewWorkspace.active").waitFor();
  await page.locator("#reviewText").fill("Edited browser text");
  await page.locator("#reviewSaveButton").click();
  await page.locator("#reviewResult.visible").waitFor();
  const reviewed = await page.request.get(
    fixture.url +
      (await page.locator("#reviewSourceDownload").getAttribute("href")),
  );
  assert.ok((await reviewed.text()).includes("Edited browser text"));
  checks.push(
    "existing review upload -> edit -> save -> actual revised SRT download",
  );

  await page.locator("#reviewText").fill("Unsaved browser change");
  let unsavedDialog = false;
  page.once("dialog", async (dialog) => {
    unsavedDialog = dialog.type() === "beforeunload";
    await dialog.dismiss();
  });
  await page.reload().catch(() => {});
  assert.ok(unsavedDialog, "Missing unsaved-changes guard");
  assert.equal(
    await page.locator("#reviewText").inputValue(),
    "Unsaved browser change",
  );
  await page.locator("#reviewSaveButton").click();
  await page.waitForFunction(
    () => !document.getElementById("reviewSaveButton").disabled,
  );
  checks.push("unsaved review changes block reload until saved");

  await page
    .locator("#zhInput")
    .setInputFiles({
      name: "zh.srt",
      mimeType: "text/plain",
      buffer: Buffer.from(
        "1\n00:00:00,000 --> 00:00:01,000\n真实网页字幕预览\n",
      ),
    });
  await page.locator("#assButton").click();
  await page.locator("#assResult.visible").waitFor();
  const ass = await page.request.get(
    fixture.url + (await page.locator("#assDownload").getAttribute("href")),
  );
  assert.ok((await ass.text()).includes("真实网页字幕预览"));
  assert.ok(
    await page
      .locator("#assStylePreview")
      .evaluate((image) => image.complete && image.naturalWidth > 0),
  );
  checks.push(
    "reviewed source + Chinese upload -> real ASS generation/libass preview -> download",
  );

  await page.locator("#settingsPanel").evaluate((element) => {
    element.open = true;
  });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(output, "desktop.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  assert.ok(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    "Mobile horizontal overflow",
  );
  await page.screenshot({ path: path.join(output, "mobile.png") });
  await page.locator("#jobsPanel summary").click();
  await page.locator("#jobsPanel summary").click();
  await page.locator("#jobsPanel").scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(output, "mobile-tasks.png") });
  checks.push(
    "desktop 1440x1000 and mobile 390x844 without horizontal overflow; details controls respond",
  );
  assert.deepEqual(errors, [], "Browser console/network errors");
  await writeFile(
    path.join(output, "browser-smoke.json"),
    JSON.stringify(
      {
        browser: "Chromium",
        browser_plugin: "not available",
        url: fixture.url,
        viewports: ["1440x1000", "390x844"],
        provider: "synthetic, no Codex/network model calls",
        checks,
        errors,
        screenshots: ["desktop.png", "mobile.png", "mobile-tasks.png"],
      },
      null,
      2,
    ),
  );
  console.log(JSON.stringify({ checks, errors, output }, null, 2));
} finally {
  if (browser) await browser.close();
  child.stdin.end();
  await new Promise((resolve) => {
    if (child.exitCode !== null) resolve();
    else {
      child.once("exit", resolve);
      setTimeout(() => {
        child.kill("SIGKILL");
        resolve();
      }, 10000).unref();
    }
  });
  lines.close();
}
