@echo off
chcp 65001 >nul
SETLOCAL Enabledelayedexpansion

echo =======================================
echo       Universal GitHub Sync Tool
echo =======================================

:: 1. 智能安全检查：检查是否有任何在 .gitignore 里的文件被强制添加了
:: 这个逻辑比指定文件名更通用
git status --short | findstr "!!" >nul
if !errorlevel! equ 0 (
    echo [!] Warning: Some ignored files are staged for commit.
    echo [!] Please check your .gitignore and run 'git rm -r --cached .'
    pause
    exit /b
)

:: 2. 检查是否有未加密的 .env 文件（这是程序员最通用的敏感文件）
git status --short | findstr ".env" >nul
if !errorlevel! equ 0 (
    echo [❌ Safety Stop] .env detected in stage! Check .gitignore immediately.
    pause
    exit /b
)

:: 3. 执行同步
echo [1/3] Adding changes...
git add .

set /p msg="Update message (Enter for default): "
if "%msg%"=="" set msg=Automated Update %date% %time%

echo [2/3] Committing: %msg%
git commit -m "%msg%"

echo [3/3] Pushing to remote...
git push

echo =======================================
echo [OK] Sync Completed!
echo =======================================
pause