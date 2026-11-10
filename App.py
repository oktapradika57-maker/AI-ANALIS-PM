import hashlib
import hmac
import html
import inspect
import io
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

try:
    import gspread
    from gspread.exceptions import APIError
except Exception:  # gspread belum terpasang -> aplikasi tetap jalan dalam mode lokal
    gspread = None

    class APIError(Exception):
        pass


# ==========================================================================
# 1. KONSTANTA
# ==========================================================================
APP_NAME = "SiPM Performance Intelligence"
APP_VERSION = "2.0"

DEFAULT_SPREADSHEET_ID = "1HvgVicTWwO4RMQI6ZR3Mu3IgGicwjcLZl9mDN1auvJU"
DEFAULT_WORKSHEET = "SiPM"
DEFAULT_LOCK_WORD = "KUNCI"      # dipakai hanya jika lock_keyword belum diatur di secrets
MAX_FAILED_ATTEMPTS = 5

DRAFT, LOCKED = "DRAFT", "LOCKED"

# Urutan kolom di sheet. Kolom bertanda (*) boleh berubah saat kunci/buka kunci.
SCHEMA = [
    "Period", "Lock_Status", "Tipe_PM", "NOP", "Status", "Schedule Date",
    "Submitted Date", "Sumber File", "Saved_At", "Saved_By", "Locked_At",
    "Locked_By", "Lock_Hash", "Audit_Trail", "Config_JSON", "Payload_JSON",
]
# Kolom yang diikutkan dalam sidik data (hash) untuk deteksi perubahan & integritas.
CANON_COLS = ["Tipe_PM", "NOP", "Status", "Schedule Date", "Submitted Date",
              "Sumber File", "Payload_JSON"]
# Dua blok kolom yang ditulis ulang saat kunci/buka kunci: Lock_Status dan Locked_At..Config_JSON
LOCK_SPANS = [("Lock_Status", "Lock_Status"), ("Locked_At", "Config_JSON")]

CORE_ALIASES = {"status": "Status", "nop": "NOP",
                "schedule date": "Schedule Date", "submitted date": "Submitted Date"}
REQUIRED_COLS = ["Status", "NOP", "Schedule Date"]
RESERVED_COLS = {"Status", "NOP", "Schedule Date", "Submitted Date", "Sumber File", "Tipe_PM"}
NO_NOP = "(Tanpa NOP)"
TIPE_OPTIONS = ["PMS", "PMG", "Lainnya"]
TIPE_LABEL = {"PMS": "PMS (Site)", "PMG": "PMG (Genset)", "Lainnya": "Lainnya"}

MONTHS_ID = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
             "Agustus", "September", "Oktober", "November", "Desember"]

MODE_UPLOAD = "Upload file baru"
MODE_ARCHIVE = "Buka arsip tersimpan"
NAV_ITEMS = ["Dashboard", "Arsip & Riwayat"]

# Palet (dipakai juga oleh grafik)
C_INK, C_BLUE, C_TEAL, C_AMBER = "#0F1F33", "#1457B8", "#0F8B8D", "#B7791F"
C_RED, C_GREEN, C_MUTED, C_LINE = "#B42318", "#12805C", "#64748B", "#DDE3EA"


# ==========================================================================
# 2. UTILITAS UMUM (secrets, waktu, periode, teks)
# ==========================================================================
def secret(key, default=None):
    """Ambil nilai dari st.secrets tanpa error bila file secrets belum ada."""
    try:
        value = st.secrets[key]
        return default if value in (None, "") else value
    except Exception:
        return default


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(str(secret("timezone", "Asia/Makassar")))
    except Exception:
        return ZoneInfo("Asia/Makassar")


def now_local() -> datetime:
    return datetime.now(_tz())


def today_local():
    return now_local().date()


def stamp() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M:%S %Z")


def esc(x) -> str:
    return html.escape(str(x), quote=True)


def fmt_int(n) -> str:
    return f"{int(n):,}".replace(",", ".")


def fmt_pct(x, nd=1) -> str:
    return f"{float(x):.{nd}f}%"


def fmt_date(d) -> str:
    d = pd.Timestamp(d)
    return f"{d.day:02d} {MONTHS_ID[d.month - 1]} {d.year}"


def period_label(p: str) -> str:
    return f"{MONTHS_ID[int(p[5:7]) - 1]} {p[:4]}"


def shift_month(p: str, delta: int) -> str:
    t = int(p[:4]) * 12 + (int(p[5:7]) - 1) + delta
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def period_options(saved, extras=()):
    """Pilihan periode untuk dropdown: 12 bulan ke belakang, bulan depan, dan yang sudah tersimpan."""
    cur = today_local().strftime("%Y-%m")
    base = {shift_month(cur, i) for i in range(-12, 2)}
    return sorted(base | set(saved) | {e for e in extras if e}, reverse=True)


def detect_period(df: pd.DataFrame):
    s = df["Schedule Date"].dropna()
    if s.empty:
        return None
    return s.dt.strftime("%Y-%m").mode().iloc[0]


def _s(x) -> str:
    if x is None:
        return ""
    try:
        if pd.isna(x):
            return ""
    except (TypeError, ValueError):
        pass
    return str(x).strip()


def _clean(v):
    """Nilai sel -> tipe yang aman untuk JSON (None bila kosong)."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, (pd.Timestamp, datetime)):
        if (v.hour, v.minute, v.second) == (0, 0, 0):
            return v.strftime("%Y-%m-%d")
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


def assign_tipe_pm(filename) -> str:
    f = str(filename).upper()
    if "SITE" in f or "PMS" in f:
        return "PMS"
    if "GENSET" in f or "PMG" in f:
        return "PMG"
    return "Lainnya"


def default_config(statuses) -> dict:
    """Pengaturan awal kurva (sama dengan aplikasi versi sebelumnya)."""
    done = [s for s in ["SUBMITTED", "CLOSED", "DONE", "APPROVED", "WAITING AMNESTY"] if s in statuses]
    if not done and statuses:
        done = [statuses[0]]
    take = [s for s in statuses if "TAKEOUT" in s or "WAITING APPROVAL" in s]
    return {"completed": done, "takeout": take}


def parse_config(raw, df=None) -> dict:
    try:
        cfg = json.loads(raw)
        if isinstance(cfg, dict) and "completed" in cfg and "takeout" in cfg:
            return {"completed": list(cfg["completed"]), "takeout": list(cfg["takeout"])}
    except Exception:
        pass
    statuses = sorted({s.upper() for s in df["Status"] if s}) if df is not None else []
    return default_config(statuses)


# ==========================================================================
# 3. PEMBACAAN & STANDARISASI DATA
# ==========================================================================
def finalize(df: pd.DataFrame) -> pd.DataFrame:
    """Bentuk akhir kolom inti, dipakai baik oleh data upload maupun data dari arsip."""
    d = df.copy()
    d["NOP"] = d["NOP"].map(_s).replace("", NO_NOP)
    d["Status"] = d["Status"].map(_s)
    for c in ("Schedule Date", "Submitted Date"):
        d[c] = pd.to_datetime(d[c], errors="coerce").dt.normalize()
    return d


@st.cache_data(show_spinner=False, max_entries=32)
def parse_excel(data: bytes, filename: str):
    """Baca satu file Excel -> (DataFrame | None, daftar peringatan, pesan error | None)."""
    try:
        raw = pd.read_excel(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        return None, [], f"File tidak dapat dibaca ({type(e).__name__}: {e})"
    raw.columns = [str(c).strip() for c in raw.columns]
    raw = raw.loc[:, ~raw.columns.duplicated()]
    raw = raw.rename(columns={c: CORE_ALIASES[c.lower()] for c in raw.columns if c.lower() in CORE_ALIASES})
    missing = [c for c in REQUIRED_COLS if c not in raw.columns]
    if missing:
        return None, [], "Kolom wajib tidak ditemukan: " + ", ".join(missing)
    warns = []
    if "Submitted Date" not in raw.columns:
        raw["Submitted Date"] = pd.NaT
        warns.append("Kolom 'Submitted Date' tidak ada, realisasi tidak punya tanggal.")
    raw = raw.dropna(how="all").reset_index(drop=True)
    if raw.empty:
        return None, [], "File tidak berisi baris data."
    return finalize(raw), warns, None


def build_upload(entries) -> pd.DataFrame:
    """entries: [(nama_file, df, tipe)] -> satu DataFrame gabungan."""
    frames = []
    for name, df, tipe in entries:
        f = df.copy()
        f["Sumber File"] = name
        f["Tipe_PM"] = tipe
        frames.append(f)
    out = pd.concat(frames, ignore_index=True)
    first = ["Tipe_PM", "NOP", "Status", "Schedule Date", "Submitted Date"]
    rest = [c for c in out.columns if c not in first + ["Sumber File"]]
    return finalize(out[first + rest + ["Sumber File"]])


def _iso(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.strftime("%Y-%m-%d").fillna("")


def to_store_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Data upload -> format baris sheet (semua kolom berupa teks)."""
    other = [c for c in df.columns if c not in RESERVED_COLS]
    out = pd.DataFrame({
        "Tipe_PM": df["Tipe_PM"].map(_s),
        "NOP": df["NOP"].map(_s),
        "Status": df["Status"].map(_s),
        "Schedule Date": _iso(df["Schedule Date"]),
        "Submitted Date": _iso(df["Submitted Date"]),
        "Sumber File": df["Sumber File"].map(_s),
    })
    payload = []
    for rec in df[other].to_dict("records"):
        clean = {k: _clean(v) for k, v in rec.items()}
        payload.append(json.dumps({k: v for k, v in clean.items() if v is not None},
                                  ensure_ascii=False, sort_keys=True, default=str))
    out["Payload_JSON"] = payload
    return out.reset_index(drop=True)


def canon_hash(df: pd.DataFrame) -> str:
    """Sidik SHA-256 isi data (tidak peka urutan baris)."""
    if df is None or len(df) == 0:
        return hashlib.sha256(b"").hexdigest()
    lines = sorted("\x1f".join(str(v) for v in row)
                   for row in df[CANON_COLS].itertuples(index=False, name=None))
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def expand_period(g: pd.DataFrame, with_payload: bool = True) -> pd.DataFrame:
    """Baris-baris sheet satu periode -> DataFrame analisis (kolom asli dipulihkan dari Payload_JSON)."""
    g = g.reset_index(drop=True)
    base = pd.DataFrame({
        "Tipe_PM": g["Tipe_PM"], "NOP": g["NOP"], "Status": g["Status"],
        "Schedule Date": pd.to_datetime(g["Schedule Date"], format="%Y-%m-%d", errors="coerce"),
        "Submitted Date": pd.to_datetime(g["Submitted Date"], format="%Y-%m-%d", errors="coerce"),
    })
    if with_payload:
        pay = pd.DataFrame([json.loads(x) if x else {} for x in g["Payload_JSON"]])
        if len(pay.columns):
            base = pd.concat([base, pay], axis=1)
    base["Sumber File"] = g["Sumber File"]
    return finalize(base)


# ==========================================================================
# 4. LOGIKA ANALISIS (Kurva S, KPI, Leaderboard, Rekap)
# ==========================================================================
def done_mask(df: pd.DataFrame, completed) -> pd.Series:
    up = {str(s).upper() for s in completed}
    return df["Status"].astype(str).str.upper().isin(up)


def apply_takeout(df: pd.DataFrame, takeout) -> pd.DataFrame:
    if not takeout:
        return df
    up = {str(s).upper() for s in takeout}
    return df[~df["Status"].astype(str).str.upper().isin(up)]


def calculate_scurve(df: pd.DataFrame, completed):
    """Timeline harian Plan vs Actual kumulatif (%). None bila tidak ada data berjadwal."""
    d = df.dropna(subset=["Schedule Date"]).copy()
    total = len(d)
    if total == 0:
        return None
    w = 100.0 / total
    done = d[done_mask(d, completed)]
    sub = done["Submitted Date"].dropna()
    min_s, max_s = d["Schedule Date"].min(), d["Schedule Date"].max()
    start = min(min_s, sub.min()) if not sub.empty else min_s
    end = max(max_s, sub.max()) if not sub.empty else max_s

    tl = pd.DataFrame({"Date": pd.date_range(start, end, freq="D")})
    tgt = d.groupby("Schedule Date").size()
    tl["Target_Unit"] = tl["Date"].map(tgt).fillna(0)
    tl["Target_Kumulatif"] = (tl["Target_Unit"] * w).cumsum()
    if not sub.empty:
        act = done.dropna(subset=["Submitted Date"]).groupby("Submitted Date").size()
        tl["Actual_Unit"] = tl["Date"].map(act).fillna(0)
        tl["Actual_Kumulatif"] = (tl["Actual_Unit"] * w).cumsum()
        tl["Actual_Kumulatif_Plot"] = np.where(tl["Date"] <= sub.max(), tl["Actual_Kumulatif"], np.nan)
    else:
        tl["Actual_Unit"] = 0
        tl["Actual_Kumulatif"] = 0.0
        tl["Actual_Kumulatif_Plot"] = np.nan
    return tl


def as_of_date(tl):
    """Tanggal acuan: hari ini, dibatasi ke rentang timeline."""
    if tl is None or tl.empty:
        return None
    t = pd.Timestamp(today_local())
    return min(max(t, tl["Date"].min()), tl["Date"].max())


def plan_at(tl, as_of) -> float:
    if tl is None or as_of is None:
        return 0.0
    return float(tl.loc[tl["Date"] == as_of, "Target_Kumulatif"].iloc[0])


def kpis(d: pd.DataFrame) -> dict:
    """d wajib punya kolom Is_Selesai."""
    def pct(a, b):
        return a / b * 100 if b else 0.0
    out = {"tgt": len(d), "act": int(d["Is_Selesai"].sum())}
    for t in ("PMS", "PMG"):
        x = d[d["Tipe_PM"] == t]
        out[f"tgt_{t.lower()}"] = len(x)
        out[f"act_{t.lower()}"] = int(x["Is_Selesai"].sum())
    oth = d[~d["Tipe_PM"].isin(["PMS", "PMG"])]
    out["tgt_other"], out["act_other"] = len(oth), int(oth["Is_Selesai"].sum())
    out["pct"] = pct(out["act"], out["tgt"])
    out["pct_pms"] = pct(out["act_pms"], out["tgt_pms"])
    out["pct_pmg"] = pct(out["act_pmg"], out["tgt_pmg"])
    return out


def generate_leaderboard(d: pd.DataFrame) -> pd.DataFrame:
    cols = ["Level", "NOP", "PM", "Target", "Terealisasi", "Ach", "Rank"]
    if d.empty:
        return pd.DataFrame(columns=cols)
    nop = d.groupby("NOP").agg(Target=("NOP", "size"), Real=("Is_Selesai", "sum")).reset_index()
    nop["Ach"] = nop["Real"] / nop["Target"]
    nop["Rank"] = nop["Ach"].rank(method="min", ascending=False).astype(int)
    det = d.groupby(["NOP", "Tipe_PM"]).agg(Target=("NOP", "size"), Real=("Is_Selesai", "sum")).reset_index()
    det["Ach"] = det["Real"] / det["Target"]

    rows, g_t, g_r = [], 0, 0
    for _, r in nop.sort_values(["Rank", "NOP"]).iterrows():
        rows.append(dict(Level="nop", NOP=r["NOP"], PM="Total NOP", Target=int(r["Target"]),
                         Terealisasi=int(r["Real"]), Ach=float(r["Ach"]), Rank=int(r["Rank"])))
        g_t += int(r["Target"])
        g_r += int(r["Real"])
        for _, x in det[det["NOP"] == r["NOP"]].sort_values("Tipe_PM").iterrows():
            rows.append(dict(Level="detail", NOP="", PM=x["Tipe_PM"], Target=int(x["Target"]),
                             Terealisasi=int(x["Real"]), Ach=float(x["Ach"]), Rank=None))
    rows.append(dict(Level="grand", NOP="Grand Total", PM="", Target=g_t, Terealisasi=g_r,
                     Ach=(g_r / g_t if g_t else 0.0), Rank=None))
    return pd.DataFrame(rows, columns=cols)


def daily_pivot(done: pd.DataFrame) -> pd.DataFrame:
    """Realisasi harian per tipe (kolom PMS, PMG, Lainnya, Total) dengan index tanggal."""
    p = (done.assign(Tgl=done["Submitted Date"].dt.date)
         .groupby(["Tgl", "Tipe_PM"]).size().unstack(fill_value=0))
    for c in ("PMS", "PMG"):
        if c not in p.columns:
            p[c] = 0
    p["Total"] = p.sum(axis=1)
    return p.sort_index()


def build_daily_recap(done: pd.DataFrame) -> pd.DataFrame:
    """Rekap harian berjenjang: baris tanggal (total) lalu rincian per NOP."""
    cols = ["Tanggal Submit", "NOP", "Selesai PMS (Site)", "Selesai PMG (Genset)", "Total Disubmit"]
    if done.empty:
        return pd.DataFrame(columns=cols)
    pt = (done.assign(Tgl=done["Submitted Date"].dt.date)
          .groupby(["Tgl", "NOP", "Tipe_PM"]).size().unstack(fill_value=0))
    for c in ("PMS", "PMG"):
        if c not in pt.columns:
            pt[c] = 0
    pt["Total"] = pt.sum(axis=1)
    pt = pt.reset_index()
    rows = []
    for tgl in sorted(pt["Tgl"].unique(), reverse=True):
        x = pt[pt["Tgl"] == tgl]
        rows.append({cols[0]: fmt_date(tgl), cols[1]: "TOTAL HARIAN", cols[2]: int(x["PMS"].sum()),
                     cols[3]: int(x["PMG"].sum()), cols[4]: int(x["Total"].sum())})
        for _, r in x.sort_values("NOP").iterrows():
            rows.append({cols[0]: "", cols[1]: r["NOP"], cols[2]: int(r["PMS"]),
                         cols[3]: int(r["PMG"]), cols[4]: int(r["Total"])})
    return pd.DataFrame(rows, columns=cols)


def data_quality(d: pd.DataFrame) -> list:
    msgs = []
    n = int(d["Schedule Date"].isna().sum())
    if n:
        msgs.append(f"{fmt_int(n)} baris tanpa Schedule Date tidak ikut dihitung di Kurva S.")
    n = int((d["Is_Selesai"] & d["Submitted Date"].isna()).sum())
    if n:
        msgs.append(f"{fmt_int(n)} tiket berstatus selesai tidak punya Submitted Date, "
                    "sehingga tidak muncul di kurva aktual dan rekap harian.")
    n = int((d["Tipe_PM"] == "Lainnya").sum())
    if n:
        msgs.append(f"{fmt_int(n)} baris berasal dari file yang tipe PM-nya belum dikenali (Lainnya).")
    n = int((d["NOP"] == NO_NOP).sum())
    if n:
        msgs.append(f"{fmt_int(n)} baris tidak memiliki NOP.")
    return msgs


def build_insights(d, k, lb, plan_pct, as_of, done) -> list:
    """Ringkasan eksekutif otomatis -> [(nada, teks)]. Nada: good | warn | bad | info."""
    out = []
    if as_of is not None and k["tgt"]:
        dev = k["pct"] - plan_pct
        tone = "good" if dev >= 0 else ("warn" if dev > -5 else "bad")
        verb = "di atas" if dev >= 0 else "di bawah"
        out.append((tone, f"Progres {fmt_pct(k['pct'])} berada {abs(dev):.1f} poin {verb} plan "
                          f"({fmt_pct(plan_pct)}) per {fmt_date(as_of)}."))
    if as_of is not None:
        late = d[(~d["Is_Selesai"]) & (d["Schedule Date"] < as_of)]
        if len(late):
            by = late.groupby("Tipe_PM").size()
            parts = ", ".join(f"{TIPE_LABEL.get(t, t)}: {fmt_int(n)}" for t, n in by.items())
            out.append(("warn", f"{fmt_int(len(late))} pekerjaan sudah melewati jadwal dan belum selesai ({parts})."))
    nops = lb[lb["Level"] == "nop"]
    if len(nops) >= 2:
        best, worst = nops.iloc[0], nops.iloc[-1]
        out.append(("info", f"Pencapaian tertinggi: {best['NOP']} ({best['Ach'] * 100:.0f}%). "
                            f"Perlu perhatian: {worst['NOP']} ({worst['Ach'] * 100:.0f}%)."))
    if not done.empty and done["Submitted Date"].notna().any():
        last = done["Submitted Date"].max()
        x = done[done["Submitted Date"] == last]
        out.append(("info", f"Realisasi terakhir pada {fmt_date(last)}: {fmt_int(len(x))} unit "
                            f"(PMS {fmt_int((x['Tipe_PM'] == 'PMS').sum())}, "
                            f"PMG {fmt_int((x['Tipe_PM'] == 'PMG').sum())})."))
    return out


# ==========================================================================
# 5. PENYIMPANAN GOOGLE SHEETS (tab "SiPM")
# ==========================================================================
@dataclass
class SyncResult:
    status: str          # created | added | updated | unchanged | locked | error
    message: str = ""
    rows: int = 0


class SheetSchemaError(Exception):
    """Tab sudah berisi data lain; aplikasi menolak menimpanya."""


class ConflictError(Exception):
    """Isi sheet berubah (mis. diedit pengguna lain) di tengah proses penyimpanan."""


def _col(n: int) -> str:
    """Nomor kolom (mulai 1) -> huruf kolom A1."""
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _runs(rows):
    """[3,4,5,9,10] -> [(3,5),(9,10)]"""
    rows = sorted(rows)
    out, start, prev = [], rows[0], rows[0]
    for r in rows[1:]:
        if r != prev + 1:
            out.append((start, prev))
            start = r
        prev = r
    out.append((start, prev))
    return out


def _ensure_worksheet(sh, name: str):
    """Ambil tab; buat bila belum ada. Menolak tab berisi data dengan struktur lain."""
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=2000, cols=len(SCHEMA))
        ws.update(values=[SCHEMA], range_name="A1")
        _format_header(ws)
        return ws

    values = ws.get_all_values()
    has_data = any(any(c.strip() for c in row) for row in values)
    if has_data:
        head = [c.strip() for c in (values[0] if values else [])][:len(SCHEMA)]
        if head != SCHEMA:
            raise SheetSchemaError(
                f"Tab '{name}' sudah berisi data dengan struktur lain. Aplikasi tidak akan menimpanya. "
                f"Kosongkan tab tersebut, atau isi worksheet_name di secrets dengan nama tab baru.")
    else:
        if ws.col_count < len(SCHEMA):
            ws.resize(cols=len(SCHEMA))
        ws.update(values=[SCHEMA], range_name="A1")
        _format_header(ws)
    if ws.col_count < len(SCHEMA):
        ws.resize(cols=len(SCHEMA))
    return ws


def _format_header(ws):
    try:
        ws.format(f"A1:{_col(len(SCHEMA))}1", {"textFormat": {"bold": True}})
    except Exception:  # noqa: BLE001  (pemformatan hanya kosmetik)
        pass


class SheetStore:
    """Semua operasi baca/tulis ke tab SiPM. Satu baris sheet = satu tiket PM."""

    def __init__(self, ws):
        self.ws = ws
        self._lock = threading.RLock()   # serialisasi penulisan antar sesi dalam satu server

    # ---- util -------------------------------------------------------------
    def _retry(self, fn, *args, **kwargs):
        delay = 1.0
        for attempt in range(5):
            try:
                return fn(*args, **kwargs)
            except APIError as e:
                code = getattr(getattr(e, "response", None), "status_code", 0)
                if code in (429, 500, 502, 503) and attempt < 4:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise

    def read(self) -> pd.DataFrame:
        """Seluruh isi sheet sebagai teks, ditambah kolom _row (nomor baris di sheet)."""
        vals = self._retry(self.ws.get_all_values)
        n = len(SCHEMA)
        rows = [(list(r) + [""] * n)[:n] for r in vals[1:]]
        df = pd.DataFrame(rows, columns=SCHEMA)
        df.insert(0, "_row", range(2, len(df) + 2))
        return df[df["Period"].str.strip() != ""].reset_index(drop=True)

    def _append(self, values, chunk: int = 1500):
        for i in range(0, len(values), chunk):
            self._retry(self.ws.append_rows, values[i:i + chunk], value_input_option="RAW")

    def _verify(self, rows, period, types=None, allow_locked=False):
        """Pastikan baris target masih milik periode yang dimaksud sebelum diubah/dihapus."""
        vals = self._retry(self.ws.get, f"A1:C{max(rows)}")
        for r in rows:
            row = list(vals[r - 1]) if r - 1 < len(vals) else []
            row += [""] * (3 - len(row))
            ok = (row[0] == period and (types is None or row[2] in types)
                  and (allow_locked or row[1] != LOCKED))
            if not ok:
                raise ConflictError("Isi sheet berubah saat proses berjalan (kemungkinan diedit pengguna lain). "
                                    "Tidak ada baris yang dihapus. Silakan ulangi.")

    def _delete_rows(self, rows):
        reqs = [{"deleteDimension": {"range": {"sheetId": self.ws.id, "dimension": "ROWS",
                                              "startIndex": s - 1, "endIndex": e}}}
                for s, e in reversed(_runs(rows))]          # dari bawah ke atas agar indeks tetap valid
        self._retry(self.ws.spreadsheet.batch_update, {"requests": reqs})

    def _write_lock_cols(self, upd: pd.DataFrame):
        upd = upd.sort_values("_row")
        data = []
        for s, e in _runs(upd["_row"].tolist()):
            sub = upd[(upd["_row"] >= s) & (upd["_row"] <= e)]
            for a, b in LOCK_SPANS:
                ia, ib = SCHEMA.index(a), SCHEMA.index(b)
                data.append({"range": f"{_col(ia + 1)}{s}:{_col(ib + 1)}{e}",
                             "values": sub[SCHEMA[ia:ib + 1]].values.tolist()})
        self._retry(self.ws.batch_update, data, value_input_option="RAW")

    # ---- operasi bisnis ---------------------------------------------------
    def save_period(self, period: str, frame: pd.DataFrame, operator: str, config: dict) -> SyncResult:
        """Simpan hasil upload sebagai DRAFT. Yang diganti hanya baris periode ini dengan tipe PM yang sama."""
        with self._lock:
            cur = self.read()
            pr = cur[cur["Period"] == period]
            if (pr["Lock_Status"] == LOCKED).any():
                return SyncResult("locked", "Periode sudah dikunci.")
            types = set(frame["Tipe_PM"].unique())
            scope = pr[pr["Tipe_PM"].isin(types)]
            if not scope.empty and canon_hash(scope) == canon_hash(frame):
                return SyncResult("unchanged", rows=len(frame))

            out = frame.copy()
            out.insert(0, "Period", period)
            out["Lock_Status"] = DRAFT
            out["Saved_At"], out["Saved_By"] = stamp(), operator or "-"
            for c in ("Locked_At", "Locked_By", "Lock_Hash", "Audit_Trail"):
                out[c] = ""
            out["Config_JSON"] = json.dumps(config, ensure_ascii=False)
            self._append(out[SCHEMA].values.tolist())          # tulis data baru dulu

            if scope.empty:
                return SyncResult("created" if pr.empty else "added", rows=len(out))
            rows = scope["_row"].tolist()
            self._verify(rows, period, types)                  # lalu hapus baris lama dengan aman
            self._delete_rows(rows)
            return SyncResult("updated", rows=len(out))

    def lock_period(self, period: str, operator: str, config: dict) -> bool:
        with self._lock:
            cur = self.read()
            pr = cur[cur["Period"] == period]
            if pr.empty:
                raise ValueError("Periode tidak ditemukan di sheet.")
            if (pr["Lock_Status"] == LOCKED).any():
                return False
            self._verify(pr["_row"].tolist(), period)
            now = stamp()
            prev = next((a for a in pr["Audit_Trail"] if a), "")
            upd = pr.copy()
            upd["Lock_Status"], upd["Locked_At"], upd["Locked_By"] = LOCKED, now, operator
            upd["Lock_Hash"] = canon_hash(pr)
            upd["Audit_Trail"] = (prev + " | " if prev else "") + f"LOCK {now} oleh {operator}"
            upd["Config_JSON"] = json.dumps(config, ensure_ascii=False)
            self._write_lock_cols(upd)
            return True

    def unlock_period(self, period: str, operator: str) -> bool:
        with self._lock:
            cur = self.read()
            pr = cur[cur["Period"] == period]
            if pr.empty or not (pr["Lock_Status"] == LOCKED).any():
                return False
            self._verify(pr["_row"].tolist(), period, allow_locked=True)
            now = stamp()
            prev = next((a for a in pr["Audit_Trail"] if a), "")
            upd = pr.copy()
            upd["Lock_Status"] = DRAFT
            for c in ("Locked_At", "Locked_By", "Lock_Hash"):
                upd[c] = ""
            upd["Audit_Trail"] = (prev + " | " if prev else "") + f"UNLOCK {now} oleh {operator}"
            self._write_lock_cols(upd)
            return True

    def delete_draft(self, period: str) -> int:
        with self._lock:
            cur = self.read()
            pr = cur[cur["Period"] == period]
            if pr.empty:
                return 0
            if (pr["Lock_Status"] == LOCKED).any():
                raise PermissionError("Periode terkunci tidak dapat dihapus.")
            rows = pr["_row"].tolist()
            self._verify(rows, period)
            self._delete_rows(rows)
            return len(rows)


@st.cache_resource(show_spinner=False)
def get_store():
    """-> (SheetStore | None, pesan error | None). Dicache agar tidak login ulang di setiap interaksi."""
    if gspread is None:
        return None, "Library gspread belum terpasang (jalankan: pip install gspread)."
    creds = secret("gcp_service_account")
    if not creds:
        return None, "Kredensial Google belum diatur (bagian [gcp_service_account] di secrets)."
    try:
        info = dict(creds)
        if "private_key" in info:
            info["private_key"] = str(info["private_key"]).replace("\\n", "\n")
        gc = gspread.service_account_from_dict(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        sh = gc.open_by_key(str(secret("spreadsheet_id", DEFAULT_SPREADSHEET_ID)))
        ws = _ensure_worksheet(sh, str(secret("worksheet_name", DEFAULT_WORKSHEET)))
        return SheetStore(ws), None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


# ---- ringkasan riwayat ------------------------------------------------------
SUMMARY_COLS = ["Period", "Status", "Rows", "Target", "Real", "Ach", "Ach_PMS", "Ach_PMG",
                "Saved_At", "Saved_By", "Locked_At", "Locked_By", "Integrity", "Audit", "Config"]


def build_summary(hist: pd.DataFrame) -> pd.DataFrame:
    if hist.empty:
        return pd.DataFrame(columns=SUMMARY_COLS)
    out = []
    for p, g in hist.groupby("Period", sort=True):
        locked = bool((g["Lock_Status"] == LOCKED).any())
        latest = g.sort_values("Saved_At").iloc[-1]
        core = expand_period(g, with_payload=False)
        cfg = parse_config(latest["Config_JSON"], core)
        d = apply_takeout(core, cfg["takeout"]).copy()
        d["Is_Selesai"] = done_mask(d, cfg["completed"])
        k = kpis(d)
        integ = ""
        if locked:
            lh = next((x for x in g["Lock_Hash"] if x), "")
            integ = "valid" if lh and lh == canon_hash(g) else "changed"
        lk = g[g["Lock_Status"] == LOCKED]
        out.append({
            "Period": p, "Status": LOCKED if locked else DRAFT, "Rows": len(g),
            "Target": k["tgt"], "Real": k["act"], "Ach": k["pct"],
            "Ach_PMS": k["pct_pms"], "Ach_PMG": k["pct_pmg"],
            "Saved_At": latest["Saved_At"], "Saved_By": latest["Saved_By"],
            "Locked_At": lk["Locked_At"].iloc[0] if locked else "",
            "Locked_By": lk["Locked_By"].iloc[0] if locked else "",
            "Integrity": integ,
            "Audit": next((a for a in g["Audit_Trail"] if a), ""),
            "Config": json.dumps(cfg, ensure_ascii=False),
        })
    return pd.DataFrame(out, columns=SUMMARY_COLS).sort_values("Period", ascending=False).reset_index(drop=True)


@st.cache_data(ttl=60, show_spinner=False)
def load_history(_store):
    """Baca seluruh arsip sekali (cache 60 dtk, dibersihkan setiap ada penulisan)."""
    hist = _store.read()
    return hist, build_summary(hist)


# ==========================================================================
# 6. TAMPILAN: CSS, KOMPONEN HTML, GRAFIK, EKSPOR
# ==========================================================================
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap');
:root{--ink:#0F1F33;--text:#1E293B;--muted:#64748B;--line:#DDE3EA;--soft:#F8FAFC;--blue:#1457B8;--teal:#0F8B8D;--amber:#B7791F;--red:#B42318;--green:#12805C}
.stApp,.stApp h1,.stApp h2,.stApp h3,.stApp h4,.stApp label,.stApp button,.stApp input,.stApp textarea{font-family:'IBM Plex Sans','Segoe UI','Helvetica Neue',Arial,sans-serif}
.block-container{padding-top:1.3rem;padding-bottom:3rem;max-width:1480px}
#MainMenu,footer,.stDeployButton{visibility:hidden;display:none}
header[data-testid="stHeader"]{background:transparent}
section[data-testid="stSidebar"]{background:#fff;border-right:1px solid var(--line)}
button[data-baseweb="tab"]{font-weight:600}

.cmd{background:var(--ink);border-radius:8px;padding:18px 24px;display:flex;justify-content:space-between;align-items:center;gap:20px;flex-wrap:wrap}
.cmd-l{display:flex;align-items:center;gap:14px}
.cmd-t{font-size:21px;font-weight:600;color:#fff;line-height:1.2}
.cmd-s{font-size:13px;color:#9FB1C7;margin-top:3px}
.cmd-r{display:flex;gap:8px;flex-wrap:wrap}
.chip{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;padding:5px 12px;border-radius:999px;border:1px solid rgba(255,255,255,.22);color:#E6EDF6;white-space:nowrap}
.chip i{width:7px;height:7px;border-radius:50%;display:inline-block;background:#94A3B8}
.chip.ok i{background:#34D399}.chip.warn i{background:#F5B342}.chip.lock i{background:#7FB2FF}.chip.bad i{background:#F87171}

.sb-brand{display:flex;align-items:center;gap:10px;margin:2px 0 6px}
.sb-name{font-weight:600;font-size:15px;color:var(--ink);line-height:1.2}
.sb-ver{font-size:12px;color:var(--muted)}
.sb-label{font-size:12.5px;font-weight:600;color:var(--muted);margin:18px 0 4px}

.banner{display:flex;align-items:center;gap:12px;border:1px solid var(--line);border-left-width:4px;border-radius:6px;background:#fff;padding:11px 16px;margin:14px 0 6px;font-size:13.5px;color:var(--text)}
.banner b{color:var(--ink)}
.banner.locked{border-left-color:var(--blue)}.banner.draft{border-left-color:var(--amber)}.banner.local{border-left-color:var(--muted)}
.tag{font-size:12px;font-weight:600;padding:2px 9px;border-radius:4px;white-space:nowrap}
.tag.locked{background:#E6EFFB;color:var(--blue)}.tag.draft{background:#FBF1DC;color:#8A5A0B}.tag.local{background:#EDF1F5;color:var(--muted)}

.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));background:#fff;border:1px solid var(--line);border-radius:8px;margin:12px 0 6px}
.kpi{padding:15px 18px;border-right:1px solid var(--line);min-width:0}
.kpi:last-child{border-right:0}
.kpi-l{font-size:12.5px;color:var(--muted);font-weight:500}
.kpi-v{font-size:27px;font-weight:600;color:var(--ink);line-height:1.25;font-variant-numeric:tabular-nums;margin:3px 0 2px}
.kpi-v.good{color:var(--green)}.kpi-v.bad{color:var(--red)}.kpi-v.warn{color:var(--amber)}
.kpi-s{font-size:12px;color:var(--muted)}
.meter{height:5px;background:#E8EDF3;border-radius:3px;margin-top:8px;overflow:hidden}.meter i{display:block;height:100%;background:var(--blue)}

.note{border:1px solid var(--line);border-left:3px solid var(--muted);background:#fff;padding:9px 14px;margin:6px 0;font-size:13.5px;border-radius:0 6px 6px 0;color:var(--text)}
.note.good{border-left-color:var(--green)}.note.warn{border-left-color:var(--amber)}.note.bad{border-left-color:var(--red)}.note.info{border-left-color:var(--blue)}

.sec{margin:28px 0 8px}.sec-t{font-size:17px;font-weight:600;color:var(--ink)}.sec-s{font-size:13px;color:var(--muted);margin-top:1px}

.lbwrap{background:#fff;border:1px solid var(--line);border-radius:8px;overflow-x:auto}
table.lb{width:100%;border-collapse:collapse;font-size:13.5px;font-variant-numeric:tabular-nums}
table.lb th{background:var(--soft);color:var(--muted);font-weight:600;font-size:12.5px;text-align:left;padding:10px 14px;border-bottom:1px solid var(--line);white-space:nowrap}
table.lb th.n,table.lb td.n{text-align:right}
table.lb td{padding:8px 14px;border-bottom:1px solid #EEF2F6;color:#475569;white-space:nowrap}
table.lb tr.nop td{background:#F5F8FC;font-weight:600;color:var(--ink);border-top:1px solid var(--line)}
table.lb tr.det td:nth-child(2){padding-left:28px}
table.lb tr.grand td{background:#E8EEF7;font-weight:700;color:var(--ink);border-top:2px solid var(--blue);border-bottom:0}
.pbar{display:inline-block;vertical-align:middle;width:110px;height:7px;background:#E8EDF3;border-radius:4px;overflow:hidden;margin-right:9px}.pbar i{display:block;height:100%}
.rk{display:inline-block;min-width:24px;text-align:center;padding:1px 7px;border-radius:4px;font-weight:600;background:#EDF1F5;color:var(--ink)}
.rk.r1{background:#FBEFC8;color:#7A5A00}.rk.r2{background:#E6EAF0;color:#3B4757}.rk.r3{background:#F6DFCB;color:#7A4318}

.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin:8px 0 12px}
.fact{background:#fff;border:1px solid var(--line);border-radius:6px;padding:10px 14px}
.fact-l{font-size:12px;color:var(--muted)}.fact-v{font-size:14.5px;font-weight:600;color:var(--ink);margin-top:2px;word-break:break-word}
.foot{margin-top:36px;padding-top:12px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}
</style>
"""

LOGO = ('<svg width="34" height="34" viewBox="0 0 34 34" aria-hidden="true"><rect width="34" height="34" rx="7" fill="#1457B8"/>'
        '<path d="M7 25 C15 25 13 9 21 9 L27 9" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/>'
        '<circle cx="27" cy="9" r="2.4" fill="#fff"/></svg>')

FONT_STACK = "IBM Plex Sans, Segoe UI, Helvetica Neue, Arial, sans-serif"
_PLOT_HAS_WIDTH = "width" in inspect.signature(st.plotly_chart).parameters


def show_plot(fig, key=None):
    """st.plotly_chart yang kompatibel lintas versi Streamlit."""
    if _PLOT_HAS_WIDTH:
        st.plotly_chart(fig, width="stretch", key=key)
    else:
        st.plotly_chart(fig, use_container_width=True, key=key)


def show_df(df, **kw):
    """st.dataframe yang kompatibel lintas versi Streamlit."""
    try:
        st.dataframe(df, width="stretch", **kw)
    except Exception:  # noqa: BLE001  (versi lama: width harus angka)
        st.dataframe(df, use_container_width=True, **kw)


def section(title: str, sub: str = ""):
    s = f'<div class="sec-s">{esc(sub)}</div>' if sub else ""
    st.markdown(f'<div class="sec"><div class="sec-t">{esc(title)}</div>{s}</div>', unsafe_allow_html=True)


def side_label(text: str):
    st.markdown(f'<div class="sb-label">{esc(text)}</div>', unsafe_allow_html=True)


def command_bar(chips) -> str:
    chip_html = "".join(f'<span class="chip {c}"><i></i>{esc(t)}</span>' for c, t in chips)
    return (f'<div class="cmd"><div class="cmd-l">{LOGO}<div><div class="cmd-t">{esc(APP_NAME)}</div>'
            f'<div class="cmd-s">Kurva S, leaderboard NOP, dan arsip bulanan untuk Preventive Maintenance PMS dan PMG</div>'
            f'</div></div><div class="cmd-r">{chip_html}</div></div>')


def kpi_strip(items) -> str:
    """items: [(label, nilai, sub_html, nada, persen_meter|None)]"""
    cells = []
    for label, value, sub, tone, meter in items:
        m = f'<div class="meter"><i style="width:{max(0, min(100, meter)):.1f}%"></i></div>' if meter is not None else ""
        cells.append(f'<div class="kpi"><div class="kpi-l">{esc(label)}</div>'
                     f'<div class="kpi-v {tone}">{esc(value)}</div><div class="kpi-s">{sub}</div>{m}</div>')
    return f'<div class="kpis">{"".join(cells)}</div>'


def note(tone: str, text: str) -> str:
    return f'<div class="note {tone}">{esc(text)}</div>'


def facts(pairs) -> str:
    return ('<div class="facts">' + "".join(
        f'<div class="fact"><div class="fact-l">{esc(k)}</div><div class="fact-v">{esc(v)}</div></div>'
        for k, v in pairs) + '</div>')


def ach_color(p: float) -> str:
    return C_GREEN if p >= 0.9 else (C_BLUE if p >= 0.7 else (C_AMBER if p >= 0.5 else C_RED))


def leaderboard_html(lb: pd.DataFrame) -> str:
    rows = []
    for r in lb.itertuples(index=False):
        p = float(r.Ach)
        bar = (f'<span class="pbar"><i style="width:{max(0, min(100, p * 100)):.1f}%;background:{ach_color(p)}"></i></span>'
               f'{p * 100:.0f}%')
        rk = ""
        if r.Level == "nop" and not pd.isna(r.Rank):
            n = int(r.Rank)
            rk = f'<span class="rk {"r" + str(n) if n <= 3 else ""}">{n}</span>'
        name = esc(r.NOP) if r.Level != "detail" else ""
        pm = esc(TIPE_LABEL.get(r.PM, r.PM))
        rows.append(f'<tr class="{r.Level}"><td>{name}</td><td>{pm}</td><td class="n">{fmt_int(r.Target)}</td>'
                    f'<td class="n">{fmt_int(r.Terealisasi)}</td><td>{bar}</td><td class="n">{rk}</td></tr>')
    head = ('<tr><th>NOP</th><th>Kategori</th><th class="n">Target</th><th class="n">Terealisasi</th>'
            '<th>Pencapaian</th><th class="n">Peringkat</th></tr>')
    return f'<div class="lbwrap"><table class="lb"><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div>'


# ---- grafik -----------------------------------------------------------------
def _style(fig, height, title=None):
    fig.update_layout(
        title=dict(text=f"<b>{title}</b>", x=0, font=dict(size=15, color=C_INK)) if title else None,
        height=height, margin=dict(l=24, r=24, t=58 if title else 24, b=24),
        plot_bgcolor="white", paper_bgcolor="white", hovermode="x unified",
        font=dict(family=FONT_STACK, size=12, color="#334155"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
    fig.update_xaxes(showgrid=False, linecolor=C_LINE)
    fig.update_yaxes(gridcolor="#EEF2F6", linecolor=C_LINE)
    return fig


def draw_scurve_chart(tl, title, unit_text, as_of=None):
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(x=tl["Date"], y=tl["Target_Unit"], name=f"Target harian ({unit_text})",
                         opacity=0.4, marker_color="#C5CFDB", hoverinfo="x+y"), secondary_y=True)
    fig.add_trace(go.Scatter(x=tl["Date"], y=tl["Target_Kumulatif"], mode="lines", name="Plan kumulatif (%)",
                             line=dict(color=C_INK, width=2.5, dash="dash")), secondary_y=False)
    fig.add_trace(go.Scatter(x=tl["Date"], y=tl["Actual_Kumulatif_Plot"], mode="lines+markers",
                             name="Aktual kumulatif (%)", line=dict(color=C_TEAL, width=3), marker=dict(size=5),
                             fill="tozeroy", fillcolor="rgba(15,139,141,0.10)"), secondary_y=False)
    li = tl["Actual_Kumulatif_Plot"].last_valid_index()
    if li is not None:
        fig.add_annotation(x=tl.loc[li, "Date"], y=tl.loc[li, "Actual_Kumulatif_Plot"],
                           text=f"<b>{tl.loc[li, 'Actual_Kumulatif_Plot']:.1f}%</b>", showarrow=True, arrowhead=2,
                           arrowcolor=C_TEAL, ax=-40, ay=-32, font=dict(color="white", size=12),
                           bgcolor=C_TEAL, bordercolor=C_TEAL, borderpad=4)
    fig.add_annotation(x=tl["Date"].iloc[-1], y=tl["Target_Kumulatif"].iloc[-1],
                       text=f"<b>{tl['Target_Kumulatif'].iloc[-1]:.1f}%</b>", showarrow=True, arrowhead=2,
                       arrowcolor=C_INK, ax=-40, ay=32, font=dict(color="white", size=12),
                       bgcolor=C_INK, bordercolor=C_INK, borderpad=4)
    if as_of is not None:
        x = min(max(pd.Timestamp(as_of), tl["Date"].min()), tl["Date"].max()).to_pydatetime()
        fig.add_shape(type="line", x0=x, x1=x, y0=0, y1=1, xref="x", yref="paper",
                      line=dict(color="#94A3B8", width=1.5, dash="dot"))
        fig.add_annotation(x=x, y=1, yref="paper", text=f"Per {fmt_date(as_of)}", showarrow=False,
                           yanchor="bottom", font=dict(size=11, color=C_MUTED))
    _style(fig, 430, title)
    fig.update_xaxes(tickformat="%d %b")
    fig.update_yaxes(title_text="Progres (%)", range=[0, 105], secondary_y=False)
    fig.update_yaxes(title_text="Volume", showgrid=False, secondary_y=True)
    return fig


def draw_daily_chart(dp: pd.DataFrame):
    x = pd.to_datetime(dp.index)
    fig = go.Figure()
    for col, name, color in (("PMS", "PMS (Site)", C_BLUE), ("PMG", "PMG (Genset)", C_AMBER)):
        fig.add_trace(go.Bar(x=x, y=dp[col], name=name, marker_color=color,
                             text=[v if v else "" for v in dp[col]], textposition="outside"))
    _style(fig, 400, "Volume realisasi harian")
    fig.update_layout(barmode="group")
    fig.update_xaxes(tickformat="%d %b %Y", title_text="Tanggal submit")
    fig.update_yaxes(title_text="Unit")
    return fig


def draw_nop_chart(lb: pd.DataFrame):
    n = lb[lb["Level"] == "nop"].iloc[::-1]
    fig = go.Figure(go.Bar(
        y=n["NOP"], x=n["Ach"] * 100, orientation="h", marker_color=[ach_color(p) for p in n["Ach"]],
        text=[f"{p * 100:.0f}%" for p in n["Ach"]], textposition="outside",
        customdata=np.stack([n["Terealisasi"], n["Target"]], axis=-1),
        hovertemplate="%{y}<br>%{customdata[0]} dari %{customdata[1]} unit<extra></extra>"))
    _style(fig, max(260, 46 * len(n) + 90), "Pencapaian per NOP")
    fig.update_layout(hovermode="closest", showlegend=False)
    fig.update_xaxes(range=[0, 112], title_text="Pencapaian (%)", showgrid=True, gridcolor="#EEF2F6")
    fig.update_yaxes(showgrid=False)
    return fig


def draw_history_chart(summary: pd.DataFrame):
    s = summary.sort_values("Period")
    x = [period_label(p) for p in s["Period"]]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=x, y=s["Ach_PMS"], name="PMS (Site)", marker_color=C_BLUE))
    fig.add_trace(go.Bar(x=x, y=s["Ach_PMG"], name="PMG (Genset)", marker_color=C_AMBER))
    fig.add_trace(go.Scatter(x=x, y=s["Ach"], name="Total", mode="lines+markers",
                             line=dict(color=C_INK, width=2.5), marker=dict(size=7)))
    _style(fig, 380, "Pencapaian akhir per periode")
    fig.update_layout(barmode="group")
    fig.update_yaxes(range=[0, 105], title_text="Pencapaian (%)")
    return fig


# ---- ekspor Excel -----------------------------------------------------------
def _dates_to_date(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.date
    return out


def build_excel(period_txt: str, k: dict, lb: pd.DataFrame, recap: pd.DataFrame, d: pd.DataFrame) -> bytes:
    from openpyxl.styles import Font, PatternFill

    summary = pd.DataFrame([
        ("Periode", period_txt),
        ("Total target (unit)", k["tgt"]), ("Total terealisasi (unit)", k["act"]),
        ("Progres keseluruhan (%)", round(k["pct"], 2)),
        ("PMS target", k["tgt_pms"]), ("PMS terealisasi", k["act_pms"]), ("PMS progres (%)", round(k["pct_pms"], 2)),
        ("PMG target", k["tgt_pmg"]), ("PMG terealisasi", k["act_pmg"]), ("PMG progres (%)", round(k["pct_pmg"], 2)),
    ], columns=["Indikator", "Nilai"])
    lbx = lb.rename(columns={"PM": "Kategori", "Ach": "Pencapaian (%)", "Rank": "Peringkat"}).drop(columns=["Level"])
    lbx["Pencapaian (%)"] = (lbx["Pencapaian (%)"] * 100).round(1)
    lbx["Peringkat"] = lbx["Peringkat"].apply(lambda v: "" if pd.isna(v) else int(v))
    pend = lambda t: _dates_to_date(d[(d["Tipe_PM"] == t) & (~d["Is_Selesai"])])  # noqa: E731
    sheets = {"Ringkasan": summary, "Leaderboard": lbx, "Rekap Harian": recap,
              "Pending PMS": pend("PMS"), "Pending PMG": pend("PMG"), "Data Lengkap": _dates_to_date(d)}
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for name, df in sheets.items():
            df.to_excel(w, sheet_name=name, index=False)
            ws = w.sheets[name]
            for c in ws[1]:
                c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0F1F33")
            for i, col in enumerate(df.columns, 1):
                longest = max([len(str(col))] + [len(str(v)) for v in df[col].head(300)]) if len(df) else len(str(col))
                ws.column_dimensions[_col(i)].width = min(48, longest + 2)
            ws.freeze_panes = "A2"
    return buf.getvalue()


# ---- kata pengunci ----------------------------------------------------------
def check_lock_keyword(value: str) -> bool:
    cfg = secret("lock_keyword")
    if cfg:
        return hmac.compare_digest(value.strip().encode(), str(cfg).strip().encode())
    return value.strip().upper() == DEFAULT_LOCK_WORD


def check_admin_keyword(value: str) -> bool:
    cfg = secret("admin_keyword")
    return bool(cfg) and hmac.compare_digest(value.strip().encode(), str(cfg).strip().encode())


@st.cache_resource
def _attempts() -> dict:
    """Penghitung percobaan gagal yang dipakai bersama semua sesi di server ini."""
    return {"count": 0, "until": 0.0}


def attempts_blocked() -> bool:
    return time.time() < _attempts()["until"]


def register_fail():
    a = _attempts()
    a["count"] += 1
    if a["count"] >= MAX_FAILED_ATTEMPTS:
        a["until"], a["count"] = time.time() + 300, 0


def reset_attempts():
    _attempts()["count"] = 0


# ==========================================================================
# 7. ANTARMUKA
# ==========================================================================
STATUS_TXT = {DRAFT: "Draft", LOCKED: "Terkunci"}
INTEGRITY_TXT = {"valid": "Terverifikasi", "changed": "Data berubah", "": "-"}


@dataclass
class Ctx:
    mode: str = MODE_UPLOAD
    operator: str = ""
    period: str | None = None
    df_raw: pd.DataFrame | None = None
    meta: dict | None = None          # ringkasan periode di arsip (None = belum tersimpan)
    sync: SyncResult | None = None
    completed: list = field(default_factory=list)
    takeout: list = field(default_factory=list)
    nops: list = field(default_factory=list)
    locked: bool = False


def open_in_dashboard(period: str):
    st.session_state["src_mode"] = MODE_ARCHIVE
    st.session_state["arch_period"] = period
    st.session_state["nav"] = NAV_ITEMS[0]


def _meta(summary: pd.DataFrame, period):
    if period is None or summary.empty or period not in set(summary["Period"]):
        return None
    return summary[summary["Period"] == period].iloc[0].to_dict()


def render_sidebar(store, store_err, hist, summary):
    ctx = Ctx()
    saved = list(summary["Period"]) if len(summary) else []
    status_of = dict(zip(summary["Period"], summary["Status"])) if len(summary) else {}

    def fmt(p):
        return f"{period_label(p)} ({STATUS_TXT.get(status_of.get(p), 'Baru')})"

    with st.sidebar:
        st.markdown(f'<div class="sb-brand">{LOGO}<div><div class="sb-name">SiPM</div>'
                    f'<div class="sb-ver">Versi {APP_VERSION}</div></div></div>', unsafe_allow_html=True)
        side_label("Operator")
        ctx.operator = st.text_input("Nama operator", key="operator", label_visibility="collapsed",
                                     placeholder="Nama Anda, tercatat di riwayat").strip()

        side_label("Sumber data")
        modes = [MODE_UPLOAD] + ([MODE_ARCHIVE] if saved else [])
        if st.session_state.get("src_mode") not in modes:
            st.session_state["src_mode"] = MODE_UPLOAD
        ctx.mode = st.radio("Sumber data", modes, key="src_mode", label_visibility="collapsed")

        if ctx.mode == MODE_UPLOAD:
            files = st.file_uploader(
                "File PM (Excel)", type=["xlsx", "xls"], accept_multiple_files=True, key="uploader",
                help="Unggah file PM Site dan PM Genset. Tipe dikenali dari nama file (SITE/PMS atau GENSET/PMG).")
            parsed = []
            for f in files or []:
                df, warns, err = parse_excel(f.getvalue(), f.name)
                if err:
                    st.error(f"{f.name}: {err}")
                else:
                    parsed.append((f, df, warns))

            if parsed:
                unknown = any(assign_tipe_pm(f.name) == "Lainnya" for f, _, _ in parsed)
                with st.expander("Validasi file", expanded=unknown):
                    entries = []
                    for i, (f, df, warns) in enumerate(parsed):
                        det = assign_tipe_pm(f.name)
                        tipe = st.selectbox(f"{f.name} ({fmt_int(len(df))} baris)", TIPE_OPTIONS,
                                            index=TIPE_OPTIONS.index(det), format_func=TIPE_LABEL.get,
                                            key=f"tipe_{i}_{f.name}_{f.size}")
                        for w in warns:
                            st.caption(w)
                        entries.append((f.name, df, tipe))
                df_up = build_upload(entries)

                detected = detect_period(df_up)
                default = detected or today_local().strftime("%Y-%m")
                opts = period_options(saved, [default])
                side_label("Periode data")
                ctx.period = st.selectbox("Periode data", opts, index=opts.index(default), format_func=fmt,
                                          key=f"period_{default}", label_visibility="collapsed")
                if detected and ctx.period != detected:
                    st.warning(f"Mayoritas Schedule Date ada di {period_label(detected)}, "
                               f"sedangkan periode yang dipilih {period_label(ctx.period)}.")

                ctx.df_raw = df_up
                if store is not None:
                    statuses = sorted({s.upper() for s in df_up["Status"] if s})
                    frame = to_store_frame(df_up)
                    sig = (ctx.period, canon_hash(frame))
                    if st.session_state.get("sync_sig") != sig:      # simpan hanya bila isinya benar-benar baru
                        with st.spinner("Menyimpan ke Google Sheets..."):
                            try:
                                res = store.save_period(ctx.period, frame, ctx.operator, default_config(statuses))
                            except Exception as e:  # noqa: BLE001
                                res = SyncResult("error", f"{type(e).__name__}: {e}")
                        st.session_state["sync_sig"], st.session_state["sync_res"] = sig, res
                        load_history.clear()
                        try:
                            hist, summary = load_history(store)
                        except Exception as e:  # noqa: BLE001
                            st.session_state["sync_res"] = SyncResult("error", f"Gagal membaca arsip: {e}")
                    ctx.sync = st.session_state.get("sync_res")
                    if ctx.sync and ctx.sync.status != "error" and ctx.period in set(hist["Period"]):
                        ctx.df_raw = expand_period(hist[hist["Period"] == ctx.period])
                    ctx.meta = _meta(summary, ctx.period)
        else:
            side_label("Periode tersimpan")
            if st.session_state.get("arch_period") not in saved:
                st.session_state.pop("arch_period", None)
            ctx.period = st.selectbox("Periode tersimpan", saved, key="arch_period", format_func=fmt,
                                      label_visibility="collapsed")
            ctx.df_raw = expand_period(hist[hist["Period"] == ctx.period])
            ctx.meta = _meta(summary, ctx.period)

        # ---- pengaturan kurva & filter -----------------------------------
        if ctx.df_raw is not None and len(ctx.df_raw):
            statuses = sorted({s.upper() for s in ctx.df_raw["Status"] if s})
            ctx.locked = bool(ctx.meta and ctx.meta["Status"] == LOCKED)
            cfg = parse_config(ctx.meta["Config"], ctx.df_raw) if ctx.meta else default_config(statuses)
            tag = hashlib.md5(("|".join(statuses) + str(ctx.locked)).encode()).hexdigest()[:8]
            side_label("Logika kurva")
            ctx.completed = st.multiselect(
                "Status dihitung selesai", statuses, default=[s for s in cfg["completed"] if s in statuses],
                key=f"done_{ctx.period}_{tag}", disabled=ctx.locked,
                help="Pembentuk kurva aktual dan angka realisasi.")
            ctx.takeout = st.multiselect(
                "Status dikeluarkan dari target", statuses, default=[s for s in cfg["takeout"] if s in statuses],
                key=f"take_{ctx.period}_{tag}", disabled=ctx.locked,
                help="Tiket dengan status ini tidak dihitung sama sekali dalam target.")
            if ctx.locked:
                st.caption("Periode terkunci: pengaturan mengikuti snapshot saat penguncian.")
            side_label("Filter area")
            nops = sorted(ctx.df_raw["NOP"].unique())
            ntag = hashlib.md5("|".join(nops).encode()).hexdigest()[:8]
            ctx.nops = st.multiselect("NOP", nops, default=nops, key=f"nop_{ctx.period}_{ntag}",
                                      label_visibility="collapsed")

        # ---- status koneksi ------------------------------------------------
        st.markdown("---")
        if store is not None:
            st.caption(f"Google Sheets terhubung. Tab: {secret('worksheet_name', DEFAULT_WORKSHEET)}")
            if st.button("Muat ulang data arsip", key="reload_hist"):
                load_history.clear()
                st.rerun()
        else:
            st.caption("Penyimpanan belum terhubung (mode lokal).")
            if store_err:
                with st.expander("Detail"):
                    st.write(store_err)
            if st.button("Coba sambungkan lagi", key="reconnect"):
                get_store.clear()
                load_history.clear()
                st.rerun()
    return ctx, hist, summary


def render_banner(store, ctx):
    m = ctx.meta
    if m is None:
        body = ("Data belum tersimpan. Hubungkan Google Sheets agar setiap upload tersimpan otomatis."
                if store is None else "Periode ini belum tersimpan di arsip.")
        st.markdown(f'<div class="banner local"><span class="tag local">Lokal</span><span>{esc(body)}</span></div>',
                    unsafe_allow_html=True)
    elif ctx.locked:
        integ = {"valid": " Integritas data terverifikasi.",
                 "changed": " Peringatan: isi sheet berbeda dari saat periode dikunci."}.get(m["Integrity"], "")
        st.markdown(f'<div class="banner locked"><span class="tag locked">Terkunci</span><span>'
                    f'<b>{esc(period_label(ctx.period))}</b> dikunci oleh <b>{esc(m["Locked_By"])}</b> '
                    f'pada {esc(m["Locked_At"])}.{esc(integ)}</span></div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="banner draft"><span class="tag draft">Draft</span><span>'
                    f'<b>{esc(period_label(ctx.period))}</b> tersimpan otomatis di Google Sheets '
                    f'({esc(m["Saved_At"])}, oleh {esc(m["Saved_By"])}). Data bisa diganti dengan upload ulang '
                    f'sampai periode dikunci.</span></div>', unsafe_allow_html=True)

    r = ctx.sync
    if ctx.mode == MODE_UPLOAD and r is not None:
        msg = {
            "created": ("good", f"Periode baru: {fmt_int(r.rows)} baris tersimpan otomatis sebagai Draft."),
            "added": ("good", f"{fmt_int(r.rows)} baris ditambahkan ke Draft periode ini."),
            "updated": ("info", f"Draft diperbarui: {fmt_int(r.rows)} baris menggantikan data lama dengan tipe PM yang sama."),
            "locked": ("warn", "Periode ini sudah terkunci. File yang diunggah tidak disimpan; "
                               "dashboard menampilkan arsip terkunci."),
            "error": ("bad", f"Gagal menyimpan ke Google Sheets ({r.message}). "
                             "Dashboard menampilkan data upload yang belum tersimpan."),
        }.get(r.status)
        if msg:
            st.markdown(note(*msg), unsafe_allow_html=True)
        if r.status == "error" and st.button("Coba simpan ulang", key="retry_sync"):
            st.session_state.pop("sync_sig", None)
            st.rerun()


def render_lock_panel(store, ctx):
    if store is None or ctx.meta is None or ctx.locked:
        return
    if ctx.sync is not None and ctx.sync.status == "error":
        return
    with st.expander("Finalisasi dan kunci periode"):
        st.markdown(f"Mengunci **{period_label(ctx.period)}** menyimpan data dan pengaturan kurva saat ini sebagai "
                    "arsip tetap. Setelah terkunci, upload ulang tidak akan mengubahnya dan hanya administrator "
                    "yang dapat membuka kunci.")
        configured = bool(secret("lock_keyword"))
        with st.form(f"lock_{ctx.period}", clear_on_submit=True):
            kw = st.text_input("Kata pengunci", type="password",
                               placeholder="Masukkan kata pengunci" if configured
                               else f"Ketik {DEFAULT_LOCK_WORD} untuk konfirmasi")
            sure = st.checkbox("Saya yakin data periode ini sudah final")
            submit = st.form_submit_button("Kunci periode", type="primary")
        if submit:
            if attempts_blocked():
                st.error("Terlalu banyak percobaan gagal. Coba lagi dalam 5 menit.")
            elif not ctx.operator:
                st.warning("Isi nama operator di sidebar terlebih dahulu.")
            elif not sure:
                st.warning("Centang konfirmasi terlebih dahulu.")
            elif not check_lock_keyword(kw):
                register_fail()
                st.error("Kata pengunci salah.")
            else:
                try:
                    store.lock_period(ctx.period, ctx.operator,
                                      {"completed": ctx.completed, "takeout": ctx.takeout})
                except Exception as e:  # noqa: BLE001
                    st.error(f"Gagal mengunci periode: {type(e).__name__}: {e}")
                    return
                reset_attempts()
                load_history.clear()
                st.toast(f"{period_label(ctx.period)} dikunci dan diarsipkan.")
                st.rerun()


def render_empty_state():
    section("Mulai dengan mengunggah file PM")
    st.markdown(
        "1. Unggah file PM Site dan PM Genset di sidebar.\n"
        "2. Periode terdeteksi otomatis dari Schedule Date. Ubah lewat dropdown bila perlu.\n"
        "3. Data tersimpan otomatis sebagai Draft. Kunci periode setelah datanya final.")


def render_dashboard(store, ctx, d):
    if ctx.df_raw is None or len(ctx.df_raw) == 0:
        render_empty_state()
        return
    render_banner(store, ctx)
    render_lock_panel(store, ctx)
    if d is None or d.empty:
        st.warning("Tidak ada data yang sesuai dengan filter saat ini.")
        return

    ptxt = period_label(ctx.period) if ctx.period else "-"
    k = kpis(d)
    tl_all = calculate_scurve(d, ctx.completed)
    as_of = as_of_date(tl_all)
    plan = plan_at(tl_all, as_of)
    dev = k["pct"] - plan
    late = int(((~d["Is_Selesai"]) & (d["Schedule Date"] < as_of)).sum()) if as_of is not None else 0
    lb = generate_leaderboard(d)
    done = d[d["Is_Selesai"] & d["Submitted Date"].notna()]
    recap = build_daily_recap(done)
    other = f" | Lainnya {fmt_int(k['tgt_other'])}" if k["tgt_other"] else ""

    st.markdown(kpi_strip([
        ("Total target", fmt_int(k["tgt"]), f"PMS {fmt_int(k['tgt_pms'])} | PMG {fmt_int(k['tgt_pmg'])}{other}", "", None),
        ("Terealisasi", fmt_int(k["act"]), f"PMS {fmt_int(k['act_pms'])} | PMG {fmt_int(k['act_pmg'])}", "", None),
        ("Progres keseluruhan", fmt_pct(k["pct"]), f"{fmt_int(k['act'])} dari {fmt_int(k['tgt'])} unit", "", k["pct"]),
        ("Plan per tanggal acuan", fmt_pct(plan) if as_of is not None else "-",
         f"Per {fmt_date(as_of)}" if as_of is not None else "Tidak ada jadwal", "", None),
        ("Selisih terhadap plan", f"{dev:+.1f} poin" if as_of is not None else "-", "Progres dikurangi plan",
         "" if as_of is None else ("good" if dev >= 0 else ("warn" if dev > -5 else "bad")), None),
        ("Lewat jadwal", fmt_int(late), "Belum selesai", "good" if late == 0 else "warn", None),
    ]), unsafe_allow_html=True)

    _, right = st.columns([4, 1])
    with right:
        try:
            xl = build_excel(ptxt, k, lb, recap, d)
            st.download_button("Unduh laporan Excel", xl, file_name=f"SiPM_{ctx.period or 'data'}.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               key="dl_report")
        except Exception as e:  # noqa: BLE001
            st.caption(f"Ekspor Excel tidak tersedia: {e}")

    section("Ringkasan eksekutif", f"Periode {ptxt}")
    items = [note(t, x) for t, x in build_insights(d, k, lb, plan, as_of, done)]
    items += [note("warn", m) for m in data_quality(d)]
    st.markdown("".join(items), unsafe_allow_html=True)

    section("Tren Kurva S", "Plan dan aktual kumulatif, dipisah untuk PMS dan PMG")
    c1, c2 = st.columns(2)
    for col, tipe, title, unit in ((c1, "PMS", "Kurva S PM Site (PMS)", "Site"),
                                   (c2, "PMG", "Kurva S PM Genset (PMG)", "Genset")):
        with col:
            x = d[d["Tipe_PM"] == tipe]
            tl = calculate_scurve(x, ctx.completed) if not x.empty else None
            if tl is None:
                st.info(f"Data {tipe} tidak tersedia pada filter saat ini.")
            else:
                with st.container(border=True):
                    show_plot(draw_scurve_chart(tl, title, unit, as_of), key=f"sc_{tipe}")

    section("Realisasi harian dan pencapaian per NOP")
    c1, c2 = st.columns([3, 2])
    with c1:
        with st.container(border=True):
            if done.empty:
                st.info("Belum ada realisasi yang memiliki tanggal submit.")
            else:
                show_plot(draw_daily_chart(daily_pivot(done)), key="daily")
    with c2:
        with st.container(border=True):
            show_plot(draw_nop_chart(lb), key="nopbar")

    section("Peringkat pencapaian per NOP")
    st.markdown(leaderboard_html(lb), unsafe_allow_html=True)

    section("Rekap harian berjenjang per NOP", "Jumlah tiket yang disubmit, dikelompokkan per tanggal")
    if recap.empty:
        st.info("Belum ada data dengan status terealisasi.")
    else:
        def style_recap(row):
            if row["NOP"] == "TOTAL HARIAN":
                return ["background-color:#E8EEF7;font-weight:600;color:#0F1F33"] * len(row)
            return ["background-color:#FFFFFF;color:#475569"] * len(row)
        show_df(recap.style.apply(style_recap, axis=1), hide_index=True)

    section("Laporan WhatsApp", "Pilih tanggal, lalu salin teks laporan")
    dates = sorted(done["Submitted Date"].dt.date.unique(), reverse=True)
    if dates:
        pick, _ = st.columns([1, 3])
        with pick:
            sel = st.selectbox("Tanggal tarikan data", dates, format_func=fmt_date, key=f"wa_{ctx.period}")
        last = done[done["Submitted Date"].dt.date == sel]
        n_pms, n_pmg = int((last["Tipe_PM"] == "PMS").sum()), int((last["Tipe_PM"] == "PMG").sum())
        wa = f"""📊 *UPDATE PROGRESS PM HARIAN (SiPM)* 📊
🗓️ Tanggal Update: {fmt_date(sel)}
📅 Periode: {ptxt}

Berdasarkan data terbaru, rekap penambahan realisasi PM yang berhasil disubmit pada tanggal tersebut:

✅ *Realisasi Harian: {n_pms + n_pmg} Unit*
🗼 PMS: {n_pms} Site
⚡ PMG: {n_pmg} Genset

📈 *STATUS PENCAPAIAN KUMULATIF*
🎯 Total Target Aktif: {k['tgt']} Unit
✅ Total Terealisasi: {k['act']} Unit ({k['pct']:.2f}%)
🗼 PMS Terealisasi: {k['act_pms']} / {k['tgt_pms']} ({k['pct_pms']:.2f}%)
⚡ PMG Terealisasi: {k['act_pmg']} / {k['tgt_pmg']} ({k['pct_pmg']:.2f}%)

Mohon kerja samanya untuk terus mengawal progress penyelesaian pekerjaan sesuai jadwal. Terima kasih! 🙏"""
        st.caption("Arahkan kursor ke kotak di bawah, lalu klik ikon salin di pojok kanan atas.")
        st.code(wa, language="text")
    else:
        st.info("Belum ada realisasi bertanggal untuk dibuatkan laporan.")

    section("Detail tiket", "Pantau sisa dan pekerjaan yang masih pending")
    t_all, t_pms, t_pmg = st.tabs(["Keseluruhan data", "Pending PMS", "Pending PMG"])
    with t_all:
        show_df(_dates_to_date(d))
    for tab, tipe, noun in ((t_pms, "PMS", "site PMS"), (t_pmg, "PMG", "genset PMG")):
        with tab:
            pend = d[(d["Tipe_PM"] == tipe) & (~d["Is_Selesai"])]
            st.caption(f"{fmt_int(len(pend))} {noun} belum mencapai status selesai yang dipilih.")
            show_df(_dates_to_date(pend))


def render_setup_guide():
    st.markdown(
        "**Menghubungkan Google Sheets**\n\n"
        "1. Buat service account di Google Cloud, aktifkan Google Sheets API, lalu unduh kunci JSON.\n"
        "2. Bagikan spreadsheet ke alamat email service account dengan akses Editor.\n"
        "3. Salin isi kunci JSON ke bagian `[gcp_service_account]` di `.streamlit/secrets.toml` "
        "(lihat `secrets.toml.example`).\n"
        "4. Muat ulang aplikasi. Tab SiPM dibuat otomatis bila belum ada.")


def render_archive(store, store_err, hist, summary, ctx):
    section("Arsip dan riwayat periode", "Semua periode yang tersimpan di tab SiPM pada Google Sheets")
    if store is None:
        st.markdown(note("bad", f"Penyimpanan belum terhubung: {store_err or 'tidak diketahui'}"),
                    unsafe_allow_html=True)
        render_setup_guide()
        return
    if summary.empty:
        st.info("Belum ada periode tersimpan. Unggah file PM untuk membuat periode pertama.")
        return

    show = pd.DataFrame({
        "Periode": [period_label(p) for p in summary["Period"]],
        "Status": [STATUS_TXT[s] for s in summary["Status"]],
        "Target": summary["Target"], "Realisasi": summary["Real"],
        "Pencapaian": summary["Ach"].round(1), "PMS (%)": summary["Ach_PMS"].round(1),
        "PMG (%)": summary["Ach_PMG"].round(1), "Terakhir disimpan": summary["Saved_At"],
        "Disimpan oleh": summary["Saved_By"], "Dikunci pada": summary["Locked_At"],
        "Dikunci oleh": summary["Locked_By"], "Integritas": [INTEGRITY_TXT[i] for i in summary["Integrity"]],
    })
    show_df(show, hide_index=True, column_config={
        "Pencapaian": st.column_config.ProgressColumn("Pencapaian", min_value=0, max_value=100, format="%.1f%%")})
    if len(summary) >= 2:
        with st.container(border=True):
            show_plot(draw_history_chart(summary), key="hist_chart")

    section("Detail periode")
    plist = list(summary["Period"])
    if st.session_state.get("arch_inspect") not in plist:
        st.session_state.pop("arch_inspect", None)
    p = st.selectbox("Pilih periode", plist, key="arch_inspect",
                     format_func=lambda x: f"{period_label(x)} ({STATUS_TXT[summary.set_index('Period').loc[x, 'Status']]})")
    m = summary[summary["Period"] == p].iloc[0]
    locked = m["Status"] == LOCKED
    st.markdown(facts([
        ("Status", STATUS_TXT[m["Status"]]), ("Jumlah baris", fmt_int(m["Rows"])),
        ("Pencapaian", fmt_pct(m["Ach"])), ("Terakhir disimpan", f"{m['Saved_At']} ({m['Saved_By']})"),
        ("Dikunci", f"{m['Locked_At']} ({m['Locked_By']})" if locked else "-"),
        ("Integritas data", INTEGRITY_TXT[m["Integrity"]]),
    ]), unsafe_allow_html=True)
    if m["Integrity"] == "changed":
        st.markdown(note("bad", "Isi sheet untuk periode ini berbeda dari saat dikunci. Periksa riwayat versi "
                                "Google Sheets untuk melihat siapa yang mengubahnya."), unsafe_allow_html=True)
    if m["Audit"]:
        st.caption(f"Jejak audit: {m['Audit']}")

    full = _dates_to_date(expand_period(hist[hist["Period"] == p]))
    b1, b2, b3, _ = st.columns([1, 1, 1, 2])
    b1.button("Buka di dashboard", on_click=open_in_dashboard, args=(p,), type="primary", key="open_dash")
    b2.download_button("Unduh CSV", full.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"SiPM_{p}.csv", mime="text/csv", key="dl_csv")
    xbuf = io.BytesIO()
    full.to_excel(xbuf, index=False, sheet_name=p)
    b3.download_button("Unduh Excel", xbuf.getvalue(), file_name=f"SiPM_{p}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_xlsx")

    with st.expander("Tindakan administrator"):
        if locked:
            if not secret("admin_keyword"):
                st.info("Buka kunci dinonaktifkan. Isi admin_keyword di secrets untuk mengaktifkannya.")
            else:
                with st.form(f"unlock_{p}", clear_on_submit=True):
                    kw = st.text_input("Kata sandi administrator", type="password")
                    go_ = st.form_submit_button("Buka kunci periode")
                if go_:
                    if attempts_blocked():
                        st.error("Terlalu banyak percobaan gagal. Coba lagi dalam 5 menit.")
                    elif not ctx.operator:
                        st.warning("Isi nama operator di sidebar terlebih dahulu.")
                    elif not check_admin_keyword(kw):
                        register_fail()
                        st.error("Kata sandi administrator salah.")
                    else:
                        try:
                            store.unlock_period(p, ctx.operator)
                        except Exception as e:  # noqa: BLE001
                            st.error(f"Gagal membuka kunci: {type(e).__name__}: {e}")
                        else:
                            reset_attempts()
                            load_history.clear()
                            st.toast(f"Kunci {period_label(p)} dibuka. Periode kembali menjadi Draft.")
                            st.rerun()
        else:
            st.caption("Menghapus draft akan menghapus seluruh baris periode ini dari tab SiPM. "
                       "Gunakan bila periode salah dipilih saat upload.")
            with st.form(f"delete_{p}", clear_on_submit=True):
                confirm = st.text_input(f"Ketik {p} untuk konfirmasi")
                kw = st.text_input("Kata sandi administrator" if secret("admin_keyword") else "Kata pengunci",
                                   type="password")
                go_ = st.form_submit_button("Hapus draft")
            if go_:
                ok_kw = check_admin_keyword(kw) if secret("admin_keyword") else check_lock_keyword(kw)
                if attempts_blocked():
                    st.error("Terlalu banyak percobaan gagal. Coba lagi dalam 5 menit.")
                elif confirm.strip() != p:
                    st.warning("Teks konfirmasi tidak cocok dengan kode periode.")
                elif not ok_kw:
                    register_fail()
                    st.error("Kata sandi salah.")
                else:
                    try:
                        n = store.delete_draft(p)
                    except Exception as e:  # noqa: BLE001
                        st.error(f"Gagal menghapus draft: {type(e).__name__}: {e}")
                    else:
                        reset_attempts()
                        load_history.clear()
                        st.session_state.pop("sync_sig", None)
                        st.toast(f"Draft {period_label(p)} dihapus ({fmt_int(n)} baris).")
                        st.rerun()


# ==========================================================================
# 8. ALUR UTAMA
# ==========================================================================
def main():
    st.set_page_config(page_title=f"{APP_NAME}", page_icon="📈", layout="wide",
                       initial_sidebar_state="expanded")
    st.markdown(CSS, unsafe_allow_html=True)

    store, store_err = get_store()
    hist = pd.DataFrame(columns=SCHEMA)
    summary = pd.DataFrame(columns=SUMMARY_COLS)
    if store is not None:
        try:
            hist, summary = load_history(store)
        except Exception as e:  # noqa: BLE001
            store, store_err = None, f"Gagal membaca Google Sheets: {type(e).__name__}: {e}"

    ctx, hist, summary = render_sidebar(store, store_err, hist, summary)

    d = None
    if ctx.df_raw is not None and len(ctx.df_raw):
        d = ctx.df_raw
        if ctx.nops:
            d = d[d["NOP"].isin(ctx.nops)]
        d = apply_takeout(d, ctx.takeout).copy()
        d["Is_Selesai"] = done_mask(d, ctx.completed)

    chips = [("", period_label(ctx.period))] if ctx.period else []
    if ctx.meta is not None:
        chips.append(("lock", "Terkunci") if ctx.locked else ("warn", "Draft"))
    elif ctx.period:
        chips.append(("", "Belum tersimpan"))
    chips.append(("ok", "Google Sheets terhubung") if store is not None else ("bad", "Mode lokal"))
    st.markdown(command_bar(chips), unsafe_allow_html=True)

    if st.session_state.get("nav") not in NAV_ITEMS:
        st.session_state["nav"] = NAV_ITEMS[0]
    if hasattr(st, "segmented_control"):
        nav = st.segmented_control("Navigasi", NAV_ITEMS, key="nav", label_visibility="collapsed")
    else:
        nav = st.radio("Navigasi", NAV_ITEMS, key="nav", horizontal=True, label_visibility="collapsed")
    nav = nav or NAV_ITEMS[0]

    if nav == NAV_ITEMS[0]:
        render_dashboard(store, ctx, d)
    else:
        render_archive(store, store_err, hist, summary, ctx)

    st.markdown(f'<div class="foot">{esc(APP_NAME)} v{APP_VERSION}. Waktu ditampilkan dalam zona '
                f'{esc(str(_tz()))}. Data periode disimpan di Google Sheets, tab '
                f'{esc(str(secret("worksheet_name", DEFAULT_WORKSHEET)))}.</div>', unsafe_allow_html=True)


if __name__ == "__main__":
    main()
