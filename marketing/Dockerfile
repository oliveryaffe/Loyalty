FROM python:3.11-slim

WORKDIR /site
COPY index.html .
COPY privacy.html .
COPY terms.html .
COPY fonts/ ./fonts/

EXPOSE 8080

CMD ["sh", "-c", "python3 -m http.server ${PORT:-8080}"]
