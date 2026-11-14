import test from 'node:test';
import assert from 'node:assert/strict';
import { profileDisabledNotice, profileOptionLabel } from './profileStatus.js';

test('profiles disabled for capacity or a port conflict are marked in the chooser', () => {
  assert.equal(profileOptionLabel({ id: 'default', label: 'Default', disabled_reason: null }), 'Default');
  assert.equal(profileOptionLabel({ id: 'a', label: 'Worker' }), 'Worker');  // older servers: no field
  assert.equal(profileOptionLabel({ id: 'a', label: 'Worker', disabled_reason: 'memory_capacity' }), 'Worker（記憶體不足，已停用）');
  assert.equal(profileOptionLabel({ id: 'a', label: 'Worker', disabled_reason: 'profile_limit' }), 'Worker（超過帳號上限，已停用）');
  assert.equal(profileOptionLabel({ id: 'a', label: 'Worker', disabled_reason: 'port_conflict' }), 'Worker（port 衝突，已停用）');
  assert.equal(profileOptionLabel({ id: 'a', label: 'Worker', disabled_reason: 'future_reason' }), 'Worker（已停用）');
});

test('the selected disabled profile explains why and how much memory it needs', () => {
  assert.equal(profileDisabledNotice({ label: 'Worker', disabled_reason: null }), '');
  assert.equal(profileDisabledNotice(undefined), '');
  const memory = profileDisabledNotice({ label: 'Worker', disabled_reason: 'memory_capacity', required_memory_mib: 1156 });
  assert.match(memory, /記憶體上限不足/);
  assert.match(memory, /1156 MiB/);
  assert.doesNotMatch(profileDisabledNotice({ disabled_reason: 'memory_capacity' }), /MiB/);
  assert.match(profileDisabledNotice({ disabled_reason: 'port_conflict' }), /port/);
  assert.match(profileDisabledNotice({ disabled_reason: 'profile_limit' }), /帳號上限/);
  assert.match(profileDisabledNotice({ disabled_reason: 'future_reason' }), /停用/);
});
