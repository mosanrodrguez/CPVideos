# Etapa 1: Compilación
FROM golang:1.23-alpine AS builder

WORKDIR /app

# Instalar dependencias de compilación
RUN apk add --no-cache git

# Copiar archivos de dependencias primero para aprovechar el cache de Docker
COPY go.mod go.sum ./
RUN go mod download

# Copiar el resto del código y compilar
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o bot .

# Etapa 2: Ejecución (Imagen ligera)
FROM alpine:latest

# Instalar dependencias necesarias:
# 1. python3 y py3-pip para yt-dlp
# 2. ffmpeg para unir audio y video (CRUCIAL)
# 3. ca-certificates para conexiones HTTPS seguras
RUN apk add --no-cache \
    python3 \
    py3-pip \
    ffmpeg \
    ca-certificates

# Crear y activar entorno virtual para cumplir con las políticas de PEP 668 en Alpine
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Instalar yt-dlp dentro del entorno virtual
RUN pip install --no-cache-dir yt-dlp

WORKDIR /app

# Copiar el binario desde la etapa de compilación
COPY --from=builder /app/bot .

# Crear el directorio de descargas temporales
RUN mkdir -p /app/temp_downloads && chmod 777 /app/temp_downloads

# Variable de entorno por defecto (recuerda pasarla al ejecutar el contenedor)
ENV TELEGRAM_BOT_TOKEN=""

CMD ["./bot"]