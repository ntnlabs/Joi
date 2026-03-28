# Search VM Stage 3 Walkthrough (Search Service Deployment)

Use this after stage 2 (Nebula) is complete and Joi can reach the Search VM.

## What Stage 3 Does

- Install Python dependencies (`flask`, `httpx`, `trafilatura`)
- Deploy search service (`execution/search/`)
- Install systemd unit + defaults file
- Wire HMAC secret shared with Joi
- Validate end-to-end: Joi → Search VM → DDG → result returned

## Dependencies

```bash
pip install flask httpx trafilatura
```

## TODO

- [ ] Create `execution/search/` service
- [ ] Create systemd unit and defaults file
- [ ] Generate and wire HMAC secret
- [ ] End-to-end validation
