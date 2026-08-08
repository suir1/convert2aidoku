# Changelog

## Unreleased

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
