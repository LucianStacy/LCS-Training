@echo off
rem Starts LCS-Training without Docker and opens it in your browser.
cd /d "%~dp0"
python -m pip install --quiet -r requirements.txt
python -m lcs_training
