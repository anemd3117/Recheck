# Recheck managed analysis environment

## Goal and scope

Keep the Windows application and existing GPT sign-in workflow. The public Windows
ZIP contains the executable, Python/Qt runtime and reviewed Docker source contexts.
It does not redistribute the old private Docker archives. Users do not need host
Git, Python, Java, Ghidra, REA or MCP to launch the executable and prepare the managed
environment. Docker Desktop and its Linux amd64 engine are still needed for analysis.
Rebuild and folder comparison are outside this change.

The normal sequence is **connect AI → prepare environment → choose two programs
→ compare → inspect evidence**. GPT remains the default. Ollama is optional and
starts only when the user selects local AI.

## Ownership and boundaries

- Recheck owns orchestration, input snapshots, cancellation, evidence validation,
  persistence, comparison, and AI evidence selection.
- A static worker runs pinned REA MCP and Ghidra in one container. Its local MCP
  client initializes the server, opens the snapshot, inventories procedures,
  queries functions, and closes the target and server. This container never runs
  the submitted program.
- A separate execution image runs each test without network access. Both compared
  programs use the same inspected immutable image ID.
- An optional Ollama service uses a managed persistent model volume and a loopback
  endpoint. No GPT credentials, Docker socket, or unrelated host folders enter
  these containers.
- Docker Desktop and WSL 2 remain host prerequisites. Recheck diagnoses them and
  offers actionable instructions; it does not change global Docker context or
  operating-system configuration.

## Distribution and trust

`resources/runtime/environment.json` retains the exact image identities and archive
digests of the earlier developer/offline environment. Existing verified offline
archives remain supported: preparation hashes them before loading, then inspects
the image. Registry downloads require explicitly trusted immutable digest references.
Those historical TAR files are not included in the public Windows ZIP.

For the public ZIP, the separate **build analysis environment** setup action downloads
official upstream components using the shipped Dockerfiles and lockfile, then builds
the static and execution images locally. This action needs internet access, several
GB of free space and potentially several minutes. It requires an explicit user
action; normal probe, comparison and legacy preparation do not build or download
source environments. Optional Ollama is built only when explicitly requested; no
model is downloaded or service/container started by the builder.

The builder requires a ready local Linux amd64 Docker endpoint, copies each reviewed
source context into temporary app-owned staging, hashes every file plus the manifest,
and checks those hashes before and after building. Docker writes an immutable ID to
a unique iidfile. The manager checks this exact ID, platform, component, build-contract
and source-context labels before atomically saving a local receipt under
`%LOCALAPPDATA%\Recheck\runtime\local-builds.json` (or `RECHECK_DATA_DIR`). The
shipped manifest is never rewritten and mutable tags grant no trust. A changed
context/manifest, missing image or invalid receipt prevents that local image from
being selected; rebuilding is the explicit recovery action. Static and execution
adapters both use these checked image IDs. A failed/cancelled build leaves a previous
receipt intact and does not register partial success. Docker may retain intermediate
build cache; the app does not remove unrelated images or volumes.

Image IDs built on another PC can differ because package repositories and Docker
build metadata change even when source contexts match. Their locally inspected IDs
and context hashes are recorded instead of presenting them as the old published
image IDs. Images, receipts and optional model data remain separate from credentials.

Static and execution images are required. Ollama is not required for GPT. The
manager probes without making changes; preparation and service start are explicit
application operations. It only removes its own temporary containers and preserves
model data. Existing local Ollama installations retain their endpoint.

## Static analysis contract

The host sends one bounded JSON request to a non-root, network-disabled worker.
Inputs are mounted read-only, output is isolated, capabilities are dropped, and
CPU, memory and time are bounded. The worker returns a receipt and a versioned
result containing the binary hash, functions, tool versions, coverage limitations,
and raw MCP evidence. Missing CFG, decompilation or truncated inventory must stay
explicit; these are never presented as complete observations.

The host validates schema, binary and result hashes, paths and container identity.
Only complete validated results are cacheable, keyed by binary, image and contract.
Selection occurs before analysis: automatic mode prefers a ready managed worker,
otherwise an available direct Ghidra installation. Explicit REA selection never
silently changes backend after failure. The direct adapter remains available for
compatibility and evidence comparison.

## Implementation and acceptance sequence

1. Add and test runtime trust policy, offline preparation, and optional Ollama
   lifecycle. Test invalid identity, archive tampering, remote context, cancellation
   and ownership boundaries before integrating the UI.
2. Build a pinned REA/Ghidra worker; record actual released MCP schemas and raw
   requests/results. Analyze the shipped ELF fixture without executing it.
3. Add the host static adapter, routing and backend-aware evidence provenance.
   Compare real REA results with direct Ghidra on the same fixture; document
   differences and unsupported evidence.
4. Integrate environment preparation into the existing beginner workflow, keeping
   advanced settings optional. Verify GPT does not start or download Ollama.
5. Build labelled execution and optional local-AI images. Record exact identities,
   export verified archives, and test a real containerized comparison and offline
   preparation. Document Windows-target limitations of a Linux Docker engine.
6. Run the appropriate regression suite, package the verified application, verify
   the distribution independently, and record source identity, commands, results
   and remaining limitations before publishing the feature branch.

Development evidence is stored outside the source payload under
`verification/container-runtime-20261115`. A design or passing mock test alone does
not establish a working distribution; actual worker and deployment checks are
required.

## Verified implementation observations (2026-11-15)

- The real isolated worker inventoried 21 procedures: 17 native functions and
  four external references. All 17 native dossiers and pseudocode requests
  succeeded; 13 returned a complete CFG. Missing linkage-thunk blocks and unknown
  inventory relationship coverage remain explicit, so the result is `partial`.
- On the same input and Ghidra version, the direct exporter and REA returned the
  same 17 native addresses and names. For `main` and `bounded_sum`, body sizes,
  instruction counts, mnemonics, resolved calls and trimmed pseudocode matched.
  The direct exporter's `main` graph also includes three call-target edges; REA
  keeps those calls separate from its four intrafunction CFG edges.
- A real Engine comparison of the sample and a deliberately changed loop bound
  returned three matches and two differences for inputs 0, 8, 100, 101 and 150.
  All ten executions used the same pinned execution image. Static stages stayed
  partial; structure, execution and report stages completed. All 13 created
  containers had no network and were removed.
- Windows Docker bind paths failed at a deep output directory. Static analysis
  now uses short owned transport staging and copies validated evidence into the
  requested run directory. The same deep-path comparison and running-container
  cancellation were repeated successfully, with staging and containers removed.
- The optional CPU service passed actual local inference with
  `qwen3:4b-instruct`. Its catalog and process digest matched. Strict basic,
  JSON and tool roundtrip proof was persisted and restored by a new profile
  manager. The thinking alias `qwen3:4b` failed the strict check and is not the
  preset. The nonce prompt was clarified; exact comparison remains mandatory.
- Image archives are approximately 900 MB (static), 31 MB (execution) and 72 MB
  (CPU Ollama). All three exported hashes, sizes and internal image identities
  were checked. Model weights are a separate first-use download of about 2.5 GB;
  they and generated local service keys are excluded from the distribution.

Backend completion and evidence completeness are distinct: an AI follow-up may
finish after a valid partial extraction, while the extraction and overall run
retain their partial status. Failed or blocked stages still prevent completion.
No unbounded retries or permission escalation are introduced.

The actual GPT check validated the interpretation and citations in its original
4-of-5-request workflow. A later agent-only check finished in 2 of 5 requests as
a separate workflow. The first trace exposed unnecessary nested JSON encoding:
valid structured context was being serialized twice. Keeping bounded dict/list
context structured retained every field and limitation, saved about 5 KB, and
allowed all seven measured tool results plus the final answer within the same
40,000-character cap. String inputs stay untrusted strings; oversized context
still uses a bounded excerpt with an omitted-character count. The recorded
five-request replay is a regression test, not an additional live-run claim.

The Windows executable was also exercised with a new application data folder:
one local-AI button verified the cached model, selected the verified connection,
prepared the environment, and opened the comparison screen. After all three owned
image IDs were removed, the same packaged app restored Ollama on startup and
restored static/execution images through its automatic-preparation button, using
only the adjacent verified archives. No Ghidra/Java paths were entered and model
weights were retained. The remaining optional service was stopped after QA.
The empty-image check also exposed a misleading Docker-installation instruction;
the final guidance distinguishes a running engine from an unimported image.

## Primary references

- [REA](https://github.com/morluto/rea)
- [MCP architecture](https://modelcontextprotocol.io/docs/learn/architecture)
- [Docker image save](https://docs.docker.com/reference/cli/docker/image/save/)
- [Docker image load](https://docs.docker.com/reference/cli/docker/image/load/)
- [Docker Desktop Windows prerequisites](https://docs.docker.com/desktop/setup/install/windows-install/)
- [Ollama Docker](https://docs.ollama.com/docker)
- [Ollama FAQ](https://docs.ollama.com/faq)
