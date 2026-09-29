# syntax=docker/dockerfile:1.7
# Multi-stage build: the Maven wrapper pins Maven, the runtime is a JRE only, runs as
# non-root, and the jar is extracted into layers so dependency layers are cached.

FROM eclipse-temurin:21-jdk AS build
WORKDIR /src
COPY .mvn .mvn
COPY mvnw pom.xml ./
RUN --mount=type=cache,target=/root/.m2 ./mvnw -B -q dependency:go-offline
COPY src src
RUN --mount=type=cache,target=/root/.m2 ./mvnw -B -q package -DskipTests \
 && java -Djarmode=tools -jar target/jvm-specialist-agent.jar extract --layers --launcher \
      --destination /extracted

FROM eclipse-temurin:21-jre AS runtime
ARG VERSION=dev
LABEL org.opencontainers.image.title="jvm-specialist-agent" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT"
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /extracted/dependencies/ ./
COPY --from=build /extracted/spring-boot-loader/ ./
COPY --from=build /extracted/snapshot-dependencies/ ./
COPY --from=build /extracted/application/ ./
# The agent practices what it preaches: heap sized from the container limit, and a clean
# exit (so the orchestrator restarts it) instead of limping on after an OOM.
ENV JAVA_TOOL_OPTIONS="-XX:MaxRAMPercentage=75 -XX:+ExitOnOutOfMemoryError"
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=5 \
  CMD bash -c 'exec 3<>/dev/tcp/127.0.0.1/8080 && printf "GET /actuator/health/readiness HTTP/1.0\r\n\r\n" >&3 && grep -q UP <&3'
ENTRYPOINT ["java", "org.springframework.boot.loader.launch.JarLauncher"]
