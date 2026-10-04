# Vendor every dependency at build time: there is no outbound network at run time.
FROM python:3.12-alpine
WORKDIR /app
COPY app.py engine.py passwords.py ./
ENV PORT=8080
EXPOSE 8080
CMD ["python", "-u", "app.py"]
