# Lofi — 6-artifact builder (.deb / .dmg / .exe)
# 1 arm + 1 native for each OS: debian (linux), macOS, Windows
# Each PyInstaller host builds only its own arch; CI builds all 6 natively.
# Use:
#   make help
#   make build            # this host's binary + OS-native package
#   make artifacts        # describe the 6-way matrix
#   make dist             # build + package (.deb/.dmg/.exe) for this host
#   make dist-all         # emulate 6 packages locally (copies binary 6x for inspection)
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
  PACKAGE  := lofi-$(VERSION)-$(OS)-$(ARCH)-setup.exe
else ifeq ($(OS),macos)
  PACKAGE  := lofi-$(VERSION)-$(OS)-$(ARCH).dmg
else
  PACKAGE  := lofi-$(VERSION)-$(OS)-$(ARCH).deb
endif

.PHONY: help build dist artifacts dist-all check clean venv install

help:
	@echo "Lofi $(VERSION) — 6 native OS packages (1 arm + 1 native per OS: .deb/.dmg/.exe)"
	@echo ""
	@echo "  Host detected: $(OS)/$(ARCH) → $(ARTIFACT)  $(PACKAGE)"
	@echo ""
	@echo "Targets:"
	@echo "  make venv        create $(VENV) and install deps + PyInstaller"
	@echo "  make build       PyInstaller binary + OS package for THIS host → dist/$(PACKAGE)"
	@echo "  make dist        build + package (.deb/.dmg/.exe) for THIS host"
	@echo "  make artifacts   show the 6-way matrix (no build)"
	@echo "  make dist-all    emulate 6 packages locally by copying THIS binary 6× (inspection)"
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
	@echo "Package at dist/$(PACKAGE):"
	@ls -lh dist/$(PACKAGE) 2>/dev/null || ls -lh dist/*.deb dist/*.dmg dist/*.exe 2>/dev/null | head -n 20

artifacts:
	@$(PY) scripts/build.py --all

dist-all: build
	@echo "Emulating 6 packages (.deb/.dmg/.exe) from this host's binary for inspection..."
	@$(PY) scripts/make_dist_all.py

check:
	@BIN="dist/$(ARTIFACT)"; \
	if [ ! -f "$$BIN" ]; then BIN="dist/lofi"; fi; \
	if [ ! -f "$$BIN" ]; then echo "No binary yet. Run 'make build'." && exit 1; fi; \
	LOFI_TOKEN=ci-dummy-token LOFI_HOME=$$(mktemp -d) "$$BIN" --check; echo "exit $$?"

clean:
	rm -rf build/ dist/ __pycache__/ .pytest_cache/
	$(PY) scripts/build.py --clean 2>/dev/null || true

