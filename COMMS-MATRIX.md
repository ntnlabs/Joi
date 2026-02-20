# Communication Matrix

All network flows between components. Use this for firewall rules and security audits.

## VM IP Map

| VM | vmbr1 (internal) | Nebula | WAN |
|----|------------------|--------|-----|
| mesh | 172.22.22.1 | 10.42.0.1 | DHCP |
| joi | 172.22.22.2 | 10.42.0.10 | none |
| ntp | 172.22.22.3 | none | DHCP |
| gateway | 172.22.22.4 | none | yes |

## Network Flows

| From | To | Interface/Net | Proto | Port | Purpose | Temp |
|------|-----|---------------|-------|------|---------|------|
| **Mesh VM** |
| mesh | joi | Nebula | TCP | 8443 | Forward messages to Joi API | |
| mesh | Signal servers | WAN | TCP | 443 | Signal protocol | |
| mesh | ntp | vmbr1 | UDP | 123 | Time sync | |
| mesh | DNS | WAN | UDP | 53 | DNS resolution | |
| **Joi VM** |
| joi | mesh | Nebula | TCP | 8444 | Send outbound messages | |
| joi | ntp | vmbr1 | UDP | 123 | Time sync | |
| joi | localhost | docker0 | TCP | 11434 | Ollama API | |
| joi | any | WAN | TCP | 53,80,443 | Git/apt/pip | TEMP |
| **NTP VM** |
| ntp | upstream NTP | WAN (eth0) | UDP | 123 | Sync from internet | |
| ntp | DNS | WAN (eth0) | UDP | 53 | Resolve NTP hostnames | |
| ntp | DHCP server | WAN (eth0) | UDP | 67/68 | Get IP address | |
| **Gateway** |
| gateway | mesh | vmbr1 | TCP | 22 | SSH admin | |
| gateway | joi | vmbr1 | TCP | 22 | SSH admin | |
| gateway | ntp | vmbr1 | TCP | 22 | SSH admin | |
| gateway | Internet | WAN | TCP | 80,443 | Forward updates for internal VMs | TEMP |
| gateway | Internet | WAN | UDP | 53 | Forward DNS for internal VMs | TEMP |
| **Update Routing (via Gateway)** |
| mesh | gateway | vmbr1 | TCP | 80,443 | APT updates | TEMP |
| mesh | gateway | vmbr1 | UDP | 53 | DNS for updates | TEMP |
| joi | gateway | vmbr1 | TCP | 80,443 | APT updates | TEMP |
| joi | gateway | vmbr1 | UDP | 53 | DNS for updates | TEMP |
| ntp | gateway | vmbr1 | TCP | 80,443 | APK updates | TEMP |
| ntp | gateway | vmbr1 | UDP | 53 | DNS for updates | TEMP |

## Nebula Overlay

| From | To | Nebula IP | Proto | Port | Purpose |
|------|-----|-----------|-------|------|---------|
| mesh | joi | 10.42.0.10 | TCP | 8443 | Joi inbound API |
| joi | mesh | 10.42.0.1 | TCP | 8444 | Mesh outbound API |

Nebula itself uses UDP 4242 on vmbr1 between mesh and joi.

## Temporary Flows

Flows marked **TEMP** are disabled by default and enabled only during maintenance:

- **Gateway update routing**: All internal VMs (mesh, joi, ntp) use gateway as default route. Gateway firewall blocks HTTP/HTTPS/DNS forwarding by default. Use `gateway-update.sh --enable` to temporarily allow updates, then `--disable` when done.

- **joi WAN access**: Legacy direct WAN access - should be removed. Use gateway routing instead.

## Notes

- Joi has NO direct WAN interface - updates go through gateway
- All mesh<->joi traffic goes through Nebula tunnel (encrypted)
- NTP VM bridges internal time sync to upstream NTP servers
- Gateway is default route for all internal VMs - acts as update router when enabled
- mesh/ntp have dedicated WAN paths for Signal/NTP, but general traffic (updates) goes via gateway
