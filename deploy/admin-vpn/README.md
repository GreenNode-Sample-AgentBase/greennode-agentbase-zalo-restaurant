# Admin access: client-to-site VPN

The Langfuse UI is **private**. Administrators reach it only through a **client-to-site VPN** (OpenVPN on pfSense) into the
customer VPC. GreenNode documents this setup as "VPN Client to Server" for pfSense on HCM03. This page adapts it to
the Zalo Restaurant deployment.

```
Admin laptop (OpenVPN client)
   -> Internet -> pfSense vServer (public subnet, Floating IP)  UDP 1194 (or TCP 443)
   -> tunnel network, e.g. 10.8.0.0/24 -> customer VPC private subnet -> Langfuse http://<langfuse-private-ip>:3000
```

Nothing else is exposed: the webhook proxy ([`../webhook-proxy`](../webhook-proxy/README.md)) is the only other
internet-facing component and it serves only `POST /webhook/zalo`.

## 1. Plan the addresses

| Range | Example | Rule |
|---|---|---|
| Customer VPC | `10.20.0.0/16` | must not overlap `172.30.0.0/16` (AgentBase) |
| VPN tunnel network (client addresses) | `10.8.0.0/24` | a **separate** range that overlaps neither the VPC, `172.30.0.0/16`, nor your office LANs |
| Local network pushed to clients | the private subnet(s) that host Langfuse (for example `10.20.1.0/24`) | push only what admins need |

## 2. pfSense vServer (follows the GreenNode doc)

1. Create the pfSense vServer from the GreenNode marketplace image ("pfSense on HCM03") in a **public subnet** with a
   Floating IP. Follow the GreenNode pfSense installation page for the base setup.
2. Security group of the pfSense vServer, inbound: **UDP 1194** (OpenVPN; the GreenNode doc names UDP 1194 and TCP 443)
   from your administrators' public IP ranges. Allow `0.0.0.0/0` only if the admins' addresses are not predictable, and then rely on the
   certificates and MFA below. Never expose the pfSense web interface to the Internet.
3. In pfSense, as in the GreenNode doc:
   1. **System > Cert Manager**: create a **CA**, then a **server certificate** signed by it.
   2. **System > User Manager**: create a user per administrator, and a **user certificate** for each (one person, one
      certificate; no shared accounts).
   3. **VPN > OpenVPN > Wizards**: **Local User Access**; select the CA and server certificate; set the **tunnel network**
      (`10.8.0.0/24`), the **local network** (the Langfuse subnet) and the number of concurrent connections.
   4. **Firewall rules**: let the wizard create the WAN rule for the OpenVPN port, and the rule on the OpenVPN interface
      (see "Least privilege" below: restrict it instead of "allow any").
   5. Install the **openvpn-client-export** package and export one client configuration per administrator.

## 3. GreenNode network side

1. **Route back to the tunnel network.** Langfuse must answer clients in `10.8.0.0/24` through pfSense. In the VPC
   **Route table** (Network > Route table > Edit routes) add a route: destination `10.8.0.0/24`, target the pfSense LAN
   address. Whether a route can target a vServer address, and whether the pfSense NIC needs source/destination checks
   relaxed, is not in the documents reviewed: **verify with GreenNode**.
2. **Security group of the Langfuse vServer** (or load balancer / nodes): inbound TCP `3000` from `10.8.0.0/24` (and from the
   agent path, see the Langfuse README). Inbound TCP `22` from `10.8.0.0/24` only, if admins need SSH.
3. **Network ACL** (if used on the subnet): it is stateless and evaluated before the security group. Allow inbound TCP 3000
   from `10.8.0.0/24` and the return traffic on ephemeral ports.
4. The VPC private connection to AgentBase is independent of this VPN: the VPN carries **only administrators**; the agent
   reaches Langfuse over the AgentBase private connection.

## 4. Open the Langfuse UI

1. Install an OpenVPN client (from openvpn.net), import the exported `.ovpn`, connect with your user and password.
2. Check the tunnel: you can ping the pfSense LAN address (as the GreenNode doc suggests).
3. Browse to `http://<langfuse-private-ip>:3000` (vServer private IP or internal load balancer IP). The URL must equal
   `NEXTAUTH_URL` (compose) or `langfuse.nextauth.url` (Helm), otherwise the login redirects fail. Traffic is encrypted inside the tunnel.
4. Optional: add an internal DNS name (for example `langfuse.internal`) and use it consistently for the URL and `NEXTAUTH_URL`.
5. Disconnect when finished.

```bash
# quick check from the admin laptop while connected
curl -s http://<langfuse-private-ip>:3000/api/public/health
```

## 5. MFA and least privilege

| Control | How |
|---|---|
| Strong authentication | certificate **and** password per admin. pfSense can add a TOTP second factor for OpenVPN users (authentication server with TOTP); this is a pfSense feature, not part of the GreenNode doc: check the menu for your pfSense version |
| One identity per person | one user and one certificate each; revoke the certificate (System > Cert Manager > Revocation) when someone leaves; short certificate lifetimes |
| Minimal reachability | the rule on the OpenVPN interface allows only TCP `3000` to the Langfuse address (and `22` to specific hosts if needed), then **deny all**. Do not push the whole VPC |
| Push only needed routes | the wizard's local network: only the Langfuse subnet, not the VPC `/16` |
| No exposed management | pfSense web GUI reachable only from the tunnel or a management range; SSH key-only |
| Admin roles in Langfuse | few org owners; self-service sign-up disabled; use project roles (viewer/member) for everyone else |
| Monitoring | review pfSense OpenVPN logs and Langfuse audit-relevant events regularly; alert on new connections outside business hours |
| Hygiene | keep pfSense and the OpenVPN client updated; back up the pfSense configuration (it holds the CA key) off-site |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Client cannot connect | the security group does not allow UDP 1194 from your IP; WAN rule missing; wrong Floating IP or port in the `.ovpn` |
| Connects, no access to Langfuse | missing route `10.8.0.0/24` in the VPC route table; security group lacks TCP 3000 from the tunnel range; the OpenVPN interface firewall rule blocks it |
| Connects, `ping` to pfSense works, browser times out | Langfuse security group or Network ACL; Langfuse bound to another address than the one you use |
| Login redirects to an unreachable URL | `NEXTAUTH_URL` differs from the URL in the browser |
| Overlapping address errors | the tunnel network overlaps your office LAN or the VPC: choose another range |

## Verify with GreenNode

- Whether a VPC route may point to a vServer (pfSense) address as next hop, and any NIC settings the pfSense instance needs
  for forwarding traffic.
- Whether a managed client VPN exists on GreenNode other than the pfSense marketplace image.
- The recommended exposure of the OpenVPN port (UDP 1194 versus TCP 443) from the Floating IP.
