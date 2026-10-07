# Debian / Ubuntu packaging — `.deb`

Two packages are published, one per architecture, built with `dpkg-deb`:

```bash
sudo dpkg -i lofi-linux-amd64.deb && sudo apt-get install -f -y   # amd64
sudo dpkg -i lofi-linux-arm64.deb && sudo apt-get install -f -y   # arm64: Pi, Graviton
sudo apt install ffmpeg libopus0            # runtime deps, not bundled
lofi --check                                # binary at /usr/local/bin/lofi
lofi                                        # asks for the token if it has none
```

`postinst` asks for the bot token when it is run interactively on a machine
that does not have one yet, and writes it into `/etc/lofi/lofi.env` (0600).
Under `apt`, a Docker build or configuration management there is nobody to
answer, so it asks nothing and prints the instructions instead — a blocking
read there would hang the install.

## What is inside

```
DEBIAN/control                 Package, Version, Architecture, Depends
DEBIAN/postinst|prerm|postrm   maintainer scripts
usr/local/bin/lofi             0755, the PyInstaller one-file binary
usr/share/lofi/                lofi.env, lofi.service, config.example.json
usr/share/doc/lofi/            README.md, LICENSE, config.example.json, copyright
usr/share/doc/lofi/examples/   lofi.env, lofi.service
usr/share/man/man1/lofi.1.gz   man page
lib/systemd/system/lofi.service
```

`Depends: ffmpeg, libopus0, ca-certificates`. **`python3` is not a dependency**:
the binary carries its own interpreter, so adding it would install an
interpreter the package never uses.

The examples live in `/usr/share/lofi/` as well as `/usr/share/doc/`, because
slim Debian/Ubuntu images configure `path-exclude=/usr/share/doc/*` in
`/etc/dpkg/dpkg.cfg.d/` and the postinst needs the env file to exist.

## Running it as a service

The unit is installed **disabled** — the bot needs a token before it can start.

```bash
sudo useradd --system --home /var/lib/lofi --create-home --shell /usr/sbin/nologin lofi
sudo editor /etc/lofi/lofi.env          # LOFI_TOKEN=...
lofi --check
sudo systemctl enable --now lofi
journalctl -u lofi -f
```

The unit runs as the `lofi` user with `LOFI_HOME=/var/lib/lofi`
(`ProtectSystem=full`, `PrivateDevices=true`, `NoNewPrivileges=true`). Drop
`User=`/`Group=` from the unit if you would rather run it as root.

## Templates

`scripts/build.py` renders `@VERSION@`, `@ARCH@` and `@INSTALLED_SIZE@` into
`control`, and copies the maintainer scripts as-is. **Debian `control` files do
not allow `#` comments** — `dpkg-deb` fails with *field name '#' must be
followed by colon* — so the notes about dependency choices live here instead.

## Building and checking one

```bash
python scripts/build.py                     # → dist/lofi-<ver>-linux-<arch>.deb
dpkg-deb -I dist/lofi-*.deb                 # control
dpkg-deb -c dist/lofi-*.deb                 # contents
python scripts/verify_packages.py dist/     # byte-level gate
sudo dpkg -i dist/lofi-*.deb && sudo apt-get install -f -y
```

Docker (amd64, same as the `ubuntu-22.04` runner):

```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libopus0 dpkg-dev \
 && pip install -r requirements.txt pyinstaller \
 && python scripts/build.py && dpkg-deb -I dist/lofi-*.deb
```
