#!/usr/bin/env bash
# Point Blender at this tree, instead of at a copy of it.
#
# The addon lives in two places by default: this repo, and a snapshot under
# `~/.config/blender/<ver>/scripts/addons/exmateria_map`. Two copies is the
# whole bug -- on 2026-08-27 the artist was clicking a snapshot a test run had
# installed hours earlier while the repo, and both green suites, described
# something else. A SYMLINK removes the second copy, so "did my change land"
# stops being a question you can get wrong.
#
#   ./tools/dev_install.sh            # every Blender version found
#   ./tools/dev_install.sh 5.2        # just that one
#   ./tools/dev_install.sh --copy 5.2 # a real copy (for testing a release)
#
# An existing real directory is MOVED ASIDE, never deleted -- it may be an
# install you meant to keep.
set -euo pipefail

MODE=link
if [ "${1:-}" = "--copy" ]; then MODE=copy; shift; fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../addons/exmateria_map" && pwd)"
[ -f "$SRC/__init__.py" ] || { echo "no addon at $SRC" >&2; exit 1; }

if [ $# -gt 0 ]; then
  ROOTS=()
  for v in "$@"; do ROOTS+=("$HOME/.config/blender/$v"); done
else
  mapfile -t ROOTS < <(find "$HOME/.config/blender" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort)
fi
[ ${#ROOTS[@]} -gt 0 ] || { echo "no Blender config under ~/.config/blender" >&2; exit 1; }

for root in "${ROOTS[@]}"; do
  dest="$root/scripts/addons/exmateria_map"
  mkdir -p "$(dirname "$dest")"
  if [ -L "$dest" ]; then
    rm "$dest"
  elif [ -e "$dest" ]; then
    aside="$dest.aside-$(date +%Y%m%d-%H%M%S)"
    mv "$dest" "$aside"
    echo "  moved the existing copy to $aside"
  fi
  if [ "$MODE" = link ]; then
    ln -s "$SRC" "$dest"
    echo "linked  $dest -> $SRC"
  else
    cp -r "$SRC" "$dest"
    echo "copied  $SRC -> $dest"
  fi
  # A stale __pycache__ beside newer sources is a second way to run old code.
  find "$dest/" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
done

echo
echo "Restart Blender (or disable/re-enable the addon) -- module imports are cached."
echo "On enable it prints where it loaded from; that line is the check."
