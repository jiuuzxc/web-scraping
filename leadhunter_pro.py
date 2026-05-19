"""
╔══════════════════════════════════════════════════════════════╗
║           LeadHunter Pro — Apollo-style Lead Scraper         ║
║           Powered by Gemini Flash 2.0 + GPT-4o Mini          ║
╚══════════════════════════════════════════════════════════════╝

INSTALL DEPENDENCIES FIRST:
  pip install aiohttp beautifulsoup4 google-generativeai openai spacy rapidfuzz playwright requests
  python -m spacy download en_core_web_sm
  playwright install chromium

RUN:
  python leadhunter_pro.py
"""

import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
import asyncio
import aiohttp
from bs4 import BeautifulSoup
import json
import csv
import re
import os
import threading
from datetime import datetime
from pathlib import Path
import random

# ── Optional imports (graceful fallback if not installed) ────────────────────
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

try:
    import spacy
    SPACY_AVAILABLE = True
except ImportError:
    SPACY_AVAILABLE = False

try:
    from rapidfuzz import fuzz
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False

# ── Constants ────────────────────────────────────────────────────────────────
DB_FILE = "leads_database.json"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/119.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/118.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Safari/605.1.15",
]

EXTRACT_PROMPT = """
You are a lead extraction AI. Extract all business contacts from the text below.
Return ONLY a valid JSON array. No markdown, no explanation, no backticks. Raw JSON only.

Each contact must have these exact fields (use null if not found):
- name        (string) full name of person
- email       (string) email address
- company     (string) company or organization name
- job_title   (string) job title or role
- cellphone   (string) mobile/cell phone number
- telephone   (string) office or landline phone number
- linkedin    (string) LinkedIn profile URL
- address     (string) physical address if available

Return format:
[{{"name":..., "email":..., "company":..., "job_title":..., "cellphone":..., "telephone":..., "linkedin":..., "address":...}}]

If no contacts found, return: []

Content to extract from:
{content}
"""


# ╔══════════════════════════════════════════════════════════════╗
# ║  DATABASE                                                    ║
# ╚══════════════════════════════════════════════════════════════╝
class LeadDatabase:
    def __init__(self):
        self.db_file = DB_FILE
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.db_file):
            try:
                with open(self.db_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"leads": [], "scraped_urls": [], "sessions": []}

    def save(self):
        with open(self.db_file, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def add_leads(self, leads):
        self.data["leads"].extend(leads)
        self.save()

    def add_url(self, url):
        if url not in self.data["scraped_urls"]:
            self.data["scraped_urls"].append(url)

    def is_url_scraped(self, url):
        return url in self.data["scraped_urls"]

    def get_all_leads(self):
        return self.data["leads"]

    def add_session(self, session_data):
        self.data["sessions"].append(session_data)
        self.save()

    def clear(self):
        self.data = {"leads": [], "scraped_urls": [], "sessions": []}
        self.save()


# ╔══════════════════════════════════════════════════════════════╗
# ║  ASYNC SCRAPER                                               ║
# ╚══════════════════════════════════════════════════════════════╝
class AsyncScraper:
    def __init__(self, log_cb, delay=1.5):
        self.log = log_cb
        self.delay = delay

    def _headers(self):
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }

    async def _fetch(self, session, url):
        try:
            await asyncio.sleep(random.uniform(0.5, self.delay))
            timeout = aiohttp.ClientTimeout(total=15)
            async with session.get(url, headers=self._headers(), timeout=timeout, ssl=False) as r:
                if r.status == 200:
                    html = await r.text(errors="ignore")
                    self.log(f"✅ Fetched: {url}", "success")
                    return url, html
                self.log(f"⚠️ HTTP {r.status}: {url}", "warning")
                return url, None
        except Exception as e:
            self.log(f"❌ Fetch error {url[:50]}: {str(e)[:50]}", "error")
            return url, None

    async def scrape_all(self, urls, progress_cb=None):
        results = {}
        conn = aiohttp.TCPConnector(limit=10, ssl=False)
        async with aiohttp.ClientSession(connector=conn) as session:
            tasks = [self._fetch(session, u) for u in urls]
            total = len(tasks)
            done = 0
            for coro in asyncio.as_completed(tasks):
                url, html = await coro
                results[url] = html
                done += 1
                if progress_cb:
                    progress_cb(done / total * 50)  # scraping = first 50%
        return results

    def clean_html(self, html):
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "meta", "link", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        # Trim to ~8000 chars to keep token cost low
        return " ".join(text.split())[:8000]


# ╔══════════════════════════════════════════════════════════════╗
# ║  AI EXTRACTOR (Gemini primary → GPT-4o Mini fallback)       ║
# ╚══════════════════════════════════════════════════════════════╝
class AIExtractor:
    def __init__(self, gemini_key, openai_key, log_cb):
        self.log = log_cb
        self.gemini_model = None
        self.oai_client = None
        self._init(gemini_key, openai_key)

    def _init(self, g_key, o_key):
        if g_key and GEMINI_AVAILABLE:
            try:
                genai.configure(api_key=g_key)
                self.gemini_model = genai.GenerativeModel("gemini-2.0-flash")
                self.log("✅ Gemini Flash 2.0 initialized", "success")
            except Exception as e:
                self.log(f"⚠️ Gemini init failed: {e}", "warning")
        if o_key and OPENAI_AVAILABLE:
            try:
                self.oai_client = OpenAI(api_key=o_key)
                self.log("✅ GPT-4o Mini fallback initialized", "success")
            except Exception as e:
                self.log(f"⚠️ OpenAI init failed: {e}", "warning")

    def _parse(self, raw):
        text = re.sub(r"```json|```", "", raw or "").strip()
        try:
            data = json.loads(text)
            return data if isinstance(data, list) else []
        except Exception:
            m = re.search(r'\[.*\]', text, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except Exception:
                    pass
        return []

    def _gemini(self, content):
        if not self.gemini_model:
            return None
        try:
            resp = self.gemini_model.generate_content(
                EXTRACT_PROMPT.format(content=content)
            )
            return self._parse(resp.text)
        except Exception as e:
            self.log(f"⚠️ Gemini error: {str(e)[:60]}", "warning")
            return None

    def _openai(self, content):
        if not self.oai_client:
            return None
        try:
            resp = self.oai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": EXTRACT_PROMPT.format(content=content)}],
                max_tokens=2000,
            )
            return self._parse(resp.choices[0].message.content)
        except Exception as e:
            self.log(f"⚠️ GPT fallback error: {str(e)[:60]}", "warning")
            return None

    def extract(self, content, url=""):
        short_url = url[:45]
        result = self._gemini(content)
        if result is not None:
            self.log(f"🤖 Gemini → {len(result)} records | {short_url}", "success")
            return result
        self.log(f"🔄 Gemini failed, trying GPT-4o Mini | {short_url}", "warning")
        result = self._openai(content)
        if result is not None:
            self.log(f"🤖 GPT-4o Mini → {len(result)} records | {short_url}", "success")
            return result
        self.log(f"❌ Both AI extractors failed | {short_url}", "error")
        return []


# ╔══════════════════════════════════════════════════════════════╗
# ║  spaCy NER — Enhances name & company accuracy               ║
# ╚══════════════════════════════════════════════════════════════╝
class NERProcessor:
    def __init__(self, log_cb):
        self.log = log_cb
        self.nlp = None
        if SPACY_AVAILABLE:
            try:
                self.nlp = spacy.load("en_core_web_sm")
                self.log("✅ spaCy NER loaded (en_core_web_sm)", "success")
            except OSError:
                self.log("⚠️ spaCy model missing. Run: python -m spacy download en_core_web_sm", "warning")

    def enhance(self, record, raw_text=""):
        if not self.nlp or not raw_text:
            return record
        doc = self.nlp(raw_text[:600])
        for ent in doc.ents:
            if ent.label_ == "PERSON" and not record.get("name"):
                record["name"] = ent.text
            elif ent.label_ == "ORG" and not record.get("company"):
                record["company"] = ent.text
        return record

    def enhance_batch(self, records, raw_text=""):
        return [self.enhance(r, raw_text) for r in records]


# ╔══════════════════════════════════════════════════════════════╗
# ║  DEDUPLICATOR — rapidfuzz fuzzy matching                    ║
# ╚══════════════════════════════════════════════════════════════╝
class Deduplicator:
    def __init__(self, threshold=85):
        self.threshold = threshold

    def _sig(self, r):
        return (r.get("name") or "").lower().strip(), (r.get("email") or "").lower().strip()

    def deduplicate(self, records):
        if not RAPIDFUZZ_AVAILABLE:
            # Exact email match only
            seen, unique = set(), []
            for r in records:
                key = (r.get("email") or "").lower()
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                unique.append(r)
            return unique, len(records) - len(unique)

        unique, removed = [], 0
        for rec in records:
            name, email = self._sig(rec)
            dup = False
            for ex in unique:
                ex_name, ex_email = self._sig(ex)
                # Exact email match
                if email and ex_email and email == ex_email:
                    dup = True
                    break
                # Fuzzy name match within same company
                if name and ex_name and fuzz.ratio(name, ex_name) >= self.threshold:
                    same_co = (rec.get("company") or "").lower() == (ex.get("company") or "").lower()
                    if same_co:
                        dup = True
                        break
            if dup:
                removed += 1
            else:
                unique.append(rec)
        return unique, removed


# ╔══════════════════════════════════════════════════════════════╗
# ║  FILTER & NORMALIZE                                         ║
# ╚══════════════════════════════════════════════════════════════╝
class LeadFilter:
    ALL_FIELDS = ["name", "email", "company", "job_title", "cellphone", "telephone", "linkedin", "address"]

    @staticmethod
    def apply(records, active_filters):
        if not active_filters:
            return records
        return [r for r in records if all(r.get(f) for f in active_filters)]

    @staticmethod
    def normalize(records):
        return [r for r in records if all(r.get(f) for f in LeadFilter.ALL_FIELDS)]


# ╔══════════════════════════════════════════════════════════════╗
# ║  CSV EXPORTER                                               ║
# ╚══════════════════════════════════════════════════════════════╝
class CSVExporter:
    FIELDS = ["name", "email", "company", "job_title", "cellphone",
              "telephone", "linkedin", "address", "source_url", "scraped_at"]

    @staticmethod
    def export(records, filepath):
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSVExporter.FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(records)
        return len(records)


# ╔══════════════════════════════════════════════════════════════╗
# ║  GUI APPLICATION                                            ║
# ╚══════════════════════════════════════════════════════════════╝
class LeadHunterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("LeadHunter Pro")
        self.root.geometry("1020x800")
        self.root.configure(bg="#1e1e2e")
        self.root.resizable(True, True)

        self.db = LeadDatabase()
        self.is_running = False
        self.current_leads = []
        self._url_file = None
        self._gemini_key = ""
        self._openai_key = ""

        self._styles()
        self._build()
        self.log("🚀 LeadHunter Pro ready. Add API keys in Settings, then start scraping.", "info")
        self.log(f"💾 Database loaded: {len(self.db.get_all_leads())} existing leads.", "info")

    # ── Styles ───────────────────────────────────────────────────────────────
    def _styles(self):
        s = ttk.Style()
        s.theme_use("clam")
        bg = "#1e1e2e"
        s.configure("TFrame",        background=bg)
        s.configure("TLabel",        background=bg, foreground="#cdd6f4", font=("Segoe UI", 10))
        s.configure("TLabelframe",   background=bg, foreground="#89b4fa")
        s.configure("TLabelframe.Label", background=bg, foreground="#89b4fa", font=("Segoe UI", 10, "bold"))
        s.configure("TCheckbutton",  background=bg, foreground="#cdd6f4", font=("Segoe UI", 9))
        s.configure("TRadiobutton",  background=bg, foreground="#cdd6f4", font=("Segoe UI", 9))
        s.configure("TNotebook",     background=bg, borderwidth=0)
        s.configure("TNotebook.Tab", background="#313244", foreground="#cdd6f4",
                    padding=[14, 5], font=("Segoe UI", 9))
        s.map("TNotebook.Tab",
              background=[("selected", "#89b4fa")],
              foreground=[("selected", "#1e1e2e")])
        s.configure("TProgressbar", troughcolor="#313244", background="#89b4fa", thickness=8)

    def _btn(self, parent, text, cmd, bg="#89b4fa", fg="#1e1e2e", **kw):
        def lighten(c):
            h = c.lstrip("#")
            r, g, b = int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
            return f"#{min(255,r+28):02x}{min(255,g+28):02x}{min(255,b+28):02x}"
        btn = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                        relief="flat", cursor="hand2",
                        font=("Segoe UI", 9, "bold"), padx=12, pady=5, **kw)
        btn.bind("<Enter>", lambda e: btn.config(bg=lighten(bg)))
        btn.bind("<Leave>", lambda e: btn.config(bg=bg))
        return btn

    # ── Build UI ─────────────────────────────────────────────────────────────
    def _build(self):
        # Header
        hdr = tk.Frame(self.root, bg="#181825", pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="⚡ LeadHunter Pro", bg="#181825", fg="#89b4fa",
                 font=("Segoe UI", 16, "bold")).pack(side="left", padx=16)
        tk.Label(hdr, text="Apollo-style lead scraper  •  Gemini Flash 2.0 + GPT-4o Mini",
                 bg="#181825", fg="#6c7086", font=("Segoe UI", 9)).pack(side="left")

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=8)

        self._tab_scraper(nb)
        self._tab_history(nb)
        self._tab_settings(nb)

    # ── Tab 1: Scraper ────────────────────────────────────────────────────────
    def _tab_scraper(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="  🔍 Scraper  ")

        left = tk.Frame(tab, bg="#1e1e2e", width=295)
        left.pack(side="left", fill="y", padx=(6,3), pady=6)
        left.pack_propagate(False)

        right = tk.Frame(tab, bg="#1e1e2e")
        right.pack(side="left", fill="both", expand=True, padx=(3,6), pady=6)

        # ── URL Input ────────────────────────────────────────────────────────
        f = ttk.LabelFrame(left, text="📌 URL Input", padding=8)
        f.pack(fill="x", pady=(0,6))

        tk.Label(f, text="Single URL:", bg="#1e1e2e", fg="#a6adc8", font=("Segoe UI",9)).pack(anchor="w")
        self.url_entry = tk.Entry(f, bg="#313244", fg="#cdd6f4", relief="flat",
                                   insertbackground="#cdd6f4", font=("Segoe UI",9))
        self.url_entry.pack(fill="x", pady=(2,6), ipady=4)

        tk.Label(f, text="Or .txt file (one URL per line):", bg="#1e1e2e", fg="#a6adc8", font=("Segoe UI",9)).pack(anchor="w")
        row = tk.Frame(f, bg="#1e1e2e")
        row.pack(fill="x", pady=(2,4))
        self.file_lbl = tk.Label(row, text="No file selected", bg="#1e1e2e",
                                  fg="#6c7086", font=("Segoe UI",8))
        self.file_lbl.pack(side="left", fill="x", expand=True)
        self._btn(row, "Browse", self._browse, "#313244", "#cdd6f4").pack(side="right")

        self.skip_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Skip already-scraped URLs", variable=self.skip_var).pack(anchor="w", pady=(4,0))

        # ── Filters ──────────────────────────────────────────────────────────
        f2 = ttk.LabelFrame(left, text="🔽 Field Filters", padding=8)
        f2.pack(fill="x", pady=(0,6))
        tk.Label(f2, text="Only export records that have:", bg="#1e1e2e",
                 fg="#a6adc8", font=("Segoe UI",8)).pack(anchor="w", pady=(0,4))

        self.fvars = {}
        for key, label in [
            ("name",      "👤 Name"),
            ("email",     "📧 Email"),
            ("cellphone", "📱 Cellphone"),
            ("telephone", "☎️  Telephone"),
            ("company",   "🏢 Company name"),
            ("job_title", "💼 Job title"),
            ("linkedin",  "🔗 LinkedIn URL"),
        ]:
            v = tk.BooleanVar(value=False)
            self.fvars[key] = v
            ttk.Checkbutton(f2, text=label, variable=v).pack(anchor="w", pady=1)

        # ── Normalization ─────────────────────────────────────────────────────
        f3 = ttk.LabelFrame(left, text="⚙️ Normalization", padding=8)
        f3.pack(fill="x", pady=(0,6))
        self.norm_var = tk.StringVar(value="all")
        ttk.Radiobutton(f3, text="Export all records (nulls OK)",
                        variable=self.norm_var, value="all").pack(anchor="w", pady=2)
        ttk.Radiobutton(f3, text="Normalized only (zero null fields)",
                        variable=self.norm_var, value="normalize").pack(anchor="w", pady=2)

        # ── Controls ──────────────────────────────────────────────────────────
        f4 = tk.Frame(left, bg="#1e1e2e")
        f4.pack(fill="x")
        self.start_btn = self._btn(f4, "▶  Start Scraping", self._start, "#a6e3a1", "#1e1e2e")
        self.start_btn.pack(fill="x", pady=(0,4))
        self.stop_btn = self._btn(f4, "⏹  Stop", self._stop, "#f38ba8", "#1e1e2e")
        self.stop_btn.pack(fill="x", pady=(0,4))
        self.stop_btn.config(state="disabled")
        self._btn(f4, "📥  Export CSV", self._export, "#89b4fa", "#1e1e2e").pack(fill="x", pady=(0,4))
        self._btn(f4, "🗑  Clear Database", self._clear, "#585b70", "#cdd6f4").pack(fill="x")

        # ── Progress ──────────────────────────────────────────────────────────
        pf = tk.Frame(right, bg="#1e1e2e")
        pf.pack(fill="x", pady=(0,6))
        tk.Label(pf, text="Progress", bg="#1e1e2e", fg="#a6adc8", font=("Segoe UI",9)).pack(anchor="w")
        self.prog = ttk.Progressbar(pf, mode="determinate")
        self.prog.pack(fill="x", pady=(2,0), ipady=2)

        # ── Log ───────────────────────────────────────────────────────────────
        lf = ttk.LabelFrame(right, text="📋 Live Log", padding=4)
        lf.pack(fill="both", expand=True, pady=(0,6))
        self.logbox = scrolledtext.ScrolledText(
            lf, bg="#11111b", fg="#cdd6f4", font=("Consolas", 9),
            relief="flat", wrap="word", state="disabled"
        )
        self.logbox.pack(fill="both", expand=True)
        self.logbox.tag_configure("success", foreground="#a6e3a1")
        self.logbox.tag_configure("error",   foreground="#f38ba8")
        self.logbox.tag_configure("warning", foreground="#f9e2af")
        self.logbox.tag_configure("info",    foreground="#89b4fa")

        # ── Summary ────────────────────────────────────────────────────────────
        sf = ttk.LabelFrame(right, text="📊 Summary", padding=8)
        sf.pack(fill="x")
        self.stats = {}
        cards = [
            ("urls",    "URLs scraped"),
            ("found",   "Records found"),
            ("dups",    "Duplicates removed"),
            ("filt",    "After filters"),
            ("nulls",   "Nulls removed"),
            ("export",  "Final export"),
            ("total",   "Total in database"),
        ]
        grid = tk.Frame(sf, bg="#1e1e2e")
        grid.pack(fill="x")
        for i, (key, label) in enumerate(cards):
            col, row = i % 4, i // 4
            card = tk.Frame(grid, bg="#313244", padx=8, pady=6)
            card.grid(row=row, column=col, padx=3, pady=3, sticky="ew")
            grid.columnconfigure(col, weight=1)
            tk.Label(card, text=label, bg="#313244", fg="#a6adc8", font=("Segoe UI",8)).pack()
            lbl = tk.Label(card, text="—", bg="#313244", fg="#89b4fa", font=("Segoe UI",13,"bold"))
            lbl.pack()
            self.stats[key] = lbl

    # ── Tab 2: History ────────────────────────────────────────────────────────
    def _tab_history(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="  🗂 History  ")

        ctrl = tk.Frame(tab, bg="#1e1e2e")
        ctrl.pack(fill="x", padx=8, pady=6)
        self._btn(ctrl, "🔄 Refresh", self._refresh, "#89b4fa", "#1e1e2e").pack(side="left")
        self._btn(ctrl, "📥 Export All as CSV", self._export_all, "#a6e3a1", "#1e1e2e").pack(side="left", padx=6)
        self.count_lbl = tk.Label(ctrl, text="", bg="#1e1e2e", fg="#a6adc8", font=("Segoe UI",9))
        self.count_lbl.pack(side="right", padx=8)

        cols = ("name","email","company","job_title","cellphone","telephone","linkedin","source_url")
        self.tree = ttk.Treeview(tab, columns=cols, show="headings", height=28)
        widths =    [130,    160,     130,      120,         110,          110,         150,         200]
        for c, w in zip(cols, widths):
            self.tree.heading(c, text=c.replace("_"," ").title())
            self.tree.column(c, width=w, minwidth=50)

        xsb = ttk.Scrollbar(tab, orient="horizontal", command=self.tree.xview)
        ysb = ttk.Scrollbar(tab, orient="vertical",   command=self.tree.yview)
        self.tree.configure(xscrollcommand=xsb.set, yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True, padx=8)
        xsb.pack(fill="x", padx=8, pady=(0,4))
        self._refresh()

    # ── Tab 3: Settings ───────────────────────────────────────────────────────
    def _tab_settings(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="  ⚙️ Settings  ")
        wrap = tk.Frame(tab, bg="#1e1e2e")
        wrap.pack(padx=20, pady=20, anchor="nw")

        # API Keys
        af = ttk.LabelFrame(wrap, text="🔑 API Keys", padding=12)
        af.pack(fill="x", pady=(0,12))
        for i, (label, attr) in enumerate([
            ("Gemini API Key (primary AI):", "gemini_ent"),
            ("OpenAI API Key (GPT-4o Mini fallback):", "openai_ent"),
        ]):
            tk.Label(af, text=label, bg="#1e1e2e", fg="#a6adc8", font=("Segoe UI",9)).grid(row=i, column=0, sticky="w", pady=5, padx=(0,8))
            ent = tk.Entry(af, width=52, bg="#313244", fg="#cdd6f4", show="*",
                           relief="flat", insertbackground="#cdd6f4", font=("Segoe UI",9))
            ent.grid(row=i, column=1, ipady=4)
            setattr(self, attr, ent)
        self._btn(af, "💾 Save Keys", self._save_keys, "#89b4fa", "#1e1e2e").grid(row=2, column=1, sticky="e", pady=8)

        # Scraper tuning
        sf = ttk.LabelFrame(wrap, text="🕷️ Scraper Tuning", padding=12)
        sf.pack(fill="x", pady=(0,12))
        self.delay_var = tk.DoubleVar(value=1.5)
        self.dedup_var = tk.IntVar(value=85)
        for i, (label, var, mn, mx, inc) in enumerate([
            ("Delay between requests (sec):", self.delay_var, 0.5, 10.0, 0.5),
            ("Dedup similarity threshold (%):", self.dedup_var, 60, 100, 5),
        ]):
            tk.Label(sf, text=label, bg="#1e1e2e", fg="#a6adc8", font=("Segoe UI",9)).grid(row=i, column=0, sticky="w", pady=5, padx=(0,8))
            tk.Spinbox(sf, from_=mn, to=mx, increment=inc, textvariable=var, width=8,
                       bg="#313244", fg="#cdd6f4", relief="flat",
                       font=("Segoe UI",9), buttonbackground="#313244").grid(row=i, column=1, sticky="w")

        # Install guide
        ig = ttk.LabelFrame(wrap, text="📦 Install Dependencies", padding=12)
        ig.pack(fill="x")
        cmds = (
            "pip install aiohttp beautifulsoup4 google-generativeai openai spacy rapidfuzz playwright requests\n"
            "python -m spacy download en_core_web_sm\n"
            "playwright install chromium"
        )
        t = tk.Text(ig, height=4, bg="#11111b", fg="#a6e3a1",
                    font=("Consolas",9), relief="flat", wrap="none")
        t.insert("1.0", cmds)
        t.config(state="disabled")
        t.pack(fill="x")

    # ── Helpers ───────────────────────────────────────────────────────────────
    def log(self, msg, tag="info"):
        def _w():
            ts = datetime.now().strftime("%H:%M:%S")
            self.logbox.config(state="normal")
            self.logbox.insert("end", f"[{ts}] {msg}\n", tag)
            self.logbox.see("end")
            self.logbox.config(state="disabled")
        self.root.after(0, _w)

    def _set_prog(self, v):
        self.root.after(0, lambda: self.prog.config(value=v))

    def _set_stat(self, key, val):
        self.root.after(0, lambda: self.stats[key].config(text=str(val)))

    def _browse(self):
        p = filedialog.askopenfilename(filetypes=[("Text files","*.txt"),("All","*.*")])
        if p:
            self._url_file = p
            self.file_lbl.config(text=Path(p).name, fg="#a6e3a1")

    def _save_keys(self):
        self._gemini_key = self.gemini_ent.get().strip()
        self._openai_key = self.openai_ent.get().strip()
        self.log("✅ API keys saved for this session.", "success")

    def _get_urls(self):
        urls = []
        s = self.url_entry.get().strip()
        if s:
            urls.append(s)
        if self._url_file:
            try:
                with open(self._url_file, "r") as f:
                    for line in f:
                        u = line.strip()
                        if u.startswith("http"):
                            urls.append(u)
            except Exception as e:
                self.log(f"❌ File error: {e}", "error")
        return list(dict.fromkeys(urls))

    # ── Actions ───────────────────────────────────────────────────────────────
    def _start(self):
        urls = self._get_urls()
        if not urls:
            messagebox.showwarning("No URLs", "Enter a URL or load a .txt file of URLs.")
            return
        gk = self._gemini_key or self.gemini_ent.get().strip()
        ok = self._openai_key or self.openai_ent.get().strip()
        if not gk and not ok:
            messagebox.showwarning("No API Keys", "Enter at least one API key in the Settings tab.")
            return
        self.is_running = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._set_prog(0)
        threading.Thread(target=self._run, args=(urls, gk, ok), daemon=True).start()

    def _stop(self):
        self.is_running = False
        self.log("⏹ Stop requested — finishing current URL...", "warning")

    def _run(self, urls, gk, ok):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # Skip already-scraped
            if self.skip_var.get():
                orig = len(urls)
                urls = [u for u in urls if not self.db.is_url_scraped(u)]
                skipped = orig - len(urls)
                if skipped:
                    self.log(f"⏭ Skipped {skipped} already-scraped URL(s)", "warning")

            if not urls:
                self.log("ℹ️ All URLs already scraped. Add new URLs or uncheck skip.", "info")
                self._done()
                return

            self.log(f"🚀 Scraping {len(urls)} URL(s)...", "info")

            scraper = AsyncScraper(self.log, delay=self.delay_var.get())
            ai      = AIExtractor(gk, ok, self.log)
            ner     = NERProcessor(self.log)
            dedup   = Deduplicator(threshold=self.dedup_var.get())

            # ── Step 3-4: Async scrape ────────────────────────────────────────
            results = loop.run_until_complete(scraper.scrape_all(urls, self._set_prog))
            if not self.is_running:
                self._done(); return

            # ── Step 5: AI extract ────────────────────────────────────────────
            all_records = []
            fetched = {u: h for u, h in results.items() if h}
            self._set_stat("urls", len(fetched))
            total = len(fetched)

            for i, (url, html) in enumerate(fetched.items()):
                if not self.is_running:
                    break
                clean = scraper.clean_html(html)
                records = ai.extract(clean, url)
                for r in records:
                    r["source_url"] = url
                    r["scraped_at"] = datetime.now().isoformat()
                # ── Step 6: NER enhance ───────────────────────────────────────
                records = ner.enhance_batch(records, clean)
                all_records.extend(records)
                self.db.add_url(url)
                self._set_prog(50 + (i + 1) / total * 30)  # 50-80%

            self.log(f"📦 Raw records collected: {len(all_records)}", "info")
            self._set_stat("found", len(all_records))

            # ── Step 7: Deduplicate ───────────────────────────────────────────
            all_records, dups = dedup.deduplicate(all_records)
            self.log(f"🔁 Duplicates removed: {dups}", "warning" if dups else "info")
            self._set_stat("dups", dups)
            self._set_prog(85)

            # ── Step 8: Filters ───────────────────────────────────────────────
            active = [k for k, v in self.fvars.items() if v.get()]
            filtered = LeadFilter.apply(all_records, active)
            self.log(f"🔽 After field filters: {len(filtered)} records", "info")
            self._set_stat("filt", len(filtered))

            # ── Normalize ─────────────────────────────────────────────────────
            null_removed = 0
            if self.norm_var.get() == "normalize":
                before = len(filtered)
                filtered = LeadFilter.normalize(filtered)
                null_removed = before - len(filtered)
                self.log(f"🧹 Null records removed: {null_removed}", "warning" if null_removed else "info")
            self._set_stat("nulls", null_removed)
            self._set_stat("export", len(filtered))
            self._set_prog(95)

            # ── Step 9-10: Save ───────────────────────────────────────────────
            self.db.add_leads(filtered)
            self.current_leads = filtered
            total_db = len(self.db.get_all_leads())
            self._set_stat("total", total_db)
            self._set_prog(100)

            self.log(f"✅ Done! {len(filtered)} new leads saved. Total in DB: {total_db}", "success")
            self._refresh()

            if filtered:
                self.root.after(0, self._prompt_export)

        except Exception as e:
            self.log(f"❌ Fatal error: {str(e)}", "error")
        finally:
            self._done()

    def _done(self):
        self.is_running = False
        self.root.after(0, lambda: self.start_btn.config(state="normal"))
        self.root.after(0, lambda: self.stop_btn.config(state="disabled"))

    def _prompt_export(self):
        if messagebox.askyesno("Export", f"Scraping complete!\n\nExport {len(self.current_leads)} leads to CSV?"):
            self._export()

    def _export(self):
        leads = self.current_leads or self.db.get_all_leads()
        if not leads:
            messagebox.showinfo("No Data", "No leads to export yet.")
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=f"leads_{ts}.csv",
            filetypes=[("CSV files","*.csv")]
        )
        if path:
            n = CSVExporter.export(leads, path)
            self.log(f"📥 Exported {n} leads → {path}", "success")
            messagebox.showinfo("Exported", f"✅ {n} leads saved to:\n{path}")

    def _export_all(self):
        self.current_leads = self.db.get_all_leads()
        self._export()

    def _clear(self):
        if messagebox.askyesno("Clear Database", "⚠️ This will permanently delete ALL saved leads.\n\nAre you sure?"):
            self.db.clear()
            self.current_leads = []
            self._refresh()
            for v in self.stats.values():
                v.config(text="—")
            self.log("🗑 Database cleared.", "warning")

    def _refresh(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        leads = self.db.get_all_leads()
        for lead in reversed(leads[-1000:]):  # show last 1000
            self.tree.insert("", "end", values=(
                lead.get("name","")      or "",
                lead.get("email","")     or "",
                lead.get("company","")   or "",
                lead.get("job_title","") or "",
                lead.get("cellphone","") or "",
                lead.get("telephone","") or "",
                lead.get("linkedin","")  or "",
                lead.get("source_url","")or "",
            ))
        n = len(leads)
        self.count_lbl.config(text=f"{n:,} total leads in database")
        self._set_stat("total", n)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    root = tk.Tk()
    app = LeadHunterApp(root)
    root.mainloop()