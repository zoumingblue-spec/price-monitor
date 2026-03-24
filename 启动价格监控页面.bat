@echo off
chcp 65001 >nul
echo.
echo  ====================================
echo   竞品亚马逊价格监控 - 启动服务
echo  ====================================
echo.
echo  正在启动服务器...
echo  浏览器即将打开: http://localhost:8899
echo.
start "" http://localhost:8899
python "%~dp0server.py"
pause
