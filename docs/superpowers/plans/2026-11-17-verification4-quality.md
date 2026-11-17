# Recheck Verification4 and Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Repair verified correctness defects, audit all maintained code, and publish a verified Windows prerelease.

**Architecture:** Preserve the existing Python/PySide6 engine, adapters, records and release pipeline. Validate ZIP structures independently, share preflight/discovery boundaries, tolerate historical environmental evidence at rendering boundaries, and add only demonstrated quality improvements.

**Tech Stack:** Python >=3.11 (local 3.12), PySide6, pytest, Node static workers, Docker, PyInstaller, GitHub Actions.

**Spec:** docs/superpowers/specs/2026-11-17-verification4-quality-design.md

## Global Constraints

- Preserve user files, credentials, existing runs and original evidence hashes.
- No force pushes, history rewriting, tag/asset replacement or global integration changes.
- Keep required CI job name `tests` and explicitly distinguish source, frozen, Docker, GUI, VM and live AI evidence.
- Local QA remains under ignored `verification/`; public audit is `docs/PROJECT_AUDIT_REPORT.md`.
- Actual fixes require a failing regression before production edits.

## Review Focus

- Descriptor ZIPs and empty entries must remain valid while contradictory sizes, incomplete DEFLATE and trailing compressed bytes are rejected.
- Historical nested null/nonmapping environment values must render without mutating records or fabricating tool success.
- CLI preflight failures must not leave an analysis run, but failures after execution starts must retain evidence.
- Direct selection must not bypass explicit preparation or accidentally build managed images.
- Frozen reports and release inventory must match the reviewed source and preserve library notices/source obligations.

---

### Task 1: Discovery and baseline

Files: all tracked first-party files; local `verification/audit/`; final report.

- [x] Read root audit instructions and clone main into requested folder on `audit/verification4-quality`, preserving supplied Markdown.
- [x] Consolidate all verification4 docs/evidence and relevant verification3 history; inventory source versus generated/vendor artifacts.
- [x] Run locked-environment full pytest and Node baseline; record tool availability and dependency vulnerabilities.
- [x] Review design against actual contracts; ledger any necessary corrections.

### Task 2: Strict ZIP integrity

Files: `recheck/package_comparison.py`, `tests/test_package_comparison.py`, `docs/PACKAGE_COMPARISON.md`.

Interfaces: preserve `inventory_package(path, *, limits=None, cancel=None)` and `compare_packages(...)` result schema.

- [x] Add regressions using hand-built/retained contradictory STORED, DEFLATE, zero-length and local-header-size fixtures; assert incomplete and comparison=None.
- [x] Run these tests and observe expected failures.
- [x] Validate local/central/descriptor metadata and independent actual compressed stream/CRC/length, bounded by current limits; reject unsupported combinations explicitly.
- [x] Run all existing package tests and the original package124 independent harness against source; review supported normal cases.
- [x] Commit only reviewed task files.

### Task 3: Historical report resilience

Files: `recheck/report.py`, `tests/test_report.py` and related report tests.

Interfaces: `render_report(run, destination)` must preserve input and existing report contract.

- [x] Add nested-null/nonmapping history regressions and observe failure.
- [x] Normalize optional mappings at read boundaries and present explicit unavailable/unrecorded values; preserve raw evidence.
- [x] Render retained real Server=null record without changing its hash; run report/review/failure tests.
- [x] Commit reviewed task files.

### Task 4: Preflight, adapter preparation and discovery consistency

Files: `recheck/cli.py`, `recheck/engine.py`, `recheck/analysis_environment.py`, `recheck/prerequisites.py`, `recheck/setup_discovery.py`, adapter/Ollama modules as demonstrated; associated tests.

- [x] Trace CLI/GUI start and reproduce all MODE01/MODE02/DIAG observations before fixing.
- [x] Pin analysis-preflight rejection with no new run for unsupported modes/formats; preserve post-start failed run behavior.
- [x] Pin direct/managed preparation behavior and shared Java/Ollama discovery with controlled process boundaries.
- [x] Implement minimal common-boundary fixes and run mode/setup/CLI/adapter tests.
- [x] Commit reviewed task files.

### Task 5: Complete code audit and demonstrated improvements

Files: first-party modules, scripts, tests, `.github/workflows/`, `pyproject.toml`, locked dependencies, `.gitignore`, final report.

- [x] Account for actual manual review coverage, static analysis, complexity and duplication; do not claim unread files reviewed.
- [x] Check SonarQube, CodeQL, OSV-Scanner, OpenSSF Scorecard and Ruff; use justified local alternatives and record unavailable tools.
- [x] Fix reproducible P0/P1 and useful P2 findings with RED/GREEN regressions. Avoid broad mechanical formatting churn.
- [x] Add bounded, least-permission automated quality/security checks compatible with current CI and verify configuration/results.
- [x] Run full pytest/Node, final static/dependency scans and independent code review; fix material findings.
- [x] Write `docs/PROJECT_AUDIT_REPORT.md` with inventory counts, before/after results, exact limitations and remaining work.

### Task 6: Build, real verification and GitHub publication

Files: version metadata, selected docs, source identity, release notes; local build/proof artifacts.

- [x] Choose next unused version (expected 0.1.5), update consistent user-facing/version metadata and prepare exact release notes.
- [x] Run `scripts/verify_source.py --output verification/builds/<release>/source-checks`, then package with existing notices/source verification.
- [x] Verify new frozen ZIP regressions, historical report, actual GUI and known Docker behavior oracle using separate app data; retain native proof and inventory.
- [ ] Commit reviewed source identity; push branch, open PR using established format and verify exact-head CI.
- [ ] Merge reviewed SHA using repository privacy-preserving workflow; verify main CI, create new draft prerelease, upload immutable ZIP and check server SHA256/size.
- [ ] Publish and re-read final release page/assets/commit/CI/links. Report exact links and any genuinely unmet conditions.
