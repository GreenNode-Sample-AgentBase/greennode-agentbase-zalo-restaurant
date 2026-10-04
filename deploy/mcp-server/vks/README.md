# MCP server on VKS (Kubernetes)

Run the restaurant MCP server (`src/mcp-server`) in a **VKS** cluster of the customer VPC. The Private MCP Gateway
reaches it through an **internal load balancer**. Use this instead of [`../vserver`](../vserver/README.md) when the
customer already operates VKS.

```
Agent (AgentBase Runtime) -> Private MCP Gateway (172.30.0.0/16) -> private connection
   -> https://<internal-lb-ip>:<port>/mcp  (internal LB / ingress, TLS terminated here)
   -> Service restaurant-mcp-server -> 1 Pod :8080 -> PVC (SQLite)
```

The connector URL is an **HTTPS** URL (the docs describe a full HTTPS URL), so TLS must be terminated in front of the pod; see the TODO list.

| File | Purpose |
|---|---|
| `namespace.yaml` | Namespace `restaurant-mcp` |
| `secret.example.yaml` | Example `MCP_API_KEYS` secret (prefer `kubectl create secret`) |
| `pvc.yaml` | 5 Gi `ReadWriteOnce` claim for the SQLite database |
| `deployment.yaml` | 1 replica (`Recreate`), non-root, read-only root filesystem, `/health` probes, PVC on `/app/data` |
| `service.yaml` | `LoadBalancer` with an internal-LB annotation TODO, plus a `NodePort` alternative |
| `networkpolicy.yaml` | Optional: ingress only from `172.30.0.0/16` (and ranges you add) |

## Why one replica

SQLite is a single-writer file database on a `ReadWriteOnce` volume. Running more than one pod would either fail to
mount the volume or corrupt expectations. For the restaurant workload one pod is sufficient; availability comes from
Kubernetes rescheduling the pod and from backups. If you need HA, replace SQLite with a managed database: that is a
code change in `src/mcp-server/main.py`.

## Prerequisites

- A VKS cluster in a VPC that is **privately connected to AgentBase** (ask GreenNode support to activate it), with
  `kubectl` pointed at it, and DNS resolution enabled on the VPC.
- The cluster and the gateway subnets must not overlap `172.30.0.0/16`.
- A default `StorageClass` (or set `storageClassName` in `pvc.yaml`).

## Steps

```bash
cd deploy/mcp-server/vks

# 1. Build and push the image (from the repo root)
docker build --platform linux/amd64 -t vcr.vngcloud.vn/<repo>/zalo-mcp-server:v1 ../../../src/mcp-server
docker push vcr.vngcloud.vn/<repo>/zalo-mcp-server:v1
#    -> update `image:` in deployment.yaml (and add imagePullSecrets for a private repository)

# 2. Namespace
kubectl apply -f namespace.yaml

# 3. Secret: create it from the command line, never commit real values
export MCP_KEY=$(openssl rand -hex 32)       # RECORD IT, you store the same value in Access Control
kubectl -n restaurant-mcp create secret generic restaurant-mcp-secret --from-literal=MCP_API_KEYS="$MCP_KEY"

# 4. Volume, Deployment, Service
kubectl apply -f pvc.yaml -f deployment.yaml -f service.yaml
kubectl -n restaurant-mcp rollout status deploy/restaurant-mcp-server

# 5. Optional NetworkPolicy
kubectl apply -f networkpolicy.yaml

# 6. Internal LB address
kubectl -n restaurant-mcp get svc restaurant-mcp-server      # EXTERNAL-IP = private IP of the LB
```

## TODO before real use

- [ ] **Internal LB annotation** in `service.yaml`: add the annotation that makes the GreenNode load balancer internal
      (per the GreenNode vLB / VKS documentation). Without it the LB may get a public address; do not expose this
      server to the Internet.
- [ ] **TLS (required for the HTTPS connector URL)**: terminate TLS at the internal LB or at an Ingress (the documented
      Ingress supports TLS on port 443 with a certificate or TLS secret), expose `https://<internal-lb-ip>:<port>/mcp`
      and confirm with GreenNode which CA the gateway trusts. The Service in `service.yaml` speaks plain HTTP on `8080`
      behind that terminator; use it directly (`http://`) only as an unverified alternative or for tests inside the VPC.
- [ ] **StorageClass** in `pvc.yaml`.
- [ ] **NetworkPolicy**: confirm the source address seen by the pod (SNAT or not), then adjust the CIDRs.

## Connect to the MCP Gateway

1. **Access Control**: API Key provider `restaurant-mcp-key` with the value of `$MCP_KEY`.
2. **Gateway**: Network mode **Private** (VPC + Subnet of the cluster/LB; DNS resolution on), Inbound Auth = IAM Permissions.
3. **Connector** `restaurant`: Endpoint `https://<internal-lb-ip>:<port>/mcp` (plain `http://<ip>:8080/mcp` only as an unverified alternative), Outbound Auth = **API Key**, header key
   `X-Api-Key`, **empty header value prefix** (the console default `Bearer ` would break the key), provider `restaurant-mcp-key`.
4. **Policy Group**: allow the seven `restaurant__*` actions for the agent principal (see the root README).

Verify from a host in the VPC: `MCP_HOST=<internal-lb-ip> MCP_PORT=<port> MCP_API_KEY=$MCP_KEY INSECURE=1 ../../check_connectivity.sh`
(defaults to HTTPS; add `MCP_SCHEME=http MCP_PORT=8080` only to test the plain Service before TLS is in place).

## Backups, rotation, updates

```bash
# Backup (consistent copy through the SQLite backup API)
POD=$(kubectl -n restaurant-mcp get pod -l app=restaurant-mcp-server -o jsonpath='{.items[0].metadata.name}')
kubectl -n restaurant-mcp exec "$POD" -- python -c "import sqlite3; s=sqlite3.connect('/app/data/restaurant.db'); d=sqlite3.connect('/app/data/backup.db'); s.backup(d); d.close()"
kubectl -n restaurant-mcp cp "$POD":/app/data/backup.db ./restaurant-$(date +%F).db
kubectl -n restaurant-mcp exec "$POD" -- rm /app/data/backup.db
# Also snapshot the volume if your storage class supports VolumeSnapshots (verify with GreenNode).

# Rotate the key (the env is only read at startup)
kubectl -n restaurant-mcp create secret generic restaurant-mcp-secret \
  --from-literal=MCP_API_KEYS="old_key,new_key" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n restaurant-mcp rollout restart deploy/restaurant-mcp-server
# change the key in Access Control, then repeat with only new_key

# Update the image
kubectl -n restaurant-mcp set image deploy/restaurant-mcp-server mcp=vcr.vngcloud.vn/<repo>/zalo-mcp-server:v2
```

Cleanup: `kubectl delete namespace restaurant-mcp` (this deletes the PVC and the data).
