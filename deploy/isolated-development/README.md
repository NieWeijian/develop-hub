# Develop Hub stable/development separation

Stable UI: http://10.10.10.175:3015 — API: 8185.
Stable checkout: `/home/nwj/develop-hub-redesign`. Do not edit, build, install,
migrate, provision devices, restart, or change permissions there for development.
The old 3000 frontend is disabled. The old 8175 backend remains untouched.

Development UI: http://10.10.10.175:3025 — API: http://10.10.10.175:8195.
SSH source checkout: `/home/nwj/develop-hub-dev/app`.
Development data: `/home/nwj/develop-hub-dev/var`; no production data seed.
Independent Python 3.12 virtualenv and npm lockfile installation live in the dev
checkout; standalone Node 22.22.1/Python tooling image is `develop-hub-dev-tools:20261014`.

Run on sdk-175:

```sh
python3 /home/nwj/develop-hub-dev/ops/manage.py status
python3 /home/nwj/develop-hub-dev/ops/manage.py build
python3 /home/nwj/develop-hub-dev/ops/manage.py test
python3 /home/nwj/develop-hub-dev/ops/manage.py restart
```

Application containers run as a non-root UID, without networking, host devices,
SSH identities, Docker socket, or stable mounts. The source/dependencies and
container root filesystem are read-only at runtime. Only dev data and bounded
temporary storage are writable. Each container is limited to 2 CPUs, 4 GiB RAM,
no additional swap, and 256 processes. Docker logs rotate at 3 x 10 MiB.
Tooling build/test containers have the same resource limits and no networking.
Only initial dependency installation uses networking; it carries no credentials.

Two fixed host-side HTTP gateways forward 3025/8195 exclusively to private Unix
sockets in dev data. Their systemd user units are named
`develop-hub-dev-web-proxy.service` and `develop-hub-dev-api-proxy.service`.
They support regular HTTP bodies with Content-Length and streaming responses;
WebSocket forwarding and chunked request bodies are not provided.

Application-level simulation guards protect registration, managed station binding,
device checks, runner preparation/claims and execution transport. They cannot
disable the external container boundaries. Real hardware integration needs a
separately authorized test station and an independently reviewed connection policy.
Large SDK builds belong to a separately constrained build environment.

Stable source/builds and dependencies are read-only. This prevents accidental
changes by the ordinary operator account. The host owner/Docker administrator can
deliberately reverse these settings, so this is not a security boundary against
a malicious host administrator. Host/kernel/storage failures remain shared.

Private stable baseline and online database backup:
`/home/nwj/develop-hub-stable-protection/baseline-20261014`.
Modes before protection are in `original-modes.json`. Do not restore modes or
overwrite stable data as part of development. Stable releases/recovery need an
explicitly authorized maintenance procedure. Code rollback alone is not database
rollback. Runtime database/artifacts stay writable for normal platform operation.

Before protection, another chat completed report changes and restarted the old
preview processes. The frozen baseline includes that completed update. This
isolation work does not restart the stable processes after protection.
