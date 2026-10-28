# The app in the browser (Helm web.enabled): the desktop app's interface, served by the platform itself, so
# people need nothing installed. Build from the repo root: docker build -f images/web/Dockerfile -t fdp/web:dev .
FROM node:20-bookworm-slim AS build
WORKDIR /build
COPY apps/desktop/package.json apps/desktop/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY apps/desktop/ ./
# Web mode: the server is the page's own address; nothing to choose at sign-in.
RUN VITE_FDP_WEB=1 npx vite build

# nginx (BSD-2-Clause), running as an unprivileged user on port 8080.
FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY --from=build /build/dist /usr/share/nginx/html
COPY images/web/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
