@echo off
title OnePlay Live Subscription Tracker
cd /d "%~dp0"
echo ===================================================
echo Starting OnePlay Subscription Tracker...
echo Target Plans: Haven HD (799), Haven 4K (1099), Enthusiast (1299), Prime (1599)
echo ===================================================
python tracker.py
pause
