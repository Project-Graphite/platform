#!/bin/bash
# The MinIO image has no curl, wget or grep, so this asks the health endpoint
# over bash's /dev/tcp and checks the status line with bash itself
exec 3<>/dev/tcp/127.0.0.1/9000
printf 'GET /minio/health/live HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n' >&3
read -r status <&3
[[ "$status" == *" 200 "* ]]
