"use strict";
// Execute the complete pinned handleTestWorkflow, including its route/decorate
// and catch paths. Only schema parsing, trigger discovery and the official
// dispatcher boundary are synthetic. No backend or network connection is used.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const [file, scenario] = process.argv.slice(2);
const source = fs.readFileSync(file, "utf8");
const start = source.indexOf("async function handleTestWorkflow(");
const end = source.indexOf("\nasync function ", start + 20);
assert(start > 0 && end > start);
let calls = 0, executions = 0;
const seen = [];
const first = { success: false, code: "INVALID_ARGS", error:
  "Input validation error: Invalid arguments for tool execute_workflow: " + JSON.stringify([
    { code: "invalid_union_discriminator", path: ["inputs", "type"], options: ["chat", "form", "webhook"] },
  ]) };
const second = scenario === "retry-backend-error"
  ? { success: false, code: "EXECUTION_FAILED", error: "synthetic execution failed", data: { executionId: "owned" } }
  : { success: false, code: "TIMEOUT", error: "synthetic timeout after execution started", details: { executionMayHaveStarted: true } };
const client = { getWorkflow: async () => ({ nodes: [{ name: "Start" }] }) };
const nativeClient = {};
const sandbox = {
  Error,
  nativeClientFixture: nativeClient,
  official_mcp_access_1: { getOfficialMcpClient: () => nativeClient },
  testWorkflowSchema: { parse: value => value },
  getN8nApiClient: () => client,
  ensureApiConfigured: () => client,
  agents_action_map_1: { PINNED_TIMEOUT_MS: 30000 },
  OFFICIAL_TEST_ACTION: "test_workflow",
  OFFICIAL_TEST_METHODS: new Set(["direct"]),
  mcp_exposure_1: { publicApiMatchesContext: () => true, withMcpExposure: async (_options, callback) => callback() },
  handlers_official_tools_1: { callOfficialTool: async (_context, aliases, args, _timeout, _action, idempotent) => {
    calls++;
    assert.equal(calls <= 2, true, "never dispatch a third retry");
    assert.equal(aliases[0], "execute_workflow");
    assert.equal(idempotent, false);
    seen.push(args);
    if (calls === 1) return scenario === "unrelated-error" ? second : first;
    executions++; // the accepted tagged request has already started execution
    if (scenario === "retry-throws") throw new Error("synthetic transport lost after execution started");
    if (scenario === "retry-success") return { success: true, data: { executionId: "owned" } };
    return second;
  } },
  __importStar: value => value,
  require: name => {
    assert.equal(name, "../triggers");
    return { detectTriggerFromWorkflow: () => ({ trigger: { type: "chat", node: { name: "Start" } } }) };
  },
  zod_1: { z: { ZodError: class extends Error {} } },
  n8n_errors_1: { N8nApiError: class extends Error {} },
};
vm.createContext(sandbox);
const cacheDeclaration = source.match(/^const woowDirectInputFormats = new WeakMap\(\);$/m);
assert(cacheDeclaration);
vm.runInContext(cacheDeclaration[0] + '\n' + source.slice(start, end), sandbox);
(async () => {
  const result = await sandbox.handleTestWorkflow({ workflowId: "owned", method: "direct", message: "synthetic" }, {});
  assert.equal(calls, scenario === "unrelated-error" ? 1 : 2);
  assert.equal(executions, scenario === "unrelated-error" ? 0 : 1);
  assert.equal(seen[0].inputs.type, undefined);
  if (calls === 2) assert.equal(seen[1].inputs.type, "chat");
  const cached = vm.runInContext('woowDirectInputFormats.get(nativeClientFixture)', sandbox);
  assert.equal(cached, scenario === "retry-success" ? true : undefined, "failed dispatch must not cache a schema");
  if (scenario === "retry-success") {
    assert.equal(result.success, true);
    assert.equal(result.executionId, "owned");
  } else if (scenario === "retry-throws") {
    assert.equal(result.success, false);
    assert.equal(result.error, "synthetic transport lost after execution started");
  } else {
    assert.equal(result.success, false);
    assert.equal(result.code, second.code);
    assert.equal(result.error, second.error);
    assert.equal(result.details, second.details);
    assert.equal(result.data, second.data);
  }
  assert.equal(result.method, "direct");
  assert.equal(result.backend, "official-mcp");
  console.log(JSON.stringify({ scenario, passed: true, calls, executions, cached: cached ?? null }));
})().catch(error => { console.error(error); process.exitCode = 1; });
