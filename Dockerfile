FROM docker.io/library/python:3.13-alpine

# git-http-backend steckt bei alpine im paket git-daemon, nicht in git.
# tini als pid 1: reicht SIGTERM weiter und sammelt verwaiste git-prozesse ein
RUN apk add --no-cache git git-daemon tini \
 && adduser -D -H -u 10001 gits3 \
 && install -d -o gits3 -g gits3 /cache

COPY gits3.py /usr/local/bin/gits3.py

ENV GITS3_CACHE=/cache \
    GITS3_LISTEN=0.0.0.0:8000

USER gits3
VOLUME /cache
EXPOSE 8000
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["python3", "/usr/local/bin/gits3.py"]
