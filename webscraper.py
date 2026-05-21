"""
╔══════════════════════════════════════════════════════════════╗
║         WebScraper — Apollo-style Lead Scraper               ║
║         Gemini Flash 2.0 AI  OR  Pattern-based (No AI)       ║
╚══════════════════════════════════════════════════════════════╝

INSTALL DEPENDENCIES:
  pip install aiohttp beautifulsoup4 google-generativeai spacy rapidfuzz playwright requests
  python -m spacy download en_core_web_sm
  playwright install chromium

RUN:
  python webscraper.py
"""

import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
import asyncio, aiohttp
from bs4 import BeautifulSoup
import json, csv, re, os, threading
from datetime import datetime
from pathlib import Path
from collections import Counter
import random

# ── Optional imports ─────────────────────────────────────────────────────────
try:
    import google.generativeai as genai;  GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

try:
    import spacy;  SPACY_AVAILABLE = True
except ImportError:
    SPACY_AVAILABLE = False

try:
    from rapidfuzz import fuzz;  RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False

# ── Constants ─────────────────────────────────────────────────────────────────
DB_FILE     = "leads_database.json"
ALL_FIELDS  = ["name","email","company","job_title","cellphone","telephone","linkedin","address"]
EXPORT_COLS = ["record_id"] + ALL_FIELDS + ["source_url","scraped_at"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/119.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/118.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0",
]

EXTRACT_PROMPT = """
You are a lead extraction AI. Extract ALL business contacts from the content below.
Return ONLY a valid JSON array. No markdown, no backticks, no explanation. Raw JSON only.

Each contact must have EXACTLY these fields (null if not found):
name, email, company, job_title, cellphone, telephone, linkedin, address

Format: [{{"name":...,"email":...,"company":...,"job_title":...,"cellphone":...,"telephone":...,"linkedin":...,"address":...}}]
If no contacts found return: []

Content:
{content}
"""


# ╔══════════════════════════════╗
# ║  DATABASE                   ║
# ╚══════════════════════════════╝
class LeadDatabase:
    def __init__(self):
        self.data = self._load()

    def _load(self):
        if os.path.exists(DB_FILE):
            try:
                with open(DB_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"leads": [], "scraped_urls": []}

    def save(self):
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def _next_id(self):
        existing = [r.get("record_id", 0) for r in self.data["leads"] if isinstance(r.get("record_id"), int)]
        return (max(existing) + 1) if existing else 1

    def add_leads(self, leads):
        next_id = self._next_id()
        for i, lead in enumerate(leads):
            if not lead.get("record_id"):
                lead["record_id"] = next_id + i
        self.data["leads"].extend(leads)
        self.save()
    def add_url(self, url):
        if url not in self.data["scraped_urls"]: self.data["scraped_urls"].append(url)
    def is_scraped(self, url):       return url in self.data["scraped_urls"]
    def all_leads(self):             return self.data["leads"]
    def clear(self):
        self.data = {"leads": [], "scraped_urls": []}
        self.save()


# ╔══════════════════════════════╗
# ║  ASYNC SCRAPER              ║
# ╚══════════════════════════════╝
class AsyncScraper:
    def __init__(self, log_cb, delay=1.5):
        self.log = log_cb
        self.delay = delay

    def _hdrs(self):
        return {"User-Agent": random.choice(USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5", "Connection": "keep-alive"}

    async def _fetch(self, session, url):
        try:
            await asyncio.sleep(random.uniform(0.4, self.delay))
            async with session.get(url, headers=self._hdrs(),
                                   timeout=aiohttp.ClientTimeout(total=15), ssl=False) as r:
                if r.status == 200:
                    html = await r.text(errors="ignore")
                    self.log(f"✅ Fetched: {url}", "success")
                    return url, html
                self.log(f"⚠️ HTTP {r.status}: {url}", "warning")
                return url, None
        except Exception as e:
            self.log(f"❌ {url[:50]}: {str(e)[:50]}", "error")
            return url, None

    async def scrape_all(self, urls, prog_cb=None):
        results, done = {}, 0
        conn = aiohttp.TCPConnector(limit=10, ssl=False)
        async with aiohttp.ClientSession(connector=conn) as s:
            tasks = [self._fetch(s, u) for u in urls]
            for coro in asyncio.as_completed(tasks):
                url, html = await coro
                results[url] = html
                done += 1
                if prog_cb: prog_cb(done / len(tasks) * 45)
        return results

    def clean(self, html):
        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script","style","nav","footer","header","meta","link","noscript"]):
            t.decompose()
        return " ".join(soup.get_text(" ", strip=True).split())[:8000]

    def raw_soup(self, html):
        return BeautifulSoup(html, "html.parser")


# ╔══════════════════════════════╗
# ║  PATTERN EXTRACTOR (No AI)  ║
# ╚══════════════════════════════╝
class PatternExtractor:
    EMAIL_RE    = re.compile(r'[\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,}')
    PHONE_RE    = re.compile(r'(?<!\w)[\+\(]?[\d][\d\s\-\(\)\.]{6,18}[\d](?!\w)')
    LINKEDIN_RE = re.compile(r'https?://(?:www\.)?linkedin\.com/in/[\w%\-]+/?')
    CELL_KW     = re.compile(r'mobile|cell|cellular|whatsapp|viber', re.I)
    TEL_KW      = re.compile(r'(?:^|\b)(?:phone|tel(?:ephone)?|office|direct|landline)(?:\b|:)', re.I)

    def extract(self, html, url=""):
        soup = BeautifulSoup(html, "html.parser")
        records = []

        records += self._jsonld(soup)
        records += self._hcard(soup)
        records += self._contact_blocks(soup)
        records += self._tables(soup)

        if not records:
            records += self._broad(soup)

        seen, unique = set(), []
        for r in records:
            em = (r.get("email") or "").lower()
            if em and em in seen: continue
            if em: seen.add(em)
            unique.append(r)
        return unique

    def _empty(self):
        return {f: None for f in ALL_FIELDS}

    def _jsonld(self, soup):
        out = []
        for sc in soup.find_all("script", type="application/ld+json"):
            try:
                raw = json.loads(sc.string or "")
                items = raw if isinstance(raw, list) else [raw]
                for item in items:
                    t = item.get("@type","")
                    if t in ("Person","Employee","ContactPoint","LocalBusiness"):
                        r = self._parse_person(item)
                        if any(r.values()): out.append(r)
                    elif t == "Organization":
                        for emp in item.get("employee",[]):
                            r = self._parse_person(emp)
                            r["company"] = r["company"] or item.get("name")
                            if any(r.values()): out.append(r)
                    elif isinstance(item.get("@graph"), list):
                        for node in item["@graph"]:
                            if node.get("@type","") in ("Person","Employee"):
                                r = self._parse_person(node)
                                if any(r.values()): out.append(r)
            except Exception:
                pass
        return out

    def _parse_person(self, item):
        r = self._empty()
        r["name"]      = item.get("name")
        r["job_title"] = item.get("jobTitle")
        raw_email      = item.get("email","")
        r["email"]     = raw_email.replace("mailto:","").strip() if raw_email else None
        tel            = item.get("telephone","")
        if tel: r["telephone"] = tel
        wf = item.get("worksFor",{})
        r["company"]   = wf.get("name") if isinstance(wf, dict) else (wf or None)
        addr = item.get("address",{})
        if isinstance(addr, dict):
            parts = [addr.get("streetAddress"), addr.get("addressLocality"),
                     addr.get("addressRegion"),  addr.get("addressCountry")]
            r["address"] = ", ".join(p for p in parts if p) or None
        elif addr: r["address"] = addr
        sameAs = item.get("sameAs",[])
        if isinstance(sameAs, str): sameAs = [sameAs]
        for s in sameAs:
            if "linkedin.com" in s: r["linkedin"] = s
        return r

    def _hcard(self, soup):
        out = []
        for vc in soup.find_all(class_=re.compile(r'\bvcard\b|\bhcard\b', re.I)):
            r = self._empty()
            fn = vc.find(class_=re.compile(r'\bfn\b|\bfullname\b|\bname\b', re.I))
            if fn: r["name"] = fn.get_text(strip=True)
            org = vc.find(class_=re.compile(r'\borg\b|\bcompany\b', re.I))
            if org: r["company"] = org.get_text(strip=True)
            ttl = vc.find(class_=re.compile(r'\btitle\b|\brole\b', re.I))
            if ttl: r["job_title"] = ttl.get_text(strip=True)
            ea  = vc.find("a", href=re.compile(r'^mailto:', re.I))
            if ea: r["email"] = ea["href"].replace("mailto:","").strip()
            tel = vc.find(class_=re.compile(r'\btel\b|\bphone\b', re.I))
            if tel: r["telephone"] = tel.get_text(strip=True)
            adr = vc.find(class_=re.compile(r'\badr\b|\baddress\b', re.I))
            if adr: r["address"] = adr.get_text(strip=True)
            for a in vc.find_all("a", href=True):
                if "linkedin.com/in/" in a["href"]: r["linkedin"] = a["href"]
            if any(r.values()): out.append(r)
        return out

    def _contact_blocks(self, soup):
        out = []
        blocks = soup.find_all(
            ["div","article","li","section"],
            class_=re.compile(r'team|member|staff|person|contact|employee|bio|card|profile', re.I)
        )
        for block in blocks[:80]:
            r = self._from_block(block)
            if r and sum(1 for v in r.values() if v) >= 1:
                out.append(r)
        return out

    def _from_block(self, block):
        r = self._empty()
        text = block.get_text(" ", strip=True)

        em = self.EMAIL_RE.findall(text)
        if em: r["email"] = em[0]

        for a in block.find_all("a", href=True):
            if "linkedin.com/in/" in a["href"]:
                r["linkedin"] = a["href"]; break

        labeled = False
        for el in block.find_all(True):
            cls = " ".join(el.get("class",[]))
            lbl = el.get_text(" ", strip=True)
            phones = self.PHONE_RE.findall(lbl)
            if not phones: continue
            if self.CELL_KW.search(cls + lbl):
                r["cellphone"] = phones[0]; labeled = True
            elif self.TEL_KW.search(cls + lbl):
                r["telephone"] = phones[0]; labeled = True
        if not labeled:
            phones = self.PHONE_RE.findall(text)
            if phones: r["telephone"] = phones[0]
            if len(phones) > 1: r["cellphone"] = phones[1]

        for cls in ['name','fullname','person-name','member-name','staff-name','employee-name']:
            el = block.find(class_=re.compile(rf'\b{cls}\b', re.I))
            if el: r["name"] = el.get_text(strip=True); break
        if not r["name"]:
            for tag in ["h1","h2","h3","h4","strong"]:
                el = block.find(tag)
                if el:
                    t = el.get_text(strip=True)
                    if 1 < len(t.split()) <= 5 and not self.EMAIL_RE.search(t):
                        r["name"] = t; break

        for cls in ['title','job-title','position','role','designation','subtitle','dept']:
            el = block.find(class_=re.compile(rf'\b{cls}\b', re.I))
            if el: r["job_title"] = el.get_text(strip=True); break

        for cls in ['company','organization','employer','org']:
            el = block.find(class_=re.compile(rf'\b{cls}\b', re.I))
            if el: r["company"] = el.get_text(strip=True); break

        for cls in ['address','location','addr']:
            el = block.find(class_=re.compile(rf'\b{cls}\b', re.I))
            if el: r["address"] = el.get_text(" ", strip=True); break

        return r

    def _tables(self, soup):
        out = []
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not any(k in " ".join(headers) for k in ["name","email","phone","contact"]): continue
            col = {h: i for i, h in enumerate(headers)}
            for row in table.find_all("tr")[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all(["td","th"])]
                if len(cells) < 2: continue
                r = self._empty()
                for key, aliases in [
                    ("name",      ["name","full name"]),
                    ("email",     ["email","e-mail","mail"]),
                    ("company",   ["company","organization","employer"]),
                    ("job_title", ["title","position","role","job"]),
                    ("telephone", ["phone","telephone","tel","contact"]),
                ]:
                    for alias in aliases:
                        if alias in col and col[alias] < len(cells):
                            val = cells[col[alias]].strip()
                            if val: r[key] = val; break
                if any(r.values()): out.append(r)
        return out

    def _broad(self, soup):
        out = []
        text = soup.get_text(" ")
        company = None
        meta = soup.find("meta", property="og:site_name")
        if meta: company = meta.get("content")
        if not company:
            t = soup.find("title")
            if t: company = t.get_text(strip=True).split("|")[0].split("–")[0].strip()

        for em in self.EMAIL_RE.findall(text)[:100]:
            r = self._empty()
            r["email"]   = em
            r["company"] = company
            idx = text.find(em)
            ctx = text[max(0,idx-200):idx+200]
            phs = self.PHONE_RE.findall(ctx)
            if phs: r["telephone"] = phs[0]
            out.append(r)
        return out


# ╔══════════════════════════════╗
# ║  AI EXTRACTOR (Gemini only) ║
# ╚══════════════════════════════╝
class AIExtractor:
    def __init__(self, api_key, log_cb):
        self.log = log_cb
        self.model = None
        if api_key and GEMINI_AVAILABLE:
            try:
                genai.configure(api_key=api_key)
                self.model = genai.GenerativeModel("gemini-2.0-flash")
                self.log("✅ Gemini Flash 2.0 initialized", "success")
            except Exception as e:
                self.log(f"⚠️ Gemini init error: {e}", "warning")
        elif api_key:
            self.log("⚠️ google-generativeai not installed", "warning")

    def _parse(self, raw):
        text = re.sub(r"```json|```","", raw or "").strip()
        try:
            d = json.loads(text)
            return d if isinstance(d, list) else []
        except Exception:
            m = re.search(r'\[.*\]', text, re.DOTALL)
            if m:
                try: return json.loads(m.group())
                except Exception: pass
        return []

    def extract(self, content, url=""):
        if not self.model:
            self.log("❌ Gemini not available. Check API key in Settings.", "error")
            return []
        try:
            resp = self.model.generate_content(EXTRACT_PROMPT.format(content=content))
            result = self._parse(resp.text)
            self.log(f"🤖 Gemini → {len(result)} records | {url[:45]}", "success")
            return result
        except Exception as e:
            self.log(f"⚠️ Gemini error: {str(e)[:70]}", "warning")
            return []


# ╔══════════════════════════════╗
# ║  spaCy NER                  ║
# ╚══════════════════════════════╝
class NERProcessor:
    def __init__(self, log_cb):
        self.log = log_cb
        self.nlp = None
        if SPACY_AVAILABLE:
            try:
                self.nlp = spacy.load("en_core_web_sm")
                self.log("✅ spaCy NER loaded", "success")
            except OSError:
                self.log("⚠️ spaCy model missing. Run: python -m spacy download en_core_web_sm", "warning")

    def enhance(self, record, text=""):
        if not self.nlp or not text: return record
        doc = self.nlp(text[:600])
        for ent in doc.ents:
            if ent.label_ == "PERSON"  and not record.get("name"):    record["name"]    = ent.text
            if ent.label_ == "ORG"     and not record.get("company"): record["company"] = ent.text
        return record

    def enhance_batch(self, records, text=""):
        return [self.enhance(r, text) for r in records]


# ╔══════════════════════════════╗
# ║  DEDUPLICATOR               ║
# ╚══════════════════════════════╝
class Deduplicator:
    def __init__(self, threshold=85):
        self.thr = threshold

    def run(self, records):
        if not RAPIDFUZZ_AVAILABLE:
            seen, uniq = set(), []
            for r in records:
                k = (r.get("email") or "").lower()
                if k and k in seen: continue
                if k: seen.add(k)
                uniq.append(r)
            return uniq, len(records) - len(uniq)

        uniq, removed = [], 0
        for rec in records:
            name  = (rec.get("name")  or "").lower().strip()
            email = (rec.get("email") or "").lower().strip()
            dup = False
            for ex in uniq:
                ex_e = (ex.get("email") or "").lower().strip()
                ex_n = (ex.get("name")  or "").lower().strip()
                if email and ex_e and email == ex_e: dup = True; break
                if name and ex_n and fuzz.ratio(name, ex_n) >= self.thr:
                    same = (rec.get("company") or "").lower() == (ex.get("company") or "").lower()
                    if same: dup = True; break
            if dup: removed += 1
            else:   uniq.append(rec)
        return uniq, removed


# ╔══════════════════════════════╗
# ║  FILTER & NULL HANDLER      ║
# ╚══════════════════════════════╝
class LeadFilter:
    @staticmethod
    def apply_filters(records, active):
        if not active: return records
        return [r for r in records if all(r.get(f) for f in active)]

    @staticmethod
    def handle_nulls(records, mode, placeholder="N/A"):
        if mode == "keep":
            return records, 0
        if mode == "drop":
            kept    = [r for r in records if all(r.get(f) for f in ALL_FIELDS)]
            removed = len(records) - len(kept)
            return kept, removed
        if mode == "placeholder":
            out = []
            for r in records:
                nr = dict(r)
                for f in ALL_FIELDS:
                    if not nr.get(f): nr[f] = placeholder
                out.append(nr)
            return out, 0
        if mode == "mode_fill":
            modes = {}
            for f in ALL_FIELDS:
                vals = [r[f] for r in records if r.get(f)]
                if vals:
                    modes[f] = Counter(vals).most_common(1)[0][0]
            out = []
            for r in records:
                nr = dict(r)
                for f in ALL_FIELDS:
                    if not nr.get(f): nr[f] = modes.get(f, "N/A")
                out.append(nr)
            return out, 0
        return records, 0


# ╔══════════════════════════════╗
# ║  CSV EXPORTER               ║
# ╚══════════════════════════════╝
class CSVExporter:
    @staticmethod
    def export(records, filepath):
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=EXPORT_COLS, extrasaction="ignore")
            w.writeheader()
            w.writerows(records)
        return len(records)


# ╔══════════════════════════════════════════════════════════════╗
# ║  GUI                                                         ║
# ╚══════════════════════════════════════════════════════════════╝
class WebScraperApp:
    def __init__(self, root):
        self.root = root
        self.root.title("WebScraper")
        self.root.geometry("1060x830")
        self.root.configure(bg="#1e1e2e")
        self.root.resizable(True, True)

        self.db            = LeadDatabase()
        self.is_running    = False
        self.current_leads = []
        self._url_file     = None
        self._gemini_key   = ""
        self._out_folder   = str(Path.home() / "Downloads")

        self._styles()
        self._build()
        self.log("🚀 WebScraper ready. Configure settings and start scraping.", "info")
        self.log(f"💾 Database: {len(self.db.all_leads()):,} existing leads loaded.", "info")

    # ── Style ─────────────────────────────────────────────────────────────────
    def _styles(self):
        s = ttk.Style(); s.theme_use("clam")
        bg = "#1e1e2e"
        s.configure("TFrame",        background=bg)
        s.configure("TLabel",        background=bg, foreground="#cdd6f4", font=("Segoe UI",10))
        s.configure("TLabelframe",   background=bg, foreground="#89b4fa")
        s.configure("TLabelframe.Label", background=bg, foreground="#89b4fa", font=("Segoe UI",10,"bold"))
        s.configure("TCheckbutton",  background=bg, foreground="#cdd6f4", font=("Segoe UI",9))
        s.configure("TRadiobutton",  background=bg, foreground="#cdd6f4", font=("Segoe UI",9))
        s.configure("TNotebook",     background=bg, borderwidth=0)
        s.configure("TNotebook.Tab", background="#313244", foreground="#cdd6f4",
                    padding=[14,5], font=("Segoe UI",9))
        s.map("TNotebook.Tab",
              background=[("selected","#89b4fa")],
              foreground=[("selected","#1e1e2e")])
        s.configure("TProgressbar", troughcolor="#313244", background="#89b4fa", thickness=8)

    def _btn(self, parent, text, cmd, bg="#89b4fa", fg="#1e1e2e", **kw):
        def lgt(c):
            h=c.lstrip("#"); r,g,b=int(h[:2],16),int(h[2:4],16),int(h[4:],16)
            return f"#{min(255,r+28):02x}{min(255,g+28):02x}{min(255,b+28):02x}"
        b = tk.Button(parent,text=text,command=cmd,bg=bg,fg=fg,relief="flat",
                      cursor="hand2",font=("Segoe UI",9,"bold"),padx=12,pady=5,**kw)
        b.bind("<Enter>", lambda e: b.config(bg=lgt(bg)))
        b.bind("<Leave>", lambda e: b.config(bg=bg))
        return b

    # ── Build ─────────────────────────────────────────────────────────────────
    def _build(self):
        hdr = tk.Frame(self.root, bg="#181825", pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="⚡ WebScraper", bg="#181825", fg="#89b4fa",
                 font=("Segoe UI",16,"bold")).pack(side="left", padx=16)
        tk.Label(hdr, text="AI mode (Gemini Flash 2.0)  •  Pattern mode (No AI)  •  Smart dedup  •  Full export control",
                 bg="#181825", fg="#6c7086", font=("Segoe UI",9)).pack(side="left")

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=8)
        self._tab_scraper(nb)
        self._tab_history(nb)
        self._tab_settings(nb)

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 1 — SCRAPER
    # ─────────────────────────────────────────────────────────────────────────
    def _tab_scraper(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="  🔍 Scraper  ")

        # ── Left panel with scrollable canvas ────────────────────────────────
        left_outer = tk.Frame(tab, bg="#1e1e2e", width=320)
        left_outer.pack(side="left", fill="y", padx=(6,3), pady=6)
        left_outer.pack_propagate(False)

        # Canvas + scrollbar for left panel
        left_canvas = tk.Canvas(left_outer, bg="#1e1e2e", highlightthickness=0)
        left_scroll = ttk.Scrollbar(left_outer, orient="vertical", command=left_canvas.yview)
        left_canvas.configure(yscrollcommand=left_scroll.set)
        left_scroll.pack(side="right", fill="y")
        left_canvas.pack(side="left", fill="both", expand=True)

        # Inner frame that holds all controls
        left = tk.Frame(left_canvas, bg="#1e1e2e")
        left_window = left_canvas.create_window((0, 0), window=left, anchor="nw")

        def _on_frame_configure(event):
            left_canvas.configure(scrollregion=left_canvas.bbox("all"))

        def _on_canvas_configure(event):
            left_canvas.itemconfig(left_window, width=event.width)

        left.bind("<Configure>", _on_frame_configure)
        left_canvas.bind("<Configure>", _on_canvas_configure)

        # Mouse-wheel scroll
        def _on_mousewheel(event):
            left_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        left_canvas.bind("<MouseWheel>", _on_mousewheel)
        left.bind("<MouseWheel>", _on_mousewheel)

        def _bind_mousewheel(widget):
            widget.bind("<MouseWheel>", _on_mousewheel)
            for child in widget.winfo_children():
                _bind_mousewheel(child)

        left.bind("<Map>", lambda e: _bind_mousewheel(left))

        # ── Right panel ───────────────────────────────────────────────────────
        right = tk.Frame(tab, bg="#1e1e2e")
        right.pack(side="left", fill="both", expand=True, padx=(3,6), pady=6)

        # ── Extraction Mode ──────────────────────────────────────────────────
        mf = ttk.LabelFrame(left, text="🧠 Extraction Mode", padding=8)
        mf.pack(fill="x", pady=(0,6), padx=4)
        self.mode_var = tk.StringVar(value="pattern")
        ttk.Radiobutton(mf, text="🤖 AI Mode — Gemini Flash 2.0 (higher accuracy)",
                        variable=self.mode_var, value="ai").pack(anchor="w", pady=2)
        ttk.Radiobutton(mf, text="🔍 Pattern Mode — No AI (fast, free, offline-capable)",
                        variable=self.mode_var, value="pattern").pack(anchor="w", pady=2)

        # ── URL Input ────────────────────────────────────────────────────────
        uf = ttk.LabelFrame(left, text="📌 URL Input", padding=8)
        uf.pack(fill="x", pady=(0,6), padx=4)
        tk.Label(uf, text="Single URL:", bg="#1e1e2e", fg="#a6adc8", font=("Segoe UI",9)).pack(anchor="w")
        self.url_ent = tk.Entry(uf, bg="#313244", fg="#cdd6f4", relief="flat",
                                 insertbackground="#cdd6f4", font=("Segoe UI",9))
        self.url_ent.pack(fill="x", pady=(2,6), ipady=4)
        tk.Label(uf, text="Or .txt file (one URL per line):", bg="#1e1e2e",
                 fg="#a6adc8", font=("Segoe UI",9)).pack(anchor="w")
        fr = tk.Frame(uf, bg="#1e1e2e"); fr.pack(fill="x", pady=(2,4))
        self.file_lbl = tk.Label(fr, text="No file selected", bg="#1e1e2e",
                                  fg="#6c7086", font=("Segoe UI",8))
        self.file_lbl.pack(side="left", fill="x", expand=True)
        self._btn(fr, "Browse", self._browse_url, "#313244","#cdd6f4").pack(side="right")
        self.skip_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(uf, text="Skip already-scraped URLs",
                        variable=self.skip_var).pack(anchor="w", pady=(4,0))

        # ── Field Filters ────────────────────────────────────────────────────
        # Fields match EXPORT_COLS exactly
        ff = ttk.LabelFrame(left, text="🔽 Field Filters", padding=8)
        ff.pack(fill="x", pady=(0,6), padx=4)
        tk.Label(ff, text="Only export records that have:", bg="#1e1e2e",
                 fg="#a6adc8", font=("Segoe UI",8)).pack(anchor="w", pady=(0,3))
        self.fvars = {}
        filter_fields = [
            ("record_id",  "🔢 Record ID"),
            ("name",       "👤 Name"),
            ("email",      "📧 Email"),
            ("company",    "🏢 Company"),
            ("job_title",  "💼 Job Title"),
            ("cellphone",  "📱 Cellphone"),
            ("telephone",  "☎️  Telephone"),
            ("linkedin",   "🔗 LinkedIn URL"),
            ("address",    "📍 Address"),
            ("source_url", "🌐 Source URL"),
            ("scraped_at", "🕒 Scraped At"),
        ]
        for key, label in filter_fields:
            v = tk.BooleanVar(value=False)
            self.fvars[key] = v
            ttk.Checkbutton(ff, text=label, variable=v).pack(anchor="w", pady=1)

        # ── Null Field Handling ───────────────────────────────────────────────
        nf = ttk.LabelFrame(left, text="⚙️ Null Field Handling", padding=8)
        nf.pack(fill="x", pady=(0,6), padx=4)
        tk.Label(nf, text="When a field is empty/null:", bg="#1e1e2e",
                 fg="#a6adc8", font=("Segoe UI",8)).pack(anchor="w", pady=(0,3))
        self.null_var = tk.StringVar(value="keep")
        opts = [
            ("keep",        "Keep all records (nulls OK)"),
            ("drop",        "Drop records with any null field"),
            ("placeholder", "Fill nulls with placeholder text"),
            ("mode_fill",   "Fill nulls with field's most common value"),
        ]
        for val, txt in opts:
            ttk.Radiobutton(nf, text=txt, variable=self.null_var, value=val,
                            command=self._toggle_placeholder).pack(anchor="w", pady=1)
        ph_row = tk.Frame(nf, bg="#1e1e2e")
        ph_row.pack(fill="x", pady=(4,0))
        self.ph_lbl = tk.Label(ph_row, text="Placeholder text:", bg="#1e1e2e",
                                fg="#a6adc8", font=("Segoe UI",8))
        self.ph_lbl.pack(side="left")
        self.ph_ent = tk.Entry(ph_row, width=10, bg="#313244", fg="#cdd6f4",
                                relief="flat", font=("Segoe UI",9),
                                insertbackground="#cdd6f4")
        self.ph_ent.insert(0, "N/A")
        self.ph_ent.pack(side="left", padx=(4,0), ipady=3)
        self._toggle_placeholder()

        # ── CSV Output ────────────────────────────────────────────────────────
        cf = ttk.LabelFrame(left, text="📁 CSV Output", padding=8)
        cf.pack(fill="x", pady=(0,6), padx=4)

        tk.Label(cf, text="Save folder:", bg="#1e1e2e", fg="#a6adc8", font=("Segoe UI",8)).pack(anchor="w")
        frow = tk.Frame(cf, bg="#1e1e2e"); frow.pack(fill="x", pady=(2,6))
        self.folder_lbl = tk.Label(frow, text=self._shorten(self._out_folder),
                                    bg="#1e1e2e", fg="#a6e3a1", font=("Segoe UI",8))
        self.folder_lbl.pack(side="left", fill="x", expand=True)
        self._btn(frow, "📂", self._browse_folder, "#313244","#cdd6f4").pack(side="right")

        tk.Label(cf, text="Filename:", bg="#1e1e2e", fg="#a6adc8", font=("Segoe UI",8)).pack(anchor="w")
        nrow = tk.Frame(cf, bg="#1e1e2e"); nrow.pack(fill="x", pady=(2,4))
        self.fname_ent = tk.Entry(nrow, bg="#313244", fg="#cdd6f4", relief="flat",
                                   font=("Segoe UI",9), insertbackground="#cdd6f4")
        self.fname_ent.insert(0, "leads")
        self.fname_ent.pack(side="left", fill="x", expand=True, ipady=3)
        tk.Label(nrow, text=".csv", bg="#1e1e2e", fg="#a6adc8",
                 font=("Segoe UI",9)).pack(side="left", padx=(4,0))
        self.ts_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(cf, text="Append timestamp to filename",
                        variable=self.ts_var).pack(anchor="w")

        # ── Run Controls ──────────────────────────────────────────────────────
        rc = tk.Frame(left, bg="#1e1e2e")
        rc.pack(fill="x", pady=(4,6), padx=4)
        self.start_btn = self._btn(rc, "▶  Start Scraping", self._start, "#a6e3a1","#1e1e2e")
        self.start_btn.pack(fill="x", pady=(0,4))
        self.stop_btn  = self._btn(rc, "⏹  Stop", self._stop, "#f38ba8","#1e1e2e")
        self.stop_btn.pack(fill="x", pady=(0,4))
        self.stop_btn.config(state="disabled")
        self._btn(rc, "📥  Export CSV", self._export, "#89b4fa","#1e1e2e").pack(fill="x", pady=(0,4))
        self._btn(rc, "🗑  Clear Database", self._clear, "#585b70","#cdd6f4").pack(fill="x")

        # ── Right: Progress + Log + Summary ──────────────────────────────────
        pf2 = tk.Frame(right, bg="#1e1e2e")
        pf2.pack(fill="x", pady=(0,6))
        tk.Label(pf2, text="Progress", bg="#1e1e2e", fg="#a6adc8", font=("Segoe UI",9)).pack(anchor="w")
        self.prog = ttk.Progressbar(pf2, mode="determinate")
        self.prog.pack(fill="x", pady=(2,0), ipady=2)

        lf2 = ttk.LabelFrame(right, text="📋 Live Log", padding=4)
        lf2.pack(fill="both", expand=True, pady=(0,6))
        self.logbox = scrolledtext.ScrolledText(
            lf2, bg="#11111b", fg="#cdd6f4", font=("Consolas",9),
            relief="flat", wrap="word", state="disabled")
        self.logbox.pack(fill="both", expand=True)
        for tag, col in [("success","#a6e3a1"),("error","#f38ba8"),
                          ("warning","#f9e2af"),("info","#89b4fa")]:
            self.logbox.tag_configure(tag, foreground=col)

        sf2 = ttk.LabelFrame(right, text="📊 Summary", padding=8)
        sf2.pack(fill="x")
        self.stats = {}
        cards = [("urls","URLs scraped"),("found","Records found"),("dups","Duplicates removed"),
                 ("filt","After filters"),("nulls","Null records handled"),
                 ("export","Final export"),("total","Total in database")]
        grid = tk.Frame(sf2, bg="#1e1e2e"); grid.pack(fill="x")
        for i, (key, lbl) in enumerate(cards):
            col, row = i%4, i//4
            card = tk.Frame(grid, bg="#313244", padx=8, pady=6)
            card.grid(row=row, column=col, padx=3, pady=3, sticky="ew")
            grid.columnconfigure(col, weight=1)
            tk.Label(card, text=lbl, bg="#313244", fg="#a6adc8", font=("Segoe UI",8)).pack()
            v = tk.Label(card, text="—", bg="#313244", fg="#89b4fa", font=("Segoe UI",13,"bold"))
            v.pack(); self.stats[key] = v

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 2 — HISTORY
    # ─────────────────────────────────────────────────────────────────────────
    def _tab_history(self, nb):
        tab = ttk.Frame(nb); nb.add(tab, text="  🗂 History  ")
        ctrl = tk.Frame(tab, bg="#1e1e2e"); ctrl.pack(fill="x", padx=8, pady=6)
        self._btn(ctrl, "🔄 Refresh", self._refresh, "#89b4fa","#1e1e2e").pack(side="left")
        self._btn(ctrl, "📥 Export All (Save As…)", self._export_all, "#a6e3a1","#1e1e2e").pack(side="left", padx=6)
        self.cnt_lbl = tk.Label(ctrl, text="", bg="#1e1e2e", fg="#a6adc8", font=("Segoe UI",9))
        self.cnt_lbl.pack(side="right", padx=8)

        cols = ("record_id","name","email","company","job_title","cellphone","telephone","linkedin","address","source_url","scraped_at")
        self.tree = ttk.Treeview(tab, columns=cols, show="headings", height=28)
        col_widths = [70, 130, 160, 130, 120, 110, 110, 150, 150, 200, 150]
        for c, w in zip(cols, col_widths):
            self.tree.heading(c, text=c.replace("_"," ").title())
            self.tree.column(c, width=w, minwidth=50)
        xsb = ttk.Scrollbar(tab, orient="horizontal", command=self.tree.xview)
        ysb = ttk.Scrollbar(tab, orient="vertical",   command=self.tree.yview)
        self.tree.configure(xscrollcommand=xsb.set, yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True, padx=8)
        xsb.pack(fill="x", padx=8, pady=(0,4))
        self._refresh()

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 3 — SETTINGS
    # ─────────────────────────────────────────────────────────────────────────
    def _tab_settings(self, nb):
        tab = ttk.Frame(nb); nb.add(tab, text="  ⚙️ Settings  ")
        wrap = tk.Frame(tab, bg="#1e1e2e"); wrap.pack(padx=20, pady=20, anchor="nw")

        af = ttk.LabelFrame(wrap, text="🔑 Gemini API Key (for AI Mode)", padding=12)
        af.pack(fill="x", pady=(0,12))
        tk.Label(af, text="Gemini API Key:", bg="#1e1e2e", fg="#a6adc8",
                 font=("Segoe UI",9)).grid(row=0,column=0,sticky="w",pady=5,padx=(0,8))
        self.gemini_ent = tk.Entry(af, width=55, bg="#313244", fg="#cdd6f4", show="*",
                                    relief="flat", insertbackground="#cdd6f4", font=("Segoe UI",9))
        self.gemini_ent.grid(row=0,column=1,ipady=4)
        tk.Label(af, text="Get a free key at: aistudio.google.com", bg="#1e1e2e",
                 fg="#6c7086", font=("Segoe UI",8)).grid(row=1,column=1,sticky="w")
        self._btn(af,"💾 Save Key",self._save_key,"#89b4fa","#1e1e2e").grid(row=2,column=1,sticky="e",pady=8)

        sf = ttk.LabelFrame(wrap, text="🕷️ Scraper Tuning", padding=12)
        sf.pack(fill="x", pady=(0,12))
        self.delay_var = tk.DoubleVar(value=1.5)
        self.dedup_var = tk.IntVar(value=85)
        for i,(lbl,var,mn,mx,inc) in enumerate([
            ("Delay between requests (sec):", self.delay_var, 0.5, 10.0, 0.5),
            ("Dedup similarity threshold (%):", self.dedup_var, 60, 100, 5),
        ]):
            tk.Label(sf,text=lbl,bg="#1e1e2e",fg="#a6adc8",font=("Segoe UI",9)).grid(row=i,column=0,sticky="w",pady=5,padx=(0,8))
            tk.Spinbox(sf,from_=mn,to=mx,increment=inc,textvariable=var,width=8,
                       bg="#313244",fg="#cdd6f4",relief="flat",font=("Segoe UI",9),
                       buttonbackground="#313244").grid(row=i,column=1,sticky="w")

        ig = ttk.LabelFrame(wrap, text="📦 Install Dependencies", padding=12)
        ig.pack(fill="x")
        t = tk.Text(ig, height=4, bg="#11111b", fg="#a6e3a1",
                    font=("Consolas",9), relief="flat", wrap="none")
        t.insert("1.0",
            "pip install aiohttp beautifulsoup4 google-generativeai spacy rapidfuzz playwright requests\n"
            "python -m spacy download en_core_web_sm\n"
            "playwright install chromium"
        )
        t.config(state="disabled"); t.pack(fill="x")

    # ── UI helpers ────────────────────────────────────────────────────────────
    def _toggle_placeholder(self):
        state = "normal" if self.null_var.get() == "placeholder" else "disabled"
        self.ph_ent.config(state=state)

    def _shorten(self, p, n=34):
        return p if len(p) <= n else "…" + p[-(n-1):]

    def log(self, msg, tag="info"):
        def _w():
            ts = datetime.now().strftime("%H:%M:%S")
            self.logbox.config(state="normal")
            self.logbox.insert("end", f"[{ts}] {msg}\n", tag)
            self.logbox.see("end")
            self.logbox.config(state="disabled")
        self.root.after(0, _w)

    def _sp(self, v): self.root.after(0, lambda: self.prog.config(value=v))
    def _ss(self, k, v): self.root.after(0, lambda: self.stats[k].config(text=str(v)))

    def _browse_url(self):
        p = filedialog.askopenfilename(filetypes=[("Text","*.txt"),("All","*.*")])
        if p:
            self._url_file = p
            self.file_lbl.config(text=Path(p).name, fg="#a6e3a1")

    def _browse_folder(self):
        p = filedialog.askdirectory(initialdir=self._out_folder)
        if p:
            self._out_folder = p
            self.folder_lbl.config(text=self._shorten(p))

    def _save_key(self):
        self._gemini_key = self.gemini_ent.get().strip()
        self.log("✅ Gemini API key saved.", "success")

    def _get_urls(self):
        urls = []
        s = self.url_ent.get().strip()
        if s: urls.append(s)
        if self._url_file:
            try:
                with open(self._url_file,"r") as f:
                    for line in f:
                        u = line.strip()
                        if u.startswith("http"): urls.append(u)
            except Exception as e:
                self.log(f"❌ File error: {e}", "error")
        return list(dict.fromkeys(urls))

    def _build_path(self):
        name = self.fname_ent.get().strip() or "leads"
        if self.ts_var.get():
            name += "_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        if not name.endswith(".csv"): name += ".csv"
        return os.path.join(self._out_folder, name)

    # ── Start / Stop ──────────────────────────────────────────────────────────
    def _start(self):
        urls = self._get_urls()
        if not urls:
            messagebox.showwarning("No URLs", "Enter a URL or load a .txt file."); return
        mode = self.mode_var.get()
        gk = self._gemini_key or self.gemini_ent.get().strip()
        if mode == "ai" and not gk:
            messagebox.showwarning("No API Key", "AI mode requires a Gemini API key.\nAdd it in Settings or switch to Pattern mode."); return
        self.is_running = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._sp(0)
        threading.Thread(target=self._run, args=(urls, mode, gk), daemon=True).start()

    def _stop(self):
        self.is_running = False
        self.log("⏹ Stop requested…", "warning")

    # ── Main pipeline ─────────────────────────────────────────────────────────
    def _run(self, urls, mode, gk):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            if self.skip_var.get():
                orig = len(urls)
                urls = [u for u in urls if not self.db.is_scraped(u)]
                skipped = orig - len(urls)
                if skipped: self.log(f"⏭ Skipped {skipped} already-scraped URL(s)", "warning")

            if not urls:
                self.log("ℹ️ All URLs already scraped.", "info"); self._done(); return

            self.log(f"🚀 Starting — {len(urls)} URL(s) — mode: {'AI (Gemini)' if mode=='ai' else 'Pattern'}", "info")

            scraper  = AsyncScraper(self.log, delay=self.delay_var.get())
            ai_ext   = AIExtractor(gk, self.log) if mode == "ai" else None
            pat_ext  = PatternExtractor()
            ner      = NERProcessor(self.log)
            dedup    = Deduplicator(threshold=self.dedup_var.get())

            # ── Async fetch ───────────────────────────────────────────────────
            results  = loop.run_until_complete(scraper.scrape_all(urls, self._sp))
            if not self.is_running: self._done(); return

            # ── Extract + NER enhance ─────────────────────────────────────────
            all_records = []
            fetched = {u: h for u, h in results.items() if h}
            self._ss("urls", len(fetched))
            total = len(fetched) or 1

            for i, (url, html) in enumerate(fetched.items()):
                if not self.is_running: break
                clean = scraper.clean(html)

                if mode == "ai":
                    records = ai_ext.extract(clean, url)
                else:
                    records = pat_ext.extract(html, url)
                    self.log(f"🔍 Pattern → {len(records)} records | {url[:45]}", "success")

                for r in records:
                    r["source_url"] = url
                    r["scraped_at"] = datetime.now().isoformat()
                records = ner.enhance_batch(records, clean)
                all_records.extend(records)
                self.db.add_url(url)
                self._sp(45 + (i+1)/total * 30)

            self.log(f"📦 Raw records: {len(all_records)}", "info")
            self._ss("found", len(all_records))

            # ── Dedup ─────────────────────────────────────────────────────────
            all_records, dups = dedup.run(all_records)
            self.log(f"🔁 Duplicates removed: {dups}", "warning" if dups else "info")
            self._ss("dups", dups)
            self._sp(80)

            # ── Field filters ─────────────────────────────────────────────────
            active  = [k for k,v in self.fvars.items() if v.get()]
            filtered = LeadFilter.apply_filters(all_records, active)
            self.log(f"🔽 After field filters: {len(filtered)}", "info")
            self._ss("filt", len(filtered))

            # ── Null handling ─────────────────────────────────────────────────
            null_mode = self.null_var.get()
            ph        = self.ph_ent.get().strip() or "N/A"
            filtered, null_count = LeadFilter.handle_nulls(filtered, null_mode, ph)
            null_label = {
                "keep":        "kept (mode: keep)",
                "drop":        f"dropped ({null_count} records)",
                "placeholder": f"filled with '{ph}'",
                "mode_fill":   "filled with field mode value",
            }.get(null_mode, "")
            self.log(f"⚙️ Null handling: {null_label}", "info")
            self._ss("nulls", null_count if null_mode == "drop" else f"— ({null_mode})")
            self._ss("export", len(filtered))
            self._sp(95)

            # ── Save to DB ────────────────────────────────────────────────────
            self.db.add_leads(filtered)
            self.current_leads = filtered
            total_db = len(self.db.all_leads())
            self._ss("total", f"{total_db:,}")
            self._sp(100)
            self.log(f"✅ Done! {len(filtered)} new leads saved. Total in DB: {total_db:,}", "success")
            self._refresh()

        except Exception as e:
            self.log(f"❌ Fatal error: {str(e)}", "error")
        finally:
            self._done()

    def _done(self):
        self.is_running = False
        self.root.after(0, lambda: self.start_btn.config(state="normal"))
        self.root.after(0, lambda: self.stop_btn.config(state="disabled"))

    # ── Export ─────────────────────────────────────────────────────────────────
    def _do_export(self, path, leads):
        try:
            n = CSVExporter.export(leads, path)
            self.log(f"📥 Exported {n:,} leads → {path}", "success")
            messagebox.showinfo("Exported", f"✅ {n:,} leads saved to:\n{path}")
        except Exception as e:
            messagebox.showerror("Export Error", str(e))

    def _export(self):
        leads = self.current_leads or self.db.all_leads()
        if not leads:
            messagebox.showinfo("No Data","No leads to export."); return
        self._do_export(self._build_path(), leads)

    def _export_all(self):
        leads = self.db.all_leads()
        if not leads:
            messagebox.showinfo("No Data","No leads to export."); return
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = (self.fname_ent.get().strip() or "leads") + f"_{ts}.csv"
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=name,
            initialdir=self._out_folder,
            filetypes=[("CSV files","*.csv")])
        if path:
            self.current_leads = leads
            self._do_export(path, leads)

    def _clear(self):
        if messagebox.askyesno("Clear", "⚠️ Delete ALL saved leads permanently?"):
            self.db.clear(); self.current_leads = []
            self._refresh()
            for v in self.stats.values(): v.config(text="—")
            self.log("🗑 Database cleared.", "warning")

    def _refresh(self):
        for r in self.tree.get_children(): self.tree.delete(r)
        leads = self.db.all_leads()
        tree_cols = ("record_id","name","email","company","job_title","cellphone","telephone","linkedin","address","source_url","scraped_at")
        for lead in reversed(leads[-1000:]):
            self.tree.insert("","end", values=tuple(
                lead.get(c,"") or "" for c in tree_cols
            ))
        n = len(leads)
        self.cnt_lbl.config(text=f"{n:,} total leads in database")
        self._ss("total", f"{n:,}")


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    root = tk.Tk()
    WebScraperApp(root)
    root.mainloop()