FROM ubuntu:24.04
LABEL org.recheck.image.version="1"
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 ca-certificates && rm -rf /var/lib/apt/lists/* && mkdir -p /work /input
COPY run-target.sh /usr/local/bin/recheck-run-target
RUN chmod 0555 /usr/local/bin/recheck-run-target
WORKDIR /work
USER 65534:65534
