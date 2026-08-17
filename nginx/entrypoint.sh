#!/bin/sh
set -eu

CERT_PATH="${TLS_CERT_PATH:-}"
KEY_PATH="${TLS_KEY_PATH:-}"

cat >/etc/nginx/nginx.conf <<'BASE'
worker_processes auto;

events {
  worker_connections 1024;
}

http {
  resolver 127.0.0.11 valid=5s ipv6=off;
  sendfile on;
  tcp_nopush on;
  tcp_nodelay on;
  keepalive_timeout 65;
  types_hash_max_size 2048;
  server_tokens off;

  client_max_body_size 10m;
  proxy_connect_timeout 5s;
  proxy_send_timeout 60s;
  proxy_read_timeout 60s;

  upstream app_upstream {
    server app:8000;
    keepalive 32;
  }

  server {
    listen 80;
    root /usr/share/nginx/html;
    charset utf-8;

    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    location /api/ {
      proxy_http_version 1.1;
      proxy_set_header Connection "";
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $remote_addr;
      proxy_set_header X-Forwarded-Proto $scheme;
      set $app_backend app:8000;
      proxy_pass http://$app_backend;
    }

    location / {
      try_files $uri $uri/ /index.html;
    }
  }
BASE

if [ -n "$CERT_PATH" ] && [ -n "$KEY_PATH" ] && [ -f "$CERT_PATH" ] && [ -f "$KEY_PATH" ]; then
	cat >>/etc/nginx/nginx.conf <<SSL

  server {
    listen 443 ssl;
    root /usr/share/nginx/html;
    charset utf-8;

    ssl_certificate $CERT_PATH;
    ssl_certificate_key $KEY_PATH;
    ssl_session_timeout 1d;
    ssl_session_cache shared:SSL:10m;
    ssl_session_tickets off;

    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    location /api/ {
      proxy_http_version 1.1;
      proxy_set_header Connection "";
      proxy_set_header Host \$host;
      proxy_set_header X-Real-IP \$remote_addr;
      proxy_set_header X-Forwarded-For \$remote_addr;
      proxy_set_header X-Forwarded-Proto \$scheme;
      set \$app_backend app:8000;
      proxy_pass http://\$app_backend;
    }

    location / {
      try_files \$uri \$uri/ /index.html;
    }
  }
}
SSL
else
	cat >>/etc/nginx/nginx.conf <<'HTTPONLY'
}
HTTPONLY
fi

exec nginx -g 'daemon off;'
