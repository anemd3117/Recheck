FROM ubuntu:24.04
LABEL org.recheck.purpose="generated-source-compiler" org.recheck.compiler.cpp="true"
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ libc6-dev && rm -rf /var/lib/apt/lists/*
USER 65534:65534
WORKDIR /tmp
