# Stage 3

Build and start from this directory. The image listens on `0.0.0.0` and the `PORT` environment variable (default 8080). It has no runtime package installs and no outbound network.

```sh
docker build -t pocketful-stage-3 .
docker run --rm -p 8080:8080 -e PORT=8080 pocketful-stage-3
```

Health check: `curl -s http://127.0.0.1:8080/health` returns `{"status":"ok"}`.

Without Docker, from this directory, with Python 3.12 or newer:

```sh
PORT=8080 python3 app.py
```

This folder implements stage 3 only. Later behavior is not served.
