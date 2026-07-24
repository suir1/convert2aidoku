# Source Conversion

This context describes how convert2aidoku turns one Tachi input into an auditable Aidoku package.

## Language

**Input Source**:
A standalone Tachi/Mihon module or decompiled public-reading APK supplied to a conversion.
_Avoid_: Source

**SourceIR**:
The deterministic, provider-independent description recovered from an **Input Source**.
_Avoid_: Parsed source, analysis result

**SourceIR Analysis**:
The format dispatch that sends a resolved **Input Source** to exactly one Kotlin Module or Decompiled APK Adapter and constructs its **SourceIR**.
_Avoid_: Analyzer helpers, metadata parser

**Decompiled Inspection**:
The Android manifest facts, normalized JADX Java structure, and deterministic DTO serialized-name and field/generic shapes recovered from a decompiled APK **Input Source** for both **SourceIR** analysis and generation evidence.
_Avoid_: Java cleanup helpers, APK parser output

**Input Capability Recognition**:
The deterministic dialect-aware reading and supported-cryptography capabilities recovered from Kotlin or Decompiled Java content before constructing a **SourceIR**.
_Avoid_: Capability marker map, crypto check

**Generation Manifest**:
One AI round's complete declaration of controlled Rust files, **Generated Resources**, traits, dependencies, and warnings.
_Avoid_: AI output, generated files

**Generation Manifest Contract**:
The structured diagnostics, targeted-repair scope, DTO-shape preservation, and user-readable rendering produced by evaluating one **Generation Manifest** against its **SourceIR**.
_Avoid_: Capability gap strings, repair keywords

**Targeted Repair**:
The targeted-first repair policy that selects compiler or contract excerpts, authorizes exact Rust replacements, validates the complete result, and falls back to a full **Generation Manifest** repair with an audit warning.
_Avoid_: Patch helpers, unrestricted diff, repair routing keywords

**Dependency Policy**:
The pinned Aidoku revision and optional Cargo crate allowlist, Cargo projection, provider instruction, and **SourceIR** capability requirements applied to every **Generation Manifest**.
_Avoid_: Dependency dictionary, allowed dependency set

**Generated Resources**:
The parsed and normalized Aidoku `filters.json` and `settings.json` owned by a **Generation Manifest**.
_Avoid_: Resource strings, JSON blobs

**Rust Inspection**:
The parsed syntax and indexed function facts recovered from generated Rust without applying safety or contract policy.
_Avoid_: Rust policy, contract check

**Typed AI Exchange**:
A provider request for one declared Pydantic model, including Structured Output fallback, validation feedback, usage, and warnings.
_Avoid_: Raw AI response, model call

**Conversion Round**:
One materialization, contract evaluation, validation, and **Checkpoint** update for the current effective **Generation Manifest**.
_Avoid_: AI call, repair attempt

**Conversion Intake**:
The fresh-or-resume preparation that validates output state and saved options, creates or restores the workspace, and returns its **Checkpoint Store**, **SourceIR**, and **Checkpoint** without persisting provider credentials.
_Avoid_: Workspace helpers, resume branch, conversion setup

**Conversion Completion**:
The terminal report, **Checkpoint**, audit publication, and atomic install policy that either installs a completed **Generated Source** or leaves it in a resumable workspace.
_Avoid_: Report builder, output move helper

**Test Scenario**:
A fresh minimal legal provider configuration, **SourceIR**, or project layout used by tests with case-specific differences supplied as explicit overrides.
_Avoid_: Shared fixture state, convenience helper

**Validation Plan**:
The ordered fail-fast execution and recording policy for toolchain, build, package, safety, and live stages that produces a **Validation Result**.
_Avoid_: Command list, subprocess loop

**Live Validation Evidence**:
Source-specific benchmark observations collected outside generation that may enrich repair diagnostics or prefer a value already present in a finite generated setting allowlist.
_Avoid_: Source hacks, hardcoded fixes, proof of runner connectivity

**Command Execution**:
The normalized facts produced by running one external command, including its safe environment, working directory, timeout, output, exit status, and duration.
_Avoid_: Subprocess result, command helper

**Checkpoint**:
The resumable record of conversion phase, AI rounds, current manifest, diagnostics, and validation state.
_Avoid_: Cache, session file

**Checkpoint Store**:
The safe, atomic workspace persistence and installed audit mirror for one **Checkpoint**, its **SourceIR**, and raw **Generation Manifest** history.
_Avoid_: JSON helpers, workspace cache

**Generated Source**:
The staged or installed Aidoku project materialized from an effective **Generation Manifest**.
_Avoid_: Source

**Generated Source Metadata**:
The tool-owned Aidoku `res/source.json` identity, site, version, compatibility requirements, and preserved extension fields for one **Generated Source**.
_Avoid_: Source JSON, info dictionary

**Validation Result**:
The stage-by-stage build, package, contract, and live-test evidence for a **Generated Source**.
_Avoid_: Test result

**Conversion Report**:
The user-facing status and provenance summary produced from a **Checkpoint** and **Validation Result**.
_Avoid_: Log

## Relationships

- One **Input Source** passes through exactly one **SourceIR Analysis** Adapter and produces one **SourceIR** per conversion.
- A decompiled APK **Input Source** yields one shared **Decompiled Inspection** whose facts feed **SourceIR** analysis and whose behavior and DTO-shape projections feed generation evidence.
- One **Input Capability Recognition** classifies the Kotlin or Decompiled Java content of an **Input Source** into **SourceIR** capabilities and unsupported cryptography facts.
- One **Checkpoint** records one or more **Generation Manifests** and identifies exactly one current manifest.
- One **Checkpoint Store** persists one **Checkpoint**, its **SourceIR**, and raw **Generation Manifest** history, then mirrors that audit into the **Generated Source** `.c2a` directory.
- One **Generation Manifest** owns zero or one filter resource and zero or one settings resource as **Generated Resources**.
- One **Generation Manifest** yields one **Rust Inspection** of its controlled Rust files during contract evaluation.
- One **Generation Manifest Contract** evaluates one **Generation Manifest** against one **SourceIR** and may expose a targeted repair only when every diagnostic has a supported repair kind and a relevant Rust excerpt.
- One **Targeted Repair** may replace only text present in its authorized excerpts; failure preserves its diagnostic as a warning before requesting a full replacement **Generation Manifest**.
- One **Dependency Policy** evaluates every **Generation Manifest** before provider acceptance, **Generated Source** materialization, and **Generation Manifest Contract** evaluation.
- Each AI round is produced by one successful **Typed AI Exchange**.
- Each **Conversion Round** evaluates one effective **Generation Manifest** and records its generated files, contract gaps, and **Validation Result** in the **Checkpoint**.
- One **Conversion Intake** prepares exactly one fresh or resumed conversion state; failed fresh preparation removes its incomplete workspace before returning an error.
- One **Conversion Completion** consumes the final **Validation Result**, commits the terminal **Checkpoint** and audit before installing the **Generated Source**, and preserves resumable failures in the workspace.
- Each **Test Scenario** creates new objects and directories so one test cannot mutate another test's defaults.
- One **Validation Plan** evaluates one materialized **Generated Source** and produces one ordered **Validation Result**.
- One **SourceIR** may resolve **Live Validation Evidence**; it cannot invent behavior, bypass a generated allowlist, or by itself prove live verification.
- Git and JADX Input Source ingestion, toolchain operations, and each **Validation Plan** command consume the same **Command Execution** facts while retaining their own domain error policy.
- One effective **Generation Manifest** materializes one **Generated Source**.
- One **Generated Source Metadata** document describes one **Generated Source** and projects host compatibility requirements from its effective **Generation Manifest**.
- One **Validation Result** evaluates one materialized **Generated Source**.
- One **Conversion Report** summarizes one conversion attempt without storing provider credentials.

## Example dialogue

> **Developer:** "Should the recovered filter default be written back into the raw **Generation Manifest**?"
> **Domain expert:** "No. Preserve the raw manifest for audit, apply the default through **Generated Resources**, and materialize the effective manifest into the **Generated Source**."

## Flagged ambiguities

- "source" previously meant both the Tachi input and Aidoku output; use **Input Source** and **Generated Source** respectively.
