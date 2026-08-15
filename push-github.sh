#!/usr/bin/env bash
# push-github.sh [提交说明]
#
# 将本地 master 的最新内容以"从现在开始"的快照方式推送到 GitHub
# （https://github.com/mantianxh/read.git）的 main 分支：
#   - 基于 GitHub 当前的 main 追加一个提交（不携带本地 master 的旧历史）
#   - 内容 = 本地 master 的完整工作树（含 dist/ 构建产物）
#
# 用法：
#   ./push-github.sh                # 默认提交说明：同步站点更新
#   ./push-github.sh "更新说明文字"
#
# 注意：执行前请先提交本地改动（git add -A && git commit），否则脚本会拒绝执行。
set -euo pipefail
cd "$(dirname "$0")"

MSG="${1:-同步站点更新}"

if [ -n "$(git status --porcelain)" ]; then
  echo "错误：工作区有未提交改动，请先执行：git add -A && git commit"
  exit 1
fi

echo "拉取 GitHub main 最新引用..."
git fetch github main 2>/dev/null || true

# 基于 GitHub main 创建临时分支（无旧历史）
git branch -D gh-sync 2>/dev/null || true
git checkout -B gh-sync github/main

# 用本地 master 的完整文件树替换快照
git rm -rf --ignore-unmatch -q . || true
git checkout master -- .
git add -A

if git diff --cached --quiet; then
  echo "内容与 GitHub main 一致，无需推送。"
else
  git commit -q -m "$MSG"
  echo "推送 GitHub main..."
  git push github gh-sync:main
fi

git checkout -q master
git branch -D gh-sync
echo "完成：GitHub main 已同步（快照模式，不含旧历史）。"
