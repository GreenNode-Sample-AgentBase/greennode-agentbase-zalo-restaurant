# Contributing

Thanks for your interest in improving this sample!

## Ways to help

- **Bug reports** — open an issue with: what you did, what you expected, what happened (logs from `runtime.sh logs <id>` help a lot).
- **Feature ideas** — keep the sample *minimal*: it exists to teach the GreenNode AgentBase platform (Runtime + Memory + MCP Gateway + Policy), not to be a full product.
- **Docs** — clarity fixes to the README / Portal steps are very welcome (VN or EN).

## Pull requests

1. Fork & branch: `feat/my-change`.
2. Backend stays **no-build** Python (SDK `greennode-agentbase`), frontend stays **vanilla** (no framework, no CDN).
3. Run tests locally:
   ```bash
   pip install -r src/backend/requirements.txt pytest
   pytest -q
   ```
4. Keep secrets out: `.env` / `.env.local*` are git-ignored — never commit tokens or API keys.
5. PRs should pass CI (pytest) before review.
