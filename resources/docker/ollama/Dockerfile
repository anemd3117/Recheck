# Official stable Ollama 0.40.16 linux/amd64 manifest verified 2026-11-15.
FROM ollama/ollama:0.40.16@sha256:a372a1c0d1e15b68e9521189785c3ca17795c26c2356f8b1b10122cfb59ed0d7 AS upstream
FROM ubuntu:24.04@sha256:77eb101c607d6e3d4f5ad0c8a02c2136c689b98aba1f359e6502ba517e5c57ec
LABEL org.recheck.managed="true" \
      org.recheck.component="ollama" \
      org.recheck.image.version="1" \
      org.opencontainers.image.title="Recheck optional local Ollama service" \
      org.opencontainers.image.version="0.40.16" \
      org.recheck.compute="cpu" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/ollama/ollama" \
      org.opencontainers.image.base.name="ollama/ollama:0.40.16@sha256:a372a1c0d1e15b68e9521189785c3ca17795c26c2356f8b1b10122cfb59ed0d7"
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=upstream /bin/ollama /bin/ollama
COPY --from=upstream /usr/lib/ollama/*.so* /usr/lib/ollama/
COPY --from=upstream /usr/lib/ollama/llama-server /usr/lib/ollama/llama-quantize /usr/lib/ollama/
COPY --from=upstream /usr/lib/ollama/*LICENSE* /usr/lib/ollama/*NOTICE* /usr/lib/ollama/
COPY LICENSE /usr/share/doc/ollama/LICENSE
ENV OLLAMA_HOST=0.0.0.0:11434 OLLAMA_NO_CLOUD=1 OLLAMA_VULKAN=0
EXPOSE 11434
ENTRYPOINT ["/bin/ollama"]
CMD ["serve"]
# Model weights remain optional downloads in the owned persistent volume.
