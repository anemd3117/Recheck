import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeManagedMembers} from './managed.mjs';

const sha = 'a'.repeat(64);
function evidence() {
  return {evidence_id: 'ev-managed', normalized_result: {
    artifact: {sha256: sha}, metadata: {status: 'complete'},
    module: {mvid: 'module-id'}, coverage: {state: 'complete', issues: []}, limitations: [],
    methods: [{token: '0x06000001', declaring_type: 'Sample', name: 'Add',
      signature: {parse_status: 'decoded', sha256: 'b'.repeat(64), resolved_sha256: 'e'.repeat(64), kind: 'method'},
      body: {status: 'present', il_sha256: 'c'.repeat(64), normalized_il_sha256: 'd'.repeat(64),
        instruction_count: 3, opcode_counts: {'ldarg.0': 1, 'ldc.i4.1': 1, ret: 1}, anchors: [], exception_regions: []}}],
    call_edges: [],
  }};
}
test('managed CIL remains metadata and never becomes Ghidra pseudocode or CFG', () => {
  const value = normalizeManagedMembers(evidence(), sha, 10000);
  assert.equal(value.functions[0].name, 'Sample.Add');
  assert.equal(value.functions[0].decompile, null);
  assert.deepEqual(value.functions[0].blocks, []);
  assert.equal(value.functions[0].managed_method.body.il_sha256, 'c'.repeat(64));
  assert.equal(value.analysis_kind, 'managed');
  assert.equal(value.partial, false);
});
test('managed evidence cannot be attributed to another input', () => {
  assert.throws(() => normalizeManagedMembers(evidence(), 'e'.repeat(64), 10000), /SHA/);
});
test('omitted managed methods remain partial with explicit counts', () => {
  const input = evidence();
  input.normalized_result.methods.push({...input.normalized_result.methods[0], token: '0x06000002'});
  const value = normalizeManagedMembers(input, sha, 1);
  assert.equal(value.functions.length, 1);
  assert.equal(value.partial, true);
  assert.equal(value.raw_evidence.coverage.inventory_observed, 2);
});
