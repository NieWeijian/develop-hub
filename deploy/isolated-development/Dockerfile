# All build inputs are tracked. Build from the repository root.
FROM python@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217
COPY deploy/isolated-development/toolchain.lock.json deploy/isolated-development/fetch-node.py deploy/isolated-development/install-os.py /toolchain/
COPY deploy/isolated-development/third_party/repo /usr/local/bin/repo
RUN python /toolchain/install-os.py && rm -rf /var/lib/apt/lists/*
RUN python /toolchain/fetch-node.py
ENV PATH=/opt/venv/bin:/opt/node/bin:$PATH
ENV PYTHONDONTWRITEBYTECODE=1
COPY requirements/development-linux-amd64.lock /toolchain/python.lock
RUN python -m venv /opt/venv && /opt/venv/bin/python -m pip install --only-binary=:all: --require-hashes -r /toolchain/python.lock && /opt/venv/bin/python -m pip check
COPY web/package.json web/package-lock.json /opt/web/
RUN cd /opt/web && npm ci --no-audit --no-fund && rm -rf /root/.npm
WORKDIR /app
