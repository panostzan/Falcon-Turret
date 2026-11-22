@echo off
title Turret
cd /d "%~dp0"
python launch.py
if errorlevel 1 pause
