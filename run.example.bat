@echo off
chcp 936 >nul
REM ============================================================
REM  抖音 喜欢视频 批量下载 - Windows 一键运行示例
REM  使用前:
REM   1) 复制 cookie.txt.example 为 cookie.txt, 粘贴你的完整 Cookie
REM   2) sec_user_id 二选一:
REM        a) 复制 sec_user_id.txt.example 为 sec_user_id.txt, 把值写进去 (推荐, 无需改本文件)
REM        b) 或者修改下面的 SEC_USER_ID
REM   3) 按需修改 OUT 输出目录
REM  想先预览不下载, 就在 python 那行末尾加 --dry-run
REM ============================================================

set "SEC_USER_ID=MS4wLjABAAAA请替换成你的sec_user_id"
set "OUT=D:\抖音\喜欢"
set "COOKIE_FILE=%~dp0cookie.txt"
set "SEC_FILE=%~dp0sec_user_id.txt"

if not exist "%COOKIE_FILE%" (
    echo.
    echo [错误] 找不到 Cookie 文件: "%COOKIE_FILE%"
    echo 请复制 cookie.txt.example 为 cookie.txt, 再把你的完整 Cookie 粘贴进去.
    echo.
    pause
    exit /b 1
)

if exist "%SEC_FILE%" (
    python "%~dp0dy_favorite_dl.py" --sec-user-id-file "%SEC_FILE%" --out "%OUT%" --cookie-file "%COOKIE_FILE%"
) else (
    python "%~dp0dy_favorite_dl.py" --sec-user-id "%SEC_USER_ID%" --out "%OUT%" --cookie-file "%COOKIE_FILE%"
)

echo.
echo 下载结束, 按任意键退出...
pause >nul
