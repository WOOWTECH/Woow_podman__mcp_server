import test from 'node:test';
import assert from 'node:assert/strict';
import { capabilityNav, mergeToolItems, disabledAfterToggle, matchesTool, effectiveLabel, toolAvailability, envUpdate, applyFeedback } from './capabilities.js';

test('navigation hides only unsupported operation policy and retains settings/logs', () => {
  const nav = [{ items: ['/tools', '/permissions', '/logs', '/settings'].map((to) => ({ to })) }];
  assert.deepEqual(capabilityNav(nav, { capabilities: { operation_policy: false } })[0].items.map((i) => i.to), ['/tools', '/logs', '/settings']);
  assert.equal(capabilityNav(nav, { capabilities: { operation_policy: true } })[0].items.length, 4);
  assert.equal(capabilityNav(nav, null)[0].items.length, 4);
});

test('runtime union retains legacy catalog, schemas, and saved deny on unrelated toggle', () => {
  const schema = { properties: { resource_selector: { type: 'string' } } };
  const items = mergeToolItems([{ name: 'catalog', enabled: false }], { tools: { items: [
    { name: 'runtime', policy_enabled: true, effective: true, inputSchema: schema },
    { name: 'catalog', policy_enabled: true, effective: false },
  ] } }, ['catalog', 'temporarily_absent']);
  assert.equal(items.length, 2);
  assert.equal(items[0].enabled, false);
  assert.equal(items[1].inputSchema, schema);
  assert.deepEqual(disabledAfterToggle(items, ['catalog', 'temporarily_absent'], 'runtime', true), ['catalog', 'runtime', 'temporarily_absent']);
  assert.deepEqual(disabledAfterToggle(items, ['catalog', 'temporarily_absent'], 'catalog', false), ['temporarily_absent']);
  assert(matchesTool(items[1], 'RESOURCE_SELECTOR'));
});

test('unknown effective is never represented as zero or policy count', () => {
  assert.equal(effectiveLabel(null), 'Unknown');
  assert.equal(effectiveLabel(undefined), 'Unknown');
  assert.equal(effectiveLabel(0), '0');
  assert.equal(toolAvailability({ enabled: true }), 'Availability unknown');
  assert.equal(toolAvailability({ effective: false, reason: 'policy_disabled' }), 'Disabled by saved policy');
});

test('settings deletion explicit and partial/failed apply not success', () => {
  assert.deepEqual(envUpdate({ A: '1', B: '2' }, { B: '3' }), { env: { B: '3' }, env_delete: ['A'] });
  assert.equal(applyFeedback({ status: 'partial', saved: true }), 'error');
  assert.equal(applyFeedback({ status: 'error' }), 'error');
  assert.equal(applyFeedback({ status: 'ok', apply_state: 'failed' }), 'error');
  assert.equal(applyFeedback({ status: 'ok', apply_state: 'applied' }), 'success');
});


test('legacy restart failure is not a successful apply, and save-only remains explicit', () => {
  assert.equal(applyFeedback({success: true, restarted: false}), 'error');
  assert.equal(applyFeedback({saved: true, restarted: false, apply_state: 'not_requested', status: 'ok'}), 'success');
  assert.equal(applyFeedback({saved: true, restarted: false, apply_state: 'failed', status: 'partial'}), 'error');
});
