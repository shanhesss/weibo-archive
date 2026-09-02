@echo off
chcp 936 >nul
cd /d "%~dp0"
title ΢���浵 - ���´��
echo ====================================
echo   ΢���浵 - ���´���ű�
echo ====================================

rem 1. ͣ���������е� exe�������ļ���ռ�ã��� exe ���ǲ��ˣ�
echo [1/3] �ر��������е� weibo_archive.exe ...
taskkill /f /im weibo_archive.exe >nul 2>&1
ping 127.0.0.1 -n 2 >nul

rem 2. ȷ�� PyInstaller �Ѱ�װ
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo �״�ʹ�ã����ڰ�װ PyInstaller ...
  python -m pip install pyinstaller
)

rem 3. ���´��
echo [2/3] ��ʼ�����Լ 1-2 ���ӣ����Ժ�...
python -m PyInstaller --noconfirm --onefile --noconsole --name weibo_archive --icon weibo_icon.ico --add-data "weibo_web.html;." --add-data "yuque-sync-template.md;." weibo_server.py
if errorlevel 1 (
  echo.
  echo *** ���ʧ�ܣ���鿴�Ϸ�������Ϣ��***
  pause
  exit /b 1
)

rem 4. �����м����
echo [3/3] ������ʱ�ļ� ...
if exist build rmdir /s /q build

echo.
echo ====================================
echo   �����ɣ��°汾�� dist\weibo_archive.exe
echo ====================================
pause
