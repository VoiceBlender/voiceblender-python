# VOICEBLENDER points to the VoiceBlender repository root.
# Override on the command line: make generate VOICEBLENDER=/other/path
VOICEBLENDER ?= ../VoiceBlender
OPENAPI      := $(VOICEBLENDER)/openapi.yaml
ASYNCAPI     := $(VOICEBLENDER)/asyncapi.yaml

PY ?= python3

.PHONY: generate lint format typecheck test build install-dev clean

# generate reads openapi.yaml + asyncapi.yaml and rewrites the generated files
# (_models.py, _requests.py, _responses.py, _events.py, _legs.py, _rooms.py,
# _webrtc.py, _vsi.py). Run this whenever either spec changes.
generate:
	$(PY) tools/generate.py --openapi $(OPENAPI) --asyncapi $(ASYNCAPI) --out src/voiceblender
	ruff format src/voiceblender
	ruff check --fix src/voiceblender
	$(MAKE) typecheck

lint:
	ruff check src/voiceblender tools tests

format:
	ruff format src/voiceblender tools tests

typecheck:
	mypy src/voiceblender

test:
	pytest -q

build:
	$(PY) -m build

install-dev:
	$(PY) -m pip install -e ".[dev]"

clean:
	rm -rf build/ dist/ *.egg-info src/voiceblender/__pycache__ tests/__pycache__
	rm -rf .mypy_cache .pytest_cache .ruff_cache
