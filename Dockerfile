# Etapa 1: Construir la aplicación Go
FROM golang:1.21-alpine AS builder

WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go build -o bot

# Etapa 2: Crear la imagen final mínima
FROM alpine:latest

# 1. Instalar dependencias del sistema
RUN apk --no-cache add \
    ca-certificates \
    python3 \
    py3-pip \
    ffmpeg

# 2. Crear directorio de trabajo y copiar el binario
WORKDIR /app
COPY --from=builder /app/bot .

# 3. Instalar yt-dlp en un entorno virtual aislado
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir yt-dlp

# 4. Asegurar que el sistema use el entorno virtual
ENV PATH="/opt/venv/bin:$PATH"

# 5. Crear directorio para descargas temporales
RUN mkdir -p /app/temp_downloads && chmod 755 /app/temp_downloads

# 6. Exponer puerto y ejecutar
EXPOSE 8080
CMD ["./bot"]