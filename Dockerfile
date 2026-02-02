FROM golang:1.21-alpine AS builder

WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go build -o bot

FROM alpine:latest

RUN apk --no-cache add \
    python3 \
    py3-pip \
    ffmpeg \
    && pip3 install --no-cache-dir yt-dlp

WORKDIR /app
COPY --from=builder /app/bot .
COPY --from=builder /app/temp_downloads ./temp_downloads

RUN mkdir -p temp_downloads && chmod 755 temp_downloads

EXPOSE 8080

CMD ["./bot"]