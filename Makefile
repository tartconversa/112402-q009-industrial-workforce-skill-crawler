.PHONY: test demo collect export clean

test:
	python3 -m unittest discover -s tests -v

demo:
	rm -f data/demo.sqlite3
	python3 -m parkbridge --db data/demo.sqlite3 init --samples
	python3 -m parkbridge --db data/demo.sqlite3 report --company C-1

collect:
	python3 -m parkbridge --db data/parkbridge.sqlite3 collect --config examples/local_sources.json

export:
	python3 -m parkbridge --db data/parkbridge.sqlite3 export-csv --outdir exports

clean:
	rm -rf __pycache__ parkbridge/__pycache__ tests/__pycache__ data exports
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
