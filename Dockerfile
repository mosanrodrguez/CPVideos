FROM golang:1.21-alpine AS builder

WORKDIR /app
COPY go.mod ./
RUN go mod download
COPY . .
RUN go build -o bot .

FROM alpine:latest
RUN apk --no-cache add python3 py3-pip
RUN apk --no-cache add deno
RUN pip3 install yt-dlp

WORKDIR /app
COPY --from=builder /app/bot .
CMD ["./bot"]