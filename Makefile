
.PHONY: migrate test run
migrate:
	python -m scripts.migrate
test:
	python -m unittest discover -s tests
run:
	uvicorn app.main:app --host 0.0.0.0 --port $${PORT:-8080}
