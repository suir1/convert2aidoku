# convert2aidoku

`convert2aidoku` is a local, AI-assisted converter for one standalone Tachi/Mihon
`HttpSource` module at a time. It produces a current Aidoku Rust/WASM source, repairs compiler or
live-test failures with an OpenAI-compatible API, packages `package.aix`, and writes an auditable
conversion report.

The MVP deliberately rejects multisrc/theme sources, unsupported cryptography, and image
scrambling instead of pretending that an unsafe or incomplete conversion succeeded. Explicit
AES-CBC JSON decoding and 3DES-CBC request signing are supported with pinned RustCrypto crates.
Local extension APKs can be decompiled with JADX and converted in a public-reading-only scope;
login, bookcase, and comment features are reported but excluded.

## Install

Python 3.13 and [uv](https://docs.astral.sh/uv/) are recommended:

```bash
uv sync
uv run c2a doctor
uv run c2a setup
```

`setup` asks before installing Rust stable, `wasm32-unknown-unknown`, rustfmt, Clippy, and Aidoku's
CLI/test runner pinned to a known `aidoku-rs` revision.

`c2a templates` reports source-agnostic patterns compatible with that pinned revision. These
patterns describe slots such as selectors, URL construction, filter mapping, and image headers;
they do not copy an existing Aidoku source into an AI prompt. Source-specific licenses and
provenance remain separate.

## AI configuration

The API key is accepted only through the environment. Do not put it in `c2a.toml` or a command-line
argument.

```toml
# c2a.toml
[ai]
base_url = "http://localhost:50048/v1"
model = "your-model-id"
max_repair_rounds = 1
timeout_seconds = 300
generation_reasoning_effort = "auto"
repair_reasoning_effort = "low"
```

```bash
export C2A_API_KEY='...'
uv run c2a ai-check
```

Reasoning effort accepts `auto`, `off`, `low`, `medium`, or `high`. Initial generation defaults to
provider-controlled `auto`; targeted and full repairs default to `low`; `ai-check` disables
thinking. Providers that
reject reasoning controls automatically fall back to their default behavior without consuming a
manifest validation retry. Environment overrides use `C2A_GENERATION_REASONING_EFFORT` and
`C2A_REPAIR_REASONING_EFFORT`; `convert` also accepts `--generation-reasoning` and
`--repair-reasoning`.

The client first requests JSON-Schema structured output. Providers that reject that feature fall
back to JSON Object mode and then plain JSON, both still validated locally with Pydantic.

For decompiled APKs, the initial prompt uses deterministic Java behavior slices instead of JADX
boilerplate; Kotlin modules still send their complete selected source files. The primary AI call
returns Rust files only. Static filters are generated deterministically from `SourceIR`, while
settings use a separate bounded evidence prompt whose response contains real JSON objects rather
than JSON text nested inside a manifest string. Compiler and Clippy repairs first use bounded
source excerpts and exact `old_text` → `new_text` replacements. Each replacement must come from a
supplied excerpt, match once, stay inside the generated-file allowlist, and pass the same Rust
safety checks. Full repair fallback is also Rust-only and inherits tool-owned resources.
Live-behavior failures retain the full repair context. This keeps repair prompts small without
weakening path, dependency, or code-execution boundaries.

Repair is adaptive. Compiler and contract failures are capped at one compact repair even when a
higher maximum is configured. Non-blocked live-behavior failures honor the explicit maximum.
HTTP 403, TLS challenges, maintenance responses, DNS failures, and timeouts never trigger AI;
their checkpoint is retained for a later `--resume` run.

## Analyze and convert

Inputs can be local module directories, local extension APKs, or GitHub module URLs. Both current
`build.gradle.kts` and legacy `build.gradle` extension modules are recognized. APK input requires
`jadx` (`brew install jadx` on macOS):

```bash
uv run c2a analyze ./src/zh/mycomic

uv run c2a analyze ./tachiyomi-zh.copymanga-v1.4.82.apk

uv run c2a templates ./src/zh/mycomic

uv run c2a convert \
  https://github.com/keiyoushi/extensions-source/tree/main/src/zh/mycomic \
  --out generated/zh.mycomic \
  --yes

# Resume a saved staging workspace after a failed/interrupted run.
uv run c2a convert <same-input> --out generated/zh.mycomic --resume --yes
```

CLI flags override environment variables, which override `c2a.toml`. `--no-live` skips network
smoke tests and can produce at most `build_only`; without Rust, analysis still works but packaging
and verification cannot succeed.

Live validation can explicitly use a local HTTP proxy such as Mihomo's mixed port:

```bash
uv run c2a validate generated/zh.mycomic --proxy http://127.0.0.1:7890
# or: export C2A_PROXY=http://127.0.0.1:7890
```

The proxy is passed to both `aidoku-test-runner` and the validator's availability probe. Proxy
credentials and URLs are not written to reports.

Generated output includes:

- current Aidoku Rust source and resources;
- `package.aix` after a successful build;
- `report.json` and `report.md`;
- the discovered input license as `LICENSE.input` and `PROVENANCE.md`.

Statuses are strict:

- `verified`: build, package, Aidoku verification, and core live smoke test passed;
- `build_only`: the source built, but live testing was skipped or unavailable;
- `blocked`: this CLI/test-runner network environment was prevented from completing live access;
  it does not claim the site is unavailable in a normal browser;
- `failed`: generation, compilation, packaging, or functional validation failed.

Generated code may be a derivative work. Review the copied license and confirm redistribution
rights before publishing it.
