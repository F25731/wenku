# Docker 部署说明

## 1. 推到 GitHub

把本目录提交到你的 GitHub 仓库。注意不要提交这些运行数据：

- `data/`
- `downloads/`
- `tokens.db`
- `cookies.json`
- `cookie.txt`
- `admin_token.txt`

`.dockerignore` 已经避免它们进入镜像。

## 2. 服务器拉取并启动

```bash
git clone https://github.com/你的用户名/你的仓库.git
cd 你的仓库
```

先修改 `docker-compose.yml` 里的后台口令：

```yaml
WENKU_ADMIN_TOKEN: "change-this-admin-token"
```

然后启动：

```bash
docker compose up -d --build
```

服务默认监听：

```text
http://服务器IP:5000/
```

## 3. 本地后台

在你的电脑上直接打开：

```text
admin_local.html
```

填写：

- 服务器地址：`http://服务器IP:5000` 或你的域名
- 后台口令：`docker-compose.yml` 里的 `WENKU_ADMIN_TOKEN`

## 4. 数据持久化

服务器运行数据会保存在：

```text
./data
```

里面会有：

- `tokens.db`
- `cookies.json`
- `cookie.txt`
- `downloads/`

## 5. 常用命令

```bash
docker compose logs -f
docker compose restart
docker compose pull
docker compose up -d --build
```
