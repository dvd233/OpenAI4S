// Fork-only opt-in live PaRoutes acceptance. Imports and analysis enter through the real
// composer; the child exposes read-only Store inspections over owned stdin.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline";
import { authenticate, boundedLogCollector, minimalChildEnvironment, redactSecrets, waitUntil } from "./browser_auth.mjs";

const sourceRoot = path.resolve(process.env.OPENAI4S_SOURCE_ROOT || ".");
const outputRoot = path.resolve(process.env.OPENAI4S_PAROUTES_BROWSER_OUTPUT || "paroutes-browser-evidence");
const expectedSha256 = "c0d1b48379e1ceb1129fba4bf3773f73f27bdb22bb4d468417e6e404d3210c15";
const { chromium } = createRequire(path.join(sourceRoot, "package.json"))("playwright");
fs.mkdirSync(outputRoot, { recursive: true });
const dataDir = fs.mkdtempSync(path.join(outputRoot, "owned-daemon-"));
assert.notEqual(dataDir, path.join(os.homedir(), ".openai4s"));
const helper = fileURLToPath(new URL("./native_paroutes_browser_service.py", import.meta.url));
const evidence = { source_sha: process.env.OPENAI4S_SOURCE_SHA, live_zenodo: true, fixture_boundaries: ["model replies", "standard profile readiness"], production_benchmark_admitted: false, chemical_accuracy_claim: false, phases: [], browser_errors: [], external_browser_requests: [] };
let child, context, browser, token = "", childLogs = () => "", round = 0;

async function unusedPort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  const port = server.address().port;
  await new Promise((resolve) => server.close(resolve));
  assert.notEqual(port, 8760);
  return port;
}

async function start() {
  const port = await unusedPort();
  const replies = new Map();
  let exited = false, exitCode;
  child = spawn(process.env.OPENAI4S_PYTHON || "python", ["-u", helper, "--data-dir", dataDir, "--port", String(port)], {
    cwd: sourceRoot,
    env: minimalChildEnvironment({ PYTHONPATH: sourceRoot, OPENAI4S_DATA_DIR: dataDir, OPENAI4S_PROVIDER: "openai_responses", OPENAI4S_TELEMETRY_ENDPOINT: "http://127.0.0.1:1/telemetry", OPENAI4S_KERNEL_SANDBOX: "auto" }),
    stdio: ["pipe", "pipe", "pipe"],
  });
  childLogs = boundedLogCollector(child.stderr);
  child.stdin.on("error", () => {}); // Closed-child writes still fail inspection/shutdown checks.
  const lines = createInterface({ input: child.stdout });
  lines.on("line", (line) => { try { const value = JSON.parse(line); replies.set(value.ready ? "ready" : value.stopped ? "stopped" : value.inspection, value); } catch {} });
  child.on("error", (error) => replies.set("spawn-error", String(error)));
  child.on("exit", (code) => { exited = true; exitCode = code; });
  await waitUntil("owned daemon ready", () => {
    if (exited || replies.has("spawn-error")) throw new Error("owned daemon exited before readiness");
    return replies.get("ready");
  }, 60000);
  token = fs.readFileSync(path.join(dataDir, "access-token"), "utf8").trim();
  assert.ok(token);
  const base = `http://127.0.0.1:${port}/`;
  context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.on("pageerror", (error) => evidence.browser_errors.push(redactSecrets(String(error), token)));
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (/^https?:$/.test(url.protocol) && url.origin !== new URL(base).origin) {
      evidence.external_browser_requests.push(url.origin);
      await route.abort();
    } else await route.continue();
  });
  await authenticate(page, base, token);
  let inspectionId = 0;
  const inspect = async (frameId) => {
    const id = `inspect-${++round}-${++inspectionId}`;
    child.stdin.write(JSON.stringify({ inspect: true, id, frame_id: frameId }) + "\n");
    return waitUntil("read-only Store inspection", () => replies.get(id), 10000);
  };
  const api = async (suffix, options = {}) => {
    const response = await page.request.fetch(new URL("api/v1" + suffix, base).toString(), options);
    assert.ok(response.ok(), `${suffix} HTTP ${response.status()}: ${redactSecrets(await response.text(), token)}`);
    return response.json();
  };
  const stop = async () => {
    await context.close(); context = null;
    child.stdin.end(JSON.stringify({ stop: true }) + "\n");
    await waitUntil("owned daemon exit", () => exited, 30000);
    assert.equal(exitCode, 0, redactSecrets(childLogs(), token));
    const stopped = replies.get("stopped");
    assert.ok(stopped?.stopped, "normal finalizer must run");
    evidence.phases.push({ phase: "shutdown", observed: stopped.observed, exit_code: exitCode });
    child = null;
  };
  return { page, base, api, inspect, stop };
}

async function openFrame(page, frameId, projectId) {
  await page.evaluate(async ({ frameId, projectId }) => window.openConversation(frameId, projectId), { frameId, projectId });
  if (await page.locator("#rightdock.collapsed").count()) await page.locator(".nb-tray").click();
  await page.locator("#rightdock:not(.collapsed)").waitFor({ state: "visible" });
}

async function send(page, api, frameId, request) {
  await page.locator("#composer").fill(request);
  const received = page.waitForResponse((r) => new URL(r.url()).pathname === `/api/v1/frames/${frameId}/message` && r.request().method() === "POST");
  await page.locator("#send-btn").click();
  const response = await received;
  assert.equal(response.status(), 202, redactSecrets(await response.text(), token));
  const ticket = await response.json();
  assert.ok(ticket.execution_id);
  await waitUntil("Agent turn drained", async () => {
    const state = await api(`/frames/${frameId}/execution-queue`);
    return !state.owner && !(state.queue || []).length;
  }, 120000, 150);
}

function named(snapshot, filename) {
  const rows = snapshot.artifacts.filter((a) => a.filename === filename);
  assert.equal(rows.length, 1, `one captured ${filename}`);
  return rows[0];
}

async function verifyViewer(page, api, input, first, latest, result) {
  const open = async (artifact, version) => page.evaluate(async ({ artifact, version }) => window.openViewer({ id: artifact.artifact_id, filename: artifact.filename, version_id: version.version_id }), { artifact, version });
  await open(result, result.versions[0]);
  await page.locator('[data-f16-provenance="1"]').click();
  await page.locator(".prov-subtab").filter({ hasText: "Code" }).click();
  await waitUntil("real old-version analysis source", async () => (await page.locator(".prov-body").innerText()).includes(`host.artifact_path('${first.version_id}')`));
  const code = await page.locator(".prov-body").innerText();
  assert.ok(!code.includes(`host.artifact_path('${latest.version_id}')`));
  await page.locator(".prov-subtab").filter({ hasText: "Review" }).click();
  await waitUntil("real displayed input label", async () => (await page.locator(".prov-body").innerText()).includes("datasets/n1-targets.txt"));
  const review = await page.locator(".prov-body").innerText();
  await page.screenshot({ path: path.join(outputRoot, `lineage-round-${round}.png`), animations: "disabled" });
  const projected = await api(`/artifacts/${result.artifact_id}/lineage?version=${result.versions[0].version_id}`);
  assert.equal(projected.version_id, result.versions[0].version_id);
  assert.ok(projected.dependency_mappings.inputs.includes("datasets/n1-targets.txt"));
  evidence.phases.push({ phase: "browser-provenance", exact_output_version: projected.version_id, code_names_input: first.version_id, visible_inputs_are_filename_labels: true, review });
  await page.locator('[data-f16-provenance="back"]').click();
  await open(input, first);
  await waitUntil("real target-list preview", async () => (await page.locator("#dock-viewer").innerText()).includes(first.snapshot.split(/\r?\n/)[0]));
  await page.evaluate(async (a) => window.showVersions({ id: a.artifact_id, filename: a.filename }), input);
  await page.locator(".ver-list .ver-row").first().waitFor();
  const displayed = await page.evaluate(() => Array.from(document.querySelectorAll(".ver-row")).map((row) => ({ href: row.querySelector(".ver-acts a")?.getAttribute("href"), source: row.nextElementSibling?.textContent || "" })));
  const oldRow = displayed.find((r) => r.href?.endsWith(encodeURIComponent(first.version_id)));
  const newRow = displayed.find((r) => r.href?.endsWith(encodeURIComponent(latest.version_id)));
  const firstTitle = JSON.parse(first.source).dataset.title;
  const latestTitle = JSON.parse(latest.source).dataset.title;
  assert.ok(oldRow?.source.includes(firstTitle), JSON.stringify(displayed));
  assert.ok(newRow?.source.includes(latestTitle));
  await page.screenshot({ path: path.join(outputRoot, `sources-round-${round}.png`), animations: "disabled" });
  await page.evaluate((versionId) => {
    const row = Array.from(document.querySelectorAll(".ver-row")).find((r) => r.querySelector(".ver-acts a")?.getAttribute("href")?.endsWith(encodeURIComponent(versionId)));
    if (!row?.nextElementSibling) throw new Error("old source row missing");
    row.nextElementSibling.scrollIntoView({ block: "end" });
  }, first.version_id);
  await page.screenshot({ path: path.join(outputRoot, `sources-old-round-${round}.png`), animations: "disabled" });
  evidence.phases.push({ phase: "browser-source-history", first_version: first.version_id, latest_version: latest.version_id, both_source_titles_correct: true });
}

try {
  assert.equal(process.platform, "linux", "native POSIX acceptance must run on supported Linux");
  browser = await chromium.launch({ headless: true });
  const firstDaemon = await start();
  const project = await firstDaemon.api("/projects", { method: "POST", data: { name: "PaRoutes real input acceptance" } });
  const projectId = project.id || project.project_id;
  const frame = await firstDaemon.api("/frames", { method: "POST", data: { project_id: projectId } });
  const frameId = frame.id || frame.frame_id;
  await openFrame(firstDaemon.page, frameId, projectId);
  await send(firstDaemon.page, firstDaemon.api, frameId, "PAROUTES_BROWSER_IMPORT FIRST");
  let inspected = await firstDaemon.inspect(frameId);
  const first = named(inspected, "datasets/n1-targets.txt").versions[0];
  assert.equal(first.snapshot_bytes, 465689);
  assert.equal(first.snapshot_sha256, expectedSha256);
  const reference = new Map();
  for (const line of first.snapshot.split(/\r?\n/).filter((value) => value.length)) {
    assert.ok(!line.includes("\t"), "the selected target list must contain one target per line");
    reference.set(line.length, (reference.get(line.length) || 0) + 1);
  }
  const expectedHistogram = [...reference.entries()].sort((a, b) => a[0] - b[0]).map(([target_length, targets]) => ({ target_length, targets, input_version: first.version_id }));
  assert.ok(expectedHistogram.length > 1);
  await send(firstDaemon.page, firstDaemon.api, frameId, "PAROUTES_BROWSER_IMPORT SECOND");
  inspected = await firstDaemon.inspect(frameId);
  const input = named(inspected, "datasets/n1-targets.txt");
  const latest = input.versions[0];
  assert.notEqual(first.version_id, latest.version_id);
  assert.equal(first.checksum, latest.checksum);
  assert.equal(first.snapshot, latest.snapshot);
  assert.equal(latest.snapshot_sha256, expectedSha256);
  for (const version of [first, latest]) {
    const source = JSON.parse(version.source).dataset;
    assert.equal(source.record_id, "6275421");
    assert.equal(source.record_doi, "10.5281/zenodo.6275421");
    assert.equal(source.file_key, "n1-targets.txt");
    assert.equal(source.declared_size_bytes, 465689);
    assert.equal(source.declared_checksum, "md5:5adae99357cdad829073b197c7813152");
    assert.equal(source.declared_license, "cc-by-4.0");
    assert.ok(source.title.includes("PaRoutes"));
  }
  await send(firstDaemon.page, firstDaemon.api, frameId, `PAROUTES_BROWSER_ANALYSE ${first.version_id}`);
  inspected = await firstDaemon.inspect(frameId);
  const result = named(inspected, "summary.json");
  const summary = result.versions[0];
  assert.deepEqual(JSON.parse(summary.snapshot), expectedHistogram, "independent raw-byte histogram must match the real pandas analysis");
  evidence.phases.push({ phase: "real-paroutes-analysis", input_bytes: first.snapshot_bytes, input_sha256: first.snapshot_sha256, target_rows: expectedHistogram.reduce((sum, row) => sum + row.targets, 0), histogram_bins: expectedHistogram.length, input_version: first.version_id, latest_input_version: latest.version_id, output_version: summary.version_id });
  assert.ok(summary.producing_cell_id);
  assert.equal(inspected.observed.file_reads, 2);
  assert.equal(inspected.observed.metadata_reads, 4);
  assert.deepEqual(summary.inputs, [first.version_id], "exact durable input edge must name the explicitly selected old version");
  await verifyViewer(firstDaemon.page, firstDaemon.api, input, first, latest, result);
  await firstDaemon.stop();
  const reopened = await start();
  const after = await reopened.inspect(frameId);
  const reopenedInput = named(after, "datasets/n1-targets.txt");
  const reopenedResult = named(after, "summary.json");
  assert.equal(reopenedInput.latest_version_id, latest.version_id);
  assert.equal(reopenedInput.versions.find((v) => v.version_id === first.version_id).source, first.source);
  assert.equal(reopenedResult.versions[0].snapshot, summary.snapshot);
  assert.deepEqual(reopenedResult.versions[0].inputs, [first.version_id]);
  assert.equal(after.observed.file_reads, 0, "reopening must not replay imports");
  assert.equal(after.observed.model_fixture_calls, 0, "reopening must not replay model work");
  await openFrame(reopened.page, frameId, projectId);
  await verifyViewer(reopened.page, reopened.api, reopenedInput, first, latest, reopenedResult);
  await reopened.stop();
  evidence.phases.push({ phase: "reopen", exact_input_retained: true, result_bytes_retained: true, no_import_or_model_replay: true });
  assert.deepEqual(evidence.browser_errors, []);
  assert.deepEqual(evidence.external_browser_requests, []);
  evidence.status = "passed";
} catch (error) {
  evidence.status = "failed";
  evidence.error = redactSecrets(String(error?.stack || error), token);
  evidence.daemon_log = redactSecrets(childLogs(), token, "synthetic-fixture-only");
  process.exitCode = 1;
} finally {
  if (context) await context.close().catch(() => {});
  if (browser) await browser.close().catch(() => {});
  if (child) {
    child.stdin.end(JSON.stringify({ stop: true }) + "\n");
    await waitUntil("failure-path child exit", () => child.exitCode !== null || child.signalCode !== null, 30000).catch(() => { child.kill("SIGTERM"); });
  }
  fs.writeFileSync(path.join(outputRoot, "receipt.json"), JSON.stringify(evidence, null, 2) + "\n");
  console.log(JSON.stringify(evidence));
}
