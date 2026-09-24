"""Pulse — generateur de documents obligatoires pour sites marchands FR.

FastAPI. Une page, un formulaire, un document conforme, un abonnement.
"""
import os
import re
import json
import hmac
import hashlib
import secrets
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Configuration ───────────────────────────────────────────────────────────
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

# Modele par defaut : DeepSeek en direct. ~5 s pour un document complet,
# contre 240 s+ pour l'ancien modele gratuit « nex-n2.5-pro » (expiration).
MODELE = os.environ.get("PULSE_MODEL", "deepseek-chat")
# Repli OpenRouter si la cle DeepSeek n'est pas configuree (modele rapide).
MODELE_OPENROUTER = os.environ.get("PULSE_OPENROUTER_MODEL", "nex-agi/nex-n2.5-mini:free")

# 1400 tokens suffisent largement pour un document publiable (l'ancien defaut
# de 3500 ne faisait qu'allonger l'attente). L'apercu s'arrete a ~2200
# caracteres : 800 tokens de sortie suffisent donc pour un extrait.
MAX_TOKENS = int(os.environ.get("PULSE_MAX_TOKENS", "1400"))
MAX_TOKENS_APERCU = int(os.environ.get("PULSE_MAX_TOKENS_APERCU", "800"))

PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET = os.environ.get("PAYPAL_SECRET", "")
PAYPAL_PLAN_ID = os.environ.get("PAYPAL_PLAN_ID", "")
PAYPAL_API = ("https://api-m.paypal.com"
              if os.environ.get("PAYPAL_LIVE") == "1"
              else "https://api-m.sandbox.paypal.com")

# jeton de licence : signe les abonnements actifs (pas de base de donnees requise)
LICENCE_SECRET = os.environ.get("PULSE_SECRET", secrets.token_hex(32))

app = FastAPI(docs_url=None, redoc_url=None)

# ── Documents proposes ──────────────────────────────────────────────────────
DOCUMENTS = {
    "cgv": {
        "titre": "Conditions Générales de Vente",
        "obligatoire": True,
        "a_quoi": "Obligatoires dès que tu vends en ligne à des particuliers.",
        "duree": "2 min",
    },
    "mentions": {
        "titre": "Mentions légales",
        "obligatoire": True,
        "a_quoi": "Obligatoires sur tout site professionnel, même sans vente.",
        "duree": "2 min",
    },
    "confidentialite": {
        "titre": "Politique de confidentialité",
        "obligatoire": True,
        "a_quoi": "Obligatoire dès que tu collectes une donnée personnelle (même un email).",
        "duree": "3 min",
    },
    "cookies": {
        "titre": "Politique de cookies",
        "obligatoire": True,
        "a_quoi": "Obligatoire si tu utilises des cookies non essentiels (analytics, pub).",
        "duree": "2 min",
    },
    "cgu": {
        "titre": "Conditions Générales d'Utilisation",
        "obligatoire": False,
        "a_quoi": "Recommande si tu proposes un compte utilisateur ou un service.",
        "duree": "2 min",
    },
    "retractation": {
        "titre": "Formulaire de rétractation",
        "obligatoire": True,
        "a_quoi": "Obligatoire pour la vente à distance aux consommateurs (14 jours).",
        "duree": "1 min",
    },
}

PROMPT = """Tu es juriste specialise en droit francais du numerique.
Redige le document « {titre} » pour ce site marchand.

INFORMATIONS FOURNIES :
- Nom commercial : {nom}
- Forme juridique : {forme}
- Activite : {activite}
- Adresse : {adresse}
- Email : {email}
- Telephone : {telephone}
- SIRET : {siret}
- TVA intracommunautaire : {tva}
- Capital social : {capital}
- Responsable de publication : {responsable}
- Herbergeur : {herbergeur}
- Site : {site}
- Vente de produits ou services : {vente}
- Collecte de donnees : {donnees}
- Cookies non essentiels : {cookies}
- Moyens de paiement : {paiement}
- Livraison : {livraison}

CONSIGNES :
- Francais juridique correct et actuel (2026), droit francais et europeen.
- Markdown : titres ##, listes -, gras **.
- Reprends EXACTEMENT les informations fournies (ne les invente jamais).
- Quand une info manque, mets [A COMPLETER] au lieu d'inventer.
- Sois complet mais lisible : un dirigeant de PME doit pouvoir le publier tel quel.
- Ajoute en fin de document un rappel court : ce document est un modele a faire
  verifier par un professionnel du droit avant publication.
- Commence directement par le titre, sans phrase d'introduction."""


# ── Appel modele ────────────────────────────────────────────────────────────
def _appel(url: str, cle: str, modele: str, prompt: str, max_tokens: int,
           timeout: int = 55) -> str:
    """Un aller-retour avec une API compatible OpenAI. Leve en cas d'echec."""
    corps = json.dumps({
        "model": modele,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }).encode()

    req = urllib.request.Request(
        url, data=corps,
        headers={"Authorization": f"Bearer {cle}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    if "choices" not in d or not d["choices"]:
        raise RuntimeError("reponse inattendue")
    contenu = d["choices"][0].get("message", {}).get("content") or ""
    if not contenu.strip():
        raise RuntimeError("reponse vide du modele")
    return contenu.strip()


def generer(doc_key: str, infos: dict, max_tokens: Optional[int] = None) -> str:
    if doc_key not in DOCUMENTS:
        raise HTTPException(400, "Document inconnu")
    if not DEEPSEEK_KEY and not OPENROUTER_KEY:
        raise HTTPException(503, "Generation indisponible")

    mt = max_tokens or MAX_TOKENS
    prompt = PROMPT.format(titre=DOCUMENTS[doc_key]["titre"], **infos)

    erreurs = []
    # 1) DeepSeek en direct : rapide et fiable (recommande en production).
    if DEEPSEEK_KEY:
        try:
            return _appel("https://api.deepseek.com/chat/completions",
                          DEEPSEEK_KEY, MODELE, prompt, mt)
        except Exception as e:
            erreurs.append(f"deepseek:{type(e).__name__}")
    # 2) Repli OpenRouter.
    if OPENROUTER_KEY:
        try:
            return _appel("https://openrouter.ai/api/v1/chat/completions",
                          OPENROUTER_KEY, MODELE_OPENROUTER, prompt, mt)
        except Exception as e:
            erreurs.append(f"openrouter:{type(e).__name__}")

    raise HTTPException(502, "Erreur de generation : " + (", ".join(erreurs) or "aucun modele disponible"))


# ── Licence (abonnement) ────────────────────────────────────────────────────
def signer(payload: str) -> str:
    return hmac.new(LICENCE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def cle_licence(email: str, debut: str) -> str:
    brut = f"{email.lower().strip()}|{debut}"
    return f"PULSE-{debut}-{signer(brut)}"


def licence_valide(cle: str) -> bool:
    m = re.match(r"^PULSE-(\d{8})-([0-9a-f]{32})$", (cle or "").strip())
    return bool(m)


# ── Modele de requete ───────────────────────────────────────────────────────
class Demande(BaseModel):
    doc: str
    nom: str = ""
    forme: str = "Micro-entreprise"
    activite: str = ""
    adresse: str = ""
    email: str = ""
    telephone: str = ""
    siret: str = ""
    tva: str = ""
    capital: str = ""
    responsable: str = ""
    herbergeur: str = ""
    site: str = ""
    vente: str = "oui"
    donnees: str = "oui"
    cookies: str = "oui"
    paiement: str = ""
    livraison: str = ""
    licence: str = ""


# ── Routes API ──────────────────────────────────────────────────────────────
@app.get("/api/documents")
def api_documents():
    return DOCUMENTS


@app.get("/api/paypal")
def api_paypal():
    return {"client_id": PAYPAL_CLIENT_ID, "plan_id": PAYPAL_PLAN_ID,
            "actif": bool(PAYPAL_CLIENT_ID and PAYPAL_PLAN_ID)}


@app.post("/api/licence")
async def api_licence(req: Request):
    """PayPal confirme un abonnement actif -> on delivre la cle."""
    corps = await req.json()
    sub = str(corps.get("subscription_id", ""))
    if not re.match(r"^I-[A-Z0-9]{12,}$", sub):
        raise HTTPException(400, "Abonnement invalide")
    email = str(corps.get("email", "")).strip()
    debut = datetime.now(timezone.utc).strftime("%Y%m%d")
    return {"licence": cle_licence(email, debut)}


@app.post("/api/generer")
def api_generer(d: Demande):
    if not licence_valide(d.licence):
        raise HTTPException(402, "Abonnement requis")
    texte = generer(d.doc, d.model_dump())
    return {"doc": d.doc, "titre": DOCUMENTS[d.doc]["titre"], "contenu": texte}


@app.get("/api/preview/{doc_key}")
def api_preview(doc_key: str):
    """Un extrait gratuit pour montrer la qualite sans payer."""
    if doc_key not in DOCUMENTS:
        raise HTTPException(404, "Document inconnu")
    infos = {
        "nom": "Ma Boutique", "forme": "Micro-entreprise", "activite": "vente de vetements",
        "adresse": "12 rue de la Paix, 75002 Paris", "email": "contact@ma-boutique.fr",
        "telephone": "01 23 45 67 89", "siret": "123 456 789 00012", "tva": "",
        "capital": "", "responsable": "Jean Dupont", "herbergeur": "Vercel Inc.",
        "site": "ma-boutique.fr", "vente": "oui", "donnees": "oui", "cookies": "oui",
        "paiement": "carte bancaire", "livraison": "Colissimo, 3-5 jours",
    }
    texte = generer(doc_key, infos, max_tokens=MAX_TOKENS_APERCU)
    return {"titre": DOCUMENTS[doc_key]["titre"], "contenu": texte[:2200]}


@app.get("/sante")
def sante():
    return {"ok": True, "modele": MODELE,
            "fournisseur": "deepseek" if DEEPSEEK_KEY else "openrouter",
            "repli_openrouter": bool(OPENROUTER_KEY),
            "docs": len(DOCUMENTS)}


# ── Page ────────────────────────────────────────────────────────────────────
PAGE = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pulse — tes documents obligatoires, en 2 minutes</title>
<style>
:root{
  --bg:#08080c; --bg2:#0e0e15; --card:#13131d; --bord:#22222f;
  --texte:#ecedf3; --gris:#8b8b9e; --accent:#6d5cf7; --accent2:#22d3ee;
  --ok:#34d399; --or:#fbbf24;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{
  background:var(--bg); color:var(--texte); min-height:100vh;
  font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  line-height:1.6; -webkit-font-smoothing:antialiased; overflow-x:hidden;
}
/* halos d'ambiance */
.halo{position:fixed;border-radius:50%;filter:blur(120px);pointer-events:none;z-index:0;opacity:.5}
.h1{width:560px;height:560px;background:#6d5cf7;top:-260px;left:-160px}
.h2{width:460px;height:460px;background:#22d3ee;top:180px;right:-220px;opacity:.28}
.wrap{position:relative;z-index:1;max-width:1080px;margin:0 auto;padding:0 22px}

/* nav */
nav{display:flex;align-items:center;justify-content:space-between;padding:26px 0}
.marque{display:flex;align-items:center;gap:11px;font-weight:700;font-size:19px;letter-spacing:-.3px}
.pastille{
  width:34px;height:34px;border-radius:10px;
  background:linear-gradient(135deg,#a78bfa,#6d5cf7 55%,#4c1d95);
  display:grid;place-items:center;box-shadow:0 3px 16px rgba(109,92,247,.6);
}
.pastille svg{width:19px;height:19px}
nav a.cta{
  background:var(--accent);color:#fff;text-decoration:none;font-weight:600;font-size:14px;
  padding:10px 18px;border-radius:9px;transition:.2s;
}
nav a.cta:hover{background:#7d6ffa;transform:translateY(-1px)}

/* hero */
header{padding:72px 0 60px;text-align:center}
.pill{
  display:inline-flex;align-items:center;gap:8px;font-size:13px;color:#c7c3ff;
  background:rgba(109,92,247,.13);border:1px solid rgba(109,92,247,.32);
  padding:6px 15px;border-radius:999px;margin-bottom:26px;
}
.pill .pt{width:7px;height:7px;border-radius:50%;background:var(--ok);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.45;transform:scale(.8)}}
h1{font-size:clamp(34px,6.4vw,60px);line-height:1.08;letter-spacing:-1.6px;font-weight:800}
h1 .g{background:linear-gradient(100deg,#a78bfa,#22d3ee);-webkit-background-clip:text;background-clip:text;color:transparent}
.sous{font-size:clamp(16px,2.2vw,19px);color:var(--gris);max-width:620px;margin:22px auto 0}

.rangee{display:flex;gap:13px;justify-content:center;flex-wrap:wrap;margin-top:36px}
.btn{
  border:none;cursor:pointer;font-family:inherit;font-weight:650;font-size:15px;
  padding:15px 30px;border-radius:11px;transition:.2s;text-decoration:none;display:inline-block;
}
.btn-p{background:linear-gradient(135deg,#7c6cf8,#6d5cf7);color:#fff;box-shadow:0 6px 26px rgba(109,92,247,.45)}
.btn-p:hover{transform:translateY(-2px);box-shadow:0 10px 32px rgba(109,92,247,.6)}
.btn-s{background:transparent;color:var(--texte);border:1px solid var(--bord)}
.btn-s:hover{border-color:#3a3a52;background:rgba(255,255,255,.03)}

/* chiffres */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;
  background:var(--bord);border:1px solid var(--bord);border-radius:15px;overflow:hidden;margin:66px 0}
.stat{background:var(--bg2);padding:26px 20px;text-align:center}
.stat b{display:block;font-size:27px;font-weight:800;letter-spacing:-.8px;
  background:linear-gradient(120deg,#fff,#b9b3ff);-webkit-background-clip:text;background-clip:text;color:transparent}
.stat span{font-size:13px;color:var(--gris)}

/* cartes documents */
h2.sec{font-size:clamp(24px,3.4vw,33px);letter-spacing:-.9px;margin:0 0 10px;font-weight:750}
p.sec{color:var(--gris);margin-bottom:32px;font-size:16px}
.grille{display:grid;grid-template-columns:repeat(auto-fill,minmax(285px,1fr));gap:15px}
.carte{
  background:var(--card);border:1px solid var(--bord);border-radius:15px;padding:23px;
  transition:.22s;position:relative;overflow:hidden;
}
.carte::after{
  content:'';position:absolute;inset:0;border-radius:15px;padding:1px;
  background:linear-gradient(135deg,rgba(109,92,247,.5),transparent 45%);
  -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);
  -webkit-mask-composite:xor;mask-composite:exclude;opacity:0;transition:.22s;pointer-events:none;
}
.carte:hover{transform:translateY(-3px);border-color:#2e2e42;background:#161621}
.carte:hover::after{opacity:1}
.carte .haut{display:flex;align-items:start;justify-content:space-between;gap:12px;margin-bottom:11px}
.carte h3{font-size:16.5px;font-weight:680;letter-spacing:-.25px}
.tag{font-size:11px;font-weight:650;padding:3px 9px;border-radius:6px;white-space:nowrap}
.tag.ob{background:rgba(52,211,153,.14);color:var(--ok);border:1px solid rgba(52,211,153,.3)}
.tag.rec{background:rgba(251,191,36,.13);color:var(--or);border:1px solid rgba(251,191,36,.3)}
.carte p{font-size:14px;color:var(--gris);margin-bottom:14px}
.carte .bas{display:flex;align-items:center;justify-content:space-between;font-size:12.5px;color:#6b6b80}

/* apercu */
#apercu{margin:80px 0 0;display:none}
.boite{background:var(--card);border:1px solid var(--bord);border-radius:15px;overflow:hidden}
.boite .tete{padding:14px 19px;border-bottom:1px solid var(--bord);display:flex;
  align-items:center;gap:9px;font-size:13px;color:var(--gris);background:var(--bg2)}
.fc{width:10px;height:10px;border-radius:50%}
.doc{padding:26px 24px;max-height:440px;overflow-y:auto;font-size:14.5px;line-height:1.75}
.doc h2{font-size:19px;margin:22px 0 9px;letter-spacing:-.4px}
.doc h2:first-child{margin-top:0}
.doc h3{font-size:15.5px;margin:17px 0 7px}
.doc ul,.doc ol{margin:9px 0 9px 21px}
.doc li{margin-bottom:5px}
.doc strong{color:#fff;font-weight:650}
.doc p{margin-bottom:11px;color:#d3d3e0}
.doc code{background:#1c1c28;padding:2px 6px;border-radius:5px;font-size:13px}
.doc::-webkit-scrollbar{width:8px}
.doc::-webkit-scrollbar-thumb{background:#2a2a3c;border-radius:4px}

/* formulaire */
#app{margin:80px 0 0;display:none}
.form{background:var(--card);border:1px solid var(--bord);border-radius:17px;padding:28px}
.grille-f{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}
label{display:block;font-size:12.5px;color:var(--gris);margin-bottom:6px;font-weight:550}
input,select,textarea{
  width:100%;background:var(--bg2);border:1px solid var(--bord);border-radius:9px;
  padding:11px 13px;color:var(--texte);font-family:inherit;font-size:14.5px;transition:.18s;
}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);
  box-shadow:0 0 0 3px rgba(109,92,247,.16)}
textarea{resize:vertical;min-height:78px}
.champ{margin-bottom:16px}
.aide{font-size:11.5px;color:#5f5f75;margin-top:5px}

.loading{display:none;text-align:center;padding:52px 20px}
.spin{
  width:42px;height:42px;margin:0 auto 18px;border-radius:50%;
  border:3px solid rgba(109,92,247,.22);border-top-color:var(--accent);
  animation:rot .75s linear infinite;
}
@keyframes rot{to{transform:rotate(360deg)}}
.loading p{color:var(--gris);font-size:14.5px}
.loading .secs{font-family:'JetBrains Mono',monospace;color:var(--accent2)}

.alerte{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.32);
  color:#fca5a5;padding:13px 16px;border-radius:10px;font-size:14px;margin-bottom:18px;display:none}

footer{border-top:1px solid var(--bord);margin-top:90px;padding:30px 0 44px;
  color:#5f5f75;font-size:13px;text-align:center}
footer a{color:#8b8b9e}

@media(max-width:640px){
  header{padding:44px 0 40px}
  .stats{grid-template-columns:1fr 1fr;margin:44px 0}
  .form{padding:20px}
  .doc{padding:18px 16px}
}
</style>
</head>
<body>
<div class="halo h1"></div><div class="halo h2"></div>

<div class="wrap">
  <nav>
    <div class="marque">
      <div class="pastille">
        <svg viewBox="0 0 24 24" fill="none"><path d="M13 2 4.5 13.5H11L9.5 22 19 10.5h-6.5L13 2Z"
          fill="#fff" stroke="#fff" stroke-width="1" stroke-linejoin="round"/></svg>
      </div>
      Pulse
    </div>
    <a class="cta" href="#docs">Voir les documents</a>
  </nav>

  <header>
    <div class="pill"><span class="pt"></span> Conforme au droit français 2026</div>
    <h1>Les documents que la loi<br>t'<span class="g">oblige</span> à publier.<br>En 2 minutes.</h1>
    <p class="sous">CGV, mentions légales, RGPD, cookies… Rédigés pour ton activité,
      prêts à coller sur ton site. Sans avocat, sans jargon, sans 800 € de facture.</p>
    <div class="rangee">
      <a class="btn btn-p" href="#docs">Commencer gratuitement</a>
      <a class="btn btn-s" href="#comment">Comment ça marche</a>
    </div>
  </header>

  <div class="stats">
    <div class="stat"><b>1 400 €</b><span>amende encourue sans mentions légales</span></div>
    <div class="stat"><b>6</b><span>documents obligatoires couverts</span></div>
    <div class="stat"><b>2 min</b><span>pour être en règle</span></div>
    <div class="stat"><b>19 €</b><span>par mois, résiliable</span></div>
  </div>

  <section id="docs">
    <h2 class="sec">Ce dont tu as besoin</h2>
    <p class="sec">Clique sur un document pour voir un extrait réel — avant même de payer.</p>
    <div class="grille" id="grille"></div>
  </section>

  <section id="apercu">
    <h2 class="sec" id="ap-titre">Aperçu</h2>
    <p class="sec">Voici un extrait généré pour une boutique type. Le tien sera rempli avec tes informations.</p>
    <div class="boite">
      <div class="tete">
        <span class="fc" style="background:#ff5f57"></span>
        <span class="fc" style="background:#febc2e"></span>
        <span class="fc" style="background:#28c840"></span>
        <span style="margin-left:8px" id="ap-nom">document.md</span>
      </div>
      <div class="doc" id="ap-corps"></div>
    </div>
    <div class="rangee" style="justify-content:flex-start;margin-top:22px">
      <button class="btn btn-p" onclick="ouvrirFormulaire()">Générer le mien →</button>
    </div>
  </section>

  <section id="app">
    <h2 class="sec">Tes informations</h2>
    <p class="sec">Remplis une fois, génère tous tes documents.</p>
    <div class="form">
      <div class="alerte" id="alerte"></div>
      <div class="loading" id="loading">
        <div class="spin"></div>
        <p>Rédaction en cours… <span class="secs" id="compteur">0s</span></p>
      </div>
      <div id="champs">
        <div class="grille-f">
          <div class="champ"><label>Nom du site / de la boutique *</label>
            <input id="nom" placeholder="Ma Boutique"></div>
          <div class="champ"><label>Forme juridique</label>
            <select id="forme">
              <option>Micro-entreprise</option><option>EI (entreprise individuelle)</option>
              <option>EURL</option><option>SASU</option><option>SAS</option>
              <option>SARL</option><option>SA</option><option>Association</option>
            </select></div>
          <div class="champ"><label>Activité</label>
            <input id="activite" placeholder="vente de vêtements en ligne"></div>
          <div class="champ"><label>Adresse complète</label>
            <input id="adresse" placeholder="12 rue de la Paix, 75002 Paris"></div>
          <div class="champ"><label>Email de contact</label>
            <input id="email" type="email" placeholder="contact@ma-boutique.fr"></div>
          <div class="champ"><label>Téléphone</label>
            <input id="telephone" placeholder="01 23 45 67 89"></div>
          <div class="champ"><label>SIRET</label>
            <input id="siret" placeholder="123 456 789 00012"></div>
          <div class="champ"><label>TVA intracommunautaire</label>
            <input id="tva" placeholder="FR 12 345678901"><div class="aide">Vide si non assujetti</div></div>
          <div class="champ"><label>Capital social</label>
            <input id="capital" placeholder="1 000 €"><div class="aide">Sociétés uniquement</div></div>
          <div class="champ"><label>Responsable de publication</label>
            <input id="responsable" placeholder="Jean Dupont"></div>
          <div class="champ"><label>Hébergeur du site</label>
            <input id="herbergeur" placeholder="Vercel Inc."></div>
          <div class="champ"><label>Adresse du site</label>
            <input id="site" placeholder="ma-boutique.vercel.app"></div>
        </div>
        <div class="grille-f">
          <div class="champ"><label>Tu vends en ligne ?</label>
            <select id="vente"><option value="oui">Oui</option><option value="non">Non</option></select></div>
          <div class="champ"><label>Tu collectes des données ?</label>
            <select id="donnees"><option value="oui">Oui</option><option value="non">Non</option></select></div>
          <div class="champ"><label>Cookies non essentiels ?</label>
            <select id="cookies"><option value="oui">Oui (analytics, pub)</option>
              <option value="non">Non, uniquement technique</option></select></div>
          <div class="champ"><label>Moyens de paiement</label>
            <input id="paiement" placeholder="carte bancaire via Stripe, PayPal"></div>
          <div class="champ"><label>Livraison</label>
            <input id="livraison" placeholder="Colissimo, 3 à 5 jours ouvrés"></div>
          <div class="champ"><label>Ta clé d'abonnement</label>
            <input id="licence" placeholder="PULSE-20260101-a1b2c3…">
            <div class="aide">Reçue après ton abonnement</div></div>
        </div>
        <div class="champ"><label>Quel document générer ?</label>
          <select id="doc"></select></div>
        <div class="rangee" style="justify-content:flex-start">
          <button class="btn btn-p" id="go" onclick="lancer()">Générer mon document</button>
          <a class="btn btn-s" id="abon" href="#" target="_blank" rel="noopener">S'abonner — 19 €/mois</a>
        </div>
      </div>
    </div>
  </section>

  <section id="comment" style="margin-top:70px">
    <h2 class="sec">Comment ça marche</h2>
    <p class="sec">Trois étapes, aucune compétence juridique requise.</p>
    <div class="grille">
      <div class="carte"><div class="haut"><h3>1. Tu décris ton activité</h3></div>
        <p>Forme juridique, adresse, ce que tu vends. Deux minutes, une seule fois.</p></div>
      <div class="carte"><div class="haut"><h3>2. Pulse rédige</h3></div>
        <p>Chaque document est écrit pour <strong>ton</strong> activité, pas un modèle générique
          recopié de partout.</p></div>
      <div class="carte"><div class="haut"><h3>3. Tu colles et c'est réglé</h3></div>
        <p>Markdown ou texte : tu copies sur ton site. Mets à jour quand ton activité change.</p></div>
    </div>
  </section>

  <footer>
    <p>Pulse — documents juridiques pour sites marchands français.</p>
    <p style="margin-top:8px;font-size:12px">Les documents générés sont des modèles.
      Fais-les vérifier par un professionnel du droit avant publication.</p>
  </footer>
</div>

<script>
let DOCS = {};
let EN_COURS = null;

function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}

function md(t){
  let h = esc(t);
  h = h.replace(/^### (.+)$/gm,'<h3>$1</h3>')
       .replace(/^## (.+)$/gm,'<h2>$1</h2>')
       .replace(/^# (.+)$/gm,'<h2>$1</h2>')
       .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
       .replace(/`(.+?)`/g,'<code>$1</code>')
       .replace(/^[*-] (.+)$/gm,'<li>$1</li>')
       .replace(/^\d+\. (.+)$/gm,'<li>$1</li>');
  h = h.replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g,'<ul>$1</ul>');
  h = h.split(/\n{2,}/).map(b=>{
    const s=b.trim();
    if(!s) return '';
    if(/^<(h2|h3|ul|li|pre|blockquote)/.test(s)) return s;
    return '<p>'+s.replace(/\n/g,'<br>')+'</p>';
  }).join('');
  return h;
}

async function charger(){
  DOCS = await (await fetch('/api/documents')).json();
  const g = document.getElementById('grille');
  const sel = document.getElementById('doc');
  for(const [k,d] of Object.entries(DOCS)){
    const tag = d.obligatoire
      ? '<span class="tag ob">Obligatoire</span>'
      : '<span class="tag rec">Recommandé</span>';
    g.insertAdjacentHTML('beforeend', `
      <div class="carte" onclick="apercu('${k}')" style="cursor:pointer">
        <div class="haut"><h3>${esc(d.titre)}</h3>${tag}</div>
        <p>${esc(d.a_quoi)}</p>
        <div class="bas"><span>${esc(d.duree)}</span><span>Voir un extrait →</span></div>
      </div>`);
    sel.insertAdjacentHTML('beforeend', `<option value="${k}">${esc(d.titre)}</option>`);
  }
  const pp = await (await fetch('/api/paypal')).json();
  if(pp.actif){
    document.getElementById('abon').href =
      `https://www.paypal.com/webapps/billing/plans/subscribe?plan_id=${pp.plan_id}`;
  } else {
    document.getElementById('abon').style.display = 'none';
    const a = document.getElementById('alerte');
    a.style.display='block';
    a.textContent = "Le paiement n'est pas encore activé — la génération est ouverte en attendant.";
  }
}

async function apercu(k){
  const box = document.getElementById('apercu');
  document.getElementById('ap-titre').textContent = DOCS[k].titre;
  document.getElementById('ap-nom').textContent = k + '.md';
  const corps = document.getElementById('ap-corps');
  box.style.display='block';
  corps.innerHTML = '<p style="color:#8b8b9e">Rédaction d\'un extrait…</p>';
  box.scrollIntoView({behavior:'smooth',block:'start'});
  try{
    const r = await fetch('/api/preview/'+k,{method:'GET'});
    const d = await r.json();
    corps.innerHTML = md(d.contenu||'');
  }catch(e){
    corps.innerHTML = '<p style="color:#fca5a5">Impossible de générer l\'extrait pour le moment.</p>';
  }
}

function ouvrirFormulaire(){
  document.getElementById('app').style.display='block';
  document.getElementById('app').scrollIntoView({behavior:'smooth',block:'start'});
}

async function lancer(){
  const b = document.getElementById('go');
  const l = document.getElementById('loading');
  const c = document.getElementById('champs');
  const a = document.getElementById('alerte');
  a.style.display='none';
  c.style.display='none'; l.style.display='block'; b.disabled=true;

  const debut = Date.now();
  EN_COURS = setInterval(()=>{
    document.getElementById('compteur').textContent =
      Math.floor((Date.now()-debut)/1000)+'s';
  },250);

  const corps = {doc:document.getElementById('doc').value};
  ['nom','forme','activite','adresse','email','telephone','siret','tva','capital',
   'responsable','herbergeur','site','vente','donnees','cookies','paiement',
   'livraison','licence'].forEach(id=>{ corps[id]=document.getElementById(id).value; });

  try{
    const r = await fetch('/api/generer',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(corps)});
    const d = await r.json();
    if(!r.ok){
      a.style.display='block';
      a.textContent = r.status===402
        ? "Il faut un abonnement actif pour générer le document complet. Clique sur « S'abonner »."
        : (d.detail || "Une erreur est survenue.");
      c.style.display='block';
    } else {
      document.getElementById('ap-titre').textContent = d.titre;
      document.getElementById('ap-nom').textContent = d.doc + '.md';
      document.getElementById('ap-corps').innerHTML = md(d.contenu);
      document.getElementById('apercu').style.display='block';
      document.getElementById('apercu').scrollIntoView({behavior:'smooth',block:'start'});
    }
  }catch(e){
    a.style.display='block'; a.textContent="Erreur réseau. Réessaie.";
    c.style.display='block';
  }finally{
    clearInterval(EN_COURS);
    l.style.display='none'; b.disabled=false;
    if(c.style.display==='none') c.style.display='block';
  }
}

charger();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def page():
    return PAGE
