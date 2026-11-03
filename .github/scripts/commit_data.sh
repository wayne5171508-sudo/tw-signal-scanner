#!/bin/bash
# 各 workflow 共用的提交:只 add 存在的路徑。
# 路徑不存在時 git add 會讓整步失敗(bear 的 bias_log、回補的 backfill_tried 都遇過);
# 沒有變更就成功結束,非交易日重跑不會再推一個空 commit。
# 用法: bash .github/scripts/commit_data.sh "提交訊息" 路徑 [路徑...]
# 測試設 COMMIT_DATA_NO_PUSH=1 就只 commit、不 pull/push。
set -u
if [ "$#" -lt 2 ]; then
  echo "用法: commit_data.sh \"訊息\" 路徑..." >&2
  exit 2
fi
msg=$1
shift
git config user.name "tw-signal-scanner-bot"
git config user.email "actions@users.noreply.github.com"
any=0
for p in "$@"; do
  if [ -e "$p" ]; then
    git add -- "$p"
    any=1
  else
    echo "略過不存在的 $p"
  fi
done
if [ "$any" -eq 0 ]; then
  echo "沒有可提交的檔案"
  exit 0
fi
if git diff --quiet --cached; then
  echo "沒有變更"
  exit 0
fi
git commit -m "$msg"
if [ "${COMMIT_DATA_NO_PUSH:-}" = "1" ]; then
  exit 0
fi
for i in 1 2 3; do
  if git pull --rebase origin main && git push; then
    exit 0
  fi
  git rebase --abort 2>/dev/null || true
  sleep 10
done
echo "push 失敗"
exit 1
