# LibreOffice conversion service for Hugging Face Spaces (Docker SDK).
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-impress libreoffice-writer libreoffice-calc libreoffice-draw \
      ffmpeg dcraw imagemagick \
      p7zip-full unar \
      python3 fonts-dejavu fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server.py .

ENV PORT=7860
EXPOSE 7860
CMD ["python3", "server.py"]
