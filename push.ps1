# push.ps1 [-Message "提交说明"]
#
# 本地提交后一键推送两个远端：
#   origin -> master（完整历史）
#   github -> main （快照模式：基于 GitHub 当前 main 追加提交，不携带本地旧历史）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File push.ps1
#   powershell -ExecutionPolicy Bypass -File push.ps1 -Message "更新说明"
#
# 注意：执行前请先本地提交（git add -A && git commit），否则脚本拒绝执行。
param(
    [string]$Message = "同步站点更新"
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo

# ---------- 1. 检查工作区 ----------
$dirty = git status --porcelain
if ($LASTEXITCODE -ne 0) { throw "git status 执行失败" }
if ($dirty) {
    Write-Host "错误：工作区有未提交改动，请先执行：git add -A && git commit"
    exit 1
}

# ---------- 2. 原远端（完整历史） ----------
Write-Host "==> 推送 origin master ..."
git push origin master
if ($LASTEXITCODE -ne 0) { throw "推送 origin master 失败" }

# ---------- 3. GitHub 快照模式 ----------
Write-Host "==> 拉取 GitHub main 最新引用 ..."
git fetch github main 2>$null
if ($LASTEXITCODE -ne 0) { throw "拉取 github main 失败" }

git branch -D gh-sync 2>$null | Out-Null
git checkout -B gh-sync github/main
if ($LASTEXITCODE -ne 0) { throw "创建临时分支 gh-sync 失败" }

try {
    # 用本地 master 的完整文件树替换快照
    git rm -rf --ignore-unmatch -q .
    git checkout master -- .
    git add -A

    git diff --cached --quiet
    if ($LASTEXITCODE -eq 0) {
        Write-Host "GitHub 内容与本地一致，跳过提交。"
    } else {
        git commit -q -m $Message
        if ($LASTEXITCODE -ne 0) { throw "提交失败" }
        Write-Host "==> 推送 github main ..."
        git push github gh-sync:main
        if ($LASTEXITCODE -ne 0) { throw "推送 github main 失败" }
    }
}
finally {
    git checkout -q master
    git branch -D gh-sync 2>$null | Out-Null
}

Write-Host "完成：两个远端均已同步。"
