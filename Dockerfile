FROM golang:1.21-alpine AS builder

WORKDIR /app
COPY go.mod ./
RUN go mod download
COPY . .
RUN go build -o bot .

FROM alpine:latest

# 1. Instalar Python, pip y Deno
RUN apk --no-cache add python3 py3-pip deno

# 2. Crear y activar un entorno virtual
RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

# 3. Instalar yt-dlp dentro del entorno virtual
RUN pip install yt-dlp

WORKDIR /app
COPY --from=builder /app/bot .
CMD ["./bot"]