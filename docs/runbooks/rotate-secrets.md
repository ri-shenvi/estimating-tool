# Rotate secrets

| Secret | Effect of rotation | Steps |
|---|---|---|
| `SECRET_KEY` | Invalidates all digest action links and encrypted source configs | Re-enter source secrets in Admin after rotating; next digest carries fresh links |
| `GRAPH_CLIENT_SECRET` | Polling stops until updated | Create new secret in Entra first, update config, delete old |
| `ANTHROPIC_API_KEY` | Extraction pauses | Create new key in Console, update, revoke old |
| SMTP password | Digest delivery fails | Update `SMTP_URL`; send a `digest-preview` test |

After any rotation: restart `api` and `worker`, check `/readyz`, confirm a poll and a test send.
