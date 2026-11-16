// PE/CLI is inspected by REA's execution-free managed provider, never Ghidra.
export function normalizeManagedMembers(evidence, sha256, maximum) {
  const data = evidence?.normalized_result;
  if (data?.artifact?.sha256 !== sha256) throw new Error('Managed artifact SHA-256 mismatch');
  if (!Array.isArray(data.methods) || !Array.isArray(data.call_edges) || !data.coverage)
    throw new Error('Unsupported REA managed member schema');
  if (data.coverage.state === 'unavailable' || ['absent', 'malformed'].includes(data.metadata?.status))
    throw new Error('REA could not decode the selected PE/CLI metadata');
  const reasons = [];
  if (data.coverage.state !== 'complete') reasons.push('Managed metadata coverage is incomplete; inspect retained REA evidence.');
  if (data.methods.length > maximum) reasons.push(`Method inventory capped at ${maximum} of ${data.methods.length}.`);
  const functions = data.methods.slice(0, maximum).map(method => {
    if (typeof method.token !== 'string' || typeof method.name !== 'string' || !method.signature || !method.body)
      throw new Error('Unsupported REA managed method schema');
    const partial = !['present', 'absent'].includes(method.body.status);
    if (partial) reasons.push(`CIL unavailable or incomplete for ${method.token}.`);
    return {
      id: method.token, address: method.token,
      name: `${method.declaring_type ?? '?'}.${method.name}`,
      blocks: [], edges: [], calls: data.call_edges.filter(edge => edge.caller_token === method.token).map(edge => edge.target_token),
      mnemonics: [], decompile: null, decompile_status: 'not_applicable_managed',
      evidence_id: evidence.evidence_id, managed_method: method,
      coverage: {cfg: 'not_collected_managed', assembly: 'not_applicable_managed', calls: data.coverage.state},
      partial, partial_reasons: partial ? ['Managed CIL method body is incomplete.'] : [],
    };
  });
  if (new Set(functions.map(fn => fn.id)).size !== functions.length) throw new Error('Duplicate managed method token');
  return {
    analysis_kind: 'managed', functions, partial: reasons.length > 0, partial_reasons: reasons,
    managed_metadata: {module: data.module, metadata: data.metadata, coverage: data.coverage,
      identity_scope: data.identity_scope, selection: data.selection},
    limitations: [...(data.limitations ?? []), 'Managed signatures, CIL bytes and exception metadata are static observations; no CLR is loaded and target behavior is not executed.'],
    raw_evidence: {coverage: {inventory_observed: data.methods.length, inventory_returned: functions.length,
      inventory_complete: data.methods.length <= maximum, native_functions: 0, managed_methods: functions.length,
      decompiled: 0, cfg_complete: 0}, truncated: data.methods.length > maximum, partial_reasons: reasons},
  };
}
