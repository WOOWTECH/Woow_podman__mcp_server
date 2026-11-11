"use strict";
// Execute the complete published handler + real official dispatcher. Only transport,
// configuration, exposure and trigger detection are replaced with in-process fixtures.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const noNetwork = () => { throw new Error("native fixture must not open network connections"); };
require("node:net").Socket.prototype.connect = noNetwork;
require("node:tls").connect = noNetwork;
require("node:http").request = noNetwork;
require("node:https").request = noNetwork;
globalThis.fetch = noNetwork;
const upstream = path.join(process.argv[4], "dist/mcp/handlers-n8n-manager.js");
const nativeRequire = Module.createRequire(upstream);
const [handlerPath, scenario] = process.argv.slice(2);
const { z } = nativeRequire(scenario === "formats-zod3" ? "zod/v3" : "zod");
const prefix = "Input validation error: Invalid arguments for tool execute_workflow: ";
const schemas = [
  z.object({ type: z.literal("chat"), chatInput: z.string() }).strict(),
  z.object({ type: z.literal("form"), formData: z.record(z.unknown()) }).strict(),
  z.object({ type: z.literal("webhook"), webhookData: z.record(z.unknown()) }).strict(),
];
const taggedSchema = z.object({ inputs: z.discriminatedUnion("type", schemas).optional() });
const untaggedSchema = z.object({ inputs: z.union(schemas.map(schema => schema.omit({ type: true }))).optional() });
const valid = { json: { executionId: "synthetic-execution" }, text: "{}", isError: false };
const rejection = () => {
  const parsed = taggedSchema.safeParse({ inputs: { chatInput: "synthetic" } });
  assert.equal(parsed.success, false);
  return { text: prefix + parsed.error.message, isError: true };
};
const access = {
  ...nativeRequire("./official-mcp-access"),
  getOfficialMcpClient: context => context.nativeClient,
};
const exposure = {
  publicApiMatchesContext: () => true,
  withMcpExposure: async (options, callback) => options.context.denyExposure
    ? { success: false, code: "OPERATION_DISABLED", error: "fixture exposure denied" } : callback(),
};
function compile(source, filename, overrides) {
  const module = new Module(filename);
  module.filename = filename;
  module.paths = Module._nodeModulePaths(path.dirname(upstream));
  module.require = name => Object.hasOwn(overrides, name) ? overrides[name] : nativeRequire(name);
  module._compile(source, filename);
  return module.exports;
}
const official = compile(fs.readFileSync(path.join(path.dirname(upstream), "handlers-official-tools.js"), "utf8"),
  path.join(path.dirname(upstream), "fixture-official.js"), {
    "./official-mcp-access": access,
    "../services/mcp-exposure": exposure,
    "./handlers-n8n-manager": {},
  });
const handler = compile(fs.readFileSync(handlerPath, "utf8"), upstream, {
  "./official-mcp-access": access,
  "./handlers-official-tools": official,
  "../services/mcp-exposure": exposure,
  "../services/n8n-api-client": { N8nApiClient: class { constructor(config) { return config.fixtureClient; } } },
  "../config/n8n-api": { getN8nApiConfigFromContext: context => ({ fixtureClient: context.apiClient, baseUrl: context.n8nApiUrl }) },
  "../types/instance-context": { validateInstanceContext: () => ({ valid: true }) },
  "../triggers": {
    detectTriggerFromWorkflow: workflow => ({ detected: true, trigger: { type: workflow.kind, node: { name: "Start" } } }),
    classifyTriggerNode: node => node.kind,
  },
});
let serial = 0;
function client(format, callback) {
  return {
    calls: [], writes: 0,
    capabilities: async () => ({ reachable: true, toolNames: ["execute_workflow", "test_workflow", "prepare_workflow_pin_data"] }),
    async callTool(tool, args, options) {
      this.calls.push({ tool, args: structuredClone(args), options });
      assert.equal(options.idempotent, tool === "prepare_workflow_pin_data");
      if (callback) return callback.call(this, tool, args, options);
      const result = (format === "tagged" ? taggedSchema : untaggedSchema).safeParse(args);
      if (!result.success) return { text: prefix + result.error.message, isError: true };
      this.writes++;
      return valid;
    },
  };
}
function context(nativeClient, kind = "chat") {
  return { nativeClient, instanceId: `fixture-${++serial}`, n8nApiUrl: "http://fixture.invalid", n8nApiKey: "synthetic",
    apiClient: { getWorkflow: async () => ({ kind, nodes: [{ name: "Start", kind }] }) } };
}
const inputs = kind => kind === "chat" ? { message: "synthetic" } : { data: { synthetic: true } };
const run = (ctx, args = inputs("chat")) => handler.handleTestWorkflow({ workflowId: "owned", method: "direct", ...args }, ctx);
async function main() {
  if (scenario === "formats" || scenario === "formats-zod3") {
    for (const kind of ["chat", "form", "webhook"]) {
      for (const format of ["tagged", "untagged"]) {
        const native = client(format), ctx = context(native, kind);
        const args = { ...inputs(kind), executionMode: "production", timeoutMs: 30000, triggerNodeName: "Start" };
        if (kind === "webhook") Object.assign(args, { httpMethod: "PUT", headers: { "x-synthetic": "fixture" } });
        for (let i = 0; i < 2; i++) {
          const result = await run(ctx, args);
          assert.equal(result.success, true, JSON.stringify(result));
          assert.equal(result.executionId, "synthetic-execution");
          assert.equal(result.backend, "official-mcp");
        }
        assert.equal(native.calls.length, format === "tagged" ? 3 : 2);
        assert.equal(native.writes, 2, "schema negotiation must not execute twice");
        assert.equal(native.calls[0].args.inputs.type, undefined);
        const last = native.calls.at(-1);
        assert.equal(last.args.inputs.type, format === "tagged" ? kind : undefined);
        assert.equal(last.args.executionMode, "production");
        assert.equal(last.args.triggerNodeName, "Start");
        assert.equal(last.options.timeoutMs, 30000);
        const payload = { ...last.args.inputs }; delete payload.type;
        assert.deepEqual(payload, kind === "chat" ? { chatInput: "synthetic" } : kind === "form"
          ? { formData: { synthetic: true } }
          : { webhookData: { method: "PUT", body: { synthetic: true }, headers: { "x-synthetic": "fixture" } } });
      }
    }
  } else if (scenario === "client-isolation") {
    const old = client("tagged"), fresh = client("untagged"), ctx = context(old);
    assert.equal((await run(ctx)).success, true);
    ctx.nativeClient = fresh; // same REST client/context, rotated official client
    assert.equal((await run(ctx)).success, true);
    assert.equal(fresh.calls.length, 1);
    assert.equal(fresh.calls[0].args.inputs.type, undefined);
    const anotherContext = context(old);
    assert.equal((await run(anotherContext)).success, true);
    assert.equal(old.calls.length, 3, "cache follows official client, not context");
    const replacement = client("tagged"); ctx.nativeClient = replacement;
    assert.equal((await run(ctx)).success, true);
    assert.equal(replacement.calls.length, 2, "new client renegotiates");
  } else if (scenario === "no-inputs") {
    const native = client("tagged"), ctx = context(native);
    assert.equal((await run(ctx, {})).success, true);
    assert.equal((await run(ctx)).success, true);
    assert.equal(native.calls.length, 3, "input-less success must not cache schema");
    assert.equal(Object.hasOwn(native.calls[0].args, "inputs"), false);
  } else if (scenario === "failed-retry") {
    const first = rejection();
    const native = client(null, function () { return this.calls.length === 1 ? first : { text: "denied", json: { error: "denied" }, isError: true }; });
    const result = await run(context(native));
    assert.equal(native.calls.length, 2);
    assert.equal(result.error, "denied");
    assert.equal(result.code, "OFFICIAL_MCP_ERROR");
  } else if (scenario === "retry-timeout") {
    const first = rejection();
    const native = client(null, function () {
      if (this.calls.length === 1) return first;
      this.writes++;
      throw new Error("synthetic timeout after execution");
    });
    const result = await run(context(native));
    assert.equal(native.calls.length, 2);
    assert.equal(native.writes, 1, "an uncertain execution must not trigger a third call");
    assert.match(result.error, /synthetic timeout after execution/);
    assert.notEqual(result.code, "INVALID_ARGS");
    assert.equal(result.success, false);
  } else if (scenario === "failed-retry-not-cached") {
    const first = rejection(), normal = client("tagged");
    const native = client(null, function (tool, args, options) {
      if (this.calls.length === 1) return first;
      if (this.calls.length === 2) return { text: "failed", isError: true };
      return normal.callTool(tool, args, options);
    }), ctx = context(native);
    assert.equal((await run(ctx)).success, false);
    assert.equal((await run(ctx)).success, true);
    assert.equal(native.calls.length, 4);
    assert.equal(native.calls[2].args.inputs.type, undefined);
  } else if (scenario === "changed-client-in-flight") {
    const replacement = client("tagged");
    let ctx;
    const native = client(null, () => { ctx.nativeClient = replacement; return rejection(); });
    ctx = context(native);
    assert.equal((await run(ctx)).success, false);
    assert.equal(native.calls.length, 1);
    assert.equal(replacement.calls.length, 0);
  } else if (scenario === "cached-error-no-retry") {
    const native = client("tagged"), ctx = context(native);
    assert.equal((await run(ctx)).success, true);
    native.callTool = async function (tool, args) { this.calls.push({ args }); return rejection(); };
    assert.equal((await run(ctx)).success, false);
    assert.equal(native.calls.length, 3);
  } else if (scenario === "exposure-denied") {
    const native = client("tagged"), ctx = context(native); ctx.denyExposure = true;
    assert.equal((await run(ctx, { ...inputs("chat"), exposeToMcp: true })).code, "OPERATION_DISABLED");
    assert.equal(native.calls.length, 0);
  } else if (scenario === "other-methods") {
    const native = client("tagged"), ctx = context(native);
    assert.equal((await run(ctx, { method: "prepare" })).success, true);
    assert.equal((await run(ctx, { method: "pinned", pinData: { Start: [{ json: {} }] } })).success, true);
    assert.deepEqual(native.calls.map(c => c.tool), ["prepare_workflow_pin_data", "test_workflow"]);
    assert.equal((await run(ctx)).success, true);
    assert.equal(native.calls.length, 4, "non-direct calls must not cache format");
  } else {
    let raw = rejection();
    const issues = JSON.parse(raw.text.slice(prefix.length));
    if (scenario === "wrong-path") issues[0].path = ["data", "type"];
    else if (scenario === "wrong-code") issues[0].code = "custom";
    else if (scenario === "wrong-discriminator") issues[0].discriminator = "other";
    else if (scenario === "wrong-note") issues[0].note = "unrelated union";
    else if (scenario === "nested-union-errors") issues[0].errors = [[{ code: "custom" }]];
    else if (scenario === "wrong-options") issues[0].options = ["chat", "form", "other"];
    else if (scenario === "multiple-issues") issues.push({ code: "custom", path: ["workflowId"], message: "invalid" });
    raw.text = prefix + JSON.stringify(issues);
    if (scenario === "business-error") raw = { json: { success: false, error: raw.text }, text: JSON.stringify({ error: raw.text }), isError: true };
    if (scenario === "plain-phrase") raw.text = "Invalid discriminator value";
    if (scenario === "wrong-tool") raw.text = raw.text.replace("execute_workflow", "test_workflow");
    if (scenario === "malformed-json") raw.text = prefix + "[broken";
    if (scenario === "truncated-json") raw.text = prefix + " ".repeat(2000) + JSON.stringify(issues);
    if (scenario === "authorization") raw = { text: "forbidden", json: { error: "forbidden" }, isError: true };
    if (scenario === "execution-error") raw = { text: "{}", json: { status: "error", error: "Invalid discriminator value" } };
    const native = client(null, () => { if (scenario === "timeout") throw new Error("synthetic timeout"); return raw; });
    const result = await run(context(native));
    assert.equal(result.success, false);
    assert.equal(native.calls.length, 1, "unrelated rejection must never duplicate dispatch");
    if (["wrong-path", "wrong-code", "wrong-options", "wrong-discriminator", "wrong-note", "nested-union-errors", "multiple-issues", "wrong-tool", "malformed-json"].includes(scenario)) {
      assert.equal(result.error, raw.text, "preserve original validation error exactly");
    }
  }
  console.log(JSON.stringify({ scenario, passed: true, backend: "in-process synthetic client; no network" }));
}
main().then(() => process.exit(0)).catch(error => { console.error(error); process.exit(1); });
