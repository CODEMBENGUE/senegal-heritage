#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 SÉNÉGAL HERITAGE STUDIO v5 — Agent autonome, corpus dynamique, API HTTP
================================================================================
 Plus AUCUNE base de connaissances codée en dur. Le savoir vit dans corpus/ :
   - corpus/seed/   : optionnel, seed fourni par l'utilisateur
   - corpus/auto/   : alimenté AUTOMATIQUEMENT par recherche web + LLM local
   - corpus/cache/  : réservé aux synthèses LLM non vérifiées

 Le système sait acquérir ce qu'il ignore :
   1. Cherche dans le corpus local
   2. Sinon : recherche web (domaines officiels uniquement)
   3. Sinon : synthèse LLM local (marquée non vérifiée)
   4. Sinon : message d'erreur explicite avec instructions

 Commandes :
   python senegal_heritage.py chat
   python senegal_heritage.py ask "..." [--no-web] [--no-llm]
   python senegal_heritage.py research "..." [--no-web] [--no-llm]
   python senegal_heritage.py generate <slug>
   python senegal_heritage.py batch
   python senegal_heritage.py categories
   python senegal_heritage.py search "..."
   python senegal_heritage.py stats
   python senegal_heritage.py validate
   python senegal_heritage.py seed-import <fichier.json>
   python senegal_heritage.py serve [--host 0.0.0.0] [--port 8080]

 Déploiement Fly.io : voir Dockerfile + fly.toml fournis séparément.
================================================================================
"""

import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import http.server
import socketserver
import uuid
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================

STUDIO_HOME = Path(os.environ.get("STUDIO_HOME", str(Path(__file__).resolve().parent)))
BASE_DIR = STUDIO_HOME
OUTPUT_DIR = BASE_DIR / "output"
FRAMES_DIR = OUTPUT_DIR / "frames"
AUDIO_DIR = OUTPUT_DIR / "audio"
VIDEO_DIR = OUTPUT_DIR / "video"
FINAL_DIR = OUTPUT_DIR / "final"
GENERATED_DIR = BASE_DIR / "generated"
CORPUS_DIR = BASE_DIR / "corpus"
LOG_FILE = OUTPUT_DIR / "studio.log"
MANIFEST_FILE = OUTPUT_DIR / "manifest.json"

for _d in (FRAMES_DIR, AUDIO_DIR, VIDEO_DIR, FINAL_DIR, GENERATED_DIR,
           CORPUS_DIR, CORPUS_DIR / "seed", CORPUS_DIR / "auto", CORPUS_DIR / "cache"):
    _d.mkdir(parents=True, exist_ok=True)

RENDER_WIDTH = 640
RENDER_HEIGHT = 360
OUTPUT_WIDTH = 3840
OUTPUT_HEIGHT = 2160
FPS = 24
VIDEO_BITRATE = "20M"
AUDIO_BITRATE = "192k"
TTS_LANG = "fr"
TTS_SPEED = 150
OUTPUT_MODE = "auto"
PARALLEL_WORKERS = max(1, (os.cpu_count() or 2) - 1)
if os.environ.get("MAX_WORKERS"):
    try:
        PARALLEL_WORKERS = max(1, int(os.environ["MAX_WORKERS"]))
    except ValueError:
        pass
PARALLEL_THRESHOLD_FRAMES = 48
SUBTITLE_ENABLED = True
SUBTITLE_EMBED = False
SUBTITLE_MAX_CHARS_PER_LINE = 42
SUBTITLE_MAX_LINES = 2
SUBTITLE_MIN_DURATION = 1.0
DRY_RUN = False
STRICT = False
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")
AUTO_ACQUIRE = os.environ.get("AUTO_ACQUIRE", "1") == "1"

# ==============================================================================
# 2. LOGGER
# ==============================================================================

class Logger:
    def __init__(self, path):
        self.path = path
        self.path.parent.mkdir(exist_ok=True, parents=True)
        self._lock = threading.Lock()
    def _w(self, lvl, msg):
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{lvl}] {msg}\n"
        try:
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            pass
    def info(self, m):    self._w("INFO", m)
    def warn(self, m):    self._w("WARN", m)
    def error(self, m):   self._w("ERROR", m)
    def success(self, m): self._w("OK", m)

log = Logger(LOG_FILE)

# ==============================================================================
# 3. LISTE BLANCHE DES SOURCES OFFICIELLES
# ==============================================================================

OFFICIAL_DOMAINS_GOV = {
    "presidence.sn", "primature.sn", "gouv.sn",
    "diplomatie.gouv.sn", "interieur.sec.gouv.sn", "finances.gouv.sn",
    "justice.gouv.sn", "culture.gouv.sn", "commerce.gouv.sn",
    "urbanisme.gouv.sn", "mittd.gouv.sn", "servicepublic.gouv.sn",
    "dri.gouv.sn", "forcesarmees.gouv.sn",
}
OFFICIAL_DOMAINS_ARCHIVES = {
    "archivesdusenegal.gouv.sn", "archives.sn", "vie-publique.sn",
    "jo.gouv.sn", "senarchives.org.sn", "bo.senegalservices.sn",
}
OFFICIAL_DOMAINS_RESEARCH = {
    "ifan.ucad.sn", "revues.ifan.sn", "archives.ifan.sn", "ucad.sn",
}
OFFICIAL_DOMAINS_INTL = {
    "unesco.org", "whc.unesco.org", "un.org", "undp.org", "unicef.org",
    "worldbank.org", "imf.org", "afdb.org", "au.int", "ecowas.int",
}
OFFICIAL_DOMAINS_MEDIA = {"aps.sn", "rts.sn", "seneweb.com"}
OFFICIAL_DOMAINS_DATA = {"ansd.sn", "arm.gouv.sn", "ads.sn", "secusociale.sn"}
WHITELIST_DOMAINS = (
    OFFICIAL_DOMAINS_GOV | OFFICIAL_DOMAINS_ARCHIVES | OFFICIAL_DOMAINS_RESEARCH
    | OFFICIAL_DOMAINS_INTL | OFFICIAL_DOMAINS_MEDIA | OFFICIAL_DOMAINS_DATA
)
PROVABLE_DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".odt", ".txt", ".csv", ".xlsx"}

def is_url_whitelisted(url):
    try:
        h = urllib.parse.urlparse(url).hostname
        if not h: return False
        h = h.lower()
        if h.startswith("www."): h = h[4:]
        for d in WHITELIST_DOMAINS:
            dc = d.lower()
            if dc.startswith("www."): dc = dc[4:]
            if h == dc or h.endswith("." + dc):
                return True
        return False
    except Exception:
        return False

def is_provable_document(url):
    try:
        return any(urllib.parse.urlparse(url).path.lower().endswith(e)
                   for e in PROVABLE_DOCUMENT_EXTENSIONS)
    except Exception:
        return False

def validate_source(url):
    if not url or not url.startswith(("http://", "https://")):
        return {"valid": False, "reason": "URL invalide"}
    if is_url_whitelisted(url):
        return {"valid": True, "reason": "Domaine officiel reconnu", "type": "official"}
    if is_provable_document(url):
        return {"valid": True, "reason": "Document prouvable", "type": "document"}
    return {"valid": False, "reason": "Source non reconnue (hors liste blanche)"}

# ==============================================================================
# 4. TOKENIZER + LEVENSHTEIN
# ==============================================================================

_STOP = set("""
le la les un une des du de d au aux à et ou mais donc or ni car que qui
quoi dont où ce cet cette ces son sa ses leur leurs mon ma mes ton ta tes
notre nos votre vos il elle ils elles on nous vous je tu se s est sont
être avoir a ont avait avaient fait faire plus moins très peu bien
dans sur sous avec sans pour par vers chez entre comme si
""".split())

def _tokenize(text):
    text = text.lower()
    text = re.sub(r"[^\wàâäéèêëîïôöùûüç'-]+", " ", text)
    return [w for w in text.split() if len(w) > 2 and w not in _STOP]

def _levenshtein(a, b):
    if a == b: return 0
    if abs(len(a)-len(b)) > 2: return 99
    la, lb = len(a), len(b)
    prev = list(range(lb+1))
    for i in range(1, la+1):
        cur = [i] + [0]*lb
        for j in range(1, lb+1):
            cost = 0 if a[i-1] == b[j-1] else 1
            cur[j] = min(cur[j-1]+1, prev[j]+1, prev[j-1]+cost)
        prev = cur
    return prev[lb]

def _slugify(t):
    t = re.sub(r"[^\w\s-]", "", t.lower())
    return re.sub(r"\s+", "_", t.strip())[:40] or "heritage"

def _titleize(t):
    return " ".join(w.capitalize() if len(w) > 3 else w for w in t.split())

# ==============================================================================
# 5. CORPUS DYNAMIQUE (remplace KNOWLEDGE_BASE hardcodée)
# ==============================================================================

CORPUS: list = []           # Corpus mutable en mémoire
_CORPUS_LOCK = threading.Lock()

def _load_corpus_from_disk():
    """Charge récursivement tous les JSON du dossier corpus/. Tolérant aux formats."""
    global CORPUS
    loaded = 0
    seen_ids = {e.get("id") for e in CORPUS if e.get("id")}
    for p in sorted(CORPUS_DIR.rglob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            entries = data if isinstance(data, list) else [data]
            for e in entries:
                if not isinstance(e, dict): continue
                if not all(k in e for k in ("text",)): continue
                eid = e.get("id") or f"import_{hashlib.md5(e['text'][:80].encode()).hexdigest()[:10]}"
                if eid in seen_ids: continue
                seen_ids.add(eid)
                e.setdefault("id", eid)
                e.setdefault("category", "auto")
                e.setdefault("tags", [])
                e.setdefault("source", p.stem)
                e.setdefault("origin", "disk")
                CORPUS.append(e)
                loaded += 1
        except Exception as ex:
            log.warn(f"Corpus {p.name}: {ex}")
    if loaded:
        print(f"  ✓ Corpus : {loaded} entrées chargées depuis {CORPUS_DIR}")
    return loaded

def _persist_entries(entries):
    """Persiste de nouvelles entrées dans corpus/auto/YYYY-MM-DD.json (idempotent)."""
    if not entries: return 0
    auto_dir = CORPUS_DIR / "auto"
    auto_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    path = auto_dir / f"{day}.json"
    existing = []
    if path.exists():
        try: existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception: existing = []
    existing_ids = {e.get("id") for e in existing}
    added = 0
    for e in entries:
        if e.get("id") in existing_ids: continue
        existing.append(e)
        added += 1
    if added:
        path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return added

def _add_to_corpus(entry):
    """Ajoute une entrée au corpus en mémoire (thread-safe)."""
    with _CORPUS_LOCK:
        if any(e.get("id") == entry.get("id") for e in CORPUS):
            return False
        CORPUS.append(entry)
        return True

# ==============================================================================
# 6. MOTEUR DE RECHERCHE (sur corpus dynamique, tolérant au vide)
# ==============================================================================

SYNONYMS = {
    "pierre": ["monolithe","mégalithe","rocher"], "monolithe": ["pierre","mégalithe"],
    "reine": ["linguère","souveraine"], "linguère": ["reine","souveraine"],
    "roi": ["damel","souverain"], "poète": ["senghor","écrivain"],
    "griot": ["conteur","traditionniste","oral"], "baobab": ["arbre","sacré"],
    "sépulture": ["tombe","nécropole"], "archéologie": ["fouille","archéologue"],
    "unesco": ["patrimoine","mondial"], "restauration": ["réhabilitation","sauvegarde"],
}

class ResearchEngine:
    def __init__(self, corpus=None):
        self.corpus = corpus if corpus is not None else list(CORPUS)
        self.vocab = set()
        self.docs_tokens = []
        self.categories = Counter()
        for d in self.corpus:
            text = d.get("text", "") + " " + " ".join(d.get("tags", []) or [])
            toks = _tokenize(text)
            self.docs_tokens.append(toks)
            self.vocab.update(toks)
            self.categories[d.get("category", "autre")] += 1
        self.df = Counter()
        for toks in self.docs_tokens:
            for w in set(toks):
                self.df[w] += 1
        self.n = len(self.corpus)
        self.doc_lens = [len(t) for t in self.docs_tokens]
        self.avg_len = sum(self.doc_lens) / max(1, self.n)

    def _idf(self, w):
        return math.log((self.n+1) / (self.df.get(w,0)+1)) + 1.0

    def _bm25(self, qt, i, k1=1.5, b=0.75):
        tf = Counter(self.docs_tokens[i])
        dl = self.doc_lens[i]
        s = 0.0
        for q in qt:
            if q not in tf: continue
            s += self._idf(q) * (tf[q]*(k1+1)) / (tf[q]+k1*(1-b+b*dl/self.avg_len))
        return s

    def _expand(self, toks):
        ex = list(toks)
        for t in toks:
            if t in SYNONYMS: ex.extend(SYNONYMS[t])
        for t in toks:
            if t in self.vocab: continue
            best, bd = None, 3
            for v in self.vocab:
                d = _levenshtein(t, v)
                if d < bd: best, bd = v, d
            if best: ex.append(best)
        return list(set(ex))

    def search(self, query, top_k=8, category=None):
        if self.n == 0: return []
        qt = _tokenize(query)
        if not qt: return []
        ex = self._expand(qt)
        scored = []
        for i in range(self.n):
            if category and self.corpus[i].get("category") != category: continue
            s = self._bm25(ex, i)
            if self.corpus[i].get("category") in [t.lower() for t in qt]:
                s *= 1.4
            if s > 0: scored.append((i, s))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [dict(self.corpus[i], score=s) for i, s in scored[:top_k]]

    def relevant_tags(self, query):
        q = self._expand(_tokenize(query))
        tags = Counter()
        for doc in self.corpus:
            txt = (doc.get("text","") + " " + " ".join(doc.get("tags", []) or [])).lower()
            for t in q:
                if t in txt:
                    for tag in doc.get("tags", []) or []: tags[tag] += 1
        return [t for t, _ in tags.most_common(10)]

    def list_categories(self):
        return dict(self.categories)

# ==============================================================================
# 7. RECHERCHE WEB RESTREINTE
# ==============================================================================

def restricted_web_search(query, max_results=10):
    priority = ["aps.sn","unesco.org","gouv.sn","archivesdusenegal.gouv.sn","ifan.ucad.sn","seneweb.com"]
    site_q = " OR ".join(f"site:{d}" for d in priority[:5])
    full_q = f"({query}) ({site_q})"
    candidates = []
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            for r in ddgs.text(full_q, max_results=max_results):
                candidates.append({"url": r["href"], "title": r.get("title",""), "snippet": r.get("body","")})
    except ImportError:
        log.warn("duckduckgo_search absent — fallback vers URLs directes")
        candidates = _fallback_search(query)
    except Exception as e:
        log.warn(f"Recherche Web : {e}")
        candidates = _fallback_search(query)
    valid = []
    for c in candidates:
        v = validate_source(c["url"])
        if v["valid"]:
            c["validation"] = v
            valid.append(c)
        else:
            log.info(f"Rejeté : {c['url']}")
    return valid

def _fallback_search(query):
    q = urllib.parse.quote(query)
    return [
        {"url": f"https://www.aps.sn/?s={q}", "title": "APS — Recherche", "snippet": ""},
        {"url": f"https://whc.unesco.org/en/search/?q={q}", "title": "UNESCO", "snippet": ""},
        {"url": f"https://www.vie-publique.sn/recherche?q={q}", "title": "Vie Publique", "snippet": ""},
    ]

def extract_content(url, max_chars=3000):
    try:
        try:
            import trafilatura
            d = trafilatura.fetch_url(url)
            if d:
                t = trafilatura.extract(d)
                if t: return t[:max_chars]
        except ImportError: pass
        req = urllib.request.Request(url, headers={"User-Agent": "SenegalHeritageStudio/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="ignore")
        text = re.sub(r"<script.*?</script>", "", html, flags=re.DOTALL|re.IGNORECASE)
        text = re.sub(r"<style.*?</style>", "", text, flags=re.DOTALL|re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()[:max_chars]
    except Exception as e:
        log.warn(f"Extraction {url} : {e}")
        return ""

# ==============================================================================
# 8. LLM LOCAL (Ollama)
# ==============================================================================

def detect_llm():
    try:
        base = OLLAMA_URL.split("/api/")[0]
        req = urllib.request.Request(f"{base}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2) as r:
            d = json.loads(r.read().decode("utf-8"))
            return {"available": True, "models": [m.get("name","") for m in d.get("models",[])]}
    except Exception:
        return {"available": False, "models": []}

def llm_generate(prompt, model=None, timeout=180):
    info = detect_llm()
    if not info["available"]: return None
    mdl = model or OLLAMA_MODEL
    if info["models"] and mdl not in info["models"]: mdl = info["models"][0]
    payload = {"model": mdl, "prompt": prompt, "stream": False,
               "options": {"temperature": 0.6, "num_predict": 1500}}
    try:
        req = urllib.request.Request(OLLAMA_URL, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("response","").strip()
    except Exception as e:
        log.warn(f"LLM : {e}")
        return None

# ==============================================================================
# 9. ACQUISITION DE CONNAISSANCES (le cœur du remplacement de KNOWLEDGE_BASE)
# ==============================================================================

def acquire_knowledge(query, use_web=True, use_llm=True, max_sources=5, force=False):
    """
    Acquiert des connaissances sur un sujet absent du corpus :
      Étape 1 — Recherche web (domaines officiels uniquement) → entrées vérifiées
      Étape 2 — Synthèse LLM local (marquée non vérifiée) → entrées cache
      Étape 3 — Persistance dans corpus/auto/YYYY-MM-DD.json

    Retourne la liste des entrées ajoutées au corpus en mémoire.
    """
    added = []
    existing_ids = {e.get("id") for e in CORPUS}

    # Vérifier si on a déjà quelque chose
    engine = ResearchEngine(CORPUS)
    existing = engine.search(query, top_k=3)
    if existing and not force:
        print(f"  ✓ Corpus déjà pourvu ({len(existing)} correspondances)")
        return []

    # Étape 1 : web
    if use_web:
        try:
            print(f"  🌐 Acquisition web pour « {query} »…")
            candidates = restricted_web_search(query, max_results=20)
            web_added = 0
            for c in candidates[:max_sources]:
                content = extract_content(c["url"])
                if not content or len(content) < 100: continue
                eid = f"web_{hashlib.md5(c['url'].encode()).hexdigest()[:10]}"
                if eid in existing_ids: continue
                entry = {
                    "id": eid, "category": "auto",
                    "tags": _tokenize(query)[:10],
                    "source": c.get("title") or c["url"],
                    "text": content[:1500],
                    "url": c["url"],
                    "validation": c.get("validation", {}),
                    "captured_at": datetime.now().isoformat(),
                    "origin": "web", "verified": True,
                }
                if _add_to_corpus(entry):
                    existing_ids.add(eid); added.append(entry); web_added += 1
            if web_added:
                print(f"  ✓ {web_added} source(s) web officielle(s) acquise(s)")
        except Exception as e:
            log.warn(f"Acquisition web : {e}")

    # Étape 2 : LLM fallback (si web insuffisant)
    if use_llm and len(added) < 2:
        info = detect_llm()
        if info["available"]:
            print(f"  🤖 Synthèse LLM ({OLLAMA_MODEL})…")
            prompt = (
                f"Rédige une fiche factuelle sur : « {query} ».\n"
                "3 paragraphes de 2 à 3 phrases. Cite des dates, lieux et noms précis.\n"
                "Si tu n'es pas sûr d'une information, ne l'invente pas — indique-le.\n"
                "Termine par une phrase de fiabilité (haute/moyenne/basse).\n\n"
                "FICHE :"
            )
            raw = llm_generate(prompt)
            if raw and len(raw) > 80:
                eid = f"llm_{hashlib.md5((query + raw[:100]).encode()).hexdigest()[:10]}"
                if eid not in existing_ids:
                    entry = {
                        "id": eid, "category": "auto",
                        "tags": _tokenize(query)[:10],
                        "source": f"LLM ({OLLAMA_MODEL}) — non vérifié",
                        "text": raw[:1500],
                        "captured_at": datetime.now().isoformat(),
                        "origin": "llm", "verified": False,
                    }
                    if _add_to_corpus(entry):
                        existing_ids.add(eid); added.append(entry)
                        print(f"  ⚠ Synthèse LLM ajoutée (non vérifiée)")

    # Persistance
    if added:
        n = _persist_entries(added)
        if n:
            print(f"  💾 {n} entrée(s) persistée(s) dans corpus/auto/")
    return added

# ==============================================================================
# 10. GÉNÉRATEUR DE CONTENU (avec fallback en cascade)
# ==============================================================================

ROLE_STRUCTURE = [("accroche",8,"sensorialité"),("contexte",10,"factuel"),
                  ("detail",8,"surprise"),("preuve",10,"autorité"),("chute",8,"ouverture")]

SCENE_SUGGESTIONS = {
    "accroche": ["sunset_landscape","dawn_wide","clouds_sunset"],
    "contexte": ["map_zoom","monument_3d"],
    "detail":   ["artifact_macro","baobab_silhouette","monument_3d"],
    "preuve":   ["monument_3d","artifact_macro"],
    "chute":    ["dawn_wide","sunset_landscape","griot_fire"],
}

PROMPT_GEN = """Tu es un archiviste-cinéaste sénégalais. Produis une structure de 5 séquences.

SUJET : {subject}
EXTRAITS :
{excerpts}

Réponds STRICTEMENT en JSON :
{{"slug":"...","title":"...","region":"...","category":"site|figure|tradition|objet",
 "summary":"...","sequences":[{{"id":1,"role":"accroche","duration":8,"scene":"sunset_landscape",
 "voiceover_fr":"20-30 mots","source":"..."}}, ... 5 au total]}}"""

PROMPT_LLM_DIRECT = """Tu es un archiviste-cinéaste sénégalais. Produis une structure de 5 séquences sur :
SUJET : {subject}

Aucune source documentaire n'est disponible. Tu t'appuies UNIQUEMENT sur tes connaissances.
Sois factuel et prudent : si une information est incertaine, reste vague.
Utilise des scènes parmi : sunset_landscape, clouds_sunset, map_zoom, artifact_macro, monument_3d, dawn_wide, baobab_silhouette, griot_fire.

Réponds STRICTEMENT en JSON :
{{"slug":"...","title":"...","region":"...","category":"site|figure|tradition|objet",
 "summary":"...","sequences":[{{"id":1,"role":"accroche","duration":8,"scene":"...",
 "voiceover_fr":"20-30 mots","source":"LLM local — non vérifié"}}, ... 5 au total]}}"""

class ContentGenerator:
    def __init__(self, engine=None):
        self.engine = engine or ResearchEngine(CORPUS)

    def generate(self, subject, use_web=True, use_llm=True, auto_acquire=True):
        """
        Chaîne de fallback :
          1. Corpus local
          2. Acquisition web + LLM si insuffisant
          3. LLM direct (sans sources) si toujours vide
          4. Erreur explicite
        """
        docs = self.engine.search(subject, top_k=8)

        # Fallback 1 : acquérir si insuffisant
        if len(docs) < 3 and auto_acquire and AUTO_ACQUIRE:
            print(f"  ⏳ Corpus insuffisant ({len(docs)} doc) — acquisition en cours…")
            new_entries = acquire_knowledge(subject, use_web=use_web, use_llm=use_llm)
            if new_entries:
                self.engine = ResearchEngine(CORPUS)
                docs = self.engine.search(subject, top_k=8)

        # Fallback 2 : LLM direct si toujours vide
        if not docs:
            info = detect_llm()
            if use_llm and info["available"]:
                print("  ⚠ Aucune source — génération LLM directe (NON VÉRIFIÉE)")
                r = self._llm_direct(subject)
                if r: return r
            log.error(f"Impossible de générer « {subject} » : aucune source et aucun LLM.")
            print("\n  ❌ Impossible de traiter ce sujet.")
            print("     Solutions :")
            print("       - Installer duckduckgo_search : pip install duckduckgo-search")
            print("       - Lancer Ollama : ollama serve && ollama pull llama3.2")
            print(f"       - Placer manuellement un JSON dans {CORPUS_DIR}/seed/")
            return None

        # Génération normale
        info = detect_llm()
        if use_llm and info["available"]:
            r = self._with_llm(subject, docs)
            if r: return r
        return self._with_templates(subject, docs)

    def _with_llm(self, subject, docs):
        excerpts = "\n".join(f"- [{d.get('source','')}] {d.get('text','')[:400]}" for d in docs)
        raw = llm_generate(PROMPT_GEN.format(subject=subject, excerpts=excerpts))
        if not raw: return None
        try:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if not m: return None
            data = json.loads(m.group(0))
            self._validate(data)
            return data
        except Exception as e:
            log.warn(f"JSON LLM : {e}")
            return None

    def _llm_direct(self, subject):
        raw = llm_generate(PROMPT_LLM_DIRECT.format(subject=subject))
        if not raw: return None
        try:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if not m: return None
            data = json.loads(m.group(0))
            self._validate(data)
            for s in data["sequences"]:
                if "non vérifié" not in s.get("source", ""):
                    s["source"] = (s.get("source") or "LLM") + " — non vérifié"
            return data
        except Exception as e:
            log.warn(f"JSON LLM direct : {e}")
            return None

    def _with_templates(self, subject, docs):
        tags = self.engine.relevant_tags(subject)
        used = set()
        sequences = []
        for i, (role, dur, mode) in enumerate(ROLE_STRUCTURE):
            doc = None
            for d in docs:
                if d.get("id") not in used:
                    doc = d; used.add(d.get("id")); break
            if not doc: doc = docs[i % len(docs)]
            vo = self._compose(doc, mode)
            sequences.append({
                "id": i+1, "role": role, "duration": dur,
                "scene": random.choice(SCENE_SUGGESTIONS[role]),
                "scene_params": {}, "voiceover_fr": vo,
                "source": doc.get("source", "Documentation locale"),
            })
        return {"slug": _slugify(subject), "title": _titleize(subject),
                "region": _infer_region(tags) or "Sénégal",
                "category": _infer_category(tags),
                "summary": docs[0].get("text","")[:180], "sequences": sequences}

    def _compose(self, doc, mode):
        text = doc.get("text", "").strip()
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        s = sents[0] if sents else text
        if len(s.split()) > 30:
            s = " ".join(s.split()[:28]) + "…"
        if mode == "factuel": return s
        if mode == "surprise": return f"Un détail peu connu : {s[0].lower() + s[1:]}"
        if mode == "autorité": return f"Les sources confirment : {s[0].lower() + s[1:]}"
        if mode == "ouverture" and len(sents) > 1: return sents[-1]
        return s

    def _validate(self, d):
        for k in ("slug","title","region","sequences"):
            if k not in d: raise ValueError(f"manquant {k}")
        if len(d["sequences"]) != 5: raise ValueError("5 séquences attendues")

def _infer_region(tags):
    m = {"kaffrine":"Kaffrine","fatick":"Fatick","joal":"Joal-Fadiouth","birkelane":"Kaffrine",
         "tambacounda":"Tambacounda","dékheulé":"Diourbel","diakhao":"Fatick","thiès":"Thiès",
         "gorée":"Dakar","saint":"Saint-Louis","retba":"Dakar","dakar":"Dakar"}
    for t in tags:
        if t in m: return m[t]
    return None

def _infer_category(tags):
    if any(t in tags for t in ("site","mégalithe","sanctuaire","île")): return "site"
    if any(t in tags for t in ("poète","reine","roi","damel","résistance")): return "figure"
    if any(t in tags for t in ("griot","tradition","rituel","cuisine","musique")): return "tradition"
    return "site"

# ==============================================================================
# 11. SCÈNES PROCÉDURALES
# ==============================================================================

def _clamp(v, lo=0, hi=255): return lo if v < lo else hi if v > hi else int(v)
def _lerp(a, b, t): return a + (b - a) * t

def _write_ppm(path, buf, w, h):
    with open(path, "wb") as f:
        f.write(f"P6\n{w} {h}\n255\n".encode("ascii"))
        f.write(buf)

def _fill_rect(buf, w, h, x0, y0, rw, rh, color):
    r, g, b = color
    for y in range(max(0, y0), min(h, y0+rh)):
        base = y*w*3
        for x in range(max(0, x0), min(w, x0+rw)):
            i = base + x*3
            buf[i], buf[i+1], buf[i+2] = r, g, b

def sunset_landscape(t, w, h, p):
    buf = bytearray(w*h*3)
    horizon = int(h*0.72)
    dur = max(1, p.get("duration", 8))
    sx = int(w*(0.2+0.6*(t/dur))); sy, sr, gr = int(h*0.45), int(h*0.11), int(h*0.30)
    sky = []
    for y in range(horizon):
        f = y/horizon
        if f < 0.5: c = tuple(int(_lerp((30,40,90)[i], (240,120,60)[i], f*2)) for i in range(3))
        else: c = tuple(int(_lerp((240,120,60)[i], (255,190,110)[i], (f-0.5)*2)) for i in range(3))
        sky.append(c)
    gnd = [tuple(int(_lerp((60,30,20)[i], (20,10,8)[i], (y-horizon)/max(1,h-horizon))) for i in range(3)) for y in range(horizon, h)]
    idx = 0
    for y in range(h):
        base = sky[y] if y < horizon else gnd[y-horizon]
        for x in range(w):
            r, g, b = base
            d2 = (x-sx)**2 + (y-sy)**2
            if d2 < gr*gr:
                d = math.sqrt(d2)
                if d < sr: r, g, b = 255, 245, 210
                else:
                    k = (1.0-(d-sr)/(gr-sr))**2
                    r, g, b = _clamp(r+220*k), _clamp(g+140*k), _clamp(b+40*k)
            buf[idx], buf[idx+1], buf[idx+2] = r, g, b
            idx += 3
    return buf

def clouds_sunset(t, w, h, p):
    buf = sunset_landscape(t, w, h, p)
    for y in range(int(h*0.55)):
        for x in range(w):
            n = (math.sin(x*0.03+t*0.7)+math.sin(x*0.07-t*0.5)+math.sin(y*0.05+t*0.4))/3.0
            if n > 0.4:
                i = (y*w+x)*3
                k = (n-0.4)*200
                buf[i] = _clamp(buf[i]+k*0.9); buf[i+1] = _clamp(buf[i+1]+k*0.7); buf[i+2] = _clamp(buf[i+2]+k*0.5)
    return buf

def map_zoom(t, w, h, p):
    buf = bytearray(w*h*3)
    bg, grid, acc = (12,18,30), (40,60,90), (255,200,60)
    tx, ty = w*0.55, h*0.50
    zoom = 1.0+0.5*(t/max(1,p.get("duration",10)))
    step = max(20, int(w/20/zoom))
    for y in range(h):
        for x in range(w):
            r, g, b = bg
            if x % step == 0 or y % step == 0: r, g, b = grid
            d = math.hypot(x-tx, y-ty); pulse = 40+30*math.sin(t*3)
            if d < pulse: r, g, b = acc
            elif d < pulse*2.5:
                k = 1.0-(d-pulse)/(pulse*1.5)
                r, g, b = _clamp(r+acc[0]*k*0.5), _clamp(g+acc[1]*k*0.5), _clamp(b+acc[2]*k*0.5)
            i = (y*w+x)*3
            buf[i], buf[i+1], buf[i+2] = r, g, b
    return buf

def artifact_macro(t, w, h, p):
    buf = bytearray(w*h*3)
    rng = random.Random(42)
    noise = bytes(rng.randint(0,40) for _ in range(w*h))
    lx = w*0.5+w*0.15*math.sin(t*1.2); ly = h*0.4+h*0.1*math.cos(t*0.8)
    md = math.hypot(w, h)*0.6
    for y in range(h):
        for x in range(w):
            k = max(0.0, 1.0-math.hypot(x-lx, y-ly)/md)**2
            n = noise[y*w+x]; i = (y*w+x)*3
            buf[i] = _clamp(110*(0.4+0.6*k)+n); buf[i+1] = _clamp(80*(0.4+0.6*k)+n*0.7); buf[i+2] = _clamp(55*(0.4+0.6*k)+n*0.5)
    return buf

def monument_3d(t, w, h, p):
    buf = bytearray(w*h*3)
    for y in range(h):
        f = y/h; r, g, b = _clamp(20+60*f), _clamp(30+80*f), _clamp(80+100*f)
        for x in range(w):
            i = (y*w+x)*3
            buf[i], buf[i+1], buf[i+2] = r, g, b
    hz = int(h*0.65)
    for y in range(hz, h):
        f = (y-hz)/(h-hz); r, g, b = _clamp(120-60*f), _clamp(70-30*f), _clamp(40-20*f)
        for x in range(w):
            i = (y*w+x)*3
            buf[i], buf[i+1], buf[i+2] = r, g, b
    stones = p.get("stones", [(-80,0),(80,0),(0,-80),(0,80),(-60,-60),(60,60),(-60,60),(60,-60)])
    ang = t*0.6; cx, cy, sc = w*0.5, h*0.65, h/400.0
    proj = []
    for sx, sz in stones:
        rx = sx*math.cos(ang)-sz*math.sin(ang); rz = sx*math.sin(ang)+sz*math.cos(ang)
        proj.append((rz, cx+(rx-rz)*0.7*sc, cy+(rx+rz)*0.35*sc))
    proj.sort()
    for _, px, py in proj:
        sh, sw = 60*sc, 25*sc
        _fill_rect(buf, w, h, int(px-sw/2), int(py-sh), int(sw), int(sh), (150,130,100))
        _fill_rect(buf, w, h, int(px-sw/2), int(py-sh), max(2,int(sw*0.25)), int(sh), (100,85,65))
        _fill_rect(buf, w, h, int(px-sw/2), int(py-sh), int(sw), max(2,int(sh*0.1)), (200,180,150))
    return buf

def dawn_wide(t, w, h, p):
    buf = bytearray(w*h*3)
    hz = int(h*0.7)
    for y in range(hz):
        f = y/hz; r, g, b = _clamp(_lerp(40,220,f)), _clamp(_lerp(60,160,f)), _clamp(_lerp(110,180,f))
        for x in range(w):
            i = (y*w+x)*3
            buf[i], buf[i+1], buf[i+2] = r, g, b
    for y in range(max(0, hz-30), h):
        for x in range(w):
            wv = math.sin(x*0.01+t*0.8+(y-hz)*0.05)
            fog = max(0.0, 0.5+0.5*wv)*(1.0-(y-hz+30)/h)
            i = y*w*3+x*3
            buf[i] = _clamp(_lerp(60,230,fog)); buf[i+1] = _clamp(_lerp(70,210,fog)); buf[i+2] = _clamp(_lerp(100,220,fog))
    return buf

def baobab_silhouette(t, w, h, p):
    buf = dawn_wide(t, w, h, p)
    cx = int(w*0.5); base = int(h*0.85)
    tw, th = int(w*0.06), int(h*0.35)
    _fill_rect(buf, w, h, cx-tw//2, base-th, tw, th, (30,20,15))
    for k in range(6):
        ang = (k-2.5)*0.35
        bl = int(th*0.7)
        for s in range(bl):
            bx = cx+int(math.sin(ang)*s*0.5); by = base-th-s
            if 0 <= bx < w and 0 <= by < h:
                _fill_rect(buf, w, h, bx, by, max(2, tw//4), 1, (30,20,15))
    return buf

def griot_fire(t, w, h, p):
    buf = bytearray(w*h*3)
    for y in range(h):
        f = y/h; r, g, b = _clamp(10+20*f), _clamp(5+15*f), _clamp(15+25*f)
        for x in range(w):
            i = (y*w+x)*3
            buf[i], buf[i+1], buf[i+2] = r, g, b
    fx, fy = w//2, int(h*0.75)
    for y in range(int(h*0.4), h):
        for x in range(max(0,fx-100), min(w,fx+100)):
            d = math.hypot(x-fx, y-fy)/120.0
            fl = 0.5+0.5*math.sin(t*8+x*0.1)*math.sin(t*5+y*0.15)
            if d < 1.0:
                k = (1.0-d)*fl; i = (y*w+x)*3
                buf[i] = _clamp(buf[i]+255*k); buf[i+1] = _clamp(buf[i+1]+180*k*0.7); buf[i+2] = _clamp(buf[i+2]+80*k*0.3)
    return buf

SCENES = {"sunset_landscape":sunset_landscape,"clouds_sunset":clouds_sunset,"map_zoom":map_zoom,
          "artifact_macro":artifact_macro,"monument_3d":monument_3d,"dawn_wide":dawn_wide,
          "baobab_silhouette":baobab_silhouette,"griot_fire":griot_fire}

def render_scene(name, t, w, h, params):
    if name not in SCENES: raise KeyError(f"Scène inconnue : {name}")
    return SCENES[name](t, w, h, params)

# ==============================================================================
# 12. RENDU PARALLÈLE
# ==============================================================================

def _render_worker(args): return render_scene(*args)

def _build_tasks(seq):
    p = dict(seq.get("scene_params", {})); p["duration"] = seq["duration"]
    return [(seq["scene"], i/FPS, RENDER_WIDTH, RENDER_HEIGHT, p)
            for i in range(seq["duration"]*FPS)], seq["duration"]*FPS

def should_parallelize(seq): return PARALLEL_WORKERS > 1 and seq["duration"]*FPS >= PARALLEL_THRESHOLD_FRAMES

def render_inline_seq(seq):
    p = dict(seq.get("scene_params", {})); p["duration"] = seq["duration"]
    for i in range(seq["duration"]*FPS):
        yield render_scene(seq["scene"], i/FPS, RENDER_WIDTH, RENDER_HEIGHT, p)

def render_inline_par(seq):
    tasks, n = _build_tasks(seq)
    with ProcessPoolExecutor(max_workers=max(1, min(PARALLEL_WORKERS, n))) as ex:
        for f in ex.map(_render_worker, tasks, chunksize=4): yield f

def render_frames_to_disk(slug, seq, out_dir):
    n = seq["duration"]*FPS
    paths = [out_dir / f"{slug}_seq{seq['id']:02d}_{i:05d}.ppm" for i in range(n)]
    if all(p.exists() for p in paths): return paths
    if should_parallelize(seq):
        tasks, _ = _build_tasks(seq)
        with ProcessPoolExecutor(max_workers=max(1, min(PARALLEL_WORKERS, n))) as ex:
            for p, b in zip(paths, ex.map(_render_worker, tasks, chunksize=4)):
                _write_ppm(p, b, RENDER_WIDTH, RENDER_HEIGHT)
    else:
        for p, b in zip(paths, render_inline_seq(seq)):
            _write_ppm(p, b, RENDER_WIDTH, RENDER_HEIGHT)
    print(f"    ✓ {n} frames")
    return paths

# ==============================================================================
# 13. TTS
# ==============================================================================

def detect_tts_engine():
    if sys.platform == "darwin" and shutil.which("say"): return "say"
    for e in ("espeak-ng","espeak","pico2wave"):
        if shutil.which(e): return e
    if sys.platform == "win32": return "powershell"
    return None

def synthesize(slug, seq):
    out = AUDIO_DIR / f"{slug}_seq{seq['id']:02d}.wav"
    if out.exists(): return out
    engine = detect_tts_engine()
    if not engine:
        print(f"    ⚠ TTS absent"); return None
    text = seq["voiceover_fr"]
    try:
        if engine == "say":
            aiff = out.with_suffix(".aiff")
            subprocess.run(["say","-v","Thomas","-r",str(TTS_SPEED),"-o",str(aiff),text], check=True, capture_output=True)
            subprocess.run(["afconvert","-f","WAVE","-d","LEI16@44100",str(aiff),str(out)], check=True, capture_output=True)
            aiff.unlink(missing_ok=True)
        elif engine in ("espeak-ng","espeak"):
            subprocess.run([engine,"-v",TTS_LANG,"-s",str(TTS_SPEED),"-w",str(out),text], check=True, capture_output=True)
        elif engine == "pico2wave":
            subprocess.run(["pico2wave","-l",TTS_LANG,"-w",str(out),text], check=True, capture_output=True)
        elif engine == "powershell":
            sc = ("Add-Type -AssemblyName System.Speech; "
                  "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  f"$s.SetOutputToWaveFile('{out}'); "
                  "$s.Speak([Console]::In.ReadToEnd()); $s.Dispose()")
            subprocess.run(["powershell","-Command",sc], input=text.encode("utf-8"), check=True, capture_output=True)
        print(f"    ✓ Voix : {out.name}")
        return out
    except Exception as e:
        print(f"    ⚠ TTS : {e}"); return None

# ==============================================================================
# 14. ENCODAGE VIDÉO
# ==============================================================================

def ffmpeg_available(): return shutil.which("ffmpeg") is not None

def encode_sequence(slug, seq, audio_path, retries=2):
    if not ffmpeg_available(): return None
    out = VIDEO_DIR / f"{slug}_seq{seq['id']:02d}.mp4"
    if out.exists(): return out
    for attempt in range(retries+1):
        mode = "parallèle" if should_parallelize(seq) else "séquentiel"
        print(f"    ⚙ Encodage séq.{seq['id']} ({mode}, essai {attempt+1})…")
        cmd = ["ffmpeg","-y","-f","rawvideo","-pix_fmt","rgb24","-s",f"{RENDER_WIDTH}x{RENDER_HEIGHT}","-r",str(FPS),"-i","pipe:0"]
        if audio_path and Path(audio_path).exists(): cmd += ["-i", str(audio_path)]
        cmd += ["-vf", f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:flags=lanczos,format=yuv420p",
                "-t", str(seq["duration"]), "-c:v", "libx264", "-preset", "medium",
                "-crf", "18", "-b:v", VIDEO_BITRATE, "-r", str(FPS)]
        if audio_path and Path(audio_path).exists(): cmd += ["-c:a","aac","-b:a",AUDIO_BITRATE,"-shortest"]
        cmd += [str(out)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        gen = render_inline_par(seq) if should_parallelize(seq) else render_inline_seq(seq)
        try:
            for f in gen: proc.stdin.write(f)
            proc.stdin.close()
            _, err = proc.communicate()
            if proc.returncode == 0:
                print(f"    ✓ Vidéo : {out.name}"); return out
            print(f"    ⚠ FFmpeg : {err.decode(errors='ignore')[:150]}")
        except Exception as e:
            proc.kill(); print(f"    ⚠ {e}")
        if attempt < retries: time.sleep(1)
    return None

def assemble_final(slug, parts, srt_path=None, embed=False):
    if not parts or not ffmpeg_available(): return None
    out = FINAL_DIR / f"{slug}_final.mp4"
    lf = FINAL_DIR / f"{slug}_list.txt"
    lf.write_text("\n".join(f"file '{p.resolve()}'" for p in parts), encoding="utf-8")
    r = subprocess.run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(lf),"-c","copy",str(out)], capture_output=True)
    lf.unlink(missing_ok=True)
    if r.returncode != 0: return None
    if embed and srt_path and srt_path.exists():
        tmp = out.with_suffix(".tmp.mp4")
        r2 = subprocess.run(["ffmpeg","-y","-i",str(out),"-i",str(srt_path),"-c","copy","-c:s","mov_text",str(tmp)], capture_output=True)
        if r2.returncode == 0: tmp.replace(out)
    print(f"\n🎬 Final : {out}")
    return out

# ==============================================================================
# 15. SOUS-TITRES
# ==============================================================================

def _ts(s):
    h, m, sec = int(s//3600), int((s%3600)//60), int(s%60)
    return f"{h:02d}:{m:02d}:{sec:02d},{int(round((s-int(s))*1000)):03d}"

def _wrap(text, n):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if not cur: cur = w
        elif len(cur)+1+len(w) <= n: cur += " "+w
        else: lines.append(cur); cur = w
    if cur: lines.append(cur)
    return "\n".join(lines)

def build_srt(h):
    entries, cursor = [], 0.0
    for seq in h["sequences"]:
        chunks = re.split(r"(?<=[.!?])\s+", seq["voiceover_fr"].strip()) or [seq["voiceover_fr"]]
        tw = sum(max(1,len(c.split())) for c in chunks)
        for c in chunks:
            d = max(SUBTITLE_MIN_DURATION, seq["duration"]*len(c.split())/tw)
            entries.append((cursor, cursor+d, _wrap(c, SUBTITLE_MAX_CHARS_PER_LINE)))
            cursor += d
    srt = FINAL_DIR / f"{h['slug']}.srt"
    lines = []
    for i, (s, e, t) in enumerate(entries, 1):
        lines += [str(i), f"{_ts(s)} --> {_ts(e)}", t, ""]
    srt.write_text("\n".join(lines), encoding="utf-8")
    print(f"    ✓ SRT : {srt.name}")
    return srt

# ==============================================================================
# 16. MANIFESTE
# ==============================================================================

def load_manifest():
    if MANIFEST_FILE.exists():
        try: return json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
        except Exception: pass
    return {"generations": []}

def save_manifest(m): MANIFEST_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")

def record_generation(slug, status, dur, out=None, err=None):
    m = load_manifest()
    m["generations"].append({"slug": slug, "status": status, "duration_s": round(dur, 2),
                             "output": str(out) if out else None, "error": err,
                             "timestamp": datetime.now().isoformat()})
    save_manifest(m)

# ==============================================================================
# 17. HERITAGES (exemples minimaux, PAS une base de connaissances)
# ==============================================================================

HERITAGES = [
    {"slug":"wanar","title":"Les cercles mégalithiques de Wanar","region":"Kaffrine",
     "summary":"Site UNESCO, parent pauvre du tourisme culturel.",
     "sequences":[
        {"id":1,"role":"accroche","duration":8,"scene":"sunset_landscape","scene_params":{},"source":"UNESCO 2006","voiceover_fr":"À Kaffrine, des pierres dressées veillent sur l'histoire du Sénégal depuis des siècles."},
        {"id":2,"role":"contexte","duration":10,"scene":"map_zoom","scene_params":{},"source":"APS","voiceover_fr":"Classé patrimoine mondial, Wanar reste pourtant le parent pauvre du tourisme culturel."},
        {"id":3,"role":"detail","duration":8,"scene":"artifact_macro","scene_params":{},"source":"APS","voiceover_fr":"Certaines pierres prennent la forme d'une lyre, comme un appel silencieux à écouter le passé."},
        {"id":4,"role":"preuve","duration":10,"scene":"monument_3d","scene_params":{"stones":[[-80,0],[80,0],[0,-80],[0,80],[-60,-60],[60,60]]},"source":"Laporte et al.","voiceover_fr":"Les archéologues y ont retrouvé des sépultures et des parures, preuves d'une civilisation organisée."},
        {"id":5,"role":"chute","duration":8,"scene":"dawn_wide","scene_params":{},"source":"Synthèse","voiceover_fr":"Ce trésor existe. Il ne manque qu'une chose : des regards pour le voir."},
     ]},
]

def load_generated():
    for p in GENERATED_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if all(k in d for k in ("slug","title","sequences")):
                if not any(h["slug"] == d["slug"] for h in HERITAGES): HERITAGES.append(d)
        except Exception: pass

def get_heritage(slug):
    for h in HERITAGES:
        if h["slug"] == slug: return h
    raise KeyError(f"Patrimoine inconnu : {slug}")

def save_heritage(data):
    if DRY_RUN: return
    p = GENERATED_DIR / f"{data['slug']}.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✓ Sauvegardé : {p.relative_to(BASE_DIR)}")
    if not any(h["slug"] == data["slug"] for h in HERITAGES): HERITAGES.append(data)

# ==============================================================================
# 18. ORCHESTRATION
# ==============================================================================

def generate_capsule(slug, use_web=True, use_llm=True):
    t0 = time.time()
    print(f"\n{'='*60}\n🎬  {slug.upper()}\n{'='*60}")
    try: h = get_heritage(slug)
    except KeyError:
        print(f"  ℹ Slug « {slug} » absent des exemples. Tentative de génération…")
        gen = ContentGenerator()
        h = gen.generate(slug, use_web=use_web, use_llm=use_llm)
        if not h:
            print(f"  ✗ Impossible de générer un contenu pour « {slug} »"); return
        save_heritage(h)
    print(f"  TTS     : {detect_tts_engine() or 'absent'}")
    print(f"  FFmpeg  : {'oui' if ffmpeg_available() else 'absent'}")
    print(f"  Dry-run : {DRY_RUN}\n")
    encoded, errors = [], []
    for seq in h["sequences"]:
        print(f"▶ Séq.{seq['id']} [{seq['role']}] {seq['duration']}s — {seq['scene']}")
        if DRY_RUN: continue
        try:
            audio = synthesize(slug, seq)
            if OUTPUT_MODE == "frames" or not ffmpeg_available():
                d = FRAMES_DIR / f"{slug}_seq{seq['id']:02d}"; d.mkdir(exist_ok=True)
                render_frames_to_disk(slug, seq, d)
            else:
                v = encode_sequence(slug, seq, audio)
                if v: encoded.append(v)
                else: errors.append(f"séq.{seq['id']}")
        except Exception as e:
            errors.append(str(e)); print(f"    ⚠ {e}")
            if STRICT: raise
    srt = build_srt(h) if SUBTITLE_ENABLED and not DRY_RUN else None
    out = assemble_final(slug, encoded, srt_path=srt, embed=SUBTITLE_EMBED) if OUTPUT_MODE == "auto" and encoded and not DRY_RUN else None
    dur = time.time()-t0
    status = "ok" if not errors else ("partial" if encoded else "error")
    record_generation(slug, status, dur, out, "; ".join(errors) if errors else None)
    print(f"\n{'✅' if status=='ok' else '⚠'} {dur:.1f}s — {status}\n✅ {h['title']}")

def do_ask(question, use_web=True, use_llm=True):
    print(f"\n❓ {question}\n")
    engine = ResearchEngine(CORPUS)
    docs = engine.search(question, top_k=5)

    if not docs and AUTO_ACQUIRE and (use_web or use_llm):
        print("  ⏳ Corpus local vide sur ce sujet — acquisition…")
        acquire_knowledge(question, use_web=use_web, use_llm=use_llm)
        engine = ResearchEngine(CORPUS)
        docs = engine.search(question, top_k=5)

    if not docs:
        print("  ⚠ Aucune information disponible.")
        print("     → Activez la recherche web (retirer --no-web)")
        print("     → Ou lancez Ollama (ollama serve && ollama pull llama3.2)")
        print(f"     → Ou placez un JSON dans {CORPUS_DIR}/seed/")
        return

    for d in docs:
        tag = " ✓" if d.get("verified") else " ⚠"
        print(f"  •{tag} [{d.get('source','?')}] {d.get('text','')[:300]}")
    info = detect_llm()
    if use_llm and info["available"]:
        excerpts = "\n".join(f"- {d.get('text','')[:400]}" for d in docs)
        r = llm_generate(f"Question : {question}\n\nDocuments :\n{excerpts}\n\nRéponds en 3 phrases en français, en citant les sources.")
        if r: print(f"\n🤖 {r}")

def do_research(subject, use_web=True, use_llm=True):
    print(f"\n🔎 Recherche : « {subject} »\n")
    gen = ContentGenerator()
    data = gen.generate(subject, use_web=use_web, use_llm=use_llm)
    if not data:
        print("  ✗ Échec de la recherche.")
        return
    print(f"\n📋 {data['title']} ({data['region']})\n")
    for s in data["sequences"]:
        print(f"  Séq.{s['id']} [{s['role']}] {s['duration']}s — {s['scene']}")
        print(f"    « {s['voiceover_fr']} »")
        print(f"    — {s.get('source','')}")
    save_heritage(data)
    return data

def do_search(query, category=None, limit=10):
    engine = ResearchEngine(CORPUS)
    res = engine.search(query, top_k=limit, category=category)
    if not res:
        print(f"  ⚠ Corpus vide ou aucun résultat pour « {query} ».")
        if AUTO_ACQUIRE:
            print("  ⏳ Tentative d'acquisition…")
            acquire_knowledge(query)
            engine = ResearchEngine(CORPUS)
            res = engine.search(query, top_k=limit, category=category)
    for r in res:
        tag = " ✓" if r.get("verified") else " ⚠"
        print(f"  {tag} [{r.get('category','auto')}] ({r['score']:.2f}) {r.get('text','')[:120]}…")
        print(f"      — {r.get('source','?')}\n")
    if not res:
        print("  ⚠ Toujours rien. Vérifiez votre connexion réseau ou Ollama.")

def do_chat():
    print("\n💬 Chat — 'quit' pour sortir, 'help' pour l'aide.\n")
    while True:
        try: line = input("vous> ").strip()
        except (EOFError, KeyboardInterrupt): print(); break
        if not line: continue
        if line.lower() in ("quit","exit","q"): break
        if line.lower() == "help":
            print("  ask/research/search/generate/list/stats/quit"); continue
        if line.startswith("ask "): do_ask(line[4:])
        elif line.startswith("research "): do_research(line[9:])
        elif line.startswith("search "): do_search(line[7:])
        elif line.startswith("generate "): load_generated(); generate_capsule(line[9:].strip())
        elif line == "list":
            load_generated()
            for h in HERITAGES: print(f"  • {h['slug']:<22} {h['title']}")
        elif line == "stats": show_stats()
        else: do_ask(line)

def show_stats():
    m = load_manifest(); g = m.get("generations", [])
    eng = ResearchEngine(CORPUS)
    cats = eng.list_categories()
    print(f"\n📊 {len(g)} générations · {len(CORPUS)} fragments · {len(HERITAGES)} patrimoines")
    print(f"   Corpus : {len(CORPUS)} entrées")
    if cats:
        for c, n in sorted(cats.items(), key=lambda x: -x[1]):
            print(f"     {c:<15} {n:>3}")

def do_validate():
    print("\n🔍 Validation…\n")
    load_generated()
    err = 0
    for h in HERITAGES:
        for s in h["sequences"]:
            if s.get("scene") not in SCENES:
                print(f"  ✗ {h['slug']} séq.{s['id']} scène inconnue"); err += 1
    print(f"\n{'✅ OK' if err == 0 else f'⚠ {err} erreurs'}\n")

def do_batch():
    load_generated()
    for h in list(HERITAGES):
        try: generate_capsule(h["slug"])
        except Exception as e: log.error(f"{h['slug']} : {e}")

def do_seed_import(path):
    p = Path(path)
    if not p.exists():
        print(f"  ✗ Fichier introuvable : {path}"); return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        entries = data if isinstance(data, list) else [data]
        added = 0
        for e in entries:
            if not isinstance(e, dict) or "text" not in e: continue
            e.setdefault("id", f"seed_{hashlib.md5(e['text'][:80].encode()).hexdigest()[:10]}")
            e.setdefault("category", "seed")
            e.setdefault("tags", [])
            e.setdefault("source", p.stem)
            e["origin"] = "seed"
            if _add_to_corpus(e): added += 1
        n = _persist_entries([e for e in entries if isinstance(e, dict) and "text" in e])
        print(f"  ✓ {added} entrée(s) importée(s) en mémoire, {n} persistée(s)")
    except Exception as e:
        print(f"  ✗ Erreur import : {e}")

# ==============================================================================
# 19. SERVEUR HTTP (stdlib only)
# ==============================================================================

class StudioServer:
    def __init__(self, host="0.0.0.0", port=8080):
        self.host = host
        self.port = port
        self.tasks = {}
        self.tasks_lock = threading.Lock()
        self.httpd = None

    def _new_task(self):
        tid = str(uuid.uuid4())[:8]
        with self.tasks_lock:
            self.tasks[tid] = {
                "id": tid, "status": "pending",
                "created_at": datetime.now().isoformat(),
                "result": None, "error": None,
            }
        return tid

    def _run_generate_task(self, tid, payload):
        try:
            with self.tasks_lock:
                self.tasks[tid]["status"] = "running"
            slug = payload["subject"]
            use_web = payload.get("use_web", True)
            use_llm = payload.get("use_llm", True)
            gen = ContentGenerator()
            data = gen.generate(slug, use_web=use_web, use_llm=use_llm)
            if not data:
                with self.tasks_lock:
                    self.tasks[tid].update(status="error", error="no_content")
                return
            save_heritage(data)
            generate_capsule(data["slug"])
            final = FINAL_DIR / f"{data['slug']}_final.mp4"
            with self.tasks_lock:
                self.tasks[tid].update(
                    status="done",
                    result={"slug": data["slug"], "title": data["title"],
                            "video": str(final.relative_to(BASE_DIR)) if final.exists() else None}
                )
        except Exception as e:
            log.error(f"Task {tid}: {e}")
            with self.tasks_lock:
                self.tasks[tid].update(status="error", error=str(e))

    def handle_get(self, path, query):
        if path == "/health":
            return 200, {"status": "ok", "corpus_size": len(CORPUS),
                         "llm": detect_llm()["available"],
                         "tts": detect_tts_engine(),
                         "ffmpeg": ffmpeg_available()}
        if path == "/api/stats":
            return 200, {"corpus_size": len(CORPUS),
                         "categories": ResearchEngine(CORPUS).list_categories(),
                         "heritages": [h["slug"] for h in HERITAGES]}
        if path == "/api/categories":
            return 200, ResearchEngine(CORPUS).list_categories()
        if path == "/api/heritages":
            load_generated()
            return 200, [{"slug": h["slug"], "title": h["title"], "region": h.get("region")} for h in HERITAGES]
        if path == "/api/search":
            q = query.get("q", [""])[0]
            limit = int(query.get("limit", ["10"])[0])
            cat = query.get("category", [None])[0]
            res = ResearchEngine(CORPUS).search(q, top_k=limit, category=cat)
            return 200, {"query": q, "results": res}
        if path.startswith("/api/task/"):
            tid = path.rsplit("/", 1)[-1]
            with self.tasks_lock:
                t = self.tasks.get(tid)
            if not t: return 404, {"error": "task_not_found"}
            return 200, t
        if path == "/api/videos":
            vids = []
            for p in FINAL_DIR.glob("*.mp4"):
                vids.append({"name": p.name, "size": p.stat().st_size})
            return 200, vids
        return 404, {"error": "not_found"}

    def handle_post(self, path, payload):
        if path == "/api/ask":
            q = payload.get("question", "").strip()
            if not q: return 400, {"error": "missing_question"}
            engine = ResearchEngine(CORPUS)
            docs = engine.search(q, top_k=5)
            if not docs and AUTO_ACQUIRE:
                acquire_knowledge(q, use_web=payload.get("use_web", True),
                                  use_llm=payload.get("use_llm", True))
                engine = ResearchEngine(CORPUS)
                docs = engine.search(q, top_k=5)
            return 200, {"question": q, "results": docs}
        if path == "/api/research":
            subject = payload.get("subject", "").strip()
            if not subject: return 400, {"error": "missing_subject"}
            gen = ContentGenerator()
            data = gen.generate(subject,
                                use_web=payload.get("use_web", True),
                                use_llm=payload.get("use_llm", True))
            if not data: return 500, {"error": "generation_failed"}
            save_heritage(data)
            return 200, data
        if path == "/api/generate":
            subject = payload.get("subject", "").strip()
            if not subject: return 400, {"error": "missing_subject"}
            tid = self._new_task()
            threading.Thread(target=self._run_generate_task, args=(tid, payload), daemon=True).start()
            return 202, {"task_id": tid, "status_url": f"/api/task/{tid}"}
        return 404, {"error": "not_found"}

    def start(self):
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args): pass
            def _dispatch(self):
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    if self.command == "GET":
                        status, body = server.handle_get(path, query)
                    elif self.command == "POST":
                        length = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(length) if length else b""
                        payload = json.loads(raw) if raw else {}
                        status, body = server.handle_post(path, payload)
                    else:
                        status, body = 405, {"error": "method_not_allowed"}
                except Exception as e:
                    log.error(f"API: {e}")
                    status, body = 500, {"error": str(e)}
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                try:
                    self.wfile.write(json.dumps(body, ensure_ascii=False).encode("utf-8"))
                except Exception:
                    pass
            def do_GET(self): self._dispatch()
            def do_POST(self): self._dispatch()

        self.httpd = http.server.ThreadingHTTPServer((self.host, self.port), Handler)
        print(f"\n🌐 Serveur HTTP actif sur http://{self.host}:{self.port}")
        print(f"   Endpoints : /health /api/stats /api/search /api/ask /api/research /api/generate")
        def shutdown(signum, frame):
            print("\n🛑 Arrêt demandé…")
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        try:
            self.httpd.serve_forever()
        finally:
            print("✅ Serveur arrêté.")

# ==============================================================================
# 20. CLI
# ==============================================================================

def main():
    global PARALLEL_WORKERS, OUTPUT_MODE, SUBTITLE_ENABLED, SUBTITLE_EMBED, DRY_RUN, STRICT
    p = argparse.ArgumentParser(description="Sénégal Heritage Studio v5")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--strict", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    for c in ("list","chat","stats","validate","batch","categories"): sub.add_parser(c)

    ask = sub.add_parser("ask"); ask.add_argument("question", nargs="+")
    ask.add_argument("--no-web", action="store_true"); ask.add_argument("--no-llm", action="store_true")
    rsch = sub.add_parser("research"); rsch.add_argument("subject", nargs="+")
    rsch.add_argument("--no-web", action="store_true"); rsch.add_argument("--no-llm", action="store_true")
    srch = sub.add_parser("search"); srch.add_argument("query", nargs="+")
    srch.add_argument("-c","--category", default=None); srch.add_argument("-n","--limit", type=int, default=10)
    gen = sub.add_parser("generate"); gen.add_argument("slug")
    gen.add_argument("--workers", type=int); gen.add_argument("--mode", choices=("auto","video","frames"))
    gen.add_argument("--no-subs", action="store_true"); gen.add_argument("--embed-subs", action="store_true")
    gen.add_argument("--no-web", action="store_true"); gen.add_argument("--no-llm", action="store_true")
    si = sub.add_parser("seed-import"); si.add_argument("file")
    srv = sub.add_parser("serve")
    srv.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    srv.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))

    args = p.parse_args()
    DRY_RUN = args.dry_run; STRICT = args.strict

    # Chargement initial du corpus
    _load_corpus_from_disk()

    if not args.cmd: p.print_help(); return
    if args.cmd == "list":
        load_generated()
        for h in HERITAGES: print(f"  • {h['slug']:<22} {h['title']}")
    elif args.cmd == "chat": do_chat()
    elif args.cmd == "stats": show_stats()
    elif args.cmd == "validate": do_validate()
    elif args.cmd == "batch": do_batch()
    elif args.cmd == "categories":
        eng = ResearchEngine(CORPUS)
        cats = eng.list_categories()
        if not cats:
            print(f"\n📚 Corpus vide. {CORPUS_DIR} est prêt à recevoir des JSON.")
            print("   Utilisez 'research <sujet>' pour un premier remplissage automatique.")
        else:
            print(f"\n📚 {len(CORPUS)} entrées :\n")
            for c, n in sorted(cats.items(), key=lambda x: -x[1]):
                print(f"  {c:<15} {n:>3}")
    elif args.cmd == "ask":
        do_ask(" ".join(args.question),
               use_web=not args.no_web, use_llm=not args.no_llm)
    elif args.cmd == "research":
        do_research(" ".join(args.subject),
                    use_web=not args.no_web, use_llm=not args.no_llm)
    elif args.cmd == "search":
        do_search(" ".join(args.query), category=args.category, limit=args.limit)
    elif args.cmd == "generate":
        load_generated()
        if args.workers: PARALLEL_WORKERS = max(1, args.workers)
        if args.mode: OUTPUT_MODE = args.mode
        if args.no_subs: SUBTITLE_ENABLED = False
        if args.embed_subs: SUBTITLE_EMBED = True
        generate_capsule(args.slug,
                         use_web=not args.no_web, use_llm=not args.no_llm)
    elif args.cmd == "seed-import":
        do_seed_import(args.file)
    elif args.cmd == "serve":
        srv_instance = StudioServer(host=args.host, port=args.port)
        srv_instance.start()

if __name__ == "__main__":
    main()