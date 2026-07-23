# Source Conversion

This context describes how convert2aidoku turns one Tachi input into an auditable Aidoku package.

## Language

**Input Source**:
A standalone Tachi/Mihon module or decompiled public-reading APK supplied to a conversion.
_Avoid_: Source

**SourceIR**:
The deterministic, provider-independent description recovered from an **Input Source**.
_Avoid_: Parsed source, analysis result

**Generation Manifest**:
One AI round's complete declaration of controlled Rust files, **Generated Resources**, traits, dependencies, and warnings.
_Avoid_: AI output, generated files

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

**Test Scenario**:
A fresh minimal legal provider configuration, **SourceIR**, or project layout used by tests with case-specific differences supplied as explicit overrides.
_Avoid_: Shared fixture state, convenience helper

**Validation Plan**:
The ordered fail-fast execution and recording policy for toolchain, build, package, safety, and live stages that produces a **Validation Result**.
_Avoid_: Command list, subprocess loop

**Checkpoint**:
The resumable record of conversion phase, AI rounds, current manifest, diagnostics, and validation state.
_Avoid_: Cache, session file

**Generated Source**:
The staged or installed Aidoku project materialized from an effective **Generation Manifest**.
_Avoid_: Source

**Validation Result**:
The stage-by-stage build, package, contract, and live-test evidence for a **Generated Source**.
_Avoid_: Test result

**Conversion Report**:
The user-facing status and provenance summary produced from a **Checkpoint** and **Validation Result**.
_Avoid_: Log

## Relationships

- One **Input Source** produces one **SourceIR** per conversion.
- One **Checkpoint** records one or more **Generation Manifests** and identifies exactly one current manifest.
- One **Generation Manifest** owns zero or one filter resource and zero or one settings resource as **Generated Resources**.
- One **Generation Manifest** yields one **Rust Inspection** of its controlled Rust files during contract evaluation.
- Each AI round is produced by one successful **Typed AI Exchange**.
- Each **Conversion Round** evaluates one effective **Generation Manifest** and records its generated files, contract gaps, and **Validation Result** in the **Checkpoint**.
- Each **Test Scenario** creates new objects and directories so one test cannot mutate another test's defaults.
- One **Validation Plan** evaluates one materialized **Generated Source** and produces one ordered **Validation Result**.
- One effective **Generation Manifest** materializes one **Generated Source**.
- One **Validation Result** evaluates one materialized **Generated Source**.
- One **Conversion Report** summarizes one conversion attempt without storing provider credentials.

## Example dialogue

> **Developer:** "Should the recovered filter default be written back into the raw **Generation Manifest**?"
> **Domain expert:** "No. Preserve the raw manifest for audit, apply the default through **Generated Resources**, and materialize the effective manifest into the **Generated Source**."

## Flagged ambiguities

- "source" previously meant both the Tachi input and Aidoku output; use **Input Source** and **Generated Source** respectively.
