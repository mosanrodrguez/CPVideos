FROM golang:1.25-alpine AS builder

WORKDIR /app
COPY go.mod ./
RUN go mod download
COPY . .
RUN go build -o bot .

FROM alpine:latest
RUN apk --no-cache add python3 py3-pip deno
RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"
RUN pip install yt-dlp

WORKDIR /app
COPY --from=builder /app/bot .
CMD ["./bot"]