# Atlas relay: uptermd (github.com/owenthereal/upterm, Apache-2.0), pinned and
# checksummed, restarted by entrypoint.sh whenever the gate of computers changes.
FROM alpine:3
ARG UPTERM_VERSION=0.41.1
ARG UPTERMD_SHA256=033d3bcd1938a04ebbb2c492ffde8ac96897f0cf0a4464e2613ddff75bd05732
RUN apk add --no-cache openssh-keygen \
    && wget -q -O /tmp/uptermd.tar.gz "https://github.com/owenthereal/upterm/releases/download/v${UPTERM_VERSION}/uptermd_linux_amd64.tar.gz" \
    && echo "${UPTERMD_SHA256}  /tmp/uptermd.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/uptermd.tar.gz -C /usr/local/bin uptermd \
    && rm /tmp/uptermd.tar.gz \
    && adduser -D -u 1002 relay \
    && mkdir -p /keys /gate && chown relay /keys && chmod 755 /gate
COPY entrypoint.sh /usr/local/bin/atlas-relay
USER relay
EXPOSE 2222 8080
ENTRYPOINT ["/bin/sh", "/usr/local/bin/atlas-relay"]
