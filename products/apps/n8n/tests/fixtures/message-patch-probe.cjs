"use strict";
// Run the complete compiled handleTestRunError and its real route gate from the
// pinned artifact. External API/version reads are synthetic; no network is used.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const start = source.indexOf("async function testRunRouteGate(");
const end = source.indexOf("async function handleListTestRuns(", start);
assert(start > 0 && end > start);
class ApiError extends Error {
  constructor(status, message) { super(message); this.statusCode = status; this.code = `API_${status}`; }
}
const options = { minMinor: 34, capability: "evaluate", scopeHint: "scope hint", notFoundHint: "not found hint", conflictHint: "conflict hint", postRoute: true };
const sandbox = {
  n8n_errors_1: { N8nApiError: ApiError, getUserFriendlyErrorMessage: error => error.message },
  zod_1: { z: { ZodError: class extends Error {} } },
  TEST_RUN_QUOTA_HINT: "quota hint",
  getN8nApiClient: context => ({ refreshVersion: async () => context.version }),
  n8n_version_1: { versionAtLeast: (version, major, minor) => version.major > major || (version.major === major && version.minor >= minor) },
  Error,
};
vm.createContext(sandbox);
vm.runInContext(source.slice(start, end), sandbox);
const current = { version: { major: 2, minor: 43, version: "2.43.0" } };
const old = { version: { major: 2, minor: 30, version: "2.30.0" } };
const unknown = { version: null };
async function main() {
  let cases = 0;
  for (const status of [402, 403, 404, 405, 409, 500]) {
    for (const context of [current, old, unknown]) {
      const result = await sandbox.handleTestRunError(new ApiError(status, "synthetic backend reason"), context, options);
      assert.equal(result.success, false);
      assert.equal(result.code, `API_${status}`);
      assert(result.error.includes("synthetic backend reason"), JSON.stringify({ status, result }));
      assert.equal(result.error.split("synthetic backend reason").length, 2);
      if (status === 403) assert.equal(result.error, "scope hint n8n: synthetic backend reason");
      if (status === 402) assert.equal(result.error, "quota hint n8n: synthetic backend reason");
      if (status === 409) assert.equal(result.error, "conflict hint n8n: synthetic backend reason");
      if (status === 404 && context === current) assert.equal(result.error, "not found hint n8n: synthetic backend reason");
      if ([404, 405].includes(status) && context === old) assert(result.error.startsWith("The evaluation API requires"));
      cases++;
    }
  }
  for (const message of ["scope hint", "", "   ", undefined, 123]) {
    const error = new ApiError(403, ""); error.message = message;
    assert.equal((await sandbox.handleTestRunError(error, current, options)).error, "scope hint");
    cases++;
  }
  assert.equal((await sandbox.handleTestRunError(new ApiError(403, "  trimmed  "), current, options)).error, "scope hint n8n: trimmed");
  assert.equal((await sandbox.handleTestRunError(new ApiError(409, "original"), current, { ...options, conflictHint: undefined })).error, "original");
  assert.equal((await sandbox.handleTestRunError(new Error("ordinary"), current, options)).error, "ordinary");
  console.log(JSON.stringify({ passed: true, cases: cases + 3, backend: "in-process synthetic; no network" }));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
