FROM python:3.11-slim

WORKDIR /site
COPY index.html .

EXPOSE 8080

CMD ["sh", "-c", "python3 -m http.server ${PORT:-8080}"]
