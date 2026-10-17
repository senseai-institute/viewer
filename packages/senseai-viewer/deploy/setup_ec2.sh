#!/usr/bin/env bash
# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
#
# Install or repair the SenseAI stack and the viewer on a GPU box (EC2). Safe to run again:
# every step checks before it acts. The viewer's Setup panel runs it ("Install / repair");
# the first time, run it once by hand:
#
#   git clone https://github.com/senseai-institute/viewer ~/senseai/viewer
#   bash ~/senseai/viewer/packages/senseai-viewer/deploy/setup_ec2.sh --service
#
# Lines starting with "==>" are the progress the Setup panel shows.
#
# Environment (all optional):
#   SENSEAI_STACK_DIR     where the repos and the shared venv go        (default ~/senseai)
#   SENSEAI_DATA_ROOT     where recordings are prepared                 (default /data/dsec)
#   SENSEAI_GIT_BASE      where the repos live          (default https://github.com/senseai-institute)
#   SENSEAI_INSTALL_YOLO  1 = also install ultralytics (YOLO, AGPL: benchmark only, never shipped)
#   SENSEAI_TORCH_INDEX   PyTorch wheel index (default: CUDA 12.4 with a GPU, CPU without)
#
# --service  also install and start deploy/senseai-viewer.service (needs sudo).

set -euo pipefail

main() {
  local STACK="${SENSEAI_STACK_DIR:-$HOME/senseai}"
  local DATA="${SENSEAI_DATA_ROOT:-/data/dsec}"
  local GIT_BASE="${SENSEAI_GIT_BASE:-https://github.com/senseai-institute}"
  local REPOS=(ingest tokens profiles world reflex sdk bench)
  local VENV="$STACK/.venv"
  local HERE VIEWER_PKG VIEWER_REPO
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  VIEWER_PKG="$(cd "$HERE/.." && pwd)"
  VIEWER_REPO="$(cd "$VIEWER_PKG/../.." && pwd)"
  local SERVICE=0
  [[ "${1:-}" == "--service" ]] && SERVICE=1

  mkdir -p "$STACK"
  export PATH="$HOME/.local/bin:$STACK/.tools/node/bin:$PATH"

  step "checking system tools"
  local need=()
  for t in git curl tar; do command -v "$t" >/dev/null || need+=("$t"); done
  if ((${#need[@]})); then
    if sudo -n true 2>/dev/null; then
      sudo apt-get update -qq && sudo apt-get install -y -qq "${need[@]}" build-essential
    else
      fail "missing ${need[*]}; install them once with: sudo apt-get install -y ${need[*]}"
    fi
  fi

  if ! command -v uv >/dev/null; then
    step "installing uv (Python package manager)"
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi

  step "getting the SenseAI repositories into $STACK"
  for r in "${REPOS[@]}"; do
    if [[ -d "$STACK/$r/.git" ]]; then
      git -C "$STACK/$r" pull --ff-only -q || echo "    $r: local changes, not updated (left as is)"
    else
      git clone -q "$GIT_BASE/$r" "$STACK/$r" ||
        fail "could not clone $GIT_BASE/$r. If the repositories are private, give this machine read access once (for example: gh auth login, or a deploy key) and press Install / repair again."
    fi
    echo "    $r $(git -C "$STACK/$r" rev-parse --short HEAD)"
  done
  if [[ -d "$VIEWER_REPO/.git" ]]; then
    git -C "$VIEWER_REPO" pull --ff-only -q || echo "    viewer: local changes, not updated (left as is)"
  fi

  if [[ ! -x "$VENV/bin/python" ]]; then
    step "creating the shared Python environment"
    uv venv -q --python 3.11 "$VENV"
  fi

  local index="${SENSEAI_TORCH_INDEX:-}"
  if [[ -z "$index" ]]; then
    if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then
      index="https://download.pytorch.org/whl/cu124"
    else
      index="https://download.pytorch.org/whl/cpu"
    fi
  fi
  if ! "$VENV/bin/python" -c "import torch, torchvision" 2>/dev/null; then
    step "installing PyTorch and torchvision ($(basename "$index"))"
    uv pip install -q --python "$VENV/bin/python" torch torchvision --index-url "$index"
  fi

  step "installing the SenseAI packages"
  local args=()
  for r in "${REPOS[@]}"; do
    while IFS= read -r p; do
      grep -q '^\[project\]' "$p" && args+=(-e "$(dirname "$p")")
    done < <(find "$STACK/$r" -maxdepth 3 -name pyproject.toml \
      -not -path '*/node_modules/*' -not -path '*/.venv/*' -not -path '*/tests/*' -not -path '*/examples/*' | sort)
  done
  uv pip install -q --python "$VENV/bin/python" "${args[@]}"

  if [[ "${SENSEAI_INSTALL_YOLO:-0}" == "1" ]]; then
    step "installing ultralytics (YOLO). AGPL: used only to benchmark against, never shipped"
    uv pip install -q --python "$VENV/bin/python" ultralytics
  fi

  step "installing the viewer"
  (cd "$VIEWER_PKG" && uv sync -q --extra s3)
  if ! node -e 'process.exit(+process.versions.node.split(".")[0] >= 20 ? 0 : 1)' 2>/dev/null; then
    step "installing Node.js 22 (to build the web page)"
    local arch
    arch="$(uname -m | sed 's/x86_64/x64/; s/aarch64/arm64/')"
    mkdir -p "$STACK/.tools"
    curl -fsSL "https://nodejs.org/dist/v22.20.0/node-v22.20.0-linux-$arch.tar.xz" | tar -xJ -C "$STACK/.tools"
    rm -rf "$STACK/.tools/node" && mv "$STACK/.tools/node-v22.20.0-linux-$arch" "$STACK/.tools/node"
  fi
  step "building the web page"
  (cd "$VIEWER_PKG/web" && npm ci --silent --no-audit --no-fund && npm run -s build)

  if [[ ! -d "$DATA" ]]; then
    step "creating the recordings folder $DATA"
    mkdir -p "$DATA" 2>/dev/null || { sudo -n mkdir -p "$DATA" && sudo -n chown "$(id -u):$(id -g)" "$DATA"; } ||
      fail "cannot create $DATA; run once: sudo mkdir -p $DATA && sudo chown $USER $DATA"
  fi

  step "checking the installation"
  "$VENV/bin/python" - <<'PY'
import importlib.util, sys
mods = ["senseai_ingest", "senseai_tokens", "senseai_profiles", "senseai_world", "senseai_reflex", "senseai_sdk", "senseai_bench", "torch", "torchvision"]
missing = [m for m in mods if importlib.util.find_spec(m) is None]
if missing:
    print("    missing:", ", ".join(missing)); sys.exit(1)
import torch
print("    all packages import; GPU for PyTorch:", torch.cuda.is_available())
PY

  if ((SERVICE)); then
    step "installing the systemd service (viewer on 127.0.0.1:8000)"
    sed -e "s#@USER@#$(id -un)#g" -e "s#@STACK@#$STACK#g" -e "s#@VIEWER@#$VIEWER_PKG#g" -e "s#@DATA@#$DATA#g" -e "s#@HOME@#$HOME#g" \
      "$HERE/senseai-viewer.service" | sudo tee /etc/systemd/system/senseai-viewer.service >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable --now senseai-viewer
    sudo systemctl restart senseai-viewer
  elif systemctl is-active -q senseai-viewer 2>/dev/null && sudo -n true 2>/dev/null; then
    # Running jobs survive this (KillMode=process); the page reconnects by itself.
    step "restarting the viewer to load the update"
    sudo -n systemctl restart senseai-viewer
  fi
  step "done"
}

step() { echo "==> $*"; }
fail() {
  echo "==> failed: $*" >&2
  exit 1
}

# Everything is in main() so bash has read the whole file before `git pull` can change it.
main "$@"
