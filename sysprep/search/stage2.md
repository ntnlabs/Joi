# Search VM Stage 2 Walkthrough (Nebula Runtime)

Use this after `sysprep/search/setup.sh` (stage 1) is complete.

## What Stage 2 Does

- Install Nebula (upstream binary, same as Mesh — not Ubuntu package)
- Install `nebula.service` unit
- Install Search VM Nebula config
- Validate Nebula connectivity to Joi

## Nebula

Same process as Mesh. Use upstream Nebula binary `>= 1.7`.

Search VM has two NICs:
- WAN (internet — for DDG and page fetches)
- Nebula VPN (Joi-only — for search requests/responses)

No direct exposure of the search service to WAN — only reachable via Nebula.

## TODO

- [ ] Assign Nebula IP (suggest `10.42.0.2`)
- [ ] Generate Nebula cert for search VM
- [ ] Install and validate Nebula service
