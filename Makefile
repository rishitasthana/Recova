# ReCova — Makefile
# One-command shortcuts for the most common dev and demo workflows.
#
# Fastest path to see the full pipeline work end-to-end:
#   make setup && make discover && make seed && make batch && make dashboard
#
# Note: discover and seed require live Razorpay test credentials in .env.
# Run 'make setup' first and fill in .env before calling those targets.

.PHONY: setup discover seed simulate batch dashboard api test coverage clean

# ─────────────────────────────────────────────────────────────────────────────
# setup — install dependencies and create .env from the example template
# ─────────────────────────────────────────────────────────────────────────────
setup:
	pip install -r requirements.txt
	cp -n .env.example .env || true
	@echo ""
	@echo "✅  Dependencies installed."
	@echo "👉  Next: edit .env with your Razorpay test credentials,"
	@echo "    then run: make discover"

# ─────────────────────────────────────────────────────────────────────────────
# discover — probe the live Razorpay test API and write data/discovered_codes.json
# (Step 0 — must run before anything else that uses the classifier)
# ─────────────────────────────────────────────────────────────────────────────
discover:
	python scripts/discover_decline_codes.py

# ─────────────────────────────────────────────────────────────────────────────
# seed — create test customers, plans, and subscriptions in Razorpay test mode
# ─────────────────────────────────────────────────────────────────────────────
seed:
	python scripts/seed_razorpay.py

# ─────────────────────────────────────────────────────────────────────────────
# simulate — trigger batched test-card payments via the Razorpay test API
# (requires a running 'make api' server in another terminal)
# ─────────────────────────────────────────────────────────────────────────────
simulate:
	python scripts/simulate_payments.py

# ─────────────────────────────────────────────────────────────────────────────
# batch — run 75 synthetic events through the full pipeline without a live server
# writes data/batch_results.json (read by the dashboard Batch Runner panel)
# ─────────────────────────────────────────────────────────────────────────────
batch:
	python scripts/batch_runner.py

# ─────────────────────────────────────────────────────────────────────────────
# dashboard — open the Streamlit recovery dashboard
# ─────────────────────────────────────────────────────────────────────────────
dashboard:
	streamlit run dashboard/app.py

# ─────────────────────────────────────────────────────────────────────────────
# api — start the FastAPI webhook receiver with hot-reload
# ─────────────────────────────────────────────────────────────────────────────
api:
	uvicorn api.main:app --reload

# ─────────────────────────────────────────────────────────────────────────────
# test — run the full pytest suite with verbose output
# ─────────────────────────────────────────────────────────────────────────────
test:
	pytest tests/ -v

# ─────────────────────────────────────────────────────────────────────────────
# coverage — run pytest with line-coverage report across all source modules
# Shows which lines in recova/ and api/ are not yet tested
# ─────────────────────────────────────────────────────────────────────────────
coverage:
	pytest tests/ --cov=recova --cov=api --cov-report=term-missing --cov-report=html:data/htmlcov -q
	@echo ""
	@echo "📊  HTML report written to data/htmlcov/index.html"


# ─────────────────────────────────────────────────────────────────────────────
# clean — remove the local DB and batch results
# Safe to run before a fresh demo. Re-run 'make seed' and 'make batch' after.
# ─────────────────────────────────────────────────────────────────────────────
clean:
	rm -f data/recova.db
	rm -f data/batch_results.json
	@echo "Removed data/recova.db and data/batch_results.json."
	@echo "Re-run 'make seed' and 'make batch' to regenerate."
