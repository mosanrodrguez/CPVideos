FROM golang:1.21-alpine AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go build -o bot .

FROM alpine:latest
# 1. Instala Python y pip para yt-dlp
RUN apk --no-cache add python3 py3-pip
# 2. Instala un entorno de JavaScript (Deno, recomendado por yt-dlp)
RUN apk --no-cache add deno
# 3. Instala yt-dlp
RUN pip3 install yt-dlp

WORKDIR /app
COPY --from=builder /app/bot .
CMD ["./bot"]