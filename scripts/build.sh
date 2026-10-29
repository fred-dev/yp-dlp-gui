#!/usr/bin/env bash
# Build a packaged app for the current OS (macOS or Linux).
#
#   ./scripts/build.sh                 # macOS: universal (Apple Silicon + Intel) .dmg, ad-hoc signed
#   ./scripts/build.sh --identity "Developer ID Application: …"   # macOS: properly signed
#   LINUX_FORMAT=flatpak ./scripts/build.sh                       # Linux: system (default), flatpak or appimage
#
# Output lands in dist/.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is not installed. Install it with:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

uv python install            # version comes from .python-version
uv sync --group dev

case "$(uname -s)" in
    Darwin)
        platform=macOS
        format=app
        if [ $# -eq 0 ]; then set -- --adhoc-sign; fi
        ;;
    Linux)
        platform=linux
        format=${LINUX_FORMAT:-system}
        ;;
    *)
        echo "Unsupported OS: $(uname -s). On Windows run scripts\\build.ps1" >&2
        exit 1
        ;;
esac

if [ -d "build/ytdlp_gui/$(echo "$platform" | tr '[:upper:]' '[:lower:]')" ]; then
    # Already scaffolded: refresh code and re-resolve requirements so the
    # newest yt-dlp (sites change often) goes into every build.
    uv run briefcase update "$platform" "$format" --update-requirements --no-input
else
    uv run briefcase create "$platform" "$format" --no-input
fi
uv run briefcase build "$platform" "$format" --no-input
uv run briefcase package "$platform" "$format" --no-input "$@"

echo
echo "Done. Package(s):"
ls -1 dist/
