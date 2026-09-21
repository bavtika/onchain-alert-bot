# Security Policy

## Secrets

- Never commit `.env`, Telegram tokens, or Kubernetes `Secret` objects with real values.
- Use `.env.example` and `deploy/k8s/secret.example.yaml` as templates only.
- Rotate `TELEGRAM_BOT_TOKEN` via [@BotFather](https://t.me/BotFather) if it was ever exposed.

## Reporting

If you find a vulnerability in this project, open a private GitHub security advisory or contact the maintainer.
