FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml requirements-lock.txt ./
COPY src ./src
RUN pip install --no-cache-dir -c requirements-lock.txt . && useradd --uid 10001 --create-home tradr
COPY profiles.json boosts.json config.example.json ./
RUN mkdir -p /app/.tradr && chown -R tradr:tradr /app
USER tradr
STOPSIGNAL SIGTERM
ENTRYPOINT ["tradr", "--config", "/app/config.local.json"]
CMD ["run", "--mode", "paper"]
