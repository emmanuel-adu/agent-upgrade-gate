.PHONY: install test lint typecheck check validate quick demo clean

install:
	pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check src tests

typecheck:
	mypy

check: lint typecheck test validate

validate:
	ugate validate-fixtures

# One simulated upgrade on the smoke tier: the fastest way to see a report.
quick:
	ugate run --agent configs/sim-baseline.yaml --label baseline --out demo/quick/baseline --tier smoke --trials 2
	ugate run --agent configs/sim-candidate-regressed.yaml --label candidate --out demo/quick/candidate --tier smoke --trials 2
	ugate compare demo/quick/baseline/run.json demo/quick/candidate/run.json --out demo/quick > /dev/null
	@echo "decision: $$(cat demo/quick/decision.txt)  ->  demo/quick/report.html"

# Three simulated upgrades through the real harness: APPROVE, STAGED_ROLLOUT, REJECT.
demo:
	ugate run --agent configs/sim-baseline.yaml --label baseline --out demo/runs/baseline --trials 3
	ugate run --agent configs/sim-candidate-compatible.yaml --label candidate-compatible --out demo/runs/candidate-compatible --trials 3
	ugate run --agent configs/sim-candidate-regressed.yaml --label candidate-regressed --out demo/runs/candidate-regressed --trials 3
	ugate compare demo/runs/baseline/run.json demo/runs/baseline/run.json --out demo/report-aa > /dev/null
	ugate compare demo/runs/baseline/run.json demo/runs/candidate-compatible/run.json --out demo/report-compatible > /dev/null
	ugate compare demo/runs/baseline/run.json demo/runs/candidate-regressed/run.json --out demo/report-regressed > /dev/null
	@for r in aa compatible regressed; do printf "%-12s %s\n" $$r "$$(cat demo/report-$$r/decision.txt)"; done

clean:
	rm -rf demo runs report .pytest_cache
