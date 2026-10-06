FROM docker@sha256:11e1133c30f3ceb73c6bdc7dfb78b3f9ed8e8e0d1d0400e91c5ec2eb240bf2ff AS cli
FROM ngrok/ngrok@sha256:14d80d083e5b53145f416bbbd36238336c9de4016c43fd950eb2eb845670583b AS ngrok
FROM ghcr.io/anomalyco/opencode@sha256:d654ecb68ae52ae3abcc56a2a07ff25647e33c38d5a7fc880c5ee20ee54c7d30
USER root
RUN apk add --no-cache python3 git
COPY --from=cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=ngrok /bin/ngrok /usr/local/bin/ngrok
COPY src/mergerail /opt/mergerail/src/mergerail
ENV PYTHONPATH=/opt/mergerail/src PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "-m", "mergerail.cli"]
CMD ["run", "--path", "/project", "--front", "mergerail.devbot:DevBotWebFront", "--share", "ngrok", "--no-process", "--no-update"]
