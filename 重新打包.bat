@echo off
chcp 936 >nul
cd /d "%~dp0"
title 微博存档 - 重新打包
echo ====================================
echo   微博存档 - 重新打包脚本
echo ====================================

rem 1. 停掉正在运行的 exe（否则文件被占用，新 exe 覆盖不了）
echo [1/3] 关闭正在运行的 weibo_archive.exe ...
taskkill /f /im weibo_archive.exe >nul 2>&1
ping 127.0.0.1 -n 2 >nul

rem 2. 确保 PyInstaller 已安装
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo 首次使用：正在安装 PyInstaller ...
  python -m pip install pyinstaller
)

rem 3. 重新打包
echo [2/3] 开始打包（约 1-2 分钟，请稍候）...
python -m PyInstaller --noconfirm --onefile --noconsole --name weibo_archive --add-data "weibo_web.html;." weibo_server.py
if errorlevel 1 (
  echo.
  echo *** 打包失败！请查看上方错误信息。***
  pause
  exit /b 1
)

rem 4. 清理中间产物
echo [3/3] 清理临时文件 ...
if exist build rmdir /s /q build

echo.
echo ====================================
echo   打包完成！新版本在 dist\weibo_archive.exe
echo ====================================
pause
