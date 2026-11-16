// Shapes observed from the released rea-agents 6.7.0 Ghidra MCP provider.
const array = (value, name) => {
  if (!Array.isArray(value)) throw new Error(`Unsupported REA dossier schema: ${name} must be an array`);
  return value;
};
const string = (value, name) => {
  if (typeof value !== 'string' || !value) throw new Error(`Unsupported REA dossier schema: ${name} must be a nonempty string`);
  return value;
};
const extent = (start, end) => {
  // BasicBlockModel end addresses are exclusive; function-body ranges are inclusive.
  if (!/^0x[0-9a-f]+$/i.test(start) || !/^0x[0-9a-f]+$/i.test(end)) return 0;
  const difference = BigInt(end) - BigInt(start);
  if (difference < 0n || difference > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error('Invalid REA CFG block extent');
  return Number(difference);
};
export function normalizeDossier(proc, evidence, failure = null) {
  const address = string(proc.address, 'inventory.address');
  const external = proc.procedure?.external === true;
  const result = {
    id: address, name: string(proc.value ?? proc.name, 'inventory.value'), address,
    external, thunk: proc.procedure?.thunk === true, thunk_target: proc.procedure?.thunk_target ?? null,
    size: null, instruction_count: null, blocks: [], edges: [], calls: [], mnemonics: [],
    decompile: null, decompile_status: external ? 'external' : failure ? 'error' : 'not_requested',
    coverage: {cfg: external ? 'not_applicable_external' : 'not_requested', assembly: external ? 'not_applicable_external' : 'not_requested', calls: external ? 'not_applicable_external' : 'not_requested'},
    partial: !external, partial_reasons: external ? [] : [failure || 'Function dossier omitted by the configured extraction budget.'],
    limitations: [], truncated: false,
  };
  if (!evidence) return result;
  const data = evidence.normalized_result;
  if (!data || !data.procedure || typeof data.pseudocode !== 'string' || !data.facet_availability) throw new Error('Unsupported REA dossier schema');
  if (data.procedure.address !== address) throw new Error('REA dossier address does not match its inventory entry');
  result.name = string(data.procedure.name, 'procedure.name');
  result.evidence_id = evidence.evidence_id;
  result.partial_reasons = [];
  result.partial = false;
  result.limitations = [...new Set([...(evidence.limitations ?? []), ...(data.limitations ?? []), ...(evidence.relationship_coverage?.diagnostics ?? [])])];
  result.relationship_coverage = evidence.relationship_coverage ?? null;
  const partial = reason => { result.partial = true; result.partial_reasons.push(reason); };
  const facet = name => data.facet_availability[name]?.available === true;
  if (data.procedure.body?.available === true && Number.isSafeInteger(data.procedure.body.total_bytes)) result.size = data.procedure.body.total_bytes;
  const asm = array(data.assembly, 'assembly');
  if (!asm.every(line => typeof line === 'string')) throw new Error('Unsupported REA dossier assembly schema');
  result.instruction_count = asm.length;
  result.mnemonics = asm.slice(0, 100000).map(line => {
    const match = line.match(/^(?:\S+:\s+)?([A-Za-z][A-Za-z0-9_.]*)\b/);
    if (!match) throw new Error('Unsupported REA assembly instruction representation');
    return match[1].toLowerCase();
  });
  result.coverage.assembly = facet('assembly') ? 'complete' : 'unavailable';
  if (!facet('assembly')) partial(data.facet_availability.assembly?.reason ?? 'Assembly unavailable.');
  if (asm.length > 100000) { result.truncated = true; partial('Mnemonic inventory exceeds the 100000-instruction model limit.'); }
  const blocks = array(data.basic_blocks, 'basic_blocks');
  result.blocks = blocks.slice(0, 10000).map(block => {
    const start = string(block.start, 'basic_blocks.start');
    const end = string(block.end, 'basic_blocks.end');
    return {id: start, start, end, size: extent(start, end)};
  });
  if (new Set(result.blocks.map(block => block.id)).size !== result.blocks.length) throw new Error('Duplicate REA CFG block address');
  for (const block of blocks.slice(0, 10000)) for (const successor of array(block.successors, 'basic_blocks.successors')) result.edges.push([block.start, string(successor, 'successor')]);
  const edgesBeforeLimit = result.edges.length;
  result.edges = result.edges.slice(0, 80000);
  result.coverage.cfg = facet('cfg') && blocks.length > 0 ? 'complete' : 'unavailable';
  if (result.coverage.cfg !== 'complete') partial(data.facet_availability.cfg?.reason ?? 'No observed basic blocks were returned for this function.');
  if (blocks.length > 10000 || edgesBeforeLimit > 80000) { result.truncated = true; result.coverage.cfg = 'partial'; partial('CFG exceeds the static artifact model limits.'); }
  result.calls = [...new Set(array(data.callees, 'callees').map(callee => string(callee.address, 'callees.address')))];
  result.coverage.calls = facet('callees') ? 'complete_resolved_only' : 'unavailable';
  if (!facet('callees')) partial(data.facet_availability.callees?.reason ?? 'Callee extraction unavailable.');
  if (facet('pseudocode') && data.pseudocode.trim()) {
    result.decompile = data.pseudocode.slice(0, 32768);
    result.decompile_status = data.pseudocode.length > 32768 ? 'truncated' : 'success';
    if (data.pseudocode.length > 32768) { result.truncated = true; partial('Pseudocode exceeds the 32768-character model limit; complete evidence remains in the raw record.'); }
  } else { result.decompile_status = 'unavailable'; partial(data.facet_availability.pseudocode?.reason ?? 'Pseudocode unavailable or empty.'); }
  if (evidence.relationship_coverage?.status && evidence.relationship_coverage.status !== 'complete') partial(`REA relationship coverage is ${evidence.relationship_coverage.status}.`);
  return result;
}
