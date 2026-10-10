# syntax=docker/dockerfile:1
FROM golang:1.27.2-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY *.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/demo-api .

# scratch: no shell, no package manager, nothing to patch but the binary itself.
FROM scratch
COPY --from=build /out/demo-api /demo-api
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/demo-api"]
