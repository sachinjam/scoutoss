# syntax=docker/dockerfile:1.7
# ScoutOSS web UI: static build served by unprivileged nginx (build context: services/ui).
FROM node:22.21-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.29-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY docker-entrypoint.d/40-config.sh /docker-entrypoint.d/40-config.sh
COPY --from=build /src/dist /usr/share/nginx/html
USER 101
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
