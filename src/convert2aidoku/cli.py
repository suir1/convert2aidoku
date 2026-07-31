from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .ai import OpenAICompatibleClient
from .analyzer import analyze_path
from .config import ReasoningEffort, load_ai_settings
from .constants import MAX_REPAIR_ROUNDS
from .conversion_assessment import assess_source_ir
from .converter import convert_source, validate_existing
from .errors import C2AError
from .templates import match_templates
from .toolchain import doctor as run_doctor
from .toolchain import setup_toolchain

app = typer.Typer(
    name="c2a",
    help="AI-assisted Tachi/Mihon to Aidoku source converter.",
    no_args_is_help=True,
)
console = Console()


def _abort(exc: Exception) -> None:
    console.print(f"[bold red]Error:[/bold red] {exc}")
    raise typer.Exit(1) from exc


@app.command()
def doctor(
    as_json: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON.")] = False,
) -> None:
    """Inspect the local Python, Git, Rust, WASM, and Aidoku toolchain."""
    statuses = run_doctor()
    if as_json:
        typer.echo(json.dumps([item.__dict__ for item in statuses], indent=2))
        return
    table = Table(title="convert2aidoku toolchain")
    table.add_column("Component")
    table.add_column("Status")
    table.add_column("Detail")
    for item in statuses:
        state = "[green]ready[/green]" if item.available else "[red]missing[/red]"
        table.add_row(item.name, state, item.detail or item.path or "")
    console.print(table)


@app.command()
def setup(
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Confirm installation without an interactive prompt."),
    ] = False,
) -> None:
    """Install the pinned Rust, WASM, and Aidoku build toolchain."""
    if not yes:
        confirmed = typer.confirm(
            "Install/update Rust stable, wasm32 target, Clippy, rustfmt, and pinned Aidoku tools?"
        )
        if not confirmed:
            raise typer.Abort()
    try:
        with console.status("Installing toolchain; this can take several minutes..."):
            setup_toolchain()
    except C2AError as exc:
        _abort(exc)
    console.print("[green]Toolchain setup completed.[/green]")


@app.command("ai-check")
def ai_check(
    base_url: Annotated[str | None, typer.Option(help="OpenAI-compatible /v1 base URL.")] = None,
    model: Annotated[str | None, typer.Option(help="Provider model identifier.")] = None,
    config: Annotated[Path | None, typer.Option(help="Path to c2a.toml.")] = None,
) -> None:
    """Check API connectivity and structured-output support."""
    try:
        settings = load_ai_settings(base_url=base_url, model=model, config_path=config)
        with console.status("Checking AI endpoint..."), OpenAICompatibleClient(settings) as client:
            result = client.check()
    except C2AError as exc:
        _abort(exc)
    mode = "JSON Schema" if result.structured_output else "JSON fallback"
    console.print(f"[green]Connected[/green] to [bold]{result.model}[/bold] using {mode}.")


@app.command()
def ui(
    host: Annotated[
        str,
        typer.Option(
            help="Web UI listen host; loopback is required unless network access is allowed."
        ),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(min=1, max=65_535, help="Web UI listen port."),
    ] = 51_821,
    open_browser: Annotated[
        bool,
        typer.Option("--open/--no-open", help="Open the local UI in a browser."),
    ] = True,
    allow_network: Annotated[
        bool,
        typer.Option(help="Allow binding beyond this machine's loopback interface."),
    ] = False,
) -> None:
    """Open the local browser interface for analysis, conversion, and reports."""
    from .web_runtime import start_web_ui

    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    console.print(f"C2A Web UI: [bold]http://{display_host}:{port}[/bold]")
    try:
        start_web_ui(
            host=host,
            port=port,
            open_browser=open_browser,
            allow_network=allow_network,
        )
    except C2AError as exc:
        _abort(exc)


@app.command()
def analyze(
    input_ref: Annotated[str, typer.Argument(help="Local module/APK path or GitHub module URL.")],
    output: Annotated[
        Path | None, typer.Option("--out", help="Write SourceIR JSON to a file.")
    ] = None,
) -> None:
    """Analyze one standalone Tachi HttpSource module or APK without calling AI."""
    try:
        with console.status("Analyzing Tachi source..."):
            ir = analyze_path(input_ref)
    except C2AError as exc:
        _abort(exc)
    serialized = ir.model_dump_json(indent=2, exclude={"license_text"}) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized, encoding="utf-8")
        console.print(f"Wrote [bold]{output}[/bold]")
    else:
        typer.echo(serialized, nl=False)


@app.command()
def assess(
    input_ref: Annotated[str, typer.Argument(help="Local module/APK path or GitHub module URL.")],
) -> None:
    """Estimate conversion readiness and token budget without calling AI."""
    try:
        with console.status("Assessing conversion evidence..."):
            ir = analyze_path(input_ref)
            assessment = assess_source_ir(ir)
    except C2AError as exc:
        _abort(exc)
    typer.echo(
        json.dumps(
            {
                "source_id": ir.metadata.source_id,
                "assessment": assessment.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("templates")
def templates(
    input_ref: Annotated[str, typer.Argument(help="Local module/APK path or GitHub module URL.")],
) -> None:
    """Show versioned Aidoku templates matched by a Tachi source."""
    try:
        with console.status("Matching Aidoku templates..."):
            ir = analyze_path(input_ref)
            matches = match_templates(ir)
    except C2AError as exc:
        _abort(exc)
    typer.echo(
        json.dumps(
            {
                "source_id": ir.metadata.source_id,
                "aidoku_revision": matches[0].aidoku_revision if matches else None,
                "templates": [match.model_dump(mode="json") for match in matches],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command()
def convert(
    input_ref: Annotated[str, typer.Argument(help="Local module/APK path or GitHub module URL.")],
    output: Annotated[Path, typer.Option("--out", help="Destination Aidoku source directory.")],
    base_url: Annotated[str | None, typer.Option(help="OpenAI-compatible /v1 base URL.")] = None,
    model: Annotated[str | None, typer.Option(help="Provider model identifier.")] = None,
    config: Annotated[Path | None, typer.Option(help="Path to c2a.toml.")] = None,
    query: Annotated[str | None, typer.Option(help="Optional live-smoke search query.")] = None,
    max_repairs: Annotated[
        int | None,
        typer.Option(
            min=0,
            max=MAX_REPAIR_ROUNDS,
            help="Maximum cumulative AI repairs; compiler/contract failures are capped at two.",
        ),
    ] = None,
    generation_reasoning: Annotated[
        ReasoningEffort | None,
        typer.Option(
            "--generation-reasoning",
            help="AI reasoning effort for initial source generation.",
        ),
    ] = None,
    repair_reasoning: Annotated[
        ReasoningEffort | None,
        typer.Option("--repair-reasoning", help="AI reasoning effort for repair requests."),
    ] = None,
    live: Annotated[
        bool,
        typer.Option("--live/--no-live", help="Run list/details/chapters/pages/image smoke tests."),
    ] = True,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Send selected source files without prompting."),
    ] = False,
    force: Annotated[bool, typer.Option(help="Replace an existing output directory.")] = False,
    resume: Annotated[
        bool,
        typer.Option(help="Resume saved staging without repeating completed AI rounds."),
    ] = False,
    proxy: Annotated[
        str | None,
        typer.Option(
            help="HTTP proxy for live validation; defaults to C2A_PROXY, e.g. Mihomo :7890."
        ),
    ] = None,
) -> None:
    """Generate, repair, build, package, and validate an Aidoku source."""
    try:
        settings = load_ai_settings(
            base_url=base_url,
            model=model,
            config_path=config,
            max_repair_rounds=max_repairs,
            generation_reasoning_effort=generation_reasoning,
            repair_reasoning_effort=repair_reasoning,
        )
        if not yes:
            console.print(
                "The selected Tachi source code will be sent to the configured AI provider. "
                "The API key will not be stored."
            )
            if not typer.confirm("Continue?"):
                raise typer.Abort()
        with console.status(
            "Converting source; AI and Rust validation may take several minutes..."
        ) as status:
            outcome = convert_source(
                input_ref,
                output=output,
                settings=settings,
                query=query,
                live=live,
                force=force,
                proxy=proxy,
                resume=resume,
                progress=status.update,
            )
    except C2AError as exc:
        _abort(exc)
    status_style = {
        "verified": "green",
        "build_only": "yellow",
        "blocked": "yellow",
        "failed": "red",
    }[outcome.report.status.value]
    console.print(
        f"[{status_style}]{outcome.report.status.value}[/{status_style}] "
        f"→ [bold]{outcome.output}[/bold]"
    )
    console.print(f"Report: {outcome.output / 'report.md'}")
    if outcome.report.status.value == "failed":
        raise typer.Exit(2)


@app.command()
def validate(
    source: Annotated[Path, typer.Argument(help="Generated Aidoku source directory.")],
    live: Annotated[
        bool,
        typer.Option("--live/--no-live", help="Run generated network smoke tests."),
    ] = True,
    proxy: Annotated[
        str | None,
        typer.Option(
            help="HTTP proxy for live validation; defaults to C2A_PROXY, e.g. Mihomo :7890."
        ),
    ] = None,
) -> None:
    """Re-run build, package, verification, and optional live tests."""
    try:
        with console.status("Validating Aidoku source..."):
            report = validate_existing(source, live=live, proxy=proxy)
    except (C2AError, OSError, ValueError, json.JSONDecodeError) as exc:
        _abort(exc)
    console.print(f"Validation status: [bold]{report.status.value}[/bold]")
    if report.status.value == "failed":
        raise typer.Exit(2)


if __name__ == "__main__":
    app()
