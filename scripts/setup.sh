#!/usr/bin/env bash
# 5 to 9 Shift — local setup (macOS first; Linux works for the engine).
#
#   scripts/setup.sh            # check + install the workspace's OWN environments (.venv, desktop/node_modules)
#   scripts/setup.sh --models   # also download the tested CPU transcription model (~630 MB) if no whisper.cpp model is set
#
# What it never does: upgrade Premiere, install a Premiere bridge, edit ~/.claude.json, ~/.claude/skills or any global
# MCP config, or read credential files. Proposed config changes are printed for you to apply.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
MODELS=0; [ "${1:-}" = "--models" ] && MODELS=1
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$*"; }

echo "== Machine"
uname -a | sed 's/^/  /'
if [ "$(uname)" = "Darwin" ]; then
  echo "  chip: $(sysctl -n machdep.cpu.brand_string 2>/dev/null) · RAM: $(( $(sysctl -n hw.memsize) / 1073741824 )) GB · arch: $(uname -m)"
fi
df -h "$HOME" | tail -1 | awk '{print "  free disk (home): "$4}'

echo "== Required tools"
need_fix=0
for t in git ffmpeg ffprobe node npm; do
  if command -v "$t" >/dev/null; then ok "$t: $($t --version 2>&1 | head -1)"; else bad "$t missing"; need_fix=1; fi
done
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 1)'; then PY="$(command -v $c)"; break; fi
done
if [ -n "$PY" ]; then ok "python: $($PY --version) ($PY)"; else bad "need Python 3.11–3.13"; need_fix=1; fi
NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)
[ "$NODE_MAJOR" -ge 20 ] && ok "node >= 20" || { bad "need Node 20+"; need_fix=1; }
if [ $need_fix = 1 ]; then
  echo; echo "Install the missing pieces yourself (nothing was changed):"
  echo "  brew install ffmpeg node@22 python@3.13 git"
  exit 1
fi

echo "== Claude Code (the chat's AI)"
if command -v claude >/dev/null; then
  ok "claude $(claude --version 2>/dev/null)"
  if claude auth status 2>/dev/null | grep -q '"loggedIn": *true'; then ok "logged in (the app never reads your credentials)"; else warn "not logged in — run \`claude\` once and log in"; fi
else warn "Claude Code not installed — the chat falls back to offline commands"; fi

echo "== Workspace Python environment (.venv, isolated)"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
EXTRA=""
if ! command -v whisper-cli >/dev/null && ! command -v whisper-cpp >/dev/null; then EXTRA="[sherpa]"; fi
.venv/bin/pip install -q -e ".${EXTRA}" "pytest>=8" "httpx>=0.27"
ok "engine installed ($(.venv/bin/python -c 'import shiftws, yt_dlp, claude_real_video; print("yt-dlp", yt_dlp.version.__version__, "· crv ok")'))"

echo "== Transcription backend"
if command -v whisper-cli >/dev/null || command -v whisper-cpp >/dev/null; then
  ok "whisper.cpp found: $(command -v whisper-cli || command -v whisper-cpp) — set transcription.whisper_cpp_model in settings.json to your ggml model"
elif .venv/bin/python -c 'from shiftws.config import load_config; from shiftws.transcribe import get_backend; b=get_backend(load_config()); print(b.name, b.model)' 2>/dev/null; then
  ok "local transcription model already installed (above)"
elif [ $MODELS = 1 ]; then
  D="$(.venv/bin/python -c 'from shiftws.config import load_config; print(load_config().models_dir)')"
  F=sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8
  if [ ! -d "$D/$F" ]; then
    echo "  downloading $F (~630 MB) from github.com/k2-fsa/sherpa-onnx releases…"
    curl -fL "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$F.tar.bz2" | tar xj -C "$D"
  fi
  ok "sherpa-onnx model at $D/$F"
else
  warn "no whisper.cpp found; run scripts/setup.sh --models for the tested CPU model, or install whisper.cpp"
fi

echo "== Desktop app"
(cd desktop && npm install --no-audit --no-fund >/dev/null && npm run build >/dev/null) && ok "desktop built (npm --prefix desktop start)"

echo "== Discovery (names/versions only, secrets redacted)"
mkdir -p docs/local
.venv/bin/shiftws discover > docs/local/discovery.md || true
.venv/bin/shiftws doctor > docs/local/health.md || true
sed -n '1,200p' docs/local/discovery.md | grep -E "MCP|Premiere|skills|←" | sed 's/^/  /' || true

echo "== Premiere bridge"
if grep -q "Premiere candidate" docs/local/discovery.md; then
  ok "an existing Premiere MCP server is configured — it will be preserved; set premiere.mcp_command to use it"
elif command -v premiere-pro-mcp >/dev/null; then
  ok "premiere-pro-mcp CLI present"
else
  warn "no Premiere bridge found. If you want the candidate bridge, review then run yourself:"
  echo "      npm install -g adobe-premiere-pro-mcp@1.2.8 && premiere-pro-mcp --install-cep"
fi

echo "== Workspace MCP for your own Claude Code sessions (optional, project scope)"
.venv/bin/shiftws mcp-config | sed 's/^/  /'
echo
echo "Done. Start the app:  npm --prefix desktop start   ·  reports: docs/local/discovery.md, docs/local/health.md"
