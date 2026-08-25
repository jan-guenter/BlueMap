FROM eclipse-temurin:21-jre-jammy

ARG VERSION
ARG REVISION
ARG CREATED
ARG SOURCE="https://github.com/BlueMap-Minecraft/BlueMap"

LABEL org.opencontainers.image.title="BlueMap Web" \
      org.opencontainers.image.description="Standalone BlueMap Java web UI and map-data server" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="${SOURCE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

RUN groupadd --gid 10001 bluemap \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /opt/bluemap bluemap \
    && install -d -o bluemap -g bluemap /opt/bluemap /config /data /tmp

WORKDIR /opt/bluemap

COPY --chown=10001:10001 build/release/*-webserver.jar bluemap-web.jar

USER 10001:10001

EXPOSE 8100

ENTRYPOINT ["java", "-jar", "/opt/bluemap/bluemap-web.jar"]
CMD ["--config", "/config"]
