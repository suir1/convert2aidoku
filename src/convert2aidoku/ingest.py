from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

from .constants import DEFAULT_MAX_DECOMPILED_INPUT_CHARS, DEFAULT_MAX_INPUT_CHARS
from .decompiled_input import (
    DecompiledManifest,
    decompiled_source_paths,
    normalize_decompiled_java,
)
from .errors import InputError
from .models import SourceFile

_LICENSE_NAMES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "COPYING.md")


@dataclass(frozen=True)
class GitHubLocation:
    owner: str
    repository: str
    ref: str
    subpath: str

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repository}.git"


@dataclass(frozen=True)
class ResolvedSource:
    input_ref: str
    module_path: Path
    repository_root: Path
    commit: str | None
    license_path: Path | None
    source_format: Literal["kotlin_module", "decompiled_apk"] = "kotlin_module"
    decompiled_manifest: DecompiledManifest | None = None


def parse_github_url(value: str) -> GitHubLocation | None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != "github.com":
        return None
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        raise InputError("GitHub URL must contain owner and repository")
    owner, repository = parts[:2]
    repository = repository.removesuffix(".git")
    safe_name = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    if not safe_name.fullmatch(owner) or not safe_name.fullmatch(repository):
        raise InputError("GitHub owner and repository must be simple path segments")
    if len(parts) == 2:
        return GitHubLocation(owner, repository, "HEAD", "")
    if len(parts) >= 4 and parts[2] == "tree":
        ref = parts[3]
        subpath = parts[4:]
        if (
            not ref
            or ref.startswith("-")
            or ref in {".", ".."}
            or "\\" in ref
            or any(ord(char) < 32 for char in ref)
            or any(part in {"", ".", ".."} or "\\" in part for part in subpath)
        ):
            raise InputError("GitHub ref or module path contains an unsafe segment")
        if any(part.startswith("-") for part in subpath):
            raise InputError("GitHub module path cannot start with '-'")
        return GitHubLocation(owner, repository, ref, "/".join(subpath))
    raise InputError("use a GitHub repository URL or /tree/<ref>/<module-path> URL")


def _run_git(args: list[str], *, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InputError(f"unable to run git: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise InputError(f"git command failed: {detail}")
    return result.stdout.strip()


def _find_module(path: Path) -> Path:
    if any((path / name).is_file() for name in ("build.gradle.kts", "build.gradle")):
        return path
    candidates = [
        candidate.parent
        for pattern in ("build.gradle.kts", "build.gradle")
        for candidate in path.rglob(pattern)
        if len(candidate.relative_to(path).parts) <= 5
    ]
    candidates = list(dict.fromkeys(candidates))
    if not candidates:
        raise InputError(f"no Tachi build.gradle.kts or build.gradle found under {path}")
    if len(candidates) > 1:
        preview = ", ".join(str(item.relative_to(path)) for item in candidates[:8])
        raise InputError(f"multiple Tachi modules found; provide a module directory: {preview}")
    return candidates[0]


def _find_repository_root(path: Path) -> Path:
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def _find_license(module: Path, root: Path) -> Path | None:
    current = module
    while True:
        for name in _LICENSE_NAMES:
            candidate = current / name
            if candidate.is_file():
                return candidate
        if current == root or root not in current.parents:
            break
        current = current.parent
    return None


def _run_jadx(apk: Path, destination: Path) -> None:
    executable = shutil.which("jadx")
    if executable is None:
        raise InputError(
            "JADX is required for APK input; install it first (for example: brew install jadx)"
        )
    try:
        result = subprocess.run(
            [executable, "--no-debug-info", "-d", str(destination), str(apk)],
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InputError(f"unable to run JADX: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-4_000:]
        raise InputError(f"JADX failed to decompile the APK: {detail}")
    if (
        not (destination / "resources" / "AndroidManifest.xml").is_file()
        or not (destination / "sources").is_dir()
    ):
        raise InputError("JADX output did not contain an Android manifest and Java sources")


@contextmanager
def resolve_source(input_ref: str) -> Iterator[ResolvedSource]:
    github = parse_github_url(input_ref)
    if github is None:
        local = Path(input_ref).expanduser().resolve()
        if not local.exists():
            raise InputError(f"input does not exist: {local}")
        if local.is_file() and local.suffix.lower() == ".apk":
            with tempfile.TemporaryDirectory(prefix="c2a-apk-") as temporary:
                root = Path(temporary).resolve() / "decompiled"
                _run_jadx(local, root)
                manifest = DecompiledManifest.from_path(root / "resources" / "AndroidManifest.xml")
                yield ResolvedSource(
                    input_ref,
                    root,
                    root,
                    None,
                    _find_license(local.parent, local.parent),
                    "decompiled_apk",
                    manifest,
                )
            return
        if not local.is_dir():
            raise InputError(f"input must be a Tachi module directory or an APK file: {local}")
        module = _find_module(local)
        root = _find_repository_root(module)
        commit = None
        if (root / ".git").exists():
            try:
                commit = _run_git(["rev-parse", "HEAD"], cwd=root)
            except InputError:
                commit = None
        yield ResolvedSource(input_ref, module, root, commit, _find_license(module, root))
        return

    with tempfile.TemporaryDirectory(prefix="c2a-input-") as temporary:
        temporary_root = Path(temporary).resolve()
        root = temporary_root / github.repository
        if root.parent != temporary_root:
            raise InputError("GitHub clone destination escapes the temporary directory")
        clone_args = ["clone", "--depth", "1", "--filter=blob:none"]
        if github.ref != "HEAD":
            clone_args.extend(["--branch", github.ref])
        if github.subpath:
            clone_args.append("--sparse")
        clone_args.extend([github.clone_url, str(root)])
        _run_git(clone_args)
        if github.subpath:
            root_licenses = [f"/{name}" for name in _LICENSE_NAMES]
            _run_git(
                [
                    "sparse-checkout",
                    "set",
                    "--no-cone",
                    "--",
                    github.subpath,
                    *root_licenses,
                ],
                cwd=root,
            )
            requested = (root / github.subpath).resolve()
            if root.resolve() not in requested.parents and requested != root.resolve():
                raise InputError("GitHub subpath escapes the cloned repository")
        else:
            requested = root
        module = _find_module(requested)
        commit = _run_git(["rev-parse", "HEAD"], cwd=root)
        yield ResolvedSource(input_ref, module, root, commit, _find_license(module, root))


def collect_source_files(
    resolved: ResolvedSource,
    *,
    max_chars: int | None = None,
) -> list[SourceFile]:
    if max_chars is None:
        max_chars = (
            DEFAULT_MAX_DECOMPILED_INPUT_CHARS
            if resolved.source_format == "decompiled_apk"
            else DEFAULT_MAX_INPUT_CHARS
        )
    if resolved.source_format == "decompiled_apk":
        candidates = decompiled_source_paths(
            resolved.module_path,
            manifest=resolved.decompiled_manifest,
        )
    else:
        candidates = []
    if resolved.source_format == "kotlin_module":
        build_file = next(
            (
                resolved.module_path / name
                for name in ("build.gradle.kts", "build.gradle")
                if (resolved.module_path / name).is_file()
            ),
            None,
        )
        if build_file is not None:
            candidates.append(build_file)
    src = resolved.module_path / "src"
    if resolved.source_format == "kotlin_module" and src.is_dir():
        candidates.extend(
            path
            for path in src.rglob("*")
            if path.is_file()
            and path.suffix.lower()
            in {".kt", ".kts", ".json", ".xml", ".js", ".ts", ".html", ".css"}
        )
    if resolved.source_format == "kotlin_module":
        candidates.extend(
            path
            for path in resolved.module_path.rglob("*")
            if path.is_file()
            and path.suffix.lower()
            in {".properties", ".json", ".xml", ".js", ".ts", ".html", ".css"}
            and ".gradle" not in path.parts
            and "build" not in path.parts
        )

    files: list[SourceFile] = []
    total = 0
    root = resolved.module_path.resolve()
    for index, path in enumerate(dict.fromkeys(candidates)):
        resolved_path = path.resolve()
        if path.is_symlink() or (resolved_path != root and root not in resolved_path.parents):
            raise InputError(f"source file escapes the resolved input directory: {path}")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise InputError(f"source file is not UTF-8: {path}") from exc
        if resolved.source_format == "decompiled_apk" and path.suffix.lower() == ".java":
            content = normalize_decompiled_java(
                content,
                path.relative_to(resolved.module_path),
            )
        if total + len(content) > max_chars and resolved.source_format == "decompiled_apk":
            if index < 2:
                raise InputError(f"essential APK input exceeds {max_chars:,} characters")
            continue
        total += len(content)
        if total > max_chars:
            raise InputError(
                f"source input exceeds {max_chars:,} characters; narrow the module or raise "
                "the limit"
            )
        relative = path.relative_to(resolved.module_path).as_posix()
        files.append(
            SourceFile(
                path=relative,
                content=content,
                sha256=hashlib.sha256(content.encode()).hexdigest(),
            )
        )
    if resolved.source_format == "kotlin_module" and not any(
        item.path.endswith(".kt") for item in files
    ):
        raise InputError("Tachi module contains no Kotlin source files")
    if resolved.source_format == "decompiled_apk" and not any(
        item.path.endswith(".java") for item in files
    ):
        raise InputError("APK contains no usable decompiled Java source files")
    return files


def find_icon(module_path: Path) -> Path | None:
    preferred = (
        "res/icon.png",
        "icon.png",
        "res/drawable/icon.png",
        "res/mipmap-xxxhdpi/ic_launcher.png",
        "res/mipmap-xxhdpi/ic_launcher.png",
        "res/mipmap-xhdpi/ic_launcher.png",
        "res/mipmap-hdpi/ic_launcher.png",
        "res/mipmap-mdpi/ic_launcher.png",
        "resources/res/mipmap-xxxhdpi/ic_launcher.png",
        "resources/res/mipmap-xxhdpi/ic_launcher.png",
        "resources/res/mipmap-xhdpi/ic_launcher.png",
        "resources/res/mipmap-hdpi/ic_launcher.png",
        "resources/res/mipmap-mdpi/ic_launcher.png",
    )
    for relative in preferred:
        candidate = module_path / relative
        if candidate.is_file():
            return candidate
    icons = sorted(module_path.glob("res/**/ic_launcher.*"))
    icons.extend(sorted(module_path.glob("resources/res/**/ic_launcher.*")))
    icons.extend(sorted(module_path.glob("res/**/*icon*.png")))
    return next((item for item in icons if item.is_file()), None)


def copy_input_license(resolved: ResolvedSource, destination: Path) -> str | None:
    if resolved.license_path is None:
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(resolved.license_path, destination)
    return resolved.license_path.name
