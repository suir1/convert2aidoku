# convert2aidoku

`convert2aidoku` is a local, AI-assisted converter for one standalone Tachi/Mihon
`HttpSource` module at a time. It produces a current Aidoku Rust/WASM source, repairs compiler or
live-test failures with an OpenAI-compatible API, packages `package.aix`, and writes an auditable
conversion report.

The MVP deliberately rejects multisrc/theme sources, unsupported cryptography, and image
scrambling instead of pretending that an unsafe or incomplete conversion succeeded. Explicit
AES-CBC JSON sources are supported. Local extension APKs can be decompiled with JADX and converted
in a public-reading-only scope; login, bookcase, and comment features are reported but excluded.

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
max_repair_rounds = 3
timeout_seconds = 300
```

```bash
export C2A_API_KEY='...'
uv run c2a ai-check
```

The client first requests JSON-Schema structured output. Providers that reject that feature fall
back to plain JSON, which is still validated locally with Pydantic.

## Analyze and convert

Inputs can be local module directories, local extension APKs, or GitHub module URLs. APK input
requires `jadx` (`brew install jadx` on macOS):

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
