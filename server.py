#!/usr/bin/env python3
"""Estudio de imágenes local: interfaz web para mflux (Z-Image Turbo 8-bit)."""
import http.server, json, os, re, shutil, subprocess, threading, time, random, urllib.parse, urllib.request, urllib.error, webbrowser

HOME = os.path.expanduser("~")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
GALLERY = os.environ.get("ESTUDIO_GALERIA") or os.path.join(APP_DIR, "galeria")
MODEL = os.environ.get("ESTUDIO_MODELO") or os.path.join(HOME, "mflux-models", "z-image-turbo-8bit")
MFLUX = (os.environ.get("ESTUDIO_MFLUX") or shutil.which("mflux-generate-z-image-turbo")
         or os.path.join(HOME, ".local", "bin", "mflux-generate-z-image-turbo"))
PORT = int(os.environ.get("ESTUDIO_PUERTO", "8777"))
os.makedirs(GALLERY, exist_ok=True)

STATE = {"busy": False, "step": 0, "steps": 9, "phase": "libre", "started": 0, "last": None, "error": None}
LOCK = threading.Lock()

def run_job(prompt, w, h, seed, steps):
    name = time.strftime("%Y%m%d_%H%M%S") + f"_{seed}"
    out = os.path.join(GALLERY, name + ".png")
    cmd = [MFLUX, "--model", MODEL, "--base-model", "z-image-turbo", "--steps", str(steps),
           "--width", str(w), "--height", str(h), "--seed", str(seed), "--prompt", prompt, "--output", out]
    STATE.update(step=0, steps=steps, phase="cargando modelo", started=time.time(), error=None)
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        buf = b""
        tail = ""
        while True:
            ch = p.stdout.read(256)
            if not ch:
                break
            buf += ch
            txt = buf.decode("utf-8", "ignore")
            tail = txt[-2000:]
            m = re.findall(r"(\d+)/(\d+) \[", txt[-400:])
            if m:
                s, t = map(int, m[-1])
                if t == steps:
                    STATE.update(step=s, phase="generando")
            buf = buf[-4000:]
        p.wait()
        if p.returncode == 0 and os.path.exists(out):
            meta = {"prompt": prompt, "width": w, "height": h, "seed": seed, "steps": steps,
                    "seconds": round(time.time() - STATE["started"]), "file": name + ".png"}
            json.dump(meta, open(os.path.join(GALLERY, name + ".json"), "w"), ensure_ascii=False)
            STATE.update(last=meta, phase="listo")
        else:
            STATE.update(phase="error", error=tail[-600:])
    except Exception as e:
        STATE.update(phase="error", error=str(e))
    finally:
        STATE["busy"] = False

def to_trash(fp):
    """Mueve a la Papelera de macOS (recuperable). Prueba varios caminos."""
    for cmd in (["/usr/bin/trash", fp],
                ["osascript", "-e", f'tell application "Finder" to delete (POSIX file "{fp}" as alias)']):
        if os.path.exists(cmd[0]) or cmd[0] == "osascript":
            try:
                if subprocess.run(cmd, capture_output=True, timeout=20).returncode == 0 and not os.path.exists(fp):
                    return True
            except Exception:
                pass
    try:
        dst = os.path.join(HOME, ".Trash", os.path.basename(fp))
        if os.path.exists(dst):
            dst = os.path.join(HOME, ".Trash", f"{time.time():.0f}_{os.path.basename(fp)}")
        os.rename(fp, dst)
        return True
    except Exception:
        return False

def gallery():
    items = []
    for f in sorted(os.listdir(GALLERY), reverse=True):
        if f.endswith(".json"):
            try:
                items.append(json.load(open(os.path.join(GALLERY, f))))
            except Exception:
                pass
    return items[:200]

OLLAMA = os.environ.get("ESTUDIO_OLLAMA", "http://127.0.0.1:11434")
LLM_PREF = ([os.environ["ESTUDIO_LLM"]] if os.environ.get("ESTUDIO_LLM") else []) + ["qwen3:8b", "qwen3:4b", "mistral:latest", "llama3.2:latest"]

SYS_ENHANCE = """You write prompts for Z-Image Turbo, a text-to-image model. The user describes an image idea, usually in Spanish. Reply with ONE image prompt in English and nothing else.
Rules:
- Keep the user's subject, scene, mood and every detail they asked for. Never drop or change them.
- Enrich it with concrete visual detail: framing and camera angle, lens or film look, lighting and time of day, materials and textures, colors, background, atmosphere.
- Write 70 to 130 words as flowing descriptive prose, one single paragraph, no lists.
- If the user wants specific words written inside the image (a title, a sign, a headline), keep those words exactly as the user wrote them, in their original language, inside double quotes, and say where and how they appear.
- Never add signs, labels, billboards, posters, captions or any written words unless the user explicitly asks for them. Do not invent brand names or real people either.
- If the user mentions scale or proportions, state them explicitly (for example, the height of the subject compared with cars, doors or people).
- No filler such as "8k, masterpiece, trending on ArtStation".
- No preface, no explanation, no markdown, no quotation marks around the whole prompt."""

SYS_TRANSLATE = """Translate the user's image description into natural, precise English for a text-to-image model. Keep the meaning and every detail and add nothing. If specific words must appear written inside the image, keep them exactly as written, inside double quotes. Reply with the translation only."""

def ollama_json(path, payload=None, timeout=10):
    req = urllib.request.Request(OLLAMA + path, headers={"Content-Type": "application/json"},
                                 data=None if payload is None else json.dumps(payload).encode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def ollama_models(wake=False):
    """Modelos instalados en Ollama; None si está apagado (con wake=True lo prende)."""
    def tags():
        return [m["name"] for m in ollama_json("/api/tags", timeout=3).get("models", []) if "embed" not in m["name"]]
    try:
        return tags()
    except Exception:
        if not wake:
            return None
    subprocess.Popen(["open", "-a", "Ollama"])
    for _ in range(20):
        time.sleep(1)
        try:
            return tags()
        except Exception:
            pass
    return None

def pick_model(models, want=None):
    if want in models:
        return want
    for p in LLM_PREF:
        if p in models:
            return p
    return models[0] if models else None

def enhance(text, mode, want):
    models = ollama_models(wake=True)
    if models is None:
        raise RuntimeError("No pude conectar con Ollama. Abrí la app Ollama y probá de nuevo.")
    model = pick_model(models, want)
    if not model:
        raise RuntimeError("Ollama no tiene modelos. En Terminal: ollama pull qwen3:8b")
    translate = mode == "translate"
    body = {"model": model, "stream": False, "think": False, "keep_alive": 0,
            "messages": [{"role": "system", "content": SYS_TRANSLATE if translate else SYS_ENHANCE},
                         {"role": "user", "content": text}],
            "options": {"temperature": 0.2 if translate else 0.6, "num_predict": 600}}
    t0 = time.time()
    try:
        out = ollama_json("/api/chat", body, timeout=240)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise RuntimeError(f"Ollama respondió con error {e.code}")
        body.pop("think")
        out = ollama_json("/api/chat", body, timeout=240)
    txt = (out.get("message") or {}).get("content", "")
    txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()
    if "<think>" in txt:
        raise RuntimeError("El modelo se quedó pensando sin responder. Probá otra vez o elegí otro modelo.")
    txt = re.sub(r"^(english prompt|prompt|translation)\s*:\s*", "", txt, flags=re.I).strip().strip("`").strip()
    if len(txt) > 2 and txt[0] == '"' and txt[-1] == '"' and txt.count('"') == 2:
        txt = txt[1:-1].strip()
    txt = re.sub(r"\s*\n\s*", " ", txt)
    if not txt:
        raise RuntimeError("El modelo no devolvió texto. Probá de nuevo.")
    return {"prompt": txt, "model": model, "seconds": round(time.time() - t0, 1)}

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            return self.send(200, open(os.path.join(APP_DIR, "index.html"), "rb").read(), "text/html; charset=utf-8")
        if u.path == "/api/status":
            s = dict(STATE)
            s["elapsed"] = round(time.time() - s["started"]) if s["busy"] else 0
            return self.send(200, s)
        if u.path == "/api/gallery":
            return self.send(200, gallery())
        if u.path.startswith("/img/"):
            f = os.path.basename(urllib.parse.unquote(u.path[5:]))
            fp = os.path.join(GALLERY, f)
            if f.endswith(".png") and os.path.exists(fp):
                return self.send(200, open(fp, "rb").read(), "image/png")
            return self.send(404, {"error": "no existe"})
        if u.path == "/api/open-folder":
            subprocess.Popen(["open", GALLERY])
            return self.send(200, {"ok": True})
        if u.path == "/api/reveal":
            f = os.path.basename(urllib.parse.parse_qs(u.query).get("f", [""])[0])
            subprocess.Popen(["open", "-R", os.path.join(GALLERY, f)])
            return self.send(200, {"ok": True})
        if u.path == "/api/llm":
            models = ollama_models()
            return self.send(200, {"up": models is not None, "models": models or [], "model": pick_model(models or [])})
        self.send(404, {"error": "no encontrado"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        d = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/delete":
            f = os.path.basename(d.get("file", ""))
            if not f.endswith(".png"):
                return self.send(400, {"error": "archivo inválido"})
            moved = []
            for name in (f, f[:-4] + ".json"):
                fp = os.path.join(GALLERY, name)
                if os.path.exists(fp):
                    if not to_trash(fp):
                        return self.send(500, {"error": "No pude mover a la papelera: " + name})
                    moved.append(name)
            return self.send(200, {"ok": True, "moved": moved})
        if self.path == "/api/enhance":
            text = (d.get("text") or "").strip()
            if not text:
                return self.send(400, {"error": "Escribí una descripción primero."})
            try:
                return self.send(200, enhance(text[:2000], d.get("mode"), d.get("model")))
            except Exception as e:
                return self.send(502, {"error": str(e)})
        if self.path != "/api/generate":
            return self.send(404, {"error": "no encontrado"})
        prompt = (d.get("prompt") or "").strip()
        if not prompt:
            return self.send(400, {"error": "Escribí una descripción."})
        if not os.path.isdir(MODEL):
            return self.send(400, {"error": f"No encuentro el modelo en {MODEL}. Seguí el paso 2 del README o definí ESTUDIO_MODELO."})
        w, h = int(d.get("width", 1024)), int(d.get("height", 1024))
        steps = max(4, min(12, int(d.get("steps", 9))))
        seed = d.get("seed")
        seed = int(seed) if str(seed or "").strip().isdigit() else random.randint(1, 2**31 - 1)
        with LOCK:
            if STATE["busy"]:
                return self.send(409, {"error": "Ya hay una imagen generándose."})
            STATE["busy"] = True
        threading.Thread(target=run_job, args=(prompt, w, h, seed, steps), daemon=True).start()
        self.send(200, {"ok": True, "seed": seed})

if __name__ == "__main__":
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = f"http://localhost:{PORT}"
    print(f"Estudio listo en {url}  (cerrá esta ventana para apagarlo)")
    if not os.environ.get("ESTUDIO_NO_ABRIR"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    srv.serve_forever()
