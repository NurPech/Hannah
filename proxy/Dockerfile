FROM scratch

COPY --chown=1000:1000 etc-hannah /etc/hannah-proxy
COPY --chown=1000:1000 proxy /app/proxy

USER 1000:1000

EXPOSE 7776

ENTRYPOINT ["/app/proxy"]