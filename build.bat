@echo off
echo [BUILD] Installing PyInstaller...
pip install pyinstaller pywebview --quiet

echo [BUILD] Packaging AsuraTECH Lead Gen to .exe ...
pyinstaller ^
    --name "AsuraTECH_LeadGen" ^
    --onedir ^
    --noconsole ^
    --add-data "src;src" ^
    --add-data "static;static" ^
    --hidden-import flask ^
    --hidden-import flask.templating ^
    --hidden-import psycopg2 ^
    --hidden-import psycopg2.extras ^
    --hidden-import psycopg2.extensions ^
    --hidden-import dotenv ^
    --hidden-import requests ^
    --hidden-import bs4 ^
    --hidden-import selenium ^
    --hidden-import webview ^
    --hidden-import webview.platforms.edgechromium ^
    --hidden-import email.mime.text ^
    --hidden-import email.header ^
    --hidden-import jinja2 ^
    --hidden-import werkzeug ^
    --collect-all webview ^
    app.py

echo [BUILD] Done. Output is in dist\AsuraTECH_LeadGen\
echo [BUILD] Copy your .env file into dist\AsuraTECH_LeadGen\ before running.
pause
