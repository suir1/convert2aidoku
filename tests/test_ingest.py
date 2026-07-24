import shutil
from pathlib import Path

import pytest

from convert2aidoku import ingest
from convert2aidoku.command_execution import CommandFailure, CommandResult
from convert2aidoku.errors import InputError
from convert2aidoku.ingest import (
    collect_source_files,
    parse_github_url,
    resolve_source,
)

DECOMPILED_APK_FIXTURE = Path(__file__).parent / "fixtures" / "decompiled_apk"


def _command_result(
    command: list[str],
    *,
    returncode: int | None = 0,
    stdout: str = "",
    stderr: str = "",
    failure: CommandFailure | None = None,
    error: str = "",
) -> CommandResult:
    return CommandResult(
        command=tuple(command),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.1,
        failure=failure,
        error=error,
    )


def test_parse_github_module_url() -> None:
    parsed = parse_github_url(
        "https://github.com/keiyoushi/extensions-source/tree/main/src/zh/mycomic"
    )
    assert parsed is not None
    assert parsed.owner == "keiyoushi"
    assert parsed.repository == "extensions-source"
    assert parsed.ref == "main"
    assert parsed.subpath == "src/zh/mycomic"


def test_rejects_ambiguous_github_url() -> None:
    with pytest.raises(InputError):
        parse_github_url("https://github.com/owner/repo/issues/1")


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/../repo/tree/main/src",
        "https://github.com/owner/repo/tree/main/../outside",
        "https://github.com/owner/%2e%2e/tree/main/src",
    ],
)
def test_rejects_github_path_traversal(url: str) -> None:
    with pytest.raises(InputError):
        parse_github_url(url)


def test_collects_only_module_text() -> None:
    fixture = Path(__file__).parent / "fixtures" / "simple"
    with resolve_source(str(fixture)) as resolved:
        files = collect_source_files(resolved)
    assert [item.path for item in files] == ["build.gradle.kts", "src/example/Simple.kt"]


def test_collects_legacy_groovy_module(tmp_path: Path) -> None:
    (tmp_path / "build.gradle").write_text("ext { extName = 'Legacy' }")
    source = tmp_path / "src" / "example" / "Legacy.kt"
    source.parent.mkdir(parents=True)
    source.write_text("class Legacy : HttpSource()")

    with resolve_source(str(tmp_path)) as resolved:
        files = collect_source_files(resolved)

    assert [item.path for item in files] == ["build.gradle", "src/example/Legacy.kt"]


def test_resolves_apk_with_mocked_jadx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    apk = tmp_path / "source.apk"
    apk.write_bytes(b"synthetic apk")

    def fake_jadx(_apk: Path, destination: Path) -> None:
        shutil.copytree(DECOMPILED_APK_FIXTURE, destination)

    monkeypatch.setattr(ingest, "_run_jadx", fake_jadx)
    with resolve_source(str(apk)) as resolved:
        assert resolved.source_format == "decompiled_apk"
        assert resolved.decompiled_manifest is not None
        assert resolved.decompiled_manifest.main_class_name == "CopyManga"
        files = collect_source_files(resolved)

    paths = [item.path for item in files]
    assert "resources/AndroidManifest.xml" in paths
    assert any(path.endswith("CopyManga.java") for path in paths)
    main = next(item for item in files if item.path.endswith("CopyManga.java"))
    assert "compiler noise" not in main.content


def test_git_execution_error_keeps_input_domain_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ingest,
        "execute_command",
        lambda command, **_kwargs: _command_result(
            command,
            returncode=None,
            failure="timeout",
            error="Command timed out after 120 seconds",
        ),
    )

    with pytest.raises(InputError, match="unable to run git: Command timed out"):
        ingest._run_git(["clone", "https://example.com/repo.git"])


def test_git_nonzero_exit_keeps_stderr_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ingest,
        "execute_command",
        lambda command, **_kwargs: _command_result(
            command,
            returncode=128,
            stderr="fatal: repository not found",
        ),
    )

    with pytest.raises(InputError, match="git command failed: fatal: repository not found"):
        ingest._run_git(["clone", "https://example.com/repo.git"])


def test_jadx_nonzero_exit_keeps_bounded_input_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingest.shutil, "which", lambda _name: "/usr/local/bin/jadx")
    monkeypatch.setattr(
        ingest,
        "execute_command",
        lambda command, **_kwargs: _command_result(
            command,
            returncode=1,
            stderr="prefix " + "x" * 4_100,
        ),
    )

    with pytest.raises(InputError) as failure:
        ingest._run_jadx(tmp_path / "input.apk", tmp_path / "decompiled")

    assert str(failure.value).startswith("JADX failed to decompile the APK: ")
    assert "prefix" not in str(failure.value)
    assert len(str(failure.value).removeprefix("JADX failed to decompile the APK: ")) == 4_000
