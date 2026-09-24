# Pulse

**Tes documents obligatoires, en 2 minutes.**

Pulse génère les documents juridiques obligatoires pour les sites marchands
français : mentions légales, CGV, politique de confidentialité, cookies, CGU et
formulaire de rétractation. Le dirigeant décrit son activité une fois, chaque
document est rédigé pour son activité (pas un modèle générique), prêt à coller
sur son site.

## Fonctionnement

- Page unique (`GET /`) : vitrine + aperçus gratuits + formulaire.
- Aperçu gratuit : `GET /api/preview/{doc}` — un extrait réel généré pour une
  boutique type, avant tout paiement.
- Génération complète : `POST /api/generer` — réservée aux abonnements actifs
  (clé de licence signée `PULSE-AAAAMMJJ-…`).
- Abonnement : `POST /api/licence` délivre la clé après confirmation PayPal.

## Configuration (variables d'environnement)

| Variable | Rôle |
|---|---|
| `OPENROUTER_API_KEY` | clé OpenRouter utilisée pour la rédaction |
| `PULSE_MODEL` | modèle OpenRouter (défaut : `nex-agi/nex-n2.5-pro:free`) |
| `PULSE_SECRET` | secret de signature des clés de licence |
| `PAYPAL_CLIENT_ID` / `PAYPAL_PLAN_ID` | abonnement PayPal |
| `PAYPAL_SECRET`, `PAYPAL_LIVE=1` | API PayPal (sandbox par défaut) |

Aucun secret n'est présent dans le dépôt : les clés sont injectées en variables
d'environnement (localement et sur Vercel).

## Lancer en local

```bash
export OPENROUTER_API_KEY=sk-or-...
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

## Déploiement

Le projet est déployé sur Vercel (FastAPI détecté automatiquement) :

```bash
vercel --prod
```
