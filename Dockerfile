FROM python:3.12-slim
RUN pip install --no-cache-dir wasmtime pyyaml
WORKDIR /app
COPY host/director.py       /app/director.py
COPY src/plex_director.wasm /app/plex_director.wasm
ENV USHER_CONFIG=/config/usher.yaml
EXPOSE 8099
CMD ["python", "-u", "/app/director.py"]
