# Образ сайта. Статика лежит внутри образа, а не монтируется томом снаружи.
# Контекст сборки — этот каталог, поэтому в него не попадает ничего лишнего:
# ни .env, ни .git, ни исходники бэкенда.

# Стадия сборки: Tailwind собирает CSS, terser минифицирует JS. Ни исходники,
# ни node_modules в финальный образ не попадают — оттуда берём только dist.
FROM node:22-alpine AS build

WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci

# Tailwind сканирует разметку и скрипты, поэтому им тоже место в этой стадии.
COPY tailwind.config.js ./
COPY scripts/ ./scripts/
COPY src/ ./src/
COPY js/ ./js/
COPY *.html ./
RUN npm run build

FROM nginx:1.30-alpine

COPY nginx.conf /etc/nginx/nginx.conf

# Сам Dockerfile и nginx.conf под маски ниже не подходят и в веб-корень не едут.
WORKDIR /srv/www
COPY *.html *.ico style.css mobile.css fonts.css robots.txt site.webmanifest ./
COPY images/ ./images/
# Шрифт раздаём сами: внешний CDN унёс бы IP посетителя за границу.
COPY fonts/ ./fonts/
COPY --from=build /build/dist/ ./

RUN nginx -t
