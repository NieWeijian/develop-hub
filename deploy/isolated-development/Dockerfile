FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git g++ && rm -rf /var/lib/apt/lists/*
COPY repo /usr/local/bin/repo
COPY node/bin/node /opt/node/bin/node
COPY node/lib/node_modules/npm /opt/node/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /opt/node/bin/npm && ln -s ../lib/node_modules/npm/bin/npx-cli.js /opt/node/bin/npx
ENV PATH=/opt/node/bin:$PATH
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
