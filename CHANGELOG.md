# Changelog

## Unreleased

### Architecture

- Centralize Generation Manifest Projection ordering, applicability, tracing, and rule identity in
  one deterministic registry while preserving the existing normalization interface.
- Move generated return ownership, module topology, and recovered Kotlin chapter projection out of
  the scaffold implementation so their behavior and tests have one local Module owner.

### Verified regressions

- Komiic remains live `verified` when replayed from its saved 43,714-token manifest without a new
  provider call; Copymanga remains live `verified` from a clean APK conversion with zero AI calls
  and zero tokens.

## 0.1.0b3 — 2026-08-11

This beta turns a difficult Kotlin conversion into a reproducible generated-source compatibility
pipeline and strengthens behavior shared by every AI provider.

### Generation compatibility

- Preserve valid `if let` fallback chains when folding identical Rust branches, and project legacy
  grouped Aidoku imports to the pinned crate's root re-exports without leaving shadowed imports.
- Ignore unrelated Rust constants whose string syntax is not JSON-compatible while checking
  generated settings usage, so invalid generated Rust reaches normal validation instead of a
  Python traceback.
- Project generated module visibility and helper return ownership across Rust files, including
  borrowed DTO lists, pinned Aidoku errors, current request builders, and legacy model/API paths.
- Recover standard Kotlin chapter DTO behavior when URL, title, type, date, scanlator, ordering,
  and setting evidence are complete; split raw GraphQL manga queries by requested detail/chapter
  data and support relative manga/chapter deep links.
- Convert legacy checkbox groups and their `FilterValue` consumers to current multi-select filters,
  and retry reproducible read-only GraphQL POST requests once after a transient request failure.

### Verified regressions

- Komiic now reaches live `verified` from its saved 43,714-token AI manifest without another
  provider call; Copymanga remains `verified` from a clean APK conversion with zero AI calls and
  zero tokens.

### Known limitations

- Deterministic chapter recovery requires complete, unambiguous Kotlin evidence. Otherwise the
  controlled AI generation and repair path remains responsible for the missing behavior.
- Live `verified` status still depends on source-site availability and anti-bot behavior at the
  time of validation.

## 0.1.0b2 — 2026-08-10

This beta turns the first verified APK conversion into a repeatable, provider-optional pipeline.

### Highlights

- Recover listing, details, chapters, pages, filters, and settings from supported decompiled source
  evidence and render complete Aidoku Rust traits deterministically.
- Skip the provider entirely when analysis proves a complete deterministic projection. Copymanga
  now converts from its public extension APK to a live-tested `verified` AIX with zero AI calls,
  zero AI tokens, and zero repair rounds.
- Require API configuration only after analysis determines that generation or repair needs it. The
  CLI, Web UI, reports, and resumable checkpoints preserve the distinction between provider-free
  and AI-assisted conversions.
- Fall back to controlled AI generation when evidence is incomplete, without weakening generated
  path confinement, dependency allowlisting, manifest validation, or command-execution boundaries.

### Generation and validation

- Default initial generation to disabled thinking and cap generation/repair completion budgets,
  with provider-compatible parameter fallback and reasoning/cache token reporting.
- Split decompiled Android settings evidence from Rust behavior generation and remove audit-only
  hashes and omission details from provider prompts.
- Fall back from rejected exact patches to full controlled repair without relaxing generated-file,
  dependency, or manifest validation boundaries.
- Normalize additional pinned Aidoku/Rust compatibility errors and make dynamic-domain live smoke
  recreate source instances, sample readable listings, and report per-domain failure stages.
- Copymanga reached `verified` with `deepseek-v4-flash`; its measured initial generation fell from
  227,881 to 49,858 tokens in the regression run.
- Harden deterministic projection with evidence-owned query bindings, response contracts, manga
  field mappings, source traits, settings resources, dynamic filters, and Aidoku compatibility
  normalization.

### Known limitations

- Deterministic conversion applies only when the supported evidence is complete. Other inputs may
  still require a compatible model and can fail because of provider output or token limits.
- Standalone public-reading sources remain the supported scope. Multisrc themes, login, bookcases,
  Cloudflare bypass, image scrambling, and unknown cryptography remain out of scope.
- Live `verified` status still depends on source-site availability and anti-bot behavior at the
  time of validation.

## 0.1.0b1 — 2026-07-31

First public preview of `convert2aidoku`.

### Highlights

- Local CLI and Web UI for analyzing one Tachi/Mihon source module or extension APK.
- Provider-free conversion readiness, risk, and token-budget assessment before AI is called.
- OpenAI-compatible generation with JSON Schema fallback, bounded repair prompts, checkpoints,
  resume support, WASM compilation, packaging, and live validation.
- Deterministic Aidoku scaffolding, filter and settings projection, Rust compatibility rewrites,
  dependency allowlisting, generated-path confinement, and provenance reports.
- Environment-aware bootstrap installer for macOS, Linux, and WSL2.

### Verified conversions

- Copymanga reached `verified` from its public extension APK with `deepseek-v4-pro`.
- MyComic, Komiic, Vomic, and other sources were exercised during development with generated AIX
  packages and live validation.

### Known limitations

- This is a beta. A readiness score is evidence coverage, not a success probability or guarantee.
- Only standalone public-reading sources are supported. Multisrc themes, login, bookcases,
  Cloudflare bypass, image scrambling, and unknown cryptography remain out of scope.
- Provider latency, token cost, output quality, source-site availability, and anti-bot behavior can
  still prevent a conversion from reaching `verified`.
- Native Windows installation is unavailable; use WSL2. The Web UI listens on loopback by default.
- Generated sources can be derivative works. Users must review the input license and publication
  rights before redistribution.
