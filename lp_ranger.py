#!/usr/bin/env python3
"""LP Ranger v4 — Unified GUI: Dashboard + Config + Terminal tabs"""

import gi
gi.require_version('Gtk', '3.0')
try:
    gi.require_version('AyatanaAppIndicator3', '0.1')
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
except (ValueError, ImportError):
    try:
        gi.require_version('AppIndicator3', '0.1')
        from gi.repository import AppIndicator3
    except:
        AppIndicator3 = None

from gi.repository import Gtk, GLib, Gdk, Pango
import json, os, sys, math, time, threading, subprocess, re
from datetime import datetime
from pathlib import Path
import urllib.request

APP_DIR = Path(__file__).parent.resolve()
ICONS_DIR = APP_DIR / "icons"
DATA_DIR = Path.home() / ".local" / "share" / "lp-ranger"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
HISTORY_FILE = DATA_DIR / "history.json"
STATS_FILE = DATA_DIR / "stats.json"
DEFAULT_STRAT = APP_DIR / "strategy_exit_pool.json"
if not DEFAULT_STRAT.exists(): DEFAULT_STRAT = APP_DIR / "strategy_v1.json"
BASE_RPC = "https://mainnet.base.org"
POSITION_MANAGER = "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1"
COINGECKO = "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd&include_24hr_change=true"
BINANCE = "https://api.binance.com/api/v3/ticker/24hr?symbol=ETHUSDC"
COOLDOWN_H = 4

# ── helpers ──
class Cfg:
    def __init__(self):
        self.c = {"position_id":"","strategy_file":str(DEFAULT_STRAT),"notifications":True,"range_lo":0,"range_hi":0}
        if CONFIG_FILE.exists():
            try: self.c.update(json.load(open(CONFIG_FILE)))
            except: pass
    def save(self): json.dump(self.c,open(CONFIG_FILE,"w"),indent=2)
    def get(self,k,d=None): return self.c.get(k,d)
    def set(self,k,v): self.c[k]=v; self.save()

class Hist:
    def __init__(self):
        self.h=[]
        if HISTORY_FILE.exists():
            try: self.h=json.load(open(HISTORY_FILE))
            except: pass
    def log(self,t,m,**kw):
        self.h.append({"ts":datetime.now().isoformat()[:16],"type":t,"msg":m,**kw})
        self.h=self.h[-500:]; json.dump(self.h,open(HISTORY_FILE,"w"),indent=2)
    def recent(self,n=20): return self.h[-n:]

class Stats:
    def __init__(self):
        self.d={"total_fees":0,"fees_today":0,"fees_today_date":"","total_il":0,"il_segments":[],"pool_active":True,"hold_asset":None}
        if STATS_FILE.exists():
            try: self.d.update(json.load(open(STATS_FILE)))
            except: pass
    def save(self): json.dump(self.d,open(STATS_FILE,"w"),indent=2)
    def get(self,k,d=None): return self.d.get(k,d)
    def set(self,k,v): self.d[k]=v; self.save()
    def add_fees(self,a):
        td=datetime.now().strftime("%Y-%m-%d")
        if self.d["fees_today_date"]!=td: self.d["fees_today"]=0; self.d["fees_today_date"]=td
        self.d["fees_today"]+=a; self.d["total_fees"]+=a; self.save()
    def record_il(self,olo,ohi,nlo,nhi,p):
        il=0
        if olo>0 and ohi>0:
            e=(olo+ohi)/2;r=p/e if e>0 else 1;sq=math.sqrt(abs(r)) if r>0 else 1
            s=abs(2*sq/(1+r)-1);w=(ohi-olo)/e*100 if e>0 else 15
            c=min(s*math.sqrt(100/max(w,5)),0.10);il=119.50*c
            self.d["total_il"]+=il; self.d["il_segments"].append({"date":datetime.now().isoformat()[:16],
            "old":f"${olo:.0f}-${ohi:.0f}","new":f"${nlo:.0f}-${nhi:.0f}" if nlo else "cerrada",
            "price":round(p,0),"il_pct":round(c*100,2),"il_usd":round(il,2)})
            self.d["il_segments"]=self.d["il_segments"][-100:]
        self.save(); return il

class Fetcher:
    def __init__(self): self.price=0;self.change=0;self.pos=None;self.error=None
    def fetch_price(self):
        try:
            r=urllib.request.urlopen(urllib.request.Request(COINGECKO,headers={"Accept":"application/json","User-Agent":"LPR/4"}),timeout=10)
            d=json.loads(r.read());self.price=d["ethereum"]["usd"];self.change=d["ethereum"].get("usd_24h_change",0);self.error=None;return True
        except:
            try:
                r=urllib.request.urlopen(urllib.request.Request(BINANCE,headers={"User-Agent":"LPR/4"}),timeout=10)
                d=json.loads(r.read());self.price=float(d["lastPrice"]);self.change=float(d["priceChangePercent"]);self.error=None;return True
            except Exception as e: self.error=str(e);return False
    def fetch_position(self,pid):
        if not pid: self.error="No ID";return False
        try:
            tid=hex(int(pid))[2:].zfill(64)
            pl=json.dumps({"jsonrpc":"2.0","method":"eth_call","params":[{"to":POSITION_MANAGER,"data":"0x99fbab88"+tid},"latest"],"id":1}).encode()
            r=urllib.request.urlopen(urllib.request.Request(BASE_RPC,data=pl,headers={"Content-Type":"application/json","User-Agent":"LPR/4"}),timeout=15)
            res=json.loads(r.read())
            if "result" in res and len(res["result"])>10:
                raw=res["result"][2:];flds=[raw[i*64:(i+1)*64] for i in range(12)]
                tl=int(flds[5],16);tu=int(flds[6],16)
                if tl>=2**255:tl-=2**256
                if tu>=2**255:tu-=2**256
                plo=1.0001**tl*1e12;phi=1.0001**tu*1e12
                if plo>phi:plo,phi=phi,plo
                self.pos={"lo":round(plo,2),"hi":round(phi,2),"fee":int(flds[4],16),"liq":int(flds[7],16)}
                self.error=None;return True
            self.error="Posicion no encontrada";return False
        except Exception as e: self.error=str(e);return False

class Strat:
    def __init__(self,path=None):
        self.cfg={};self.prices=[];self.load(path or DEFAULT_STRAT)
    def load(self,p):
        p=Path(p)
        if p.exists():self.cfg=json.load(open(p))
        else:self.cfg={"name":"Default","strategy_type":"exit_pool","parameters":{"base_width_pct":15,"trend_shift":0.4,"buffer_pct":5,"exit_trend_pct":10,"enter_trend_pct":2}}
    def add(self,p):self.prices.append((time.time(),p));self.prices=[(t,v) for t,v in self.prices if t>time.time()-60*86400]
    def _ema(self,per):
        ps=[p for _,p in self.prices]
        if len(ps)<2:return ps[-1] if ps else 0
        k=2/(per+1);e=ps[0]
        for p in ps[1:]:e=p*k+e*(1-k)
        return e
    def _atr(self,per=14):
        ps=[p for _,p in self.prices]
        if len(ps)<2:return 0
        ts=[abs(ps[i]-ps[i-1]) for i in range(1,len(ps))];k=2/(per+1);a=ts[0]
        for t in ts[1:]:a=t*k+a*(1-k)
        return a
    def _rsi(self,per=14):
        ps=[p for _,p in self.prices]
        if len(ps)<per+1:return 50
        ds=[ps[i]-ps[i-1] for i in range(1,len(ps))];g=[max(d,0) for d in ds];l=[max(-d,0) for d in ds]
        k=2/(per+1);ag=g[0];al=l[0]
        for i in range(1,len(g)):ag=g[i]*k+ag*(1-k);al=l[i]*k+al*(1-k)
        if al==0:return 100
        return 100-(100/(1+ag/al))
    def evaluate(self,price,rlo,rhi,pool_active=True,hold=None):
        if price<=0:return "gray",None,{"message":"Esperando datos..."}
        p=self.cfg.get("parameters",{});ic=self.cfg.get("data_sources",{}).get("indicators",{})
        st=self.cfg.get("strategy_type","exit_pool")
        ef=self._ema(ic.get("ema_fast",20));es=self._ema(ic.get("ema_slow",50))
        atr=self._atr(ic.get("atr_period",14));rsi=self._rsi(ic.get("rsi_period",14))
        vp=atr/price*100 if price>0 else 0;tu=ef>es;tp=(ef-es)/es*100 if es>0 else 0
        det={"in_range":False,"price":round(price,2),"ema_fast":round(ef,2),"ema_slow":round(es,2),
             "trend":"alcista" if tu else "bajista","trend_pct":round(tp,1),"volatility_pct":round(vp,2),
             "rsi":round(rsi,1),"atr":round(atr,2),"dist_lo_pct":0,"dist_hi_pct":0,"edge_dist_pct":0}
        if not pool_active:
            h=hold or "USDC";det["message"]=f"Pool cerrada. Holding {h}."
            nt=p.get("enter_trend_pct",2)
            if abs(tp)<nt and len(self.prices)>30:
                bw=p.get("base_width_pct",15);hw=bw/200;ts2=p.get("trend_shift",0.4)
                sh=hw*ts2*min(abs(tp)/100*8,1);nc=price*(1+sh) if tu else price*(1-sh)
                return f"closed_{h.lower()}",{"type":"enter_pool","lo":round(nc*(1-bw/200),2),"hi":round(nc*(1+bw/200),2),"width_pct":bw,"reason":f"Lateralizacion ({tp:+.1f}%). Re-entrar."},det
            if h=="ETH" and rsi<35:bw=p.get("base_width_pct",15);return f"closed_eth",{"type":"enter_pool","lo":round(price*(1-bw/200),2),"hi":round(price*(1+bw/200),2),"width_pct":bw,"reason":f"RSI {rsi:.0f}"},det
            if h=="USDC" and rsi>65:bw=p.get("base_width_pct",15);return f"closed_usdc",{"type":"enter_pool","lo":round(price*(1-bw/200),2),"hi":round(price*(1+bw/200),2),"width_pct":bw,"reason":f"RSI {rsi:.0f}"},det
            return f"closed_{h.lower()}",None,det
        if rlo<=0 or rhi<=0:det["message"]="Configura tu posicion.";return "gray",None,det
        inr=rlo<=price<=rhi;det["in_range"]=inr;rw=rhi-rlo
        if inr and rw>0:
            det["dist_lo_pct"]=round((price-rlo)/price*100,1);det["dist_hi_pct"]=round((rhi-price)/price*100,1)
            det["edge_dist_pct"]=round(min(price-rlo,rhi-price)/rw*100,1)
        else:
            det["dist_lo_pct"]=round((price-rlo)/price*100,1) if price>0 else 0
            det["dist_hi_pct"]=round((rhi-price)/price*100,1) if price>0 else 0
        if st=="exit_pool" and abs(tp)>p.get("exit_trend_pct",10) and len(self.prices)>30:
            h="ETH" if tp>0 else "USDC";r=f"Tendencia fuerte ({tp:+.1f}%). Cerrar pool, hold {h}."
            det["message"]=r;return f"exit_{h.lower()}",{"type":"exit_pool","hold":h,"reason":r},det
        buf=p.get("buffer_pct",5)/100
        if not inr and (price<rlo*(1-buf) or price>rhi*(1+buf)):
            bw=p.get("base_width_pct",15);ts2=p.get("trend_shift",0.4);hw=bw/200
            sh=hw*ts2*min(abs(tp)/100*8,1);nc=price*(1+sh) if tu else price*(1-sh)
            det["message"]="Fuera de rango. Rebalanceo necesario."
            return "red",{"type":"rebalance","lo":round(nc*(1-bw/200),2),"hi":round(nc*(1+bw/200),2),"width_pct":bw,"reason":f"Buffer superado. Tendencia {det['trend']}."},det
        if not inr:det["message"]=f"Fuera pero dentro del buffer.";return "yellow",None,det
        if det.get("edge_dist_pct",100)<5:det["message"]=f"Cerca del borde ({det['edge_dist_pct']:.0f}%).";return "yellow",None,det
        det["message"]="En rango. Posicion estable.";return "green",None,det

def parse_pid(t):
    t=t.strip()
    if "revert.finance" in t:
        for p in reversed(t.rstrip("/").split("/")):
            if p.isdigit():return p
    if t.isdigit():return t
    m=re.search(r"\d{4,}",t)
    return m.group(0) if m else t


# ── CSS ──
CSS = b"""
window { background-color: #0b0f19; }
notebook > header { background-color: #0f1420; }
notebook > header > tabs > tab { padding: 8px 18px; color: #5a6577; border: none; background: transparent; }
notebook > header > tabs > tab:checked { color: #34d399; border-bottom: 2px solid #34d399; background: rgba(52,211,153,0.05); }
.card { background-color: #111827; border-radius: 10px; padding: 14px; }
.ml { font-size: 10px; color: #4b5563; } .mv { font-size: 18px; font-weight: bold; color: #e0e6ed; }
.mv-sm { font-size: 14px; font-weight: bold; color: #e0e6ed; }
.title { font-size: 20px; font-weight: bold; color: #e0e6ed; }
.sub { font-size: 11px; color: #5a6577; } .stitle { font-size: 13px; font-weight: bold; color: #9ca3af; }
.green{color:#34d399} .yellow{color:#fbbf24} .red{color:#f87171} .blue{color:#60a5fa}
.btn-g{background:#065f46;color:#34d399;border:1px solid #059669;border-radius:8px;padding:10px;font-weight:bold}
.btn-g:hover{background:#047857}
.btn-r{background:#7f1d1d;color:#fca5a5;border:1px solid #dc2626;border-radius:8px;padding:10px;font-weight:bold}
.btn-r:hover{background:#991b1b}
.btn-b{background:#1e3a5f;color:#93c5fd;border:1px solid #3b82f6;border-radius:8px;padding:10px;font-weight:bold}
.btn-b:hover{background:#1e40af}
.btn-dim{background:#1f2937;color:#6b7280;border:1px solid #374151;border-radius:8px;padding:10px}
.btn-dim:hover{background:#374151;color:#9ca3af}
.entry{background:#0b0f19;color:#e0e6ed;border:1px solid #374151;border-radius:8px;padding:8px}
.term-view{background:#000000;color:#00ff41;font-size:11px;padding:8px}
.term-entry{background:#0a0a0a;color:#00ff41;border:1px solid #1a3a1a;border-radius:6px;padding:6px 10px;font-size:12px}
.rec-box{background:#1c1917;border:1px solid #92400e;border-radius:8px;padding:12px}
.exit-box{background:#1a0f0f;border:1px solid #991b1b;border-radius:8px;padding:12px}
.enter-box{background:#0f1a0f;border:1px solid #166534;border-radius:8px;padding:12px}
"""

# ── Terminal Widget ──
class Term(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self._cached_pw = None  # Password cached in memory for session
        self.buf = Gtk.TextBuffer()
        self.tv = Gtk.TextView(buffer=self.buf)
        self.tv.set_editable(False); self.tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR); self.tv.set_cursor_visible(False)
        self.tv.get_style_context().add_class("term-view")
        self.tv.override_font(Pango.FontDescription("monospace 11"))
        sc = Gtk.ScrolledWindow(); sc.set_vexpand(True); sc.set_hexpand(True); sc.add(self.tv)
        self.pack_start(sc, True, True, 0)
        for tag,color in [("green","#34d399"),("red","#f87171"),("yellow","#fbbf24"),("blue","#60a5fa"),("dim","#4b5563")]:
            self.buf.create_tag(tag, foreground=color)
        eb = Gtk.Box(spacing=6)
        eb.set_margin_top(4);eb.set_margin_bottom(4);eb.set_margin_start(4);eb.set_margin_end(4)
        l=Gtk.Label(label="$");l.override_color(Gtk.StateFlags.NORMAL,Gdk.RGBA(0,1,0.25,1))
        eb.pack_start(l,False,False,4)
        self.ent = Gtk.Entry()
        self.ent.set_placeholder_text("Comando: status, rebalance --dry-run, exit --hold ETH, help")
        self.ent.get_style_context().add_class("term-entry"); self.ent.connect("activate",self._cmd)
        eb.pack_start(self.ent,True,True,0)
        btn=Gtk.Button(label="Ejecutar");btn.get_style_context().add_class("btn-g");btn.connect("clicked",self._cmd)
        eb.pack_start(btn,False,False,0)
        self.pack_start(eb,False,False,0)

    def w(self,text,tag=None):
        end=self.buf.get_end_iter()
        if tag:self.buf.insert_with_tags_by_name(end,text,tag)
        else:self.buf.insert(end,text)
        self.tv.scroll_to_iter(self.buf.get_end_iter(),0,False,0,0)

    def wl(self,text,tag=None):
        self.w(f"[{datetime.now().strftime('%H:%M:%S')}] ","dim"); self.w(text+"\n",tag)

    def _cmd(self,*a):
        cmd=self.ent.get_text().strip()
        if not cmd:return
        self.ent.set_text("");self.w(f"$ {cmd}\n","green")
        # Commands that need password
        needs_pw = cmd.split()[0].lower() in ("status","rebalance","exit","enter","setup")
        if needs_pw and not self._cached_pw:
            # Ask password via GTK dialog (hidden input)
            GLib.idle_add(self._ask_password, cmd)
        else:
            threading.Thread(target=self._exec,args=(cmd,),daemon=True).start()

    def run_cmd(self, cmd):
        """Public method for buttons — goes through password dialog flow."""
        self.w(f"$ {cmd}\n","green")
        needs_pw = cmd.split()[0].lower() in ("status","rebalance","exit","enter")
        if needs_pw and not self._cached_pw:
            GLib.idle_add(self._ask_password, cmd)
        else:
            threading.Thread(target=self._exec,args=(cmd,),daemon=True).start()

    def _ask_password(self, pending_cmd):
        """Show a GTK password dialog — input is hidden with dots."""
        dialog = Gtk.Dialog(title="AutoBot — Password", parent=self.get_toplevel(), flags=Gtk.DialogFlags.MODAL)
        dialog.set_default_size(350, -1)
        dialog.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Desbloquear", Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_margin_top(16); box.set_margin_bottom(8); box.set_margin_start(16); box.set_margin_end(16)
        box.set_spacing(8)
        lbl = Gtk.Label(label="Introduce el password de tu wallet cifrada:")
        lbl.set_halign(Gtk.Align.START)
        box.add(lbl)
        pw_entry = Gtk.Entry()
        pw_entry.set_visibility(False)  # Hidden with dots
        pw_entry.set_invisible_char('●')
        pw_entry.set_placeholder_text("Password")
        pw_entry.set_activates_default(True)
        box.add(pw_entry)
        remember = Gtk.CheckButton(label="Recordar durante esta sesión")
        remember.set_active(True)
        box.add(remember)
        hint = Gtk.Label(label="El password solo se guarda en memoria. Al cerrar la app se borra.")
        hint.set_halign(Gtk.Align.START)
        hint.get_style_context().add_class("sub")
        box.add(hint)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.show_all()
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            pw = pw_entry.get_text()
            if remember.get_active():
                self._cached_pw = pw
            else:
                self._cached_pw = None
            dialog.destroy()
            self.wl("Password OK — ejecutando...","blue")
            # Now run the command with the password
            threading.Thread(target=self._exec, args=(pending_cmd, pw), daemon=True).start()
        else:
            dialog.destroy()
            self.wl("Cancelado","yellow")

    def _exec(self, cmd, password=None):
        parts=cmd.split();verb=parts[0].lower() if parts else ""
        autobot=str(APP_DIR/"lp_autobot.py");pid=self.app.config.get("position_id","")
        pw = password or self._cached_pw or ""
        try:
            if verb=="help":
                GLib.idle_add(self.wl,"Comandos disponibles:","green")
                for c,d in [("status","Ver balances y posicion"),("rebalance --price-lower X --price-upper Y","Cambiar rango"),
                    ("rebalance --price-lower X --price-upper Y --dry-run","Simular cambio"),
                    ("exit --hold ETH","Cerrar pool, quedarse en ETH"),("exit --hold USDC","Cerrar pool, quedarse en USDC"),
                    ("enter --price-lower X --price-upper Y","Crear nueva pool"),
                    ("setup","Configurar wallet (cifrar clave privada)"),
                    ("unlock","Introducir password"),("forget","Borrar password de memoria"),
                    ("reset-fees","Resetear fees e IL a 0"),
                    ("clear","Limpiar terminal"),("help","Esta ayuda")]:
                    GLib.idle_add(self.w,f"  {c:55s}","green"); GLib.idle_add(self.w,f" {d}\n","dim")
                return
            if verb=="clear": GLib.idle_add(self.buf.set_text,"");return
            if verb=="unlock":
                self._cached_pw = None
                GLib.idle_add(self._ask_password, "status")
                return
            if verb=="forget":
                self._cached_pw = None
                GLib.idle_add(self.wl,"Password borrado de memoria","yellow")
                return
            if verb in ("reset-fees","resetfees","reset"):
                self.app.stats.d["total_fees"]=0;self.app.stats.d["fees_today"]=0;self.app.stats.d["total_il"]=0
                self.app.stats.save()
                GLib.idle_add(self.wl,"Fees e IL reseteados a $0","green")
                GLib.idle_add(self.app.force_update)
                return
            if verb=="setup":
                full=[sys.executable,autobot,"--setup"]
                GLib.idle_add(self.wl,"Setup requiere terminal real. Ejecuta en una terminal:","yellow")
                GLib.idle_add(self.wl,"  lp-autobot --setup","green")
                return
            if verb=="status":full=[sys.executable,autobot,"--status","-p",pid,"-y"]
            elif verb=="rebalance":full=[sys.executable,autobot,"--rebalance","-p",pid,"-y"]+parts[1:]
            elif verb=="exit":full=[sys.executable,autobot,"--exit","-p",pid,"-y"]+parts[1:]
            elif verb=="enter":full=[sys.executable,autobot,"--enter","-y"]+parts[1:]
            else:GLib.idle_add(self.wl,f"Comando no reconocido: {verb}. Escribe help","red");return
            GLib.idle_add(self.wl,"Ejecutando...","blue")
            # Send password + auto-confirm via stdin
            stdin_data = f"{pw}\n"
            r=subprocess.run(full,capture_output=True,text=True,timeout=600,input=stdin_data,cwd=str(APP_DIR))
            for line in (r.stdout+r.stderr).split("\n"):
                if not line.strip(): continue
                # Filter out noise warnings
                skip = any(x in line for x in ["GetPassWarning","fallback_getpass","Password input may be echoed","Unlock password","Can not control echo"])
                if skip: continue
                # Color code output
                tag = None
                ll = line.lower()
                if "error" in ll or "failed" in ll: tag = "red"
                elif "success" in ll: tag = "green"
                elif "warning" in ll: tag = "yellow"
                GLib.idle_add(self.wl,line,tag)
            if r.returncode != 0 and "Wrong password" in (r.stdout+r.stderr):
                self._cached_pw = None
                GLib.idle_add(self.wl,"Password incorrecto. Usa 'unlock' para reintentar.","red")
        except subprocess.TimeoutExpired:
            GLib.idle_add(self.wl,"Timeout (10 min)","red")
        except Exception as e:GLib.idle_add(self.wl,f"Error: {e}","red")

# ── helpers for building UI ──
def _card():
    c=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=4);c.get_style_context().add_class("card");return c
def _lbl(t,css=None):
    l=Gtk.Label(label=t);l.set_halign(Gtk.Align.START)
    if css:l.get_style_context().add_class(css)
    return l

# ── Main Window ──
class MainWindow(Gtk.Window):
    def __init__(self,app):
        super().__init__(title="LP Ranger — WETH/USDC")
        self.app=app;self.set_default_size(520,750);self.set_position(Gtk.WindowPosition.CENTER)
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme",True)
        css=Gtk.CssProvider();css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(),css,Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        vbox=Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # Header
        hdr=Gtk.Box(spacing=10);hdr.set_margin_top(12);hdr.set_margin_start(16);hdr.set_margin_end(16);hdr.set_margin_bottom(4)
        self.icon=Gtk.Image();hdr.pack_start(self.icon,False,False,0)
        vb=Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        vb.pack_start(_lbl("LP Ranger","title"),False,False,0)
        vb.pack_start(_lbl("WETH/USDC · Base · Uniswap V3","sub"),False,False,0)
        hdr.pack_start(vb,True,True,0);vbox.pack_start(hdr,False,False,0)
        # Tabs
        nb=Gtk.Notebook();nb.set_tab_pos(Gtk.PositionType.TOP)
        # Tab 1: Dashboard
        self.dash_box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=8)
        self.dash_box.set_margin_top(12);self.dash_box.set_margin_bottom(12);self.dash_box.set_margin_start(16);self.dash_box.set_margin_end(16)
        c=_card();self.lbl_st=_lbl("","mv");c.pack_start(self.lbl_st,False,False,0)
        self.lbl_msg=_lbl("","sub");self.lbl_msg.set_line_wrap(True);c.pack_start(self.lbl_msg,False,False,2)
        self.dash_box.pack_start(c,False,False,0)
        c2=_card();g=Gtk.Grid();g.set_column_spacing(16);g.set_row_spacing(10);self.m={}
        for key,lb,col,row in [("price","ETH Precio",0,0),("change","Cambio 24h",1,0),("range","Tu rango",0,1),("dist_lo","Dist. inferior",1,1),("dist_hi","Dist. superior",0,2),("vol","Volatilidad",1,2),("trend","Tendencia",0,3),("rsi","RSI",1,3),("fees_24h","Fees 24h",0,4),("fees_total","Fees acumuladas",1,4),("il_total","IL acumulado",0,5),("strat","Estrategia",1,5)]:
            vb2=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=2);vb2.pack_start(_lbl(lb,"ml"),False,False,0)
            v=_lbl("—","mv-sm");vb2.pack_start(v,False,False,0);self.m[key]=v;g.attach(vb2,col,row,1,1)
        c2.pack_start(g,False,False,0);self.dash_box.pack_start(c2,False,False,0)
        self.rec_frame=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=6);self.rec_frame.set_no_show_all(True)
        self.dash_box.pack_start(self.rec_frame,False,False,0)
        sc1=Gtk.ScrolledWindow();sc1.set_policy(Gtk.PolicyType.NEVER,Gtk.PolicyType.AUTOMATIC);sc1.add(self.dash_box)
        nb.append_page(sc1,Gtk.Label(label="  Dashboard  "))
        # Tab 2: Config
        cfg_box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=10)
        cfg_box.set_margin_top(12);cfg_box.set_margin_bottom(12);cfg_box.set_margin_start(16);cfg_box.set_margin_end(16)
        c3=_card();c3.pack_start(_lbl("Position ID","stitle"),False,False,0)
        pb=Gtk.Box(spacing=8);self.e_pid=Gtk.Entry();self.e_pid.set_placeholder_text("Numero o URL Revert")
        self.e_pid.get_style_context().add_class("entry");self.e_pid.set_text(str(app.config.get("position_id","")))
        pb.pack_start(self.e_pid,True,True,0);b=Gtk.Button(label="Buscar");b.get_style_context().add_class("btn-b")
        b.connect("clicked",self._search);pb.pack_start(b,False,False,0);c3.pack_start(pb,False,False,4)
        self.lbl_pos=_lbl("","sub");c3.pack_start(self.lbl_pos,False,False,2)
        c3.pack_start(_lbl("Rango manual","stitle"),False,False,4)
        rb=Gtk.Box(spacing=8);self.e_lo=Gtk.Entry();self.e_lo.set_placeholder_text("Min");self.e_lo.get_style_context().add_class("entry")
        self.e_lo.set_text(str(app.config.get("range_lo","") or ""));rb.pack_start(self.e_lo,True,True,0)
        rb.pack_start(Gtk.Label(label="—"),False,False,0);self.e_hi=Gtk.Entry();self.e_hi.set_placeholder_text("Max")
        self.e_hi.get_style_context().add_class("entry");self.e_hi.set_text(str(app.config.get("range_hi","") or ""))
        rb.pack_start(self.e_hi,True,True,0);b2=Gtk.Button(label="Aplicar");b2.get_style_context().add_class("btn-g")
        b2.connect("clicked",self._set_range);rb.pack_start(b2,False,False,0);c3.pack_start(rb,False,False,0)
        cfg_box.pack_start(c3,False,False,0)
        # Quick actions
        c4=_card();c4.pack_start(_lbl("Acciones rapidas","stitle"),False,False,0)
        c4.pack_start(_lbl("Ejecutan comandos en la terminal","sub"),False,False,4)
        ag=Gtk.Grid();ag.set_column_spacing(8);ag.set_row_spacing(8)
        for lbl2,css,cb,col,row in [("Ver estado wallet","btn-b",lambda b:app.term.run_cmd("status"),0,0),("Simular rebalanceo","btn-dim",lambda b:app.term.wl("Usa: rebalance --price-lower X --price-upper Y --dry-run","blue"),1,0),("Salir → ETH","btn-r",lambda b:app.term.run_cmd("exit --hold ETH"),0,1),("Salir → USDC","btn-r",lambda b:app.term.run_cmd("exit --hold USDC"),1,1)]:
            bt=Gtk.Button(label=lbl2);bt.get_style_context().add_class(css);bt.connect("clicked",cb);bt.set_hexpand(True);ag.attach(bt,col,row,1,1)
        c4.pack_start(ag,False,False,0);cfg_box.pack_start(c4,False,False,0)
        # Strategy
        c5=_card();c5.pack_start(_lbl("Estrategia activa","stitle"),False,False,0)
        sb=Gtk.Box(spacing=8);self.lbl_sname=_lbl("—","mv-sm");sb.pack_start(self.lbl_sname,True,True,0)
        b3=Gtk.Button(label="Cambiar .json");b3.connect("clicked",self._load_strat);sb.pack_start(b3,False,False,0)
        c5.pack_start(sb,False,False,4);self.lbl_sinfo=_lbl("","sub");self.lbl_sinfo.set_line_wrap(True)
        c5.pack_start(self.lbl_sinfo,False,False,0);cfg_box.pack_start(c5,False,False,0)
        # History
        c6=_card();c6.pack_start(_lbl("Historial","stitle"),False,False,0)
        self.hist_box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=3);c6.pack_start(self.hist_box,False,False,4)
        cfg_box.pack_start(c6,False,False,0)
        sc2=Gtk.ScrolledWindow();sc2.set_policy(Gtk.PolicyType.NEVER,Gtk.PolicyType.AUTOMATIC);sc2.add(cfg_box)
        nb.append_page(sc2,Gtk.Label(label="  Config  "))
        # Tab 3: Terminal
        app.term=Term(app);nb.append_page(app.term,Gtk.Label(label="  Terminal  "))
        vbox.pack_start(nb,True,True,0);self.add(vbox)
        self.connect("delete-event",lambda *a:self.hide() or True)

    def update(self,status,price,rlo,rhi,det,rec,stats):
        cmap={"green":"#34d399","yellow":"#fbbf24","red":"#f87171","exit_eth":"#f87171","exit_usdc":"#f87171","closed_eth":"#60a5fa","closed_usdc":"#60a5fa","gray":"#6b7280"}
        smap={"green":"● En rango","yellow":"● Vigilar","red":"● Rebalanceo necesario","exit_eth":"🚪 Salir → ETH","exit_usdc":"🚪 Salir → USDC","closed_eth":"📦 Pool cerrada (ETH)","closed_usdc":"📦 Pool cerrada (USDC)","gray":"○ Sin datos"}
        imap={"green":"green_48","yellow":"yellow_48","red":"red_48","exit_eth":"red_48","exit_usdc":"red_48","closed_eth":"yellow_48","closed_usdc":"yellow_48","gray":"gray_48"}
        ip=str(ICONS_DIR/f"{imap.get(status,'gray_48')}.png")
        if os.path.exists(ip):self.icon.set_from_file(ip)
        self.lbl_st.set_markup(f'<span foreground="{cmap.get(status,"#6b7280")}"><b>{smap.get(status,"?")}</b></span>')
        self.lbl_msg.set_text(det.get("message",""))
        self.m["price"].set_markup(f'<span foreground="#e0e6ed">${price:,.2f}</span>' if price else "—")
        ch=self.app.fetcher.change;self.m["change"].set_markup(f'<span foreground="{"#34d399" if ch>=0 else "#f87171"}">{ch:+.1f}%</span>' if ch else "—")
        self.m["range"].set_text(f"${rlo:,.0f} — ${rhi:,.0f}" if rlo else "—")
        dlp=det.get("dist_lo_pct",0);dhp=det.get("dist_hi_pct",0)
        self.m["dist_lo"].set_markup(f'<span foreground="{"#34d399" if dlp>5 else "#fbbf24" if dlp>2 else "#f87171"}">{dlp:.1f}%</span>')
        self.m["dist_hi"].set_markup(f'<span foreground="{"#34d399" if dhp>5 else "#fbbf24" if dhp>2 else "#f87171"}">{dhp:.1f}%</span>')
        self.m["vol"].set_text(f"{det.get('volatility_pct',0):.2f}%")
        tp=det.get("trend_pct",0);self.m["trend"].set_markup(f'<span foreground="{"#34d399" if tp>0 else "#f87171"}">{det.get("trend","")} ({tp:+.1f}%)</span>')
        rv=det.get("rsi",50);self.m["rsi"].set_markup(f'<span foreground="{"#34d399" if 30<rv<70 else "#fbbf24" if 25<rv<75 else "#f87171"}">{rv:.0f}</span>')
        self.m["fees_24h"].set_markup(f'<span foreground="#34d399">${stats.get("fees_today",0):.2f}</span>')
        self.m["fees_total"].set_markup(f'<span foreground="#34d399">${stats.get("total_fees",0):.2f}</span>')
        self.m["il_total"].set_markup(f'<span foreground="#f87171">-${stats.get("total_il",0):.2f}</span>')
        self.m["strat"].set_text(self.app.strategy.cfg.get("strategy_type","—"))
        self.lbl_sname.set_text(self.app.strategy.cfg.get("name","—"))
        bt=self.app.strategy.cfg.get("backtest_results",{})
        self.lbl_sinfo.set_text(f"APR: {bt.get('net_apr','?')}% · Rebalanceos: {bt.get('rebalance_count','?')}/año")
        # Recommendation
        for c in self.rec_frame.get_children():self.rec_frame.remove(c)
        if rec:
            self.rec_frame.set_no_show_all(False);rt=rec.get("type","rebalance")
            css2="exit-box" if rt=="exit_pool" else "enter-box" if rt=="enter_pool" else "rec-box"
            bx=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=6);bx.get_style_context().add_class(css2)
            icons={"exit_pool":"🚪 CERRAR POOL","enter_pool":"🔄 RE-ENTRAR","rebalance":"♻️ RANGO PROPUESTO (orientativo)"}
            bx.pack_start(_lbl(icons.get(rt,rt),"mv-sm"),False,False,0)
            if rt!="exit_pool":bx.pack_start(_lbl(f"${rec.get('lo',0):,.0f} — ${rec.get('hi',0):,.0f} ({rec.get('width_pct',0):.0f}%)","mv"),False,False,2)
            else:bx.pack_start(_lbl(f"Hold {rec.get('hold','USDC')}","mv"),False,False,2)
            rl=_lbl(rec.get("reason",""),"sub");rl.set_line_wrap(True);bx.pack_start(rl,False,False,4)
            if rt in ("rebalance","enter_pool"):
                bx.pack_start(_lbl("Crea la pool y pega la nueva ID:","sub"),False,False,2)
                nb2=Gtk.Box(spacing=8);self.e_np=Gtk.Entry();self.e_np.set_placeholder_text("Nueva Position ID")
                self.e_np.get_style_context().add_class("entry");nb2.pack_start(self.e_np,True,True,0)
                ba=Gtk.Button(label="✓ Aplicado");ba.get_style_context().add_class("btn-g")
                ba.connect("clicked",lambda b:self._confirm(rec));nb2.pack_start(ba,False,False,0)
                bx.pack_start(nb2,False,False,4)
            else:
                ba=Gtk.Button(label=f"✓ Hecho");ba.get_style_context().add_class("btn-r")
                ba.connect("clicked",lambda b:self._exit_confirm(rec.get("hold","USDC")));bx.pack_start(ba,False,False,4)
            bd=Gtk.Button(label="✗ Descartar");bd.get_style_context().add_class("btn-dim")
            bd.connect("clicked",lambda b:self._dismiss());bx.pack_start(bd,False,False,0)
            self.rec_frame.pack_start(bx,False,False,0);self.rec_frame.show_all()
        else:self.rec_frame.hide()
        # History
        for c in self.hist_box.get_children():self.hist_box.remove(c)
        for e in self.app.history.recent(10):
            l=Gtk.Label();l.set_markup(f'<span font_size="small" foreground="#4b5563">{e["ts"]} · {e["type"]}: {e["msg"]}</span>')
            l.set_halign(Gtk.Align.START);l.set_line_wrap(True);self.hist_box.pack_start(l,False,False,0)
        self.hist_box.show_all()

    def _confirm(self,rec):
        npid=parse_pid(self.e_np.get_text()) if hasattr(self,'e_np') else ""
        p=self.app.fetcher.price;olo=self.app.config.get("range_lo",0);ohi=self.app.config.get("range_hi",0)
        if npid:
            self.app.config.set("position_id",npid)
            def f():
                ok=self.app.fetcher.fetch_position(npid)
                if ok:
                    pd=self.app.fetcher.pos;il=self.app.stats.record_il(olo,ohi,pd["lo"],pd["hi"],p)
                    if rec.get("type")=="enter_pool":self.app.stats.set("pool_active",True);self.app.stats.set("hold_asset",None)
                    GLib.idle_add(lambda:[self.app.config.set("range_lo",pd["lo"]),self.app.config.set("range_hi",pd["hi"]),
                        self.app.history.log(rec.get("type","?"),f"${pd['lo']:.0f}-${pd['hi']:.0f} (IL:${il:.2f})"),
                        setattr(self.app,'last_rec',None),self.app.term.wl(f"✓ {npid}: ${pd['lo']:.0f}-${pd['hi']:.0f}","green"),
                        self.app.force_update()])
                else:GLib.idle_add(self.app.term.wl,f"✗ {self.app.fetcher.error}","red")
            threading.Thread(target=f,daemon=True).start()
        else:
            if rec.get("type")=="enter_pool":self.app.stats.set("pool_active",True);self.app.stats.set("hold_asset",None)
            self.app.last_rec=None;self.app.history.log(rec.get("type","?"),"Confirmado");self.app.force_update()

    def _exit_confirm(self,hold):
        p=self.app.fetcher.price;olo=self.app.config.get("range_lo",0);ohi=self.app.config.get("range_hi",0)
        il=self.app.stats.record_il(olo,ohi,0,0,p)
        self.app.stats.set("pool_active",False);self.app.stats.set("hold_asset",hold)
        self.app.config.set("range_lo",0);self.app.config.set("range_hi",0)
        self.app.last_rec=None;self.app.history.log("exit_pool",f"→ {hold} @ ${p:.0f} (IL:${il:.2f})")
        self.app.term.wl(f"Pool cerrada → {hold}","yellow");self.app.force_update()

    def _dismiss(self):
        self.app.last_rec=None;self.app.last_rec_time=time.time()
        self.app.history.log("dismiss","Descartada");self.app.force_update()

    def _search(self,btn):
        pid=parse_pid(self.e_pid.get_text());self.e_pid.set_text(pid)
        self.app.config.set("position_id",pid);self.app.last_rec=None
        self.lbl_pos.set_markup('<span foreground="#fbbf24">Buscando...</span>')
        self.app.term.wl(f"Buscando posicion {pid}...","blue")
        def f():
            ok=self.app.fetcher.fetch_position(pid)
            GLib.idle_add(self._pid_ok if ok else self._pid_fail)
        threading.Thread(target=f,daemon=True).start()
    def _pid_ok(self):
        pd=self.app.fetcher.pos;self.lbl_pos.set_markup(f'<span foreground="#34d399">✓ ${pd["lo"]:.0f} — ${pd["hi"]:.0f}</span>')
        self.app.config.set("range_lo",pd["lo"]);self.app.config.set("range_hi",pd["hi"])
        self.e_lo.set_text(str(pd["lo"]));self.e_hi.set_text(str(pd["hi"]))
        self.app.term.wl(f"✓ ${pd['lo']:.0f}-${pd['hi']:.0f}","green");self.app.force_update()
    def _pid_fail(self):
        self.lbl_pos.set_markup(f'<span foreground="#f87171">✗ {self.app.fetcher.error}</span>')
        self.app.term.wl(f"✗ {self.app.fetcher.error}","red")
    def _set_range(self,btn):
        try:
            lo=float(self.e_lo.get_text());hi=float(self.e_hi.get_text())
            if lo>0 and hi>lo:self.app.config.set("range_lo",lo);self.app.config.set("range_hi",hi);self.app.last_rec=None;self.app.force_update();self.app.term.wl(f"Rango: ${lo:.0f}-${hi:.0f}","green")
        except:self.app.term.wl("Numeros invalidos","red")
    def _load_strat(self,btn):
        d=Gtk.FileChooserDialog(title="Estrategia .json",parent=self,action=Gtk.FileChooserAction.OPEN)
        d.add_buttons(Gtk.STOCK_CANCEL,Gtk.ResponseType.CANCEL,Gtk.STOCK_OPEN,Gtk.ResponseType.OK)
        ft=Gtk.FileFilter();ft.set_name("JSON");ft.add_pattern("*.json");d.add_filter(ft)
        if d.run()==Gtk.ResponseType.OK:
            p=d.get_filename();self.app.strategy.load(p);self.app.config.set("strategy_file",p)
            self.app.last_rec=None;self.app.history.log("strategy",os.path.basename(p))
            self.app.term.wl(f"Estrategia: {os.path.basename(p)}","green");self.app.force_update()
        d.destroy()

# ── App ──
class App:
    def __init__(self):
        self.config=Cfg();self.strategy=Strat(self.config.get("strategy_file"))
        self.fetcher=Fetcher();self.history=Hist();self.stats=Stats()
        self.status="gray";self.last_rec=None;self.last_rec_time=0;self.window=None;self.term=None
        self._last_fee_time=0  # Track when we last added fees
        if AppIndicator3:
            self.ind=AppIndicator3.Indicator.new("lp-ranger",str(ICONS_DIR/"gray.png"),AppIndicator3.IndicatorCategory.APPLICATION_STATUS)
            self.ind.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            menu=Gtk.Menu()
            i1=Gtk.MenuItem(label="Abrir");i1.connect("activate",self._open);menu.append(i1);menu.append(Gtk.SeparatorMenuItem())
            i2=Gtk.MenuItem(label="Actualizar");i2.connect("activate",lambda _:self.force_update());menu.append(i2);menu.append(Gtk.SeparatorMenuItem())
            i3=Gtk.MenuItem(label="Salir");i3.connect("activate",lambda _:Gtk.main_quit());menu.append(i3)
            menu.show_all();self.ind.set_menu(menu)
        else:self.ind=None
        GLib.timeout_add_seconds(2,self._init);GLib.timeout_add_seconds(300,self._poll);GLib.timeout_add_seconds(900,self._poll_pos)
    def _open(self,*a):
        if not self.window:self.window=MainWindow(self)
        self.window.show_all();self.window.present()
        if self.term:self.term.wl("LP Ranger v4 listo","green")
        self._update_win()
    def _init(self):
        self._open()
        self.force_update()
        # Ask password on startup so auto-execution works
        if self.term:
            self.term.wl("LP Ranger v4 — Modo automatico","green")
            self.term.wl("Introduce tu password para activar la ejecucion automatica.","blue")
            GLib.timeout_add_seconds(1, self._startup_password)
        return False

    def _startup_password(self):
        if self.term and not self.term._cached_pw:
            self.term._ask_password("status")  # Ask pw and run status as test
        return False
    def _poll(self):threading.Thread(target=self._fetch,daemon=True).start();return True
    def _poll_pos(self):
        pid=self.config.get("position_id")
        if pid:threading.Thread(target=self._fpos,args=(pid,),daemon=True).start()
        return True
    def _fpos(self,pid):
        self.fetcher.fetch_position(pid)
        if self.fetcher.pos:GLib.idle_add(self._apos)
    def _apos(self):
        pd=self.fetcher.pos
        if pd:self.config.set("range_lo",pd["lo"]);self.config.set("range_hi",pd["hi"]);self._run()
    def _fetch(self):self.fetcher.fetch_price();GLib.idle_add(self._run)
    def force_update(self):threading.Thread(target=self._fetch,daemon=True).start()
    def _run(self):
        p=self.fetcher.price;rlo=self.config.get("range_lo",0);rhi=self.config.get("range_hi",0)
        pool=self.stats.get("pool_active",True);hold=self.stats.get("hold_asset")
        if p>0:self.strategy.add(p)
        status,rec,det=self.strategy.evaluate(p,rlo,rhi,pool,hold)
        if pool and det.get("in_range") and p>0 and rlo>0 and rhi>0:
            now=time.time()
            if now-self._last_fee_time>=290:  # ~5 min, with small buffer
                self._last_fee_time=now
                cw=(rhi-rlo)/((rlo+rhi)/2)*100
                # $0.31/day is for a $119.50 position at 15.63% range
                # Scale by range width. This is an ESTIMATE only.
                daily_fee=0.31*(15.63/max(cw,1))
                per_5min=daily_fee/(24*12)
                self.stats.add_fees(per_5min)
        old=self.status;self.status=status
        if rec:
            el=time.time()-self.last_rec_time
            if el>COOLDOWN_H*3600 or self.last_rec is None:
                self.last_rec=rec;self.last_rec_time=time.time()
                if self.term:self.term.wl(f"Senal: {rec.get('type','')} — {rec.get('reason','')}","yellow")
                # Write proposal file for bridge
                try:json.dump({"type":rec.get("type",""),"timestamp":datetime.now().isoformat()[:19],"current_price":p,"proposed_lo":rec.get("lo",0),"proposed_hi":rec.get("hi",0),"proposed_width":rec.get("width_pct",0),"hold_asset":rec.get("hold",""),"reason":rec.get("reason",""),"ema_fast":det.get("ema_fast",0),"ema_slow":det.get("ema_slow",0),"trend":det.get("trend",""),"trend_pct":det.get("trend_pct",0),"rsi":det.get("rsi",50),"volatility_pct":det.get("volatility_pct",0)},open(DATA_DIR/"pending_proposal.json","w"),indent=2)
                except:pass
                # AUTO-EXECUTE if password is cached
                if self.term and self.term._cached_pw:
                    self._auto_execute(rec, det)
        imap={"green":"green","yellow":"yellow","red":"red","exit_eth":"red","exit_usdc":"red","closed_eth":"yellow","closed_usdc":"yellow","gray":"gray"}
        if self.ind:
            ip=str(ICONS_DIR/f"{imap.get(status,'gray')}.png")
            if os.path.exists(ip):self.ind.set_icon_full(ip,status)
        if status!=old and status in ("red","exit_eth","exit_usdc") and self.config.get("notifications"):
            t={"red":"Cambiar rango","exit_eth":"Cerrar → ETH","exit_usdc":"Cerrar → USDC"}
            try:subprocess.run(["notify-send","-i",str(ICONS_DIR/f"{imap.get(status,'gray')}_48.png"),f"LP Ranger — {t.get(status,'')}",det.get("message","")],check=False,timeout=5)
            except:pass
        self._update_win()

    def _auto_execute(self, rec, det):
        """Auto-execute a signal via lp_autobot."""
        rtype = rec.get("type","")
        pid = self.config.get("position_id","")
        pw = self.term._cached_pw
        autobot = str(APP_DIR / "lp_autobot.py")

        if not pid:
            self.term.wl("Auto-exec: no hay Position ID configurado","red")
            return

        self.term.wl(f"🤖 AUTO-EJECUTANDO: {rtype}...","blue")

        def execute():
            try:
                if rtype == "rebalance":
                    lo = rec.get("lo",0); hi = rec.get("hi",0)
                    cmd = [sys.executable, autobot, "--rebalance", "-p", pid, "-y",
                           "--price-lower", str(int(lo)), "--price-upper", str(int(hi))]
                    GLib.idle_add(self.term.wl, f"  Rebalanceando a ${lo:.0f}-${hi:.0f}...", "blue")

                elif rtype == "exit_pool":
                    hold = rec.get("hold","USDC")
                    cmd = [sys.executable, autobot, "--exit", "-p", pid, "-y", "--hold", hold]
                    GLib.idle_add(self.term.wl, f"  Cerrando pool → hold {hold}...", "blue")

                elif rtype == "enter_pool":
                    lo = rec.get("lo",0); hi = rec.get("hi",0)
                    cmd = [sys.executable, autobot, "--enter", "-y",
                           "--price-lower", str(int(lo)), "--price-upper", str(int(hi))]
                    GLib.idle_add(self.term.wl, f"  Abriendo pool ${lo:.0f}-${hi:.0f}...", "blue")
                else:
                    GLib.idle_add(self.term.wl, f"  Tipo desconocido: {rtype}", "red")
                    return

                # Execute with password (no confirmation needed thanks to -y)
                stdin_data = f"{pw}\n"
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                                   input=stdin_data, cwd=str(APP_DIR))

                # Show output (filtered)
                for line in (r.stdout + r.stderr).split("\n"):
                    if not line.strip(): continue
                    skip = any(x in line for x in ["GetPassWarning","fallback_getpass",
                              "Password input may be echoed","Unlock password","Can not control echo"])
                    if skip: continue
                    tag = None
                    ll = line.lower()
                    if "error" in ll or "failed" in ll: tag = "red"
                    elif "success" in ll: tag = "green"
                    GLib.idle_add(self.term.wl, line, tag)

                if r.returncode == 0:
                    GLib.idle_add(self.term.wl, "✅ Ejecucion completada", "green")
                    
                    # Parse new position ID from output (rebalance creates new NFT)
                    new_pid = None
                    output = r.stdout + r.stderr
                    import re as _re
                    m = _re.search(r'tokenId:\s*(\d+)', output)
                    if m:
                        new_pid = m.group(1)
                        GLib.idle_add(self.term.wl, f"  Nueva posicion ID: {new_pid}", "green")
                        self.config.set("position_id", new_pid)
                    
                    # Update state
                    olo = self.config.get("range_lo",0); ohi = self.config.get("range_hi",0)
                    price = self.fetcher.price
                    fetch_pid = new_pid or pid

                    if rtype == "exit_pool":
                        hold = rec.get("hold","USDC")
                        il = self.stats.record_il(olo,ohi,0,0,price)
                        self.stats.set("pool_active",False); self.stats.set("hold_asset",hold)
                        self.config.set("range_lo",0); self.config.set("range_hi",0)
                        GLib.idle_add(self.history.log, "auto_exit", f"→ {hold} @ ${price:.0f} (IL:${il:.2f})")

                    elif rtype == "enter_pool":
                        self.stats.set("pool_active",True); self.stats.set("hold_asset",None)
                        GLib.idle_add(self.history.log, "auto_enter", f"Pool abierta (ID:{fetch_pid})")
                        time.sleep(5)
                        if fetch_pid: self.fetcher.fetch_position(fetch_pid)
                        if self.fetcher.pos:
                            pd = self.fetcher.pos
                            self.config.set("range_lo",pd["lo"]); self.config.set("range_hi",pd["hi"])
                            GLib.idle_add(self.term.wl, f"  Rango real: ${pd['lo']:.0f}-${pd['hi']:.0f}", "green")

                    elif rtype == "rebalance":
                        il = self.stats.record_il(olo, ohi, rec.get("lo",0), rec.get("hi",0), price)
                        GLib.idle_add(self.history.log, "auto_rebalance",
                            f"ID:{fetch_pid} ${rec.get('lo',0):.0f}-${rec.get('hi',0):.0f} @ ${price:.0f} (IL:${il:.2f})")
                        time.sleep(5)
                        if fetch_pid: self.fetcher.fetch_position(fetch_pid)
                        if self.fetcher.pos:
                            pd = self.fetcher.pos
                            self.config.set("range_lo",pd["lo"]); self.config.set("range_hi",pd["hi"])
                            GLib.idle_add(self.term.wl, f"  Rango on-chain: ${pd['lo']:.0f}-${pd['hi']:.0f}", "green")

                    GLib.idle_add(self._clear_rec_and_refresh)
                else:
                    GLib.idle_add(self.term.wl, "❌ Ejecucion fallida", "red")
                    if "Wrong password" in (r.stdout+r.stderr):
                        self.term._cached_pw = None
                        GLib.idle_add(self.term.wl, "Password incorrecto. Usa 'unlock' en terminal.", "red")

            except subprocess.TimeoutExpired:
                GLib.idle_add(self.term.wl, "Timeout (10 min)", "red")
            except Exception as e:
                GLib.idle_add(self.term.wl, f"Error auto-exec: {e}", "red")

        threading.Thread(target=execute, daemon=True).start()

    def _clear_rec_and_refresh(self):
        self.last_rec = None
        self.force_update()
    def _update_win(self):
        if self.window and self.window.get_visible():
            p=self.fetcher.price;rlo=self.config.get("range_lo",0);rhi=self.config.get("range_hi",0)
            _,_,det=self.strategy.evaluate(p,rlo,rhi,self.stats.get("pool_active",True),self.stats.get("hold_asset"))
            self.window.update(self.status,p,rlo,rhi,det,self.last_rec,self.stats.d)
    def run(self):self.history.log("start","LP Ranger v4");Gtk.main()

if __name__=="__main__":
    import argparse
    pa=argparse.ArgumentParser();pa.add_argument("-p","--position-id");pa.add_argument("-s","--strategy")
    pa.add_argument("--range-lo",type=float);pa.add_argument("--range-hi",type=float)
    a=pa.parse_args();app=App()
    if a.position_id:app.config.set("position_id",parse_pid(a.position_id))
    if a.strategy:app.strategy.load(a.strategy);app.config.set("strategy_file",a.strategy)
    if a.range_lo:app.config.set("range_lo",a.range_lo)
    if a.range_hi:app.config.set("range_hi",a.range_hi)
    try:app.run()
    except KeyboardInterrupt:pass
