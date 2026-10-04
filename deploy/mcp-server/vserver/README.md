# MCP server on vServer (docker compose)

Run the restaurant MCP server (`src/mcp-server`) on a GreenNode **vServer** in a **private subnet** of the
customer VPC. It is reached only by the **Private MCP Gateway** (connector `restaurant`); the agent never calls it
directly.

```
Agent (AgentBase Runtime) -> Private MCP Gateway (AgentBase VPC 172.30.0.0/16)
   -> private connection -> customer VPC -> vServer (private subnet) -> [Caddy :8443 TLS] -> mcp :8080 -> SQLite volume
```

## 1. Prepare the network

1. Create a vServer (Ubuntu or Debian with Docker Engine and the Compose plugin) in a **private subnet** of the
   customer VPC. It does not need a public IP for inbound traffic.
2. The VPC must already be **privately connected to AgentBase** (only such VPCs are listed when you create a Private
   gateway or runtime). If it is not, ask GreenNode support to activate it.
3. The VPC must have **DNS resolution** enabled (a Private gateway requirement).
4. The customer VPC and on-premises ranges must **not overlap** the AgentBase VPC `172.30.0.0/16`.
5. Security group of this vServer, inbound:

   | Protocol | Port | Source | Purpose |
   |---|---|---|---|
   | TCP | `8443` (HTTPS, Caddy) | `172.30.0.0/16` | MCP Gateway (AgentBase VPC) |
   | TCP | `8443` | admin or CI host range, optional | `check_connectivity.sh` runs |
   | TCP | `22` | your administration range (or the VPN client range) | SSH |

   Port `8080` (plain HTTP) is bound to loopback and is **not** opened. Do not open the MCP port to `0.0.0.0/0`. Outbound traffic needs no Internet access (the server has no external
   dependency).

> Whether the gateway reaches the vServer with source `172.30.0.0/16` or source-NATed to an address of your VPC
> subnet is not documented. Verify with GreenNode and open exactly that range. See "Verify with GreenNode" in the
> [root README](../../../README.md#verify-with-greennode).

## 2. Run the containers (HTTPS)

The connector endpoint is documented as a full HTTPS URL, so the primary setup runs Caddy in front of the server:
`https://<private-ip>:8443/mcp`.

```bash
git clone <repo> && cd sample-zalo-restaurant/deploy/mcp-server/vserver
cp .env.example .env && chmod 600 .env
# edit .env:
#   MCP_API_KEYS=$(openssl rand -hex 32)   <- record it, you reuse it in Access Control
#   CADDY_SITE=<private IP or internal hostname>   TLS_BIND_ADDR=<private IP of this vServer>

docker compose --profile tls up -d --build   # builds src/mcp-server (non-root image, /app/data volume) + Caddy
docker compose ps                            # mcp: healthy, caddy: running
curl -ks https://<private-ip>:8443/health   # -k: Caddy's internal CA is not in your trust store
```

Plain HTTP (**unverified alternative**, only if GreenNode confirms the connector accepts `http://`): set `MCP_BIND_ADDR=<private IP>` in `.env`,
run `docker compose up -d --build` (no `tls` profile), open port `8080` instead of `8443` in the security group, and use `http://<private-ip>:8080/mcp`.

To use an image from Container Registry instead of building on the host, build and push it once (`docker build -t
vcr.vngcloud.vn/<repo>/zalo-mcp-server:v1 ../../../src/mcp-server && docker push ...`), set `MCP_IMAGE` in `.env`, then
`docker login vcr.vngcloud.vn && docker compose pull && docker compose up -d`.

The compose file refuses to start without `MCP_API_KEYS` (the server itself is also fail-closed: it answers `503`
on `/mcp` when no key is set, and `401` for a wrong key). `/health` is always open.

### Persistence

Bookings and loyalty points are stored in SQLite at `/app/data/restaurant.db`, on the named volume `mcp_data`.
`docker compose down` keeps the volume; `docker compose down -v` **deletes it**. Run a single replica: SQLite has one
writer, so do not scale this service out.

Backup (consistent snapshot using the SQLite backup API, safe while the server runs):

```bash
docker compose exec mcp python -c "import sqlite3; s=sqlite3.connect('/app/data/restaurant.db'); d=sqlite3.connect('/app/data/backup.db'); s.backup(d); d.close()"
docker compose cp mcp:/app/data/backup.db ./restaurant-$(date +%F).db
docker compose exec mcp rm /app/data/backup.db
```

Copy the file off the host (for example to Object Storage) and snapshot the vServer volume as an additional layer.

## 3. Certificate trust

The default `Caddyfile` uses `tls internal` (Caddy's own CA). The gateway must **trust** the certificate presented on `:8443`, and the name or IP in the
connector URL must match `CADDY_SITE`. How to give a connector a custom CA is not in the public documentation: **verify with GreenNode**. The more portable choice is a
certificate from an enterprise or public CA: mount it into `./certs` (uncomment the volume in `docker-compose.yml`) and enable the `tls /certs/server.crt
/certs/server.key` line in the `Caddyfile`.

## 4. Connect to the MCP Gateway

1. **Access Control**: create an **API Key** secret provider, for example `restaurant-mcp-key`, whose value is exactly
   `MCP_API_KEYS` from `.env`.
2. **Gateway**: create (or edit) a gateway with Network mode **Private** (VPC + Subnet of this vServer; DNS resolution
   enabled; Inbound Auth = IAM Permissions).
3. **Connector** `restaurant` (type MCP):
   - Endpoint: `https://<private-ip>:8443/mcp` (plain `http://<private-ip>:8080/mcp` only as the unverified alternative above).
   - Outbound Auth: **API Key**, mode as offered by the console for machine-to-machine, provider `restaurant-mcp-key`.
   - Header key: `X-Api-Key`. **Clear the header value prefix** (the console default is `Bearer `, which would send
     `X-Api-Key: Bearer <key>` and fail with 401).
4. **Policy Group**: allow the agent principal to call the seven `restaurant__*` actions (see the root README).

Check from another host in the same VPC before creating the connector (`INSECURE=1` accepts Caddy's internal CA):

```bash
MCP_HOST=<private-ip> MCP_API_KEY=<key> INSECURE=1 ../../check_connectivity.sh    # defaults: https, port 8443
# plain-HTTP alternative: add MCP_SCHEME=http MCP_PORT=8080
```

## Operations

| Task | Command |
|---|---|
| Logs | `docker compose logs -f mcp` (a `401 on /mcp from <ip>` line shows the real source address) |
| Update after pulling new code | `docker compose up -d --build` |
| Rotate the key | set `MCP_API_KEYS="old_key,new_key"`, `docker compose up -d`, change the value in Access Control, then remove `old_key` and `docker compose up -d` again |
| Restore a backup | `docker compose stop mcp`, `docker compose cp ./restaurant-<date>.db mcp:/app/data/restaurant.db`, `docker compose run --rm --no-deps --user root --entrypoint sh mcp -c "rm -f /app/data/restaurant.db-wal /app/data/restaurant.db-shm; chown 10001:10001 /app/data/restaurant.db"`, `docker compose start mcp` |
