# Official Ubuntu 24.04 linux/amd64 manifest verified 2026-11-15.
FROM ubuntu:24.04@sha256:77eb101c607d6e3d4f5ad0c8a02c2136c689b98aba1f359e6502ba517e5c57ec
LABEL org.recheck.managed="true" \
      org.recheck.component="execution" \
      org.recheck.image.version="1" \
      org.opencontainers.image.title="Recheck managed execution worker" \
      org.opencontainers.image.base.name="ubuntu:24.04@sha256:77eb101c607d6e3d4f5ad0c8a02c2136c689b98aba1f359e6502ba517e5c57ec"
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 ca-certificates \
    && rm -rf /var/lib/apt/lists/* && mkdir -p /work /input
COPY run-target.sh /usr/local/bin/recheck-run-target
RUN chmod 0555 /usr/local/bin/recheck-run-target
WORKDIR /work
USER 65534:65534
