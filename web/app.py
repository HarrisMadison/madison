"""Web UI — Flask app with search, email, templates, sessions, admin, and query tracking."""
from __future__ import annotations

import os, re, sys, smtplib, uuid, hashlib, time
from datetime import timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path: sys.path.insert(0, str(REPO_ROOT))

from flask import Flask, jsonify, render_template, request as req, send_file
from core import load_config, storage_client
from core.config import REPO_ROOT as ROOT
from vertex.search import search as do_search
from vertex.answer import answer as do_answer
from web.admin import track_query

app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
app.secret_key = os.urandom(24)
_cfg = None
TEMPLATE_DIR = ROOT / "templates"
OUTPUT_DIR = ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
_admin_tokens = {}

def cfg():
    global _cfg
    if _cfg is None: _cfg = load_config()
    return _cfg
def _admin_pw(c): return str(c.raw.get("admin", {}).get("password", "0714"))
def _check_admin(c):
    t = req.headers.get("X-Admin-Token", "")
    if t in _admin_tokens and time.time() - _admin_tokens[t] < 86400: return True
    return False
def _signed_url(c, uri, hours=24):
    parts = uri.replace("gs://","").split("/",1)
    if len(parts)!=2: return None
    return storage_client(c).bucket(parts[0]).blob(parts[1]).generate_signed_url(version="v4",expiration=timedelta(hours=hours),method="GET")
def _send_smtp(c, to, subject, body, attachment_path=None):
    ec=c.raw.get("email",{}) or {}; sender,pw=ec.get("sender",""),ec.get("app_password","")
    if not sender or not pw: return "Email not configured."
    msg=MIMEMultipart(); msg["From"],msg["To"],msg["Subject"]=sender,to,subject; msg.attach(MIMEText(body,"plain"))
    if attachment_path and Path(attachment_path).exists():
        with open(attachment_path,"rb") as f: part=MIMEBase("application","octet-stream"); part.set_payload(f.read())
        encoders.encode_base64(part); part.add_header("Content-Disposition",f"attachment; filename={Path(attachment_path).name}"); msg.attach(part)
    try:
        with smtplib.SMTP(ec.get("smtp_host","smtp.gmail.com"),int(ec.get("smtp_port",587)),timeout=30) as s: s.starttls(); s.login(sender,pw); s.send_message(msg)
        return None
    except Exception as e: return str(e)
def _company_info(c):
    co=c.raw.get("company",{}) or {}; logo=co.get("logo","")
    if logo and not Path(logo).is_absolute(): logo=str(ROOT/logo)
    return {"company":co.get("name",""),"phone":co.get("phone",""),"email":co.get("email",""),
            "address":co.get("address",""),"website":co.get("website",""),"tagline":co.get("tagline",""),"logo":logo}

# ── Pages ──
@app.route("/")
def index():
    c=cfg(); props=c.properties or []; ec=c.raw.get("email",{}) or {}; co=c.raw.get("company",{}) or {}
    doc_types=sorted({v for v in c.category_folders.values()}); doc_types+=[t for t in ["email","document"] if t not in doc_types]
    logo_file=co.get("logo","")
    return render_template("index.html",company=co.get("name") or c.raw.get("data_store",{}).get("display_name","SMB Search"),
        properties=props,doc_types=doc_types,email_enabled=bool(ec.get("sender") and ec.get("app_password")),
        logo_url=f"/assets/{Path(logo_file).name}" if logo_file else "")

@app.route("/assets/<path:filename>")
def serve_asset(filename):
    p=ROOT/"assets"/filename; return send_file(str(p)) if p.exists() else ("",404)

# ── Search/Answer (with query tracking) ──
@app.route("/api/query", methods=["POST"])
def api_query():
    data=req.get_json(force=True); query=data.get("query","").strip()
    if not query: return jsonify({"error":"query required"}),400
    prop,dtype,session=data.get("property") or None,data.get("doc_type") or None,data.get("session") or None
    c=cfg()
    track_query()  # Count every query for admin dashboard.
    try:
        a=do_answer(c,query,property_=prop,doc_type=dtype,session=session)
        sources=[{"reference_id":s.get("reference_id",""),"title":s.get("title",""),"property":s.get("property",""),
                  "category":s.get("category",""),"doc_type":s.get("doc_type",""),"uri":s.get("uri","")} for s in a.sources]
        if (not a.text or "could not be generated" in a.text.lower()) or not sources:
            hits=do_search(c,query,property_=prop,doc_type=dtype,page_size=10); track_query()
            fb=[{"reference_id":str(h.rank),"title":h.filename,"property":h.property,"category":h.doc_type,"uri":h.uri} for h in hits]
            if not sources: sources=fb
            text=a.text if a.text and "could not be generated" not in a.text.lower() else "Found documents but couldn't generate a summary."
        else: text=a.text
        return jsonify({"text":text,"sources":sources,"citations":a.citations,"session":a.session})
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/download")
def api_download():
    uri=req.args.get("uri","")
    if not uri.startswith("gs://"): return jsonify({"error":"invalid"}),400
    try: return jsonify({"url":_signed_url(cfg(),uri,1)})
    except Exception as e: return jsonify({"error":str(e)}),500

# ── /api/document — direct test of the named-document fetcher ──
# Lets you curl-test the new path-B fetcher without going through Gemini.
# Usage: GET /api/document?name=<filename or partial name>[&meta=1]
#   meta=1 returns metadata only (title, uri, char count, candidates)
#         instead of the full text. Useful when the doc is huge.
@app.route("/api/document")
def api_document():
    name = req.args.get("name", "").strip()
    meta_only = req.args.get("meta") in ("1", "true", "yes")
    if not name:
        return jsonify({"error": "name parameter is required"}), 400
    try:
        from vertex.document_fetch import get_document_by_name
    except Exception as e:
        return jsonify({"error": f"fetcher import failed: {e}"}), 500
    result = get_document_by_name(name, cfg=cfg())
    if meta_only:
        return jsonify({
            "ok":         result["ok"],
            "title":      result.get("title", ""),
            "uri":        result.get("uri", ""),
            "char_count": len(result.get("text") or ""),
            "candidates": result.get("candidates", []),
            "error":      result.get("error"),
        })
    return jsonify(result)

@app.route("/api/email", methods=["POST"])
def api_email():
    data=req.get_json(force=True); to,docs=data.get("to","").strip(),data.get("docs",[])
    if not to: return jsonify({"error":"Email required"}),400
    c=cfg(); lines=[]
    for d in docs:
        uri,title=d.get("uri",""),d.get("title","")
        if uri.startswith("gs://"):
            try: lines.append("- "+title+"\n  "+_signed_url(c,uri,24))
            except: pass
    if not lines: return jsonify({"error":"No links"}),500
    err=_send_smtp(c,to,f"{len(lines)} documents","\n\n".join(lines)+"\n\nLinks expire 24h.\n")
    return jsonify({"error":err}) if err else jsonify({"sent":True,"to":to,"doc_count":len(lines)})

# ── Templates ──
@app.route("/api/templates")
def api_templates():
    tpls=[]
    for f in sorted(TEMPLATE_DIR.glob("*.md")):
        text=f.read_text(encoding="utf-8"); phs=set(re.findall(r"\{\{(.+?)\}\}",text))
        title=f.stem.replace("-"," ").title()
        for line in text.split("\n"):
            if line.startswith("# "): title=line[2:].strip(); break
        tpls.append({"id":f.stem,"title":title,"fields":len(phs)})
    return jsonify({"templates":tpls})

@app.route("/api/draft/fill", methods=["POST"])
def api_draft_fill():
    data=req.get_json(force=True); tpl_id=data.get("template","").strip(); prop=data.get("property") or None
    if not tpl_id: return jsonify({"error":"template required"}),400
    tpl_path=TEMPLATE_DIR/f"{tpl_id}.md"
    if not tpl_path.exists(): return jsonify({"error":"Not found"}),404
    c=cfg()
    if not prop: prop=c.default_property
    from drafting.engine import DraftingEngine,load_query_map
    qm=load_query_map(TEMPLATE_DIR/"queries.yaml"); text=tpl_path.read_text(encoding="utf-8")
    engine=DraftingEngine(c,property_=prop,delay=4.0,log=lambda x:None)
    try: filled,results=engine.fill(text,qm)
    except Exception as e: return jsonify({"error":str(e)}),500
    fields=[{"name":r.placeholder,"answer":r.answer,"ok":r.success} for r in results]
    return jsonify({"fields":fields,"resolved":sum(1 for r in results if r.success),"total":len(results),"template_id":tpl_id,"property":prop or ""})

@app.route("/api/draft/pdf", methods=["POST"])
def api_draft_pdf():
    data=req.get_json(force=True); tpl_id=data.get("template","").strip()
    fields=data.get("fields",{}); to_email=data.get("email","").strip(); prop=data.get("property","")
    if not tpl_id: return jsonify({"error":"template required"}),400
    tpl_path=TEMPLATE_DIR/f"{tpl_id}.md"
    if not tpl_path.exists(): return jsonify({"error":"Not found"}),404
    text=tpl_path.read_text(encoding="utf-8")
    for name,value in fields.items(): text=text.replace("{{"+name+"}}",value)
    text=re.sub(r"\{\{(.+?)\}\}",r"[UNANSWERED: \1]",text)
    c=cfg(); brand=_company_info(c)
    from drafting.writer import write_pdf
    fid=uuid.uuid4().hex[:8]; pdf_name=f"{tpl_id}-{fid}.pdf"; pdf_path=OUTPUT_DIR/pdf_name
    try: write_pdf(text,pdf_path,title=tpl_id.replace("-"," ").title(),company=brand["company"],phone=brand["phone"],
                   email=brand["email"],address=brand["address"],website=brand["website"],tagline=brand["tagline"],
                   logo_path=brand["logo"],property_name=prop)
    except Exception as e: return jsonify({"error":f"PDF failed: {e}"}),500
    resp={"pdf":pdf_name,"download_url":f"/api/draft/download/{pdf_name}"}
    if to_email:
        err=_send_smtp(c,to_email,f"{tpl_id.replace('-',' ').title()} - {prop or ''}","Report attached.\n",str(pdf_path))
        if err: resp["email_error"]=err
        else: resp["emailed_to"]=to_email
    return jsonify(resp)

@app.route("/api/draft/download/<filename>")
def api_draft_download(filename):
    p=OUTPUT_DIR/filename; return send_file(str(p),as_attachment=True,download_name=filename) if p.exists() else (jsonify({"error":"Not found"}),404)

# ── Admin ──
@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    data=req.get_json(force=True); pw=data.get("password",""); c=cfg()
    if str(pw)==_admin_pw(c):
        token=hashlib.sha256(os.urandom(32)).hexdigest(); _admin_tokens[token]=time.time()
        return jsonify({"token":token})
    return jsonify({"error":"Invalid password"}),401

@app.route("/api/admin/stats")
def admin_stats():
    c=cfg()
    if not _check_admin(c): return jsonify({"error":"Unauthorized"}),401
    from web.admin import get_usage_stats
    return jsonify(get_usage_stats(c))

def create_app(): return app

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000)); print(f"\n  Web UI: http://localhost:{port}\n")
    app.run(host="0.0.0.0",port=port,debug=True)
