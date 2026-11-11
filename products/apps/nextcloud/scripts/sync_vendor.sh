#!/usr/bin/env bash
# 以來源 repo 的「已 commit HEAD」重新 vendor nextcloud_mcp_server（MIT，WOOWTECH/Woow_nextcloud_mcp_server）。
#
# 用法（在 monorepo 任意目錄執行皆可）：
#   apps/nextcloud/scripts/sync_vendor.sh <src-repo-path>
#
# 做的事：
#   1. 從 <src-repo-path> 的 HEAD（git archive，只取已 commit 內容；工作目錄未 commit 的修改不會帶入）
#      取出 src/nextcloud_mcp_server/ 與 LICENSE；
#   2. 整個取代 apps/nextcloud/nextcloud_mcp_server/ 與 apps/nextcloud/LICENSE（vendored 碼維持與來源 byte-identical，
#      部署期約束一律放在 nextcloud_mcp_admin/launcher.py，不改 vendored 檔）；
#   3. 更新 apps/nextcloud/VENDORED.md 的 commit／日期。
#
# 之後請手動：比對來源 pyproject.toml 的 dependencies 是否變動（需要時改 apps/nextcloud/pyproject.toml
# 並用 uv 0.6.4 重新 lock）、跑 `uv run --directory apps/nextcloud pytest -q`、確認工具數（Dockerfile 自檢斷言 9 支）。
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: $0 <src-repo-path>" >&2
  exit 2
fi

SRC=$(cd "$1" && pwd)
APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DEST="$APP_DIR/nextcloud_mcp_server"
VENDORED_MD="$APP_DIR/VENDORED.md"

git -C "$SRC" rev-parse --verify -q HEAD >/dev/null || { echo "not a git repository with commits: $SRC" >&2; exit 1; }
COMMIT=$(git -C "$SRC" rev-parse HEAD)
git -C "$SRC" cat-file -e "HEAD:src/nextcloud_mcp_server/__init__.py" 2>/dev/null \
  || { echo "HEAD of $SRC has no src/nextcloud_mcp_server/" >&2; exit 1; }
if [ -n "$(git -C "$SRC" status --porcelain -- src/nextcloud_mcp_server LICENSE)" ]; then
  echo "warning: $SRC has uncommitted changes under src/nextcloud_mcp_server or LICENSE; vendoring HEAD only" >&2
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
git -C "$SRC" archive --format=tar HEAD src/nextcloud_mcp_server LICENSE | tar -x -C "$TMP"

rm -rf "$DEST"
cp -R "$TMP/src/nextcloud_mcp_server" "$DEST"
cp "$TMP/LICENSE" "$APP_DIR/LICENSE"
find "$DEST" -name '__pycache__' -type d -prune -exec rm -rf {} +

VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$DEST/__init__.py")
TODAY=$(date -u +%Y-%m-%d)
if [ -f "$VENDORED_MD" ]; then
  # 只改標記行，其他說明維持原樣。
  sed -i -E \
    -e "s|^- commit：\`[0-9a-f]{40}\`.*$|- commit：\`$COMMIT\`|" \
    -e "s|^- 版本：.*$|- 版本：\`$VERSION\`（\`nextcloud_mcp_server.__version__\`）|" \
    -e "s|^- vendor 日期：.*$|- vendor 日期：$TODAY|" \
    "$VENDORED_MD"
  grep -q "$COMMIT" "$VENDORED_MD" || { echo "failed to update commit in $VENDORED_MD" >&2; exit 1; }
else
  echo "warning: $VENDORED_MD not found; commit $COMMIT not recorded" >&2
fi

echo "vendored nextcloud_mcp_server $VERSION from $COMMIT"
