# VOICEBLENDER points to the VoiceBlender repository root.
# Override on the command line: make generate VOICEBLENDER=/other/path
VOICEBLENDER ?= ../VoiceBlender
OPENAPI      := $(VOICEBLENDER)/openapi.yaml
ASYNCAPI     := $(VOICEBLENDER)/asyncapi.yaml

PY ?= python3

.PHONY: generate lint format typecheck test build package release-check publish publish-test install-dev clean

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

# package runs the full check suite, empties dist/ and builds a fresh
# sdist + wheel, then validates the metadata twine will upload. Emptying dist/
# matters: `twine upload dist/*` would otherwise also push artifacts left over
# from an earlier version.
#
# The version is not in pyproject.toml — hatch-vcs derives it from the git tag,
# so `git tag -a v1.2.3 -m ...` is the whole version bump.
package: lint typecheck test
	rm -rf dist
	$(PY) -m build
	$(PY) -m twine check dist/*
	@echo "built version $$(ls dist/*.tar.gz | sed 's|.*/voiceblender-||; s|\.tar\.gz$$||')"

# release-check refuses to upload anything that isn't a clean tagged release.
# An untagged or dirty tree builds a version like 0.12.2.dev0+ge a002ce; PyPI
# rejects local version segments outright, so catch it here with a message that
# says what to do instead of an opaque 400 after the upload.
release-check:
	@v=$$(ls dist/*.tar.gz | sed 's|.*/voiceblender-||; s|\.tar\.gz$$||'); \
	case "$$v" in \
	  *dev*|*+*) \
	    echo "refusing to publish $$v — not a release build."; \
	    echo "commit your changes, then tag the release: git tag -a vX.Y.Z -m 'vX.Y.Z'"; \
	    exit 1;; \
	esac; \
	echo "publishing $$v"

# publish uploads the freshly built dist/ to PyPI.
#
# The version is the git tag: tag `vX.Y.Z`, then run this. PyPI accepts a given
# version exactly once, and deleting a release does not free the number for
# re-upload, so a tag is effectively immutable once published.
# Credentials come from ~/.pypirc, or from the environment:
#
#     TWINE_USERNAME=__token__ TWINE_PASSWORD=pypi-<api-token> make publish
#
# Use `make publish-test` for a dry run against TestPyPI first.
publish: package release-check
	$(PY) -m twine upload dist/*

# publish-test uploads to TestPyPI (https://test.pypi.org) instead, which needs
# its own account and API token under a [testpypi] section in ~/.pypirc.
publish-test: package release-check
	$(PY) -m twine upload --repository testpypi dist/*

install-dev:
	$(PY) -m pip install -e ".[dev]"

clean:
	rm -rf build/ dist/ *.egg-info src/voiceblender/__pycache__ tests/__pycache__
	rm -rf .mypy_cache .pytest_cache .ruff_cache
