# Pinned official upstream artifacts. No workstation configuration is copied.
FROM node:24.22.1-bookworm-slim@sha256:be8204d4bd327363357f532187b752ff62759c31bedf98e9e18fc08c9cbd605c
LABEL org.recheck.managed="true" org.recheck.component="static" org.recheck.worker-contract="rea-static-1.0" org.recheck.image.version="1" org.opencontainers.image.title="Recheck managed REA static analysis" org.opencontainers.image.version="REA6.7.0-Ghidra12.1.4" org.opencontainers.image.licenses="MIT AND Apache-2.0 AND GPL-2.0-with-classpath-exception" org.opencontainers.image.source="https://github.com/morluto/rea"
ARG GHIDRA_VERSION=12.1.4
ARG GHIDRA_URL=https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_12.1.4_build/ghidra_12.1.4_PUBLIC_20260921.zip
ARG GHIDRA_SHA256=ddac49f903da9d5bac833e5cc79395098b9c33cfd3279be5f31bd00387d2d4db
ARG JDK_URL=https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.9%2B10/OpenJDK21U-jdk_x64_linux_hotspot_21.0.9_10.tar.gz
ARG JDK_SHA256=810d3773df7e0d6c4394e4e244b264c8b30e0b05a0acf542d065fd78a6b65c2f
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip procps util-linux libstdc++6 libfontconfig1 && rm -rf /var/lib/apt/lists/*
RUN curl -fL --retry 3 "$JDK_URL" -o /tmp/jdk.tar.gz && echo "$JDK_SHA256  /tmp/jdk.tar.gz" | sha256sum -c - && mkdir /opt/java && tar -xzf /tmp/jdk.tar.gz --strip-components=1 -C /opt/java && rm /tmp/jdk.tar.gz
RUN curl -fL --retry 3 "$GHIDRA_URL" -o /tmp/ghidra.zip && echo "$GHIDRA_SHA256  /tmp/ghidra.zip" | sha256sum -c - && unzip -q /tmp/ghidra.zip -d /opt && mv "/opt/ghidra_${GHIDRA_VERSION}_PUBLIC" /opt/ghidra && rm /tmp/ghidra.zip
ENV JAVA_HOME=/opt/java GHIDRA_INSTALL_DIR=/opt/ghidra PATH=/opt/java/bin:/opt/recheck/node_modules/.bin:$PATH HOME=/tmp/recheck-home REA_LOG_LEVEL=error
WORKDIR /opt/recheck
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund && npm cache clean --force
COPY worker.mjs normalize.mjs managed.mjs ./
RUN mkdir -p /output /input /tmp/recheck-home && chown -R node:node /output /input /tmp/recheck-home
USER node
ENTRYPOINT ["node", "/opt/recheck/worker.mjs"]
