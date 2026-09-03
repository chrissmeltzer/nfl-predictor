.PHONY: calibrate run test sync

calibrate:
	python3 scripts/calibrate_weights.py

run:
	uvicorn app.main:app --reload

test:
	pytest

sync:
	curl -X POST http://localhost:8000/sync
