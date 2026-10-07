# Lofi — 6-artifact builder
# 1 arm + 1 native for each OS: debian (linux), macOS, Windows
# Each PyInstaller host builds only its own arch; CI builds all 6 natively.
# Use:
#   make help
#   make build            # this host's artifact (lofi-<os>-<arch>)
#   make artifacts        # describe the 6-way matrix
#   make dist             # build + archives (tar.gz/zip) for this host
#   make dist-all         # emulate 6 archives locally (copies binary 6x for inspection)
#   make check            # run built binary with --check
#   make clean

PYTHON ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
PY  := $(VENV)/bin/python
VERSION := $(shell cat VERSION 2>/dev/null | tr -d ' \r\n' || echo 0.0.0)
OS := $(shell $(PY) -c "import sys; print({'linux':'linux','darwin':'macos','win32':'windows'}.get(sys.platform, sys.platform))" 2>/dev/null || echo linux)
ARCH := $(shell $(PY) -c "import platform; m=platform.machine().lower(); print('arm64' if 'aarch64' in m or 'arm64' in m or 'armv8' in m else 'amd64')" 2>/dev/null || echo amd64)
ARTIFACT := lofi-$(OS)-$(ARCH)
ifeq ($(OS),windows)
  ARTIFACT := lofi-$(OS)-$(ARCH).exe
  ARCHIVE  := lofi-$(VERSION)-$(OS)-$(ARCH).zip
else
  ARCHIVE  := lofi-$(VERSION)-$(OS)-$(ARCH).tar.gz
endif

.PHONY: help build dist artifacts dist-all check clean venv install

help:
	@echo "Lofi $(VERSION) — 6 native artifacts (1 arm + 1 native per OS)"
	@echo ""
	@echo "  Host detected: $(OS)/$(ARCH) → $(ARTIFACT)  $(ARCHIVE)"
	@echo ""
	@echo "Targets:"
	@echo "  make venv        create $(VENV) and install deps + PyInstaller"
	@echo "  make build       PyInstaller single-file for THIS host → dist/$(ARTIFACT)"
	@echo "  make dist        build + archive (tar.gz/zip) for THIS host"
	@echo "  make artifacts   show the 6-way matrix (no build)"
	@echo "  make dist-all    emulate 6 archives locally by copying THIS binary 6× (inspection)"
	@echo "  make check       run the built binary with --check"
	@echo "  make clean       remove build/ dist/"
	@echo ""
	@echo "CI builds all 6 natively; see .github/workflows/release.yml"
	@echo "PyInstaller cannot cross-compile: arm64 needs an arm64 runner."

venv:
	@test -d $(VENV) || $(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt pyinstaller

install: venv

build:
	@test -f lofi.spec || (echo "lofi.spec missing" && exit 2)
	@test -x $(PY) || (echo "Run 'make venv' first" && exit 2)
	$(PY) scripts/build.py

dist: build
	@echo "Archive at dist/$(ARCHIVE):"
	@ls -lh dist/$(ARCHIVE) 2>/dev/null || ls -lh dist/*.tar.gz dist/*.zip 2>/dev/null | head -n 20

artifacts:
	@$(PY) scripts/build.py --all

dist-all: build
	@echo "Emulating 6 artifacts from this host's binary for inspection..."
	@$(PY) scripts/make_dist_all.py

check:
	@BIN="dist/$(ARTIFACT)"; \
	if [ ! -f "$$BIN" ]; then BIN="dist/lofi"; fi; \
	if [ ! -f "$$BIN" ]; then echo "No binary yet. Run 'make build'." && exit 1; fi; \
	LOFI_TOKEN=ci-dummy-token LOFI_HOME=$$(mktemp -d) "$$BIN" --check; echo "exit $$?"

clean:
	rm -rf build/ dist/ __pycache__/ .pytest_cache/
	$(PY) scripts/build.py --clean 2>/dev/null || true

