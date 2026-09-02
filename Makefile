# Makefile — Razorpay AI Revenue Recovery
# ---------------------------------------------------------------
# Windows users without GNU make should use `nmake` or run the
# commands listed in each target directly in PowerShell / cmd.
# ---------------------------------------------------------------

PYTHON  = .\venv\Scripts\python.exe
PYTEST  = .\venv\Scripts\pytest.exe
STREAMLIT = .\venv\Scripts\streamlit.exe

.DEFAULT_GOAL := help

help:           ## Print available commands (default)
	@echo.
	@echo   Razorpay AI Revenue Recovery — available make targets
	@echo   -------------------------------------------------------
	@echo   make help    Print this help message (default)
	@echo   make reset   Truncate DB and reseed with DEFAULT_SEED=1
	@echo   make run     Run the full pipeline on all 57 events
	@echo   make dash    Launch the Streamlit dashboard on localhost
	@echo   make test    Run the full pytest suite with -v
	@echo.

reset:          ## Truncate DB and reseed (interactive — asks confirmation)
	$(PYTHON) -m data.reset_db

run:            ## Run the full pipeline on all 57 events
	$(PYTHON) run_pipeline.py

dash:           ## Launch Streamlit dashboard on localhost
	$(STREAMLIT) run dashboard/app.py

test:           ## Run full pytest suite with verbose output
	$(PYTEST) tests/ -v
