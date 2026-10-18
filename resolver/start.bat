@echo off
chcp 65001 >nul
cd /d "%~dp0"
title media-resolver
echo ============================================
echo  本地短视频解析器（抖音 / 小红书） / media-resolver
echo  按 Ctrl+C 退出
echo ============================================
if not exist "config.json" (
  echo [!] 缺少 config.json，请先复制 config.example.json 并填写 server / token。
  pause
  exit /b 1
)
where node >nul 2>nul
if errorlevel 1 (
  echo [!] 没有找到 node，请先安装 Node.js。
  pause
  exit /b 1
)
node agent.js
echo.
echo 解析器已退出。
pause
