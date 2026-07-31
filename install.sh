#!/bin/sh

set -eu

REPOSITORY_URL="${C2A_INSTALL_REPOSITORY_URL:-https://github.com/suir1/convert2aidoku.git}"
INSTALL_REF="${C2A_INSTALL_REF:-main}"
ARCHIVE_URL="${C2A_INSTALL_ARCHIVE_URL:-https://github.com/suir1/convert2aidoku/archive/${INSTALL_REF}.zip}"
PYTHON_VERSION="${C2A_INSTALL_PYTHON_VERSION:-3.13}"
JADX_VERSION="${C2A_INSTALL_JADX_VERSION:-1.5.3}"
JADX_SHA256="${C2A_INSTALL_JADX_SHA256:-8280f3799c0273fe797a2bcd90258c943e451fd195f13d05400de5e6451d15ec}"
DRY_RUN="${C2A_INSTALL_DRY_RUN:-0}"
FORCE_MISSING="${C2A_INSTALL_FORCE_MISSING:-0}"
USE_LOCAL="${C2A_INSTALL_USE_LOCAL:-1}"
UPDATE_PATH="${C2A_INSTALL_UPDATE_PATH:-1}"

case "$0" in
    */*)
        SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
        ;;
    *)
        SCRIPT_DIR=""
        ;;
esac

CARGO_BIN="${CARGO_HOME:-$HOME/.cargo}/bin"
PATH="$CARGO_BIN:$HOME/.local/bin:$PATH"
export PATH

log() {
    printf '\n[c2a-install] %s\n' "$*"
}

warn() {
    printf '\n[c2a-install] Warning: %s\n' "$*" >&2
}

die() {
    printf '\n[c2a-install] Error: %s\n' "$*" >&2
    exit 1
}

have() {
    if [ "$FORCE_MISSING" = "1" ]; then
        return 1
    fi
    command -v "$1" >/dev/null 2>&1
}

run() {
    if [ "$DRY_RUN" = "1" ]; then
        printf '[c2a-install] DRY-RUN:'
        for argument in "$@"; do
            printf ' %s' "$argument"
        done
        printf '\n'
        return 0
    fi
    "$@"
}

run_privileged() {
    if [ "$(id -u)" -eq 0 ]; then
        run "$@"
    elif have sudo || [ "$DRY_RUN" = "1" ]; then
        run sudo "$@"
    else
        die "Installing system packages requires root or sudo: $*"
    fi
}

download() {
    url="$1"
    destination="$2"
    run curl -fL --retry 3 --connect-timeout 20 "$url" -o "$destination"
}

detect_os() {
    detected="${C2A_INSTALL_OS:-$(uname -s)}"
    case "$detected" in
        Darwin|darwin|macos)
            OS=macos
            ;;
        Linux|linux)
            OS=linux
            ;;
        *)
            die "Unsupported operating system: $detected. Use macOS or Linux."
            ;;
    esac
}

find_brew() {
    if [ "$FORCE_MISSING" = "1" ]; then
        return 1
    fi
    if have brew; then
        command -v brew
        return 0
    fi
    for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

install_homebrew() {
    log "Homebrew is missing; installing it non-interactively"
    installer="$(mktemp -t c2a-homebrew.XXXXXX)"
    download https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh "$installer"
    if [ "$DRY_RUN" = "1" ]; then
        run env NONINTERACTIVE=1 /bin/bash "$installer"
        rm -f "$installer"
        BREW_BIN=/opt/homebrew/bin/brew
        return 0
    fi
    env NONINTERACTIVE=1 /bin/bash "$installer"
    rm -f "$installer"
    BREW_BIN="$(find_brew || true)"
    [ -n "$BREW_BIN" ] || die "Homebrew installation finished but brew was not found"
}

install_macos_dependencies() {
    if have git && have jadx; then
        log "Git and JADX are already installed"
        return 0
    fi
    brew_bin="$(find_brew || true)"
    if [ -z "$brew_bin" ]; then
        install_homebrew
        brew_bin="$BREW_BIN"
    fi
    PATH="$(dirname "$brew_bin"):$PATH"
    export PATH
    if ! have git; then
        log "Installing Git with Homebrew"
        run "$brew_bin" install git
    fi
    if ! have jadx; then
        log "Installing JADX and its Java runtime with Homebrew"
        run "$brew_bin" install jadx
    fi
}

linux_package_manager() {
    if [ -n "${C2A_INSTALL_PACKAGE_MANAGER:-}" ]; then
        printf '%s\n' "$C2A_INSTALL_PACKAGE_MANAGER"
        return 0
    fi
    for candidate in apt-get dnf yum pacman zypper; do
        if have "$candidate"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

java_is_compatible() {
    if ! have java; then
        return 1
    fi
    version="$(java -version 2>&1 | sed -n '1s/.*version "\([^"]*\)".*/\1/p')"
    major="$(printf '%s' "$version" | awk -F. '{ if ($1 == "1") print $2; else print $1 }')"
    case "$major" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "$major" -ge 11 ]
}

install_linux_packages() {
    if have git && have curl && have unzip && java_is_compatible; then
        log "Git, curl, unzip, and Java are already installed"
        return 0
    fi
    manager="$(linux_package_manager || true)"
    [ -n "$manager" ] || die "No supported Linux package manager found (apt, dnf, yum, pacman, zypper)"
    log "Installing Linux prerequisites with $manager"
    case "$manager" in
        apt-get)
            run_privileged apt-get update
            run_privileged apt-get install -y git curl unzip ca-certificates openjdk-17-jre-headless
            ;;
        dnf|yum)
            run_privileged "$manager" install -y git curl unzip ca-certificates java-17-openjdk-headless
            ;;
        pacman)
            run_privileged pacman -Sy --needed --noconfirm git curl unzip ca-certificates jre17-openjdk-headless
            ;;
        zypper)
            run_privileged zypper --non-interactive install git curl unzip ca-certificates java-17-openjdk-headless
            ;;
        *)
            die "Unsupported Linux package manager: $manager"
            ;;
    esac
}

sha256_file() {
    if have sha256sum; then
        sha256sum "$1" | awk '{print $1}'
    elif have shasum; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        die "Neither sha256sum nor shasum is available"
    fi
}

install_linux_jadx() {
    if have jadx; then
        log "JADX is already installed"
        return 0
    fi
    log "Installing pinned JADX $JADX_VERSION in the user account"
    archive="$(mktemp -t c2a-jadx.XXXXXX)"
    url="https://github.com/skylot/jadx/releases/download/v${JADX_VERSION}/jadx-${JADX_VERSION}.zip"
    download "$url" "$archive"
    target_root="$HOME/.local/share/convert2aidoku"
    target="$target_root/jadx-$JADX_VERSION"
    temporary="$target_root/.jadx-$JADX_VERSION.tmp.$$"
    if [ "$DRY_RUN" = "1" ]; then
        run verify-sha256 "$JADX_SHA256" "$archive"
        run mkdir -p "$temporary" "$HOME/.local/bin"
        run unzip -q "$archive" -d "$temporary"
        run mv "$temporary" "$target"
        run ln -sfn "$target/bin/jadx" "$HOME/.local/bin/jadx"
        rm -f "$archive"
        return 0
    fi
    actual="$(sha256_file "$archive")"
    if [ "$actual" != "$JADX_SHA256" ]; then
        rm -f "$archive"
        die "JADX checksum mismatch: expected $JADX_SHA256, got $actual"
    fi
    mkdir -p "$target_root" "$HOME/.local/bin"
    rm -rf "$temporary"
    mkdir "$temporary"
    unzip -q "$archive" -d "$temporary"
    rm -f "$archive"
    rm -rf "$target"
    mv "$temporary" "$target"
    chmod +x "$target/bin/jadx"
    ln -sfn "$target/bin/jadx" "$HOME/.local/bin/jadx"
    PATH="$HOME/.local/bin:$PATH"
    export PATH
}

find_uv() {
    if have uv; then
        command -v uv
        return 0
    fi
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

install_uv() {
    uv_bin="$(find_uv || true)"
    if [ -n "$uv_bin" ]; then
        log "uv is already installed"
        UV_BIN="$uv_bin"
        return 0
    fi
    log "uv is missing; installing it"
    installer="$(mktemp -t c2a-uv.XXXXXX)"
    download https://astral.sh/uv/install.sh "$installer"
    if [ "$DRY_RUN" = "1" ]; then
        run sh "$installer"
        rm -f "$installer"
        UV_BIN="$HOME/.local/bin/uv"
        return 0
    fi
    sh "$installer"
    rm -f "$installer"
    UV_BIN="$(find_uv || true)"
    [ -n "$UV_BIN" ] || die "uv installation finished but uv was not found"
}

install_c2a() {
    uv_bin="$1"
    if [ -n "${C2A_INSTALL_SOURCE:-}" ]; then
        requirement="$C2A_INSTALL_SOURCE"
        source_description="$requirement"
    elif [ "$USE_LOCAL" = "1" ] && [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
        requirement="$SCRIPT_DIR"
        source_description="local checkout $SCRIPT_DIR"
    else
        requirement="$ARCHIVE_URL"
        source_description="$REPOSITORY_URL public archive at $INSTALL_REF"
    fi
    log "Installing or updating C2A from $source_description"
    run "$uv_bin" tool install --python "$PYTHON_VERSION" --force \
        "$requirement"
    if [ "$DRY_RUN" = "1" ]; then
        tool_bin="$HOME/.local/bin"
    else
        tool_bin="$($uv_bin tool dir --bin)"
    fi
    C2A_BIN="$tool_bin/c2a"
    PATH="$tool_bin:$HOME/.local/bin:$PATH"
    export PATH
    if [ "$DRY_RUN" != "1" ] && [ ! -x "$C2A_BIN" ]; then
        die "C2A installation finished but $C2A_BIN was not created"
    fi
    if [ "$UPDATE_PATH" = "1" ]; then
        if ! run "$uv_bin" tool update-shell; then
            warn "Could not update the shell PATH automatically; add $tool_bin manually"
        fi
    fi
}

core_toolchain_missing() {
    for tool in rustup cargo rustfmt cargo-clippy aidoku aidoku-test-runner; do
        if ! have "$tool"; then
            return 0
        fi
    done
    if ! rustup target list --installed 2>/dev/null | grep -qx wasm32-unknown-unknown; then
        return 0
    fi
    return 1
}

install_core_toolchain() {
    if [ "$DRY_RUN" = "1" ] || core_toolchain_missing; then
        log "Installing missing Rust, WASM, and pinned Aidoku tools"
        run "$C2A_BIN" setup --yes
    else
        log "Rust, WASM, and Aidoku tools are already installed"
    fi
}

main() {
    detect_os
    log "Detected $OS"
    if [ "$OS" = "macos" ]; then
        install_macos_dependencies
    else
        install_linux_packages
        install_linux_jadx
    fi
    install_uv
    install_c2a "$UV_BIN"
    install_core_toolchain
    log "Running final environment check"
    run "$C2A_BIN" doctor
    log "Installation complete. Open a new terminal and run: c2a --help"
}

main "$@"
