#!/usr/bin/env bash
# One-way mirror of the shared Drive upload folder
# ("PIPELINE ROUTES - geojson files for Q1 2026 oil/NGL update")
# into drive-uploads/, using rclone instead of Google Drive for Desktop.
#
# One-time setup (opens a browser to log in with the GEM Google account).
# scope=drive gives read-write access so merged routes can be trashed from
# Drive without leaving the terminal:
#
#   rclone config create gem-pipeline-uploads drive \
#     scope=drive \
#     root_folder_id=11FYNDrY0yP71vaovPHQCeRThM4H8ZeHv
#
# The sync itself is Drive -> local only, and a true mirror: files removed
# from Drive (e.g. trashed after QC) are deleted locally on the next sync,
# so never hand-edit anything under drive-uploads/ (it's git-ignored).
#
# After a route is merged into data/individual-routes/, trash the Drive
# original (rclone's drive backend trashes rather than hard-deletes by
# default, so it's recoverable for 30 days):
#
#   rclone deletefile "gem-pipeline-uploads:<researcher>/P####.geojson"
set -euo pipefail

REMOTE="gem-pipeline-uploads"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$REPO_DIR/drive-uploads"

command -v rclone >/dev/null || { echo "rclone not found — brew install rclone" >&2; exit 1; }
rclone listremotes | grep -qx "${REMOTE}:" || {
  echo "rclone remote '${REMOTE}:' not configured — run the one-time setup command in this script's header" >&2
  exit 1
}

rclone sync "${REMOTE}:" "$DEST" --create-empty-src-dirs --exclude ".DS_Store" -v

echo
git -C "$REPO_DIR" status --short -- drive-uploads/
