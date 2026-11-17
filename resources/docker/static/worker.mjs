import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import { normalizeDossier } from './normalize.mjs';
import { normalizeManagedMembers } from './managed.mjs';
import workerPackage from './package.json' with { type: 'json' };

const ROOT = '/output';
const versions = {rea: workerPackage.dependencies['rea-agents'], ghidra: '12.1.4', java: '21.0.9+10', node: process.versions.node, worker_contract: 'rea-static-1.0'};
const RAW_LIMIT = 48 * 1024 * 1024;
const RESULT_LIMIT = 12 * 1024 * 1024;
let client, transport, opened = false, deadline, operation, stderr = '', evidenceBytes = 0;
const evidenceFiles = [];
const message = error => String(error?.message ?? error).slice(0, 2000);
const remaining = () => Math.max(1, deadline - Date.now());
const integer = (value, fallback, low, high, name) => {
  const v = value ?? fallback;
  if (!Number.isInteger(v) || v < low || v > high) throw new Error(`${name} must be an integer in ${low}..${high}`);
  return v;
};
async function save(name, data) {
  const json = JSON.stringify(data, null, 2) + '\n';
  const size = Buffer.byteLength(json);
  if (evidenceBytes + size > RAW_LIMIT) {
    const error = new Error('Raw evidence reached the 48 MiB worker evidence budget; retained records remain intact.');
    error.code = 'EVIDENCE_BUDGET';
    throw error;
  }
  await writeFile(`${ROOT}/raw/${name}`, json);
  evidenceBytes += size;
  evidenceFiles.push(`raw/${name}`);
}
function unpack(reply) {
  if (reply.isError) throw new Error((reply.content ?? []).filter(item => item.type === 'text').map(item => item.text).join('\n').slice(0, 2000) || 'REA tool returned an error.');
  if (reply.structuredContent) return reply.structuredContent;
  const text = (reply.content ?? []).find(item => item.type === 'text')?.text;
  if (!text) throw new Error('REA tool returned no JSON evidence.');
  return JSON.parse(text);
}
async function call(name, args, file) {
  if (remaining() < 100) throw new Error('Worker analysis deadline reached.');
  const reply = await client.callTool({name, arguments: args}, undefined, {timeout: remaining()});
  if (file) await save(file, reply);
  return unpack(reply);
}
async function connect() {
  await mkdir('/tmp/recheck-home', {recursive: true});
  transport = new StdioClientTransport({command: process.execPath, args: ['/opt/recheck/node_modules/rea-agents/scripts/rea.mjs', 'mcp'], stderr: 'pipe', env: {...process.env, HOME: '/tmp/recheck-home', REA_LOG_LEVEL: 'error', REA_ANALYSIS_PROVIDER: 'ghidra'}});
  transport.stderr?.on('data', chunk => { if (stderr.length < 1024 * 1024) stderr += chunk.toString().slice(0, 1024 * 1024 - stderr.length); });
  client = new Client({name: 'recheck-static-worker', version: '1.0.0'});
  await client.connect(transport, {timeout: remaining()});
  const tools = await client.listTools({}, {timeout: remaining()});
  for (const required of ['binary_session', 'open_binary', 'list_procedures', 'analyze_function', 'close_binary']) if (!tools.tools.some(tool => tool.name === required)) throw new Error(`Pinned REA release does not advertise ${required}.`);
  return tools;
}
async function analyze(request) {
  if (request.input !== '/input/target' || request.output !== ROOT) throw new Error('Worker contract requires input=/input/target and output=/output.');
  const maxFunctions = integer(request.max_functions, 10000, 1, 10000, 'max_functions');
  const maxDecompile = integer(request.max_decompile, 100, 0, 10000, 'max_decompile');
  await mkdir(`${ROOT}/raw`, {recursive: true});
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(request.input)) hash.update(chunk);
  const sha256 = hash.digest('hex');
  const tools = await connect();
  await save('tools-list.json', tools);
  const session = await call('binary_session', {expected_package_version: versions.rea, detail: 'complete'}, 'session-before.json');
  if (request.analysis_kind === 'managed') {
    if (!tools.tools.some(tool => tool.name === 'inspect_managed_members')) throw new Error('Pinned REA release does not advertise managed metadata inspection.');
    const evidence = await call('inspect_managed_members', {path: request.input}, 'managed-members.json');
    const normalized = normalizeManagedMembers(evidence, sha256, maxFunctions);
    const artifact = {schema_version: '1.0', sha256, backend: 'rea', tool: 'REA MCP / Managed metadata',
      backend_versions: versions, source: {container_path: request.input, sha256}, ...normalized};
    artifact.raw_evidence.files = evidenceFiles;
    const json = JSON.stringify(artifact, null, 2) + '\n';
    if (Buffer.byteLength(json) > RESULT_LIMIT) throw new Error('Managed metadata exceeds the normalized result byte budget; retained raw evidence remains available.');
    await writeFile(`${ROOT}/result.json`, json);
    return {ok: true, operation, result_path: `${ROOT}/result.json`, sha256, function_count: artifact.functions.length,
      partial: artifact.partial, partial_reasons: artifact.partial_reasons, backend_versions: versions, coverage: artifact.raw_evidence.coverage};
  }
  if (request.analysis_kind && request.analysis_kind !== 'native') throw new Error('Unsupported analysis kind.');
  const ghidra = session.result?.analysis_provider_candidates?.find(candidate => candidate.provider.id === 'ghidra');
  if (ghidra?.availability?.status !== 'available') throw new Error(`Ghidra provider unavailable: ${ghidra?.availability?.reason ?? 'no provider candidate'}`);
  opened = true;
  const openedResult = await call('open_binary', {path: request.input, provider_id: 'ghidra'}, 'open.json');
  if (openedResult.result?.sha256 !== sha256) throw new Error('REA target SHA256 differs from the original input.');
  const inventory = await call('list_procedures', {detail: 'complete'}, 'procedures.json');
  if (!Array.isArray(inventory.normalized_result)) throw new Error('Unsupported REA procedure inventory schema.');
  const procedures = inventory.normalized_result;
  const selected = procedures.slice(0, maxFunctions);
  if (new Set(selected.map(proc => proc.address)).size !== selected.length) throw new Error('Duplicate REA procedure inventory address.');
  const functions = [];
  let extracted = 0, failed = 0, budgetStopped = false, evidenceBudgetReached = false;
  for (const [index, proc] of selected.entries()) {
    if (proc.procedure?.external || extracted >= maxDecompile || budgetStopped) { functions.push(normalizeDossier(proc, null)); continue; }
    if (remaining() < 1500) { budgetStopped = true; functions.push(normalizeDossier(proc, null, 'Worker time budget reached before function extraction.')); continue; }
    extracted++;
    try {
      const dossier = await call('analyze_function', {procedure: proc.address, detail: 'complete'}, `function-${String(index).padStart(5, '0')}.json`);
      functions.push(normalizeDossier(proc, dossier));
    } catch (error) {
      failed++;
      functions.push(normalizeDossier(proc, null, message(error)));
      if (remaining() < 1500) budgetStopped = true;
      if (error.code === 'EVIDENCE_BUDGET') { budgetStopped = true; evidenceBudgetReached = true; }
    }
  }
  const reasons = [];
  if (procedures.length > maxFunctions) reasons.push(`Function inventory capped at ${maxFunctions} of ${procedures.length} observed procedures.`);
  const omitted = functions.filter(fn => !fn.external && fn.decompile_status === 'not_requested').length;
  if (omitted) reasons.push(`${omitted} function dossiers omitted by extraction or time budget.`);
  if (failed) reasons.push(`${failed} requested function dossiers failed; see per-function reasons and raw evidence.`);
  if (functions.some(fn => fn.partial && fn.decompile_status !== 'not_requested' && fn.decompile_status !== 'error')) reasons.push('Some extracted functions report incomplete facets; inspect per-function coverage.');
  if (evidenceBudgetReached) reasons.push('Raw evidence reached its 48 MiB budget; remaining dossiers were not requested.');
  else if (budgetStopped) reasons.push('Worker time budget reached.');
  if (inventory.relationship_coverage?.status !== 'complete') reasons.push(`Inventory relationship coverage: ${inventory.relationship_coverage?.status ?? 'unknown'}.`);
  const coverage = {inventory_observed: procedures.length, inventory_returned: functions.length, inventory_complete: procedures.length <= maxFunctions, native_functions: functions.filter(fn => !fn.external).length, external_functions: functions.filter(fn => fn.external).length, dossiers_requested: extracted, dossiers_succeeded: extracted - failed, decompiled: functions.filter(fn => ['success', 'truncated'].includes(fn.decompile_status)).length, cfg_complete: functions.filter(fn => fn.coverage.cfg === 'complete').length, dossier_limit: maxDecompile};
  const artifact = {schema_version: '1.0', sha256, backend: 'rea', tool: 'REA MCP / Ghidra', backend_versions: versions, source: {container_path: request.input, sha256}, functions, partial: reasons.length > 0, partial_reasons: reasons, limitations: [...new Set([...(inventory.limitations ?? []), 'Static provider observations do not establish runtime behavior. Unresolved indirect calls remain unknown.'])], raw_evidence: {files: evidenceFiles, coverage, truncated: procedures.length > maxFunctions || functions.some(fn => fn.truncated), partial_reasons: reasons}};
  // Keep the complete output directory below the host's 64 MiB admission limit:
  // 48 MiB raw records + 12 MiB result + at most 1 MiB provider stderr.
  let resultJson = JSON.stringify(artifact, null, 2) + '\n';
  if (Buffer.byteLength(resultJson) > RESULT_LIMIT) {
    reasons.push('Normalized result reached its 12 MiB budget; the largest dossiers are retained only as raw evidence.');
    artifact.partial = true;
    artifact.raw_evidence.truncated = true;
    const largest = functions.map((fn, index) => ({index, bytes: Buffer.byteLength(JSON.stringify(fn))})).sort((a, b) => b.bytes - a.bytes);
    for (const {index} of largest) {
      if (Buffer.byteLength(resultJson) <= RESULT_LIMIT) break;
      if (functions[index].external || !functions[index].evidence_id) continue;
      const evidenceId = functions[index].evidence_id;
      functions[index] = normalizeDossier(selected[index], null, 'Normalized dossier omitted by the 12 MiB result budget; consult raw evidence.');
      functions[index].evidence_id = evidenceId;
      resultJson = JSON.stringify(artifact, null, 2) + '\n';
    }
    if (Buffer.byteLength(resultJson) > RESULT_LIMIT) throw new Error('Function inventory alone exceeds the normalized result byte budget.');
    coverage.normalized_dossiers_retained = functions.filter(fn => fn.decompile !== null).length;
    resultJson = JSON.stringify(artifact, null, 2) + '\n';
  }
  await writeFile(`${ROOT}/result.json`, resultJson);
  return {ok: true, operation, result_path: `${ROOT}/result.json`, sha256, function_count: functions.length, partial: artifact.partial, partial_reasons: reasons, backend_versions: versions, coverage};
}
async function probe() {
  await connect();
  const session = await call('binary_session', {expected_package_version: versions.rea, detail: 'complete'});
  const candidate = session.result?.analysis_provider_candidates?.find(item => item.provider.id === 'ghidra');
  const checks = candidate?.availability?.diagnostics?.checks ?? [];
  const ready = candidate?.availability?.status === 'available' && checks.length > 0 && checks.every(check => check.ok === true) && session.result?.server_identity?.package?.version === versions.rea;
  return {ok: ready, operation, ready, backend: 'rea', tool: 'REA MCP / Ghidra', backend_versions: versions, provider: {id: 'ghidra', status: candidate?.availability?.status ?? 'missing', reason: candidate?.availability?.reason ?? null, checks}, worker_contract: versions.worker_contract};
}
let receipt;
try {
  let input = '';
  for await (const chunk of process.stdin) { input += chunk.toString(); if (Buffer.byteLength(input) > 65536) throw new Error('Worker request exceeds 64 KiB.'); }
  const request = JSON.parse(input.replace(/^\uFEFF/, '').trim());
  operation = request.operation;
  if (!['analyze', 'probe'].includes(operation)) throw new Error('Unsupported worker operation.');
  deadline = Date.now() + integer(request.timeout_seconds, 300, 1, 3600, 'timeout_seconds') * 1000;
  receipt = operation === 'probe' ? await probe() : await analyze(request);
} catch (error) {
  receipt = {ok: false, operation: operation ?? null, error: {type: error.name || 'Error', message: message(error)}, backend_versions: versions};
  process.exitCode = 1;
} finally {
  if (opened && client) {
    try { await client.callTool({name: 'close_binary', arguments: {}}, undefined, {timeout: 5000}); }
    catch (error) { receipt.cleanup_warning = message(error); }
  }
  if (client) await client.close().catch(() => {});
  if (stderr && operation === 'analyze') await writeFile(`${ROOT}/raw/rea-stderr.log`, stderr).catch(() => {});
}
// The only stdout write is this bounded completion receipt, never provider output.
process.stdout.write(JSON.stringify(receipt) + '\n');
